"""Le controle d'une facture d'achat locale : sa preuve, son stock, ses totaux, sa retenue.

OU S'ACCROCHE CHAQUE GESTE, ET POURQUOI LA
-------------------------------------------
- `avant_validation` (before_submit) : les controles BLOQUANTS. Apres la validation, la facture a
  produit ses ecritures et son mouvement de stock ; corriger demande une annulation. C'est donc le
  dernier moment ou refuser coute moins cher que laisser passer.
- `apres_validation` (on_submit) : la retenue a la source. Elle ne peut pas exister avant la
  facture qu'elle impute — d'ou l'ordre, et non un choix de confort.

⚠️ L'EXTRACTION SE FAIT AU PLUS TARD A LA VALIDATION, JAMAIS A CHAQUE ENREGISTREMENT. Un appel
OpenAI par sauvegarde couterait a chaque virgule corrigee. Elle est donc lancee soit a la demande
(bouton), soit une seule fois juste avant la validation si elle manque encore.
"""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import flt, getdate

from bank_retenue_sync.achat import regles

DOCTYPE_EXTRACTION = "Extraction Facture Achat"


def _reglage(champ, defaut=None):
    try:
        v = frappe.db.get_single_value("Bank Retenue Sync Settings", champ)
        return defaut if v in (None, "") else v
    except Exception:
        return defaut


def controle_actif() -> bool:
    """Le coupe-circuit des controles d'achat. Coche par defaut : ils sont la pour servir."""
    v = _reglage("controle_achat_actif", 1)
    return bool(frappe.utils.cint(v))


def _pieces_jointes(nom) -> list:
    """Les fichiers attaches a la facture. Le scan du fournisseur est l'un d'eux."""
    return frappe.db.get_all("File", filters={"attached_to_doctype": "Purchase Invoice",
                                              "attached_to_name": nom},
                             fields=["name", "file_name", "file_url"], order_by="creation")


def _pdf(pieces):
    for f in pieces:
        if (f.file_name or "").lower().endswith(".pdf"):
            return f
    return None


def _lignes_taxes(doc):
    return [{"account_head": t.account_head, "tax_amount": t.tax_amount,
             "add_deduct_tax": t.add_deduct_tax} for t in (doc.get("taxes") or [])]


def _lu(doc) -> dict:
    """La facture telle que les regles la lisent. Les regles ne connaissent pas ERPNext.

    ⚠️ NI `grand_total` NI `total_taxes_and_charges` NE DISENT CE QU'ON CROIT quand une retenue est
    deja saisie. Sur ELECTROQUIP, `grand_total` vaut 1 087,021 — le TTC APRES retenue — et
    `total_taxes_and_charges` vaut 163,321, soit la TVA de 175,311 MOINS la retenue de 11,990.
    Compares au scan, ces deux nombres accusaient la facture d'ecarts qui n'existaient pas. Les
    vrais chiffres se reconstituent depuis la table des taxes.
    """
    lignes = _lignes_taxes(doc)
    return {"pays_fournisseur": frappe.db.get_value("Supplier", doc.supplier, "country"),
            "update_stock": doc.update_stock, "set_warehouse": doc.set_warehouse,
            "bill_no": doc.bill_no, "bill_date": doc.bill_date,
            "lignes_taxes": lignes,
            "total_ttc": regles.ttc_avant_retenue(doc.grand_total, lignes),
            "total_tva": regles.tva_facturee(lignes),
            "controle_retenue": regles.controle_retenue(doc.grand_total, lignes, _seuil(),
                                                        _taux())}


# ------------------------------------------------------------------ extraction


def extraction_de(nom):
    """L'extraction deja faite pour cette facture, ou None."""
    e = frappe.db.get_value(DOCTYPE_EXTRACTION, {"purchase_invoice": nom},
                            ["name", "invoice_no", "invoice_date", "total_ht", "total_tva",
                             "total_ttc", "fichier", "modele"], as_dict=1)
    return e


def extraire(nom, forcer=False) -> dict:
    """Lit le scan par OpenAI et range le resultat. -> dict.

    Remplit `bill_no` et `bill_date` de la facture SI ils sont vides : on complete ce qui manque,
    on n'ecrase jamais ce qu'un humain a saisi — il a peut-etre corrige une lecture douteuse.
    """
    doc = frappe.get_doc("Purchase Invoice", nom)
    deja = extraction_de(nom)
    if deja and not forcer:
        return {"statut": "deja extraite", **deja}

    fichier = _pdf(_pieces_jointes(nom))
    if not fichier:
        return {"statut": "aucun pdf joint"}

    from bank_retenue_sync.ai import invoice_extract

    contenu = frappe.get_doc("File", fichier.name).get_content()
    if isinstance(contenu, str):
        contenu = contenu.encode("latin-1", errors="ignore")
    # Le texte si le PDF en a, le scan sinon : les factures fournisseurs locales arrivent en
    # papier, donc sans couche texte.
    data = invoice_extract.extract_invoice_any(
        contenu, extra_hint="Fournisseur attendu : %s" % (doc.supplier_name or doc.supplier))

    valeurs = {
        "doctype": DOCTYPE_EXTRACTION, "purchase_invoice": nom, "supplier": doc.supplier,
        "fichier": fichier.file_name,
        "invoice_no": data.get("invoice_no"), "invoice_date": data.get("invoice_date"),
        "total_ht": flt(data.get("total_ht"), 3), "total_tva": flt(data.get("total_tva"), 3),
        "total_ttc": flt(data.get("total_ttc"), 3),
        "coherent": 1 if data.get("_balanced") else 0,
        "modele": data.get("_model"), "payload": json.dumps(data, ensure_ascii=False, default=str),
    }
    if deja:
        e = frappe.get_doc(DOCTYPE_EXTRACTION, deja.name)
        e.update({k: v for k, v in valeurs.items() if k != "doctype"})
        e.save(ignore_permissions=True)
    else:
        frappe.get_doc(valeurs).insert(ignore_permissions=True)

    # Completer la partie « Facture fournisseur » de la facture d'achat.
    pose = {}
    if not doc.bill_no and valeurs["invoice_no"]:
        pose["bill_no"] = valeurs["invoice_no"]
    if not doc.bill_date and valeurs["invoice_date"]:
        # Seulement si elle est croyable : la lecture de l'ANNEE est instable (2020, 2023, 2026 sur
        # une meme facture), et une date fausse decide de l'exercice de rattachement.
        if regles.date_plausible(valeurs["invoice_date"], doc.posting_date):
            try:
                pose["bill_date"] = getdate(valeurs["invoice_date"])
            except Exception:
                pass
        else:
            pose["_date_ecartee"] = valeurs["invoice_date"]
    ecartee = pose.pop("_date_ecartee", None)
    if pose:
        frappe.db.set_value("Purchase Invoice", nom, pose, update_modified=False)
    if ecartee:
        pose["date_ecartee"] = ecartee

    return {"statut": "extraite", "pose": pose,
            **{k: v for k, v in valeurs.items() if k not in ("doctype", "payload")}}


@frappe.whitelist()
def extraire_maintenant(nom, forcer=0):
    """Bouton « Lire le scan » du formulaire."""
    frappe.only_for(["System Manager", "Accounts Manager", "Accounts User", "Purchase Manager",
                     "Purchase User"])
    res = extraire(nom, forcer=frappe.utils.cint(forcer))
    frappe.db.commit()
    return res


# ------------------------------------------------------------------ controles


def diagnostic(nom) -> dict:
    """Ce qui bloquerait la validation, sans rien bloquer. Sert au formulaire et aux tests."""
    doc = frappe.get_doc("Purchase Invoice", nom)
    facture = _lu(doc)
    extraction = extraction_de(nom)
    return {"local": regles.est_local(facture["pays_fournisseur"]),
            "manques": regles.manques(facture, _pieces_jointes(nom), extraction),
            "extraction": extraction, "retenue": facture["controle_retenue"],
            "ttc_avant_retenue": facture["total_ttc"], "tva": facture["total_tva"]}


@frappe.whitelist()
def diagnostic_maintenant(nom):
    """Bouton « Vérifier avant validation » : montre ce qui bloquerait, sans rien bloquer."""
    frappe.only_for(["System Manager", "Accounts Manager", "Accounts User", "Purchase Manager",
                     "Purchase User"])
    return diagnostic(nom)


def avant_validation(doc, method=None):
    """Hook `before_submit` : refuse la validation tant qu'un manque subsiste."""
    if not controle_actif() or doc.get("is_return"):
        return
    facture = _lu(doc)
    if not regles.est_local(facture["pays_fournisseur"]):
        return

    pieces = _pieces_jointes(doc.name)
    # L'extraction manquante n'est pas un motif de refus : on la fait. C'est le seul moment ou on
    # sait que l'utilisateur a fini de saisir.
    extraction = extraction_de(doc.name)
    if not extraction and pieces:
        try:
            extraire(doc.name)
            extraction = extraction_de(doc.name)
            doc.reload()
            facture = _lu(doc)
        except Exception:
            # Une panne d'OpenAI ne doit pas empecher de travailler : les autres controles
            # s'appliquent, et l'ecart de montants sera verifie a la prochaine lecture.
            frappe.log_error(title="Extraction facture achat %s" % doc.name,
                             message=frappe.get_traceback())

    manques = regles.manques(facture, pieces, extraction)
    if manques:
        frappe.throw(_("Facture d'achat locale — il manque :<br>• {0}").format(
            "<br>• ".join(frappe.utils.escape_html(m) for m in manques)),
            title=_("Validation refusée"))


def a_l_enregistrement(doc, method=None):
    """Hook `validate` : pose la ligne de retenue manquante, tant que la facture est modifiable.

    ⚠️ C'EST LE SEUL MOMENT OU ELLE PEUT ETRE POSEE. La retenue est une LIGNE DE TAXE en deduction,
    pas une ecriture separee : apres validation, la table des taxes est figee et il faudrait annuler
    la facture pour l'y ajouter.
    """
    if not controle_actif() or doc.get("is_return") or doc.docstatus != 0:
        return
    if not regles.est_local(frappe.db.get_value("Supplier", doc.supplier, "country")):
        return
    from bank_retenue_sync.achat import retenue

    retenue.poser_ligne(doc)


def _seuil():
    # ⚠️ Un seuil a zero rendrait TOUTE facture passible de retenue ; un taux a zero n'en calculerait
    # aucune. Ni l'un ni l'autre n'est un reglage voulu : c'est `controle_achat_actif` qui coupe.
    return flt(_reglage("ras_achat_seuil", None) or regles.SEUIL_RETENUE, 3)


def _taux():
    return flt(_reglage("ras_achat_taux", None) or regles.TAUX_RETENUE, 3)
