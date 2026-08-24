"""Le controle d'une facture d'achat locale : sa preuve, son stock, ses totaux, sa retenue.

OU S'ACCROCHE CHAQUE GESTE, ET POURQUOI LA
-------------------------------------------
- `a_l_enregistrement` (before_validate) : tout ce qui se CORRIGE — le stock, le magasin, le numero
  et la date du fournisseur, la date de comptabilisation, la ligne de retenue. Avant la validation
  d'ERPNext, pour que les totaux et l'echeance se recalculent sur ce qu'on vient de poser ; et en
  brouillon, parce que la table des taxes est figee des la validation.
- `avant_validation` (before_submit) : les deux seuls controles BLOQUANTS. Apres la validation, la
  facture a produit ses ecritures et son mouvement de stock ; corriger demande une annulation.

⚠️ ON CORRIGE PLUTOT QUE DE REFUSER, ET C'EST LA REGLE DE PARTAGE ENTRE LES DEUX CROCHETS. Refuser
une facture pour une case a cocher qu'on sait cocher soi-meme fait perdre du temps sans rien
proteger. Ne restent bloquants que l'absence de PDF et un ecart important entre la saisie et le
scan : les deux seules choses qu'aucune machine ne peut trancher a la place de l'utilisateur.

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
            "total_ht": flt(doc.net_total, 3),
            "controle_retenue": regles.controle_retenue(doc.grand_total, lignes, _seuil(),
                                                        _taux())}


# ------------------------------------------------------------------ extraction


def extraction_de(nom):
    """L'extraction deja faite pour cette facture, ou None.

    ⚠️ `coherent` FAIT PARTIE DE LA LECTURE, PAS DE LA DECORATION. Sans lui, `ecarts_importants` ne
    voit qu'un dict sans drapeau et rend la main sans rien comparer : la seule regle qui confronte
    la saisie au scan serait morte en silence, et toutes les factures passeraient.
    """
    e = frappe.db.get_value(DOCTYPE_EXTRACTION, {"purchase_invoice": nom},
                            ["name", "invoice_no", "invoice_date", "total_ht", "total_tva",
                             "total_ttc", "coherent", "fichier", "modele", "supplier_tax_id"],
                            as_dict=1)
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
        "supplier_tax_id": (data.get("supplier_tax_id") or "").strip() or None,
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


def completer(doc) -> dict:
    """Remplit le numero et la date de la facture fournisseur depuis le scan, EN MEMOIRE.

    ⚠️ APPELEE PENDANT `validate`, DONC AVANT L'ENREGISTREMENT : les valeurs posees ici partent
    avec le document. Une version precedente extrayait au moment de refuser la validation, ecrivait
    en base puis rechargeait le document — un `reload()` en plein `before_submit` remet le
    `docstatus` a celui de la base et casse la validation en cours.

    Ne fait rien si les deux champs sont deja renseignes : l'extraction coute un appel OpenAI, et
    ce qu'un humain a saisi n'est jamais ecrase.
    """
    if doc.bill_no and doc.bill_date:
        return {"statut": "deja renseigne"}
    if not _pdf(_pieces_jointes(doc.name)):
        return {"statut": "aucun pdf joint"}

    res = extraire(doc.name, forcer=False)
    lue = extraction_de(doc.name)
    if not lue:
        return res

    pose = {}
    if not doc.bill_no and lue.get("invoice_no"):
        doc.bill_no = lue["invoice_no"]
        pose["bill_no"] = lue["invoice_no"]
    if not doc.bill_date and lue.get("invoice_date"):
        # Seulement si elle est croyable : la lecture de l'ANNEE est instable, et une date fausse
        # decide de l'exercice de rattachement.
        if regles.date_plausible(lue["invoice_date"], doc.posting_date):
            doc.bill_date = getdate(lue["invoice_date"])
            pose["bill_date"] = doc.bill_date
        else:
            pose["date_ecartee"] = str(lue["invoice_date"])
    # Le matricule ne vit pas sur la facture mais sur la FICHE FOURNISSEUR : il est donc écrit en
    # base tout de suite, et non posé en mémoire comme les deux champs ci-dessus.
    mat = completer_matricule(doc.supplier, lue.get("supplier_tax_id"))
    if mat.get("statut") == "pose":
        pose["matricule"] = mat["tax_id"]
    elif mat.get("statut") == "illisible":
        pose["matricule_ecarte"] = mat["lu"]
    return {"statut": "complete", "pose": pose, **res}


def completer_matricule(fournisseur, lu) -> dict:
    """Pose sur la fiche fournisseur le matricule fiscal lu sur le scan. -> dict.

    ⚠️ IL FIGURE SUR TOUTES LES FACTURES ET SUR 17 FICHES SUR 42. Sans lui, le certificat de
    retenue a la source ne peut pas etre emis : le portail TEJ ne connait pas d'autre facon de
    designer le beneficiaire. Le faire saisir a la main au moment d'emettre, c'est arreter le
    geste pour une donnee qui etait sous les yeux depuis le debut.

    ⚠️ ON N'ECRASE JAMAIS UN MATRICULE DEJA SAISI. Un humain a pu corriger une lecture douteuse,
    et cette valeur-la vaut mieux qu'une relecture du modele. Meme regle que le n° et la date de
    la facture fournisseur.

    ⚠️ ET ON REFUSE CE QUI N'EST PAS UN MATRICULE. `matricule.normaliser` n'accepte que la forme
    tunisienne — 7 chiffres et une lettre cle, cette lettre etant CALCULEE a partir des chiffres.
    Un numero de telephone ou un registre de commerce mal lus n'y survivent pas : une fiche
    fournisseur polluee par une lecture douteuse se propagerait ensuite a chaque certificat.
    """
    from bank_retenue_sync.tej import matricule

    if not fournisseur or not lu:
        return {}
    if frappe.db.get_value("Supplier", fournisseur, "tax_id"):
        return {"statut": "deja renseigne"}
    if not matricule.normaliser(lu):
        return {"statut": "illisible", "lu": lu}
    frappe.db.set_value("Supplier", fournisseur, "tax_id", lu, update_modified=False)
    return {"statut": "pose", "tax_id": lu}


def magasin_par_defaut() -> str:
    """Le magasin ou entre la marchandise, a defaut de choix explicite.

    Le reglage d'abord, puis le magasin de Stock Settings, puis celui que la pratique designe :
    sur 181 factures d'achat renseignees, 161 pointent le meme. On ne devine pas, on constate.
    """
    reglage = _reglage("magasin_achat_defaut", None)
    if reglage and frappe.db.exists("Warehouse", reglage):
        return reglage
    defaut = frappe.db.get_single_value("Stock Settings", "default_warehouse")
    if defaut and frappe.db.exists("Warehouse", defaut):
        return defaut
    courant = frappe.db.sql("""select set_warehouse, count(*) n from `tabPurchase Invoice`
                               where docstatus = 1 and ifnull(set_warehouse, '') <> ''
                               group by set_warehouse order by n desc limit 1""")
    return courant[0][0] if courant else None


def corriger_stock(doc) -> dict:
    """Coche « Mettre a jour le stock » et pose le magasin. -> dict.

    ⚠️ CE N'EST PAS UN REFUS DEGUISE : c'est une correction. Une facture d'achat locale entre de la
    marchandise ; la case et le magasin ne sont pas des choix, ce sont des oublis quand ils
    manquent. 197 des 211 factures validees les portent deja.
    """
    pose = {}
    # Facture nee de RECUS D'ACHAT (Get Items from Purchase Receipt) : la
    # marchandise est DEJA entree a la reception — la VERIFICATION s'inverse :
    # la case doit rester DECOCHEE (la cocher ferait entrer le stock une
    # seconde fois, et ERPNext la refuse des qu'une ligne reference un recu).
    # On decoche si besoin et on le dit, au lieu de corriger en silence.
    recus = sorted({ligne.purchase_receipt for ligne in doc.get("items") or []
                    if ligne.get("purchase_receipt")})
    if recus:
        if doc.update_stock:
            doc.update_stock = 0
            pose["update_stock"] = 0
        frappe.msgprint(
            _("Stock deja entre par le(s) recu(s) {0} — « Mettre a jour le "
              "stock » reste decoche.").format(", ".join(recus)),
            alert=True, indicator="blue")
        return pose
    if not doc.update_stock:
        doc.update_stock = 1
        pose["update_stock"] = 1
    if not doc.set_warehouse:
        magasin = magasin_par_defaut()
        if magasin:
            doc.set_warehouse = magasin
            for ligne in doc.get("items") or []:
                if not ligne.warehouse:
                    ligne.warehouse = magasin
            pose["set_warehouse"] = magasin
    return pose


def aligner_dates(doc) -> dict:
    """La date de comptabilisation suit la date de la facture fournisseur.

    ⚠️ ET C'EST PRECISEMENT POURQUOI LA DATE LUE PASSE PAR `date_plausible`. Tant qu'elle ne
    servait qu'a remplir un champ d'information, une annee mal lue etait genante ; maintenant
    qu'elle decide de la date de comptabilisation, elle deciderait de l'EXERCICE. Le modele ayant
    rendu 2020, 2023 puis 2026 pour une meme facture, seule une date proche de celle deja saisie
    est posee — et donc seule une date deja croyable peut deplacer la comptabilisation.

    `set_posting_time` est indispensable : sans lui, ERPNext ramene la date du jour a chaque
    enregistrement et l'alignement serait perdu en silence.

    ⚠️ ET LA PLAUSIBILITE SE REVERIFIE ICI, MEME SI `completer` L'A DEJA FAITE. Les deux controles
    ne portent pas sur la meme chose : la-bas on filtre ce que le SCAN propose, ici on regarde ce
    que le champ CONTIENT — et il peut contenir une annee fausse posee avant que ce filtre
    n'existe. Cas reel : ACC-PINV-2026-00088 porte 2020-08-03 en date fournisseur ; sans ce garde-
    fou, le simple fait de l'enregistrer ramenait sa comptabilisation de 2026 a 2020, dans un
    exercice clos, sans que rien ne le dise.
    """
    if not doc.bill_date or str(doc.posting_date) == str(doc.bill_date):
        return {"statut": "deja aligne"}
    if not regles.date_plausible(doc.bill_date, doc.posting_date):
        return {"statut": "ecartee", "bill_date": str(doc.bill_date),
                "posting_date": str(doc.posting_date)}
    avant = str(doc.posting_date)
    doc.set_posting_time = 1
    doc.posting_date = doc.bill_date
    return {"statut": "aligne", "avant": avant, "apres": str(doc.bill_date)}


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
            "manques": regles.bloquants(facture, _pieces_jointes(nom), extraction, *_seuils()),
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
    # Dernier filet : si l'enregistrement n'a pas pu completer (piece jointe ajoutee juste avant de
    # valider, panne passagere), on retente ici — en memoire, jamais par un `reload`.
    extraction = extraction_de(doc.name)
    if pieces and (not extraction or not doc.bill_no or not doc.bill_date):
        try:
            completer(doc)
            extraction = extraction_de(doc.name)
            facture = _lu(doc)
        except Exception:
            frappe.log_error(title="Extraction facture achat %s" % doc.name,
                             message=frappe.get_traceback())

    # ⚠️ LA DATE S'ALIGNE AUSSI ICI, ET C'EST INDISPENSABLE. `a_l_enregistrement`
    # ne tourne que sur un BROUILLON (`docstatus != 0` -> il sort) : une date
    # fournisseur saisie juste avant de valider, ou arrivee apres le premier
    # enregistrement, n'etait donc JAMAIS suivie — la facture partait avec la date
    # du jour. Vu le 20/08/2026 sur ACC-PINV-2026-00095 (facture du 29/07,
    # comptabilisee au 20/08, `set_posting_time` reste a 0, preuve que
    # l'alignement n'avait pas eu lieu). Meme fonction, memes garde-fous : une
    # date invraisemblable est toujours ecartee.
    dates = aligner_dates(doc)
    if dates.get("statut") == "aligne":
        frappe.msgprint(_("Date de comptabilisation alignée sur la facture fournisseur : "
                          "{0} au lieu de {1}.").format(dates["apres"], dates["avant"]),
                        indicator="blue", alert=True)
    elif dates.get("statut") == "ecartee":
        frappe.msgprint(_("Date de comptabilisation LAISSÉE au {0} : la date fournisseur ({1}) "
                          "en est trop éloignée pour être suivie sans relecture.").format(
                              dates["posting_date"], dates["bill_date"]),
                        indicator="orange", alert=True)

    refus = regles.bloquants(facture, pieces, extraction, *_seuils())
    if refus:
        frappe.throw(_("Facture d'achat locale :<br>• {0}").format(
            "<br>• ".join(frappe.utils.escape_html(m) for m in refus)),
            title=_("Validation refusée"))


def a_l_enregistrement(doc, method=None):
    """Hook `before_validate` : corrige le stock, complete depuis le scan, aligne les dates, pose ou
    redresse la retenue — puis DIT ce qu'il a change.

    ⚠️ AVANT LA VALIDATION D'ERPNEXT, ET C'EST TOUT L'INTERET. Ce crochet deplace la date de
    comptabilisation et ajoute une ligne de taxe : place APRES, il laissait une date d'echeance
    anterieure a la facture et des totaux calcules sans la retenue. Place avant, ERPNext recalcule
    l'echeance, les taxes et les totaux a partir de ce qu'on vient de poser.

    ⚠️ ET C'EST LE SEUL MOMENT OU LA RETENUE PEUT ETRE POSEE. C'est une LIGNE DE TAXE en deduction,
    pas une ecriture separee : apres validation, la table des taxes est figee et il faudrait annuler
    la facture pour l'y ajouter.
    """
    if not controle_actif() or doc.get("is_return") or doc.docstatus != 0:
        return
    if not regles.est_local(frappe.db.get_value("Supplier", doc.supplier, "country")):
        return
    from bank_retenue_sync.achat import retenue

    # ⚠️ TOUT CE QUI PEUT ETRE CORRIGE L'EST ICI, ET RIEN N'EST REFUSE POUR CELA. Le stock, le
    # magasin, le numero, les dates et la retenue se posent seuls ; ne restent bloquants que
    # l'absence de PDF et un ecart important sur les totaux — les deux seules choses qu'aucune
    # machine ne peut trancher a la place de l'utilisateur.
    faits = []
    stock = corriger_stock(doc)
    if stock.get("update_stock"):
        faits.append(_("« Mettre à jour le stock » a été coché : la marchandise doit entrer."))
    if stock.get("set_warehouse"):
        faits.append(_("Magasin posé : {0}.").format(stock["set_warehouse"]))
    try:
        pose = (completer(doc) or {}).get("pose") or {}
        if pose.get("bill_no"):
            faits.append(_("N° de facture fournisseur lu sur le scan : {0}.").format(pose["bill_no"]))
        if pose.get("bill_date"):
            faits.append(_("Date de la facture fournisseur lue sur le scan : {0}.").format(
                frappe.utils.formatdate(pose["bill_date"])))
        if pose.get("matricule"):
            # ⚠️ CETTE CORRECTION-LA SORT DE LA FACTURE. Les autres modifient le document qu'on
            # est en train d'enregistrer ; celle-ci écrit sur la FICHE DU FOURNISSEUR, où elle
            # servira à toutes ses factures suivantes. Raison de plus pour la dire.
            faits.append(_("Matricule fiscal du fournisseur lu sur le scan et posé sur sa "
                           "fiche : {0}.").format(pose["matricule"]))
        if pose.get("matricule_ecarte"):
            faits.append(_("Matricule fiscal lu ({0}) écarté : ce n'est pas une forme tunisienne "
                           "valide, à saisir à la main sur la fiche fournisseur.").format(
                               pose["matricule_ecarte"]))
        if pose.get("date_ecartee"):
            faits.append(_("Date lue sur le scan ({0}) écartée : trop éloignée de la "
                           "comptabilisation, à saisir à la main.").format(pose["date_ecartee"]))
        dates = aligner_dates(doc)
        if dates.get("statut") == "aligne":
            faits.append(_("Date de comptabilisation alignée sur la facture fournisseur : "
                           "{0} au lieu de {1}.").format(dates["apres"], dates["avant"]))
        elif dates.get("statut") == "ecartee":
            faits.append(_("Date de comptabilisation LAISSÉE au {0} : la date fournisseur ({1}) en "
                           "est trop éloignée pour être suivie sans relecture — corrigez-la si "
                           "elle est juste.").format(dates["posting_date"], dates["bill_date"]))
    except Exception:
        # Une panne d'OpenAI ne doit pas empecher d'enregistrer : les champs resteront vides, la
        # facture se validera, et l'extraction se retentera au prochain enregistrement.
        frappe.log_error(title="Extraction facture achat %s" % doc.name,
                         message=frappe.get_traceback())
    posee = retenue.poser_ligne(doc)
    if posee.get("statut") == "posee":
        faits.append(_("Retenue à la source ajoutée : {0} — 1 % de {1} (timbre exclu).").format(
            posee["due"], posee["assiette"]))
    corrigee = retenue.corriger_ligne(doc)
    if corrigee.get("statut") == "corrigee":
        faits.append(_("Retenue à la source ramenée à {0} : {1} était saisi, et une retenue ne se "
                       "négocie pas.").format(corrigee["apres"], corrigee["avant"]))
    # ⚠️ APRES LES DEUX AUTRES, ET POUR LES BROUILLONS D'AVANT LE CORRECTIF. La ligne de retenue
    # pointe un compte de RESULTAT : sans centre de couts, ERPNext refuse la validation — et le
    # motif ne se lit nulle part sur la facture, puisque le montant, lui, est juste.
    centre = retenue.completer_centre(doc)
    if centre.get("statut") == "pose":
        faits.append(_("Centre de coûts posé sur la ligne de retenue : {0} — sans lui, la "
                       "validation serait refusée (compte de résultat).").format(
                           centre["cost_center"]))
    elif centre.get("statut") == "aucun centre de couts":
        faits.append(_("Aucun centre de coûts par défaut sur la société : la ligne de retenue en "
                       "exige un, la validation sera refusée tant qu'il manquera."))
    _annoncer(faits)


def _annoncer(faits):
    """Dit ce qui vient d'etre change sur la facture, sans rien demander.

    ⚠️ CORRIGER EN SILENCE SERAIT PIRE QUE REFUSER. Ces corrections deplacent une date de
    comptabilisation, ajoutent un mouvement de stock et changent un montant retenu : l'utilisateur
    doit voir ce qui a bouge sous sa saisie, sinon il decouvre l'ecart au controle fiscal.

    Rien n'est affiche hors d'une requete : en tache de fond ou pendant une migration, un msgprint
    ne s'adresse a personne.
    """
    if not faits or not getattr(frappe.local, "request", None):
        return
    frappe.msgprint("<ul><li>" + "</li><li>".join(faits) + "</li></ul>",
                    title=_("Corrigé automatiquement"), indicator="blue")


def _ecart_minimal():
    return flt(_reglage("ecart_achat_minimal", None) or regles.ECART_MINIMAL, 3)


def _ecart_taux():
    return flt(_reglage("ecart_achat_taux", None) or regles.ECART_TAUX, 3)


def _ecart_ttc():
    return flt(_reglage("ecart_achat_ttc", None) or regles.ECART_TTC, 3)


def _ecart_tva_taux():
    return flt(_reglage("ecart_achat_tva_taux", None) or regles.ECART_TVA_TAUX, 3)


def _seuils():
    """Les quatre reglages d'ecart, dans l'ordre attendu par `regles.bloquants`.

    Regroupes parce qu'ils voyagent toujours ensemble : les passer un a un a chaque appel, c'est
    l'occasion d'en oublier un — et un seuil oublie reprend sa valeur par defaut en silence, ce qui
    rouvre exactement la porte que ce resserrage vient de fermer.
    """
    return (_ecart_minimal(), _ecart_taux(), _ecart_ttc(), _ecart_tva_taux())


def _seuil():
    # ⚠️ Un seuil a zero rendrait TOUTE facture passible de retenue ; un taux a zero n'en calculerait
    # aucune. Ni l'un ni l'autre n'est un reglage voulu : c'est `controle_achat_actif` qui coupe.
    return flt(_reglage("ras_achat_seuil", None) or regles.SEUIL_RETENUE, 3)


def _taux():
    return flt(_reglage("ras_achat_taux", None) or regles.TAUX_RETENUE, 3)
