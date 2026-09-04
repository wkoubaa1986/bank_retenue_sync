"""La file des certificats de retenue à émettre pour les DÉPENSES DE CAISSE facturées.

POURQUOI UNE FILE, ET PAS UNE ÉMISSION AUTOMATIQUE
---------------------------------------------------
⚠️ CE QUI PART SUR TEJ EST DÉCLARATIF ET IRRÉVERSIBLE. Un certificat soumis se lit chez le
fournisseur et chez l'administration ; l'annuler laisse une trace. La retenue, elle, se pose à la
saisie — on ne va pas arrêter la caisse pour une fiche fournisseur incomplète. Les deux gestes
sont donc séparés : la caisse retient, la file déclare, et personne ne déclare sans le vouloir
(décision utilisateur 04/09/2026).

CE QUI BLOQUE UNE ÉMISSION, ET POURQUOI ON L'ACCEPTE QUAND MÊME
---------------------------------------------------------------
Le portail exige un MATRICULE FISCAL. Or une dépense de caisse ne porte qu'un nom de fournisseur
en texte libre — « MS TECHAUTOMATION sarl » n'est rattaché à aucune fiche. La retenue est posée
malgré tout et la ligne entre ici à l'état « Incomplet », avec ce qui lui manque écrit noir sur
blanc. Refuser la retenue aurait fait perdre 1 % au Trésor ; l'émettre sans matricule est
impossible. Il reste la file.

LE PÉRIMÈTRE
------------
⚠️ RIEN AVANT LE 01/09/2026 (décision utilisateur). Les écritures antérieures n'ont pas été
saisies sous cette règle : les rattraper produirait des déclarations que personne n'a préparées.
Et les écritures créées AUTOMATIQUEMENT — Aramex, Total — sont hors sujet : elles n'entrent pas
par la caisse et leurs fournisseurs ne sont pas locaux.
"""
from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import flt, getdate

from bank_retenue_sync.achat import retenue_depense as R

DOCTYPE = "BRS Retenue Achat A Emettre"
DEPUIS = "2026-09-01"

#: Ce que la remarque de la dépense écrit — c'est la seule mémoire de la retenue sur l'écriture.
_RETENUE = re.compile(r"Retenue à la source achat\s*:\s*([\d\s.,]+)\s*sur\s*([\d\s.,]+)")
_FOURNISSEUR = re.compile(r"Fournisseur\s*:\s*(.+)")
_FACTURE = re.compile(r"Facture n°\s*(\S+)")

#: Les écritures créées par les flux automatiques : jamais de retenue à déclarer dessus.
PREFIXES_EXCLUS = ("Facture Total", "Facture Aramex", "Frais bancaire", "Dépense à payer —")


def _nombre(txt):
    return flt((txt or "").replace(" ", "").replace(" ", "").replace(",", "."), 3)


def lire_piece(remarque) -> dict:
    """Ce que la remarque d'une dépense dit de sa retenue. Fonction pure.

    La remarque est la seule mémoire : pas de champ dédié sur l'écriture, donc pas de champ à
    migrer et pas de risque que le justificatif et sa trace divergent — même convention que le
    « Chq N° » que l'identification bancaire lit déjà.
    """
    texte = remarque or ""
    m = _RETENUE.search(texte)
    f = _FOURNISSEUR.search(texte)
    n = _FACTURE.search(texte)
    return {
        "retenue": _nombre(m.group(1)) if m else 0.0,
        "ttc": _nombre(m.group(2)) if m else 0.0,
        "fournisseur": (f.group(1).strip().split("\n")[0] if f else ""),
        "numero_facture": (n.group(1).strip() if n else ""),
    }


def exclue(cheque_no) -> bool:
    """Une écriture des flux automatiques n'a rien à déclarer."""
    libelle = cheque_no or ""
    return any(libelle.startswith(p) for p in PREFIXES_EXCLUS)


def candidates(depuis=None) -> list:
    """Les écritures qui portent une retenue d'achat et devraient avoir un certificat."""
    depuis = depuis or DEPUIS
    lignes = frappe.db.sql(
        """SELECT je.name, je.posting_date, je.cheque_no, je.user_remark, je.total_debit,
                  SUM(jea.credit) AS retenue
           FROM `tabJournal Entry Account` jea
           INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
           WHERE je.docstatus = 1 AND je.posting_date >= %s
             AND jea.account = %s AND jea.credit > 0
           GROUP BY je.name""", (getdate(depuis), R.compte()), as_dict=True)
    return [l for l in lignes if not exclue(l.cheque_no)]


def synchroniser(depuis=None) -> dict:
    """Remplit la file depuis les écritures. Idempotent : une ligne par écriture, jamais deux.

    Ne touche JAMAIS une ligne déjà émise : la référence du certificat est la preuve d'une
    déclaration partie, et rien ici ne doit pouvoir l'effacer.
    """
    crees, revus = 0, 0
    for c in candidates(depuis):
        lu = lire_piece(c.user_remark)
        supplier, matricule, manque = _fournisseur(lu["fournisseur"])
        if frappe.db.exists(DOCTYPE, c.name):
            doc = frappe.get_doc(DOCTYPE, c.name)
            if doc.statut == "Émis":
                continue
            revus += 1
        else:
            doc = frappe.new_doc(DOCTYPE)
            doc.journal_entry = c.name
            crees += 1
        doc.date_piece = c.posting_date
        # ⚠️ LES ÉCRITURES D'AVANT CETTE RÈGLE N'ONT PAS LE TTC DANS LEUR REMARQUE. La retenue,
        # elle, se lit toujours sur la ligne comptable. Le total débit de l'écriture EST le TTC
        # pour une dépense de caisse (charge + TVA) : il sert de repli plutôt que d'afficher un
        # zéro qui ferait douter du reste (cas ACC-JV-2026-00698, saisie à la main le 04/09).
        doc.montant_ttc = lu["ttc"] or flt(c.total_debit, 3)
        doc.retenue = flt(c.retenue, 3)
        doc.fournisseur_lu = lu["fournisseur"]
        doc.numero_facture = doc.numero_facture or lu["numero_facture"]
        if not doc.supplier:
            doc.supplier = supplier
        doc.matricule = _matricule(doc.supplier)
        manques = _manques(doc)
        doc.note = " · ".join(manques)
        if doc.statut != "Ignoré":
            doc.statut = "Incomplet" if manques else "À émettre"
        doc.flags.ignore_permissions = True
        doc.save()
    frappe.db.commit()
    return {"crees": crees, "revus": revus}


def _fournisseur(nom):
    """(supplier, matricule, manque) pour un nom lu sur la pièce."""
    if not (nom or "").strip():
        return None, "", _("aucun fournisseur nommé sur la pièce")
    exact = frappe.db.get_value("Supplier", {"supplier_name": nom.strip()}, "name")
    return exact, _matricule(exact), ""


def _matricule(supplier):
    return (frappe.db.get_value("Supplier", supplier, "tax_id") or "") if supplier else ""


def _manques(doc) -> list:
    """Ce qui empêche encore d'émettre. C'est cette liste que l'écran affiche."""
    manques = []
    if not doc.supplier:
        manques.append(_("fournisseur non rattaché (« {0} »)")
                       .format(doc.fournisseur_lu or "?"))
    elif not (doc.matricule or "").strip():
        manques.append(_("le fournisseur {0} n'a pas de matricule fiscal").format(doc.supplier))
    if not (doc.numero_facture or "").strip():
        manques.append(_("n° de facture fournisseur manquant"))
    if flt(doc.retenue) <= 0:
        manques.append(_("aucune retenue sur cette pièce"))
    return manques


# ------------------------------------------------------------------ l'emission


def contexte(ligne: str) -> dict:
    """Le contexte d'emission d'une ligne de la file, dans la forme que `tej.emis` attend.

    ⚠️ C'EST UN ADAPTATEUR, PAS UNE SECONDE IMPLEMENTATION. `tej/emis.py` sait deja tout faire —
    repetition a blanc, controle du montant calcule par le portail, cle d'idempotence, PDF
    attache. Il ne sait pas lire une ecriture de journal : on lui fournit donc les memes cles
    depuis une autre source. Dupliquer l'emission aurait fait diverger les deux chemins au
    premier changement du portail.

    ⚠️ LE HT SE DEDUIT, IL NE SE LIT PAS. Une ecriture de caisse ne porte pas de « net_total » :
    elle porte le TTC et la ligne de TVA. Le HT est leur difference, et le taux se retrouve a
    partir des deux — ce que TEJ exige, un taux unique par operation.
    """
    from bank_retenue_sync.tej import matricule as M

    doc = frappe.get_doc(DOCTYPE, ligne)
    je = frappe.get_doc("Journal Entry", doc.journal_entry)
    ht, taux = _ht_et_taux(je, flt(doc.montant_ttc), flt(doc.retenue))
    mat = M.normaliser(doc.matricule or "")

    manques = _manques(doc)
    if doc.statut == "Émis":
        manques.append(_("un certificat a déjà été émis pour cette pièce"))
    if not mat:
        manques.append(_("le matricule fiscal {0} n'est pas exploitable")
                       .format(doc.matricule or "?"))
    if taux is None:
        manques.append(_("le taux de TVA n'est pas déterminable sur cette écriture : TEJ ne "
                         "prend qu'un taux par opération"))

    return {
        # `facture` est le nom que `tej.emis` donne a la piece d'origine : ici c'est l'ecriture.
        "facture": doc.journal_entry,
        "fournisseur": doc.supplier,
        "fournisseur_nom": frappe.db.get_value("Supplier", doc.supplier, "supplier_name")
                           if doc.supplier else "",
        "matricule": mat,
        "matricule_saisi": doc.matricule or "",
        "bill_no": doc.numero_facture or "",
        "date_paiement": str(doc.date_piece or ""),
        "montant_ht": ht,
        "taux_tva": taux,
        "retenue_facture": flt(doc.retenue, 3),
        "exercice": getdate(doc.date_piece).year if doc.date_piece else None,
        "deja_emis": doc.certificat or None,
        "manques": manques,
    }


#: Les comptes de TVA deductible de la caisse. Le taux se lit dans leur nom, pas dans un champ.
_TVA = re.compile(r"TVA\s*(\d+)\s*%", re.IGNORECASE)


def _ht_et_taux(je, ttc, retenue):
    """(HT, taux de TVA) d'une ecriture de depense. -> (float, int|None).

    Le TTC est celui de la piece — retenue comprise, puisqu'elle en est deduite et non ajoutee.
    """
    tva, taux = 0.0, None
    for a in je.accounts:
        m = _TVA.search(a.account or "")
        if m and flt(a.debit) > 0:
            tva += flt(a.debit)
            t = int(m.group(1))
            # Deux taux differents sur la meme piece : TEJ n'en prend qu'un, on rend None.
            taux = t if taux in (None, t) else -1
    if taux == -1:
        return flt(ttc - tva, 3), None
    return flt(ttc - tva, 3), taux


@frappe.whitelist()
def emettre(ligne: str, dry_run: bool = True) -> dict:
    """Repete ou soumet le certificat d'une ligne de la file. -> dict.

    ⚠️ RIEN NE PART SANS `dry_run=False` EXPLICITE, demande a l'ecran — c'est la doctrine de
    `tej/emis`, et elle vaut ici pour la meme raison : un certificat soumis se lit chez le
    fournisseur et chez l'administration.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    from bank_retenue_sync.tej import emis as E

    doc = frappe.get_doc(DOCTYPE, ligne)
    ctx = contexte(ligne)
    if ctx["manques"]:
        return {"statut": "impossible", **ctx}

    res = E.emettre(doc.journal_entry, dry_run=frappe.utils.cint(dry_run) == 1, ctx=ctx)
    reference = res.get("reference") or res.get("certificat")
    if not frappe.utils.cint(dry_run) and reference:
        # L'etat ne bascule QU'AVEC une reference : sans elle, rien ne prouve qu'une declaration
        # est partie, et marquer « Émis » ferait perdre la ligne de vue pour toujours.
        doc.statut = "Émis"
        doc.certificat = reference
        doc.emis_le = frappe.utils.now_datetime()
        doc.flags.ignore_permissions = True
        doc.save()
        frappe.db.commit()
    return res


@frappe.whitelist()
def rafraichir(depuis=None) -> dict:
    """Bouton « Actualiser la file »."""
    frappe.only_for(["System Manager", "Accounts Manager"])
    return synchroniser(depuis)
