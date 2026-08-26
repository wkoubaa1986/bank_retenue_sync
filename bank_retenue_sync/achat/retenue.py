"""La retenue a la source SUR ACHAT : 1 % du TTC hors timbre, des la barre des 1 000 DT.

OU ELLE VIT, ET POURQUOI PAS AILLEURS
--------------------------------------
Dans la TABLE DES TAXES de la facture, en ligne de deduction — comme le fait deja la comptabilite :

    TVA 19 %                      175,311   Ajouter   ->  1 099,011
    Retenue a la source achat      10,990   Deduire   ->  1 088,021

La premiere version de ce module creait une ecriture de paiement separee, sur le modele de la
retenue de VENTE. C'etait une erreur : la retenue d'achat est deja portee par la facture. Les deux
ensemble auraient retenu deux fois la meme somme au meme fournisseur, une fois dans son solde et
une fois dans une ecriture — et le total facture n'aurait plus rien voulu dire.

⚠️ ELLE NE SE POSE QU'EN BROUILLON. Apres validation, la table des taxes est figee : la seule
facon d'ajouter la ligne serait d'annuler la facture. D'ou le crochet sur `validate`, et le refus
sur `before_submit` quand elle manque encore.

⚠️ L'ASSIETTE EXCLUT LE TIMBRE FISCAL. Verifie sur les 17 factures locales de 2026 depassant le
seuil : « 1 % du TTC hors timbre » tombe au millime sur dix d'entre elles. C'est la meme regle que
le portail applique aux ventes, ou l'assiette declaree vaut notre TTC moins le timbre de 1 DT.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from bank_retenue_sync.achat import regles

MODE_RETENUE_ACHAT = "Retenue a la source achat"
COMPTE_RETENUE_ACHAT = "Retenue a la source achat - A&S"
LIBELLE = "Retenue a la source achat"


def _reglage(champ, defaut=None):
    try:
        v = frappe.db.get_single_value("Bank Retenue Sync Settings", champ)
        return defaut if v in (None, "") else v
    except Exception:
        return defaut


def compte_retenue() -> str:
    return _reglage("ras_achat_compte", None) or COMPTE_RETENUE_ACHAT


def _seuil():
    # ⚠️ Un seuil a zero rendrait TOUTE facture passible de retenue ; un taux a zero n'en calculerait
    # aucune. Ni l'un ni l'autre n'est un reglage voulu : c'est `controle_achat_actif` qui coupe.
    return flt(_reglage("ras_achat_seuil", None) or regles.SEUIL_RETENUE, 3)


def _taux():
    return flt(_reglage("ras_achat_taux", None) or regles.TAUX_RETENUE, 3)


def centre_de_cout(doc):
    """Le centre de couts que doit porter la ligne de retenue. -> str | None.

    ⚠️ SANS LUI, LA FACTURE NE SE VALIDE PLUS. `Retenue a la source achat - A&S` est un compte de
    RESULTAT, et ERPNext refuse toute ecriture de resultat sans centre de couts — l'ecriture de
    taxe reprend telle quelle la colonne de la ligne, sans se rabattre sur le defaut de la societe.
    Le champ a pourtant `:Company` pour defaut : mais ce defaut n'est pose qu'a la saisie A L'ECRAN,
    jamais par un `append()` cote serveur. La ligne posee par la machine partait donc vide la ou les
    quatorze lignes saisies a la main avant elle portent toutes le centre par defaut de la societe.
    Cas reel : ACC-PINV-2026-00092, refusee a la validation apres avoir ete corrigee sans bruit.
    """
    return doc.get("cost_center") or frappe.db.get_value("Company", doc.company, "cost_center")


def _lignes(doc):
    return [{"account_head": t.account_head, "tax_amount": t.tax_amount,
             "add_deduct_tax": t.add_deduct_tax} for t in (doc.get("taxes") or [])]


def controle(doc) -> dict:
    """Ce qui est du, ce qui est saisi, et l'ecart. -> dict (cf. `regles.controle_retenue`)."""
    return regles.controle_retenue(doc.grand_total, _lignes(doc), _seuil(), _taux())


def poser_ligne(doc) -> dict:
    """Ajoute la ligne de retenue si elle manque. -> dict. Ne touche a rien d'autre.

    Une ligne DEJA saisie n'est pas touchee ici : c'est `corriger_ligne` qui la ramene au montant
    du. Les deux gestes restent separes parce qu'ils ne repondent pas de la meme chose — l'un pose
    ce qui manque, l'autre corrige ce qui est faux.
    """
    # ⚠️ RECALCULER AVANT DE DECIDER. En `before_validate`, `grand_total` porte encore la valeur du
    # dernier enregistrement : sur une facture dont on venait de retirer la ligne de retenue, il
    # valait 1 087,021 (net de l'ancienne retenue) et la nouvelle a ete calculee a 10,870 au lieu de
    # 10,990. La base doit venir des lignes du moment, pas du total d'avant.
    doc.calculate_taxes_and_totals()
    c = controle(doc)
    if not c["due"]:
        return {"statut": "sous le seuil", **c}
    if c["saisie"]:
        return {"statut": "deja saisie", **c}

    compte = compte_retenue()
    if not frappe.db.exists("Account", compte):
        return {"statut": "compte introuvable", "compte": compte, **c}

    doc.append("taxes", {
        "charge_type": "Actual",
        "account_head": compte,
        "description": LIBELLE,
        "add_deduct_tax": "Deduct",
        "category": "Total",
        "tax_amount": c["due"],
        "cost_center": centre_de_cout(doc),
    })
    # ERPNext recalcule les totaux a partir de la table : sans cet appel, `grand_total` resterait
    # celui d'avant la ligne et la facture se validerait avec un total faux.
    doc.calculate_taxes_and_totals()
    return {"statut": "posee", **c, "compte": compte}


def corriger_ligne(doc) -> dict:
    """Ramene la ligne de retenue au montant du. -> dict.

    ⚠️ REVIREMENT ASSUME. La version precedente refusait de corriger une retenue deja saisie, au
    motif qu'elle avait pu etre negociee. Demande explicitement : une retenue a la source ne se
    negocie pas, elle se calcule — 1 % de l'assiette, et le fournisseur n'a pas voix au chapitre.
    Une saisie fausse est donc une erreur a corriger, pas un choix a respecter. Sur 2026, sept
    factures sur dix-sept etaient dans ce cas.
    """
    c = controle(doc)
    if c["verdict"] != "montant faux":
        return {"statut": c["verdict"], **c}
    compte = compte_retenue()
    # ⚠️ TOUTES LES LIGNES DE RETENUE, PAS SEULEMENT LA PREMIERE. `saisie` est la SOMME des lignes
    # de deduction : n'en redresser qu'une laisse le total faux — la premiere porte le du, les
    # suivantes (doublons de saisie) sont ramenees a zero, et chacune est dite.
    lignes_retenue = [l for l in (doc.get("taxes") or [])
                      if l.add_deduct_tax == "Deduct"
                      and regles.MOT_RETENUE in (l.account_head or "")]
    if not lignes_retenue:
        return {"statut": "ligne introuvable", **c}
    avant = flt(c["saisie"], 3)
    annulees = 0
    for i, ligne in enumerate(lignes_retenue):
        ligne.tax_amount = c["due"] if i == 0 else 0
        ligne.account_head = compte if i == 0 else ligne.account_head
        if i > 0:
            annulees += 1
    doc.calculate_taxes_and_totals()
    res = {"statut": "corrigee", "avant": avant, "apres": c["due"], **c}
    if annulees:
        res["doublons_annules"] = annulees
    return res


def completer_centre(doc) -> dict:
    """Pose le centre de couts manquant sur la ligne de retenue. -> dict.

    Geste separe de `poser_ligne` et de `corriger_ligne` parce qu'il repond d'un autre etat : une
    ligne posee AVANT ce correctif est en brouillon, au bon montant — `corriger_ligne` la declare
    conforme et la laisse passer — et pourtant invalidable. Rien dans le montant ne dit qu'elle est
    cassee ; seule la colonne vide le dit.

    Un centre deja saisi n'est jamais ecrase : sur une societe a plusieurs centres, celui que
    l'utilisateur a choisi vaut mieux que le defaut.
    """
    # ⚠️ LA LIGNE D'ABORD, LE CENTRE ENSUITE. Une societe sans centre par defaut n'est un probleme
    # que s'il y a une retenue a porter : annoncer le manque sur une facture sous le seuil ferait
    # crier au loup sur une facture que rien ne menace.
    for ligne in doc.get("taxes") or []:
        if ligne.add_deduct_tax == "Deduct" and regles.MOT_RETENUE in (ligne.account_head or ""):
            if ligne.cost_center:
                return {"statut": "deja pose", "cost_center": ligne.cost_center}
            centre = centre_de_cout(doc)
            if not centre:
                return {"statut": "aucun centre de couts"}
            ligne.cost_center = centre
            return {"statut": "pose", "cost_center": centre}
    return {"statut": "ligne introuvable"}


@frappe.whitelist()
def poser_maintenant(facture):
    """Bouton du formulaire : pose la ligne si elle manque, la corrige si elle est fausse.

    ⚠️ LE BOUTON DOIT FAIRE CE QUE FAIT L'ENREGISTREMENT, NI PLUS NI MOINS. Tant qu'il ne savait que
    poser, il repondait « rien a poser (montant faux) » sur une facture que le simple fait
    d'enregistrer aurait corrigee : deux reponses differentes pour un meme etat, c'est l'ecran qui
    devient un menteur.
    """
    frappe.only_for(["System Manager", "Accounts Manager", "Accounts User", "Purchase Manager",
                     "Purchase User"])
    doc = frappe.get_doc("Purchase Invoice", facture)
    if doc.docstatus != 0:
        frappe.throw(_("La facture est validée : sa table des taxes ne peut plus changer."))
    res = poser_ligne(doc)
    if res["statut"] != "posee":
        res = corriger_ligne(doc)
    centre = completer_centre(doc)
    res = {**res, "centre": centre}
    if res["statut"] in ("posee", "corrigee") or centre["statut"] == "pose":
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return res


def inventaire(annee=None, seuil=None) -> dict:
    """Les factures locales du periode qui ne retiennent pas ce qu'elles devraient.

    L'autre sens du controle : celui-ci ne regarde pas ce qu'on saisit aujourd'hui, mais ce qui est
    deja valide. Une retenue oubliee est une somme due au Tresor que personne ne reclamera avant
    un controle fiscal.
    """
    annee = annee or frappe.utils.getdate(frappe.utils.nowdate()).year
    seuil = flt(seuil or _seuil(), 3)
    # ⚠️ PLANCHER DU PERIMETRE : meme regle que les hooks (facture._plancher). Une annee demandee
    # entierement anterieure au plancher rend un inventaire vide — c'est voulu, l'exercice est clos.
    from bank_retenue_sync.achat.facture import _plancher as _plancher_achat
    plancher = _plancher_achat()
    rows = frappe.db.sql("""select p.name, p.supplier, p.posting_date, p.grand_total
                            from `tabPurchase Invoice` p
                            join `tabSupplier` s on s.name = p.supplier
                            where p.docstatus = 1 and s.country = %(pays)s
                              and year(p.posting_date) = %(annee)s
                              and p.posting_date >= %(plancher)s
                            order by p.posting_date""",
                         {"pays": regles.PAYS_LOCAL, "annee": annee, "plancher": plancher},
                         as_dict=1)
    out = {"annee": annee, "factures": 0, "conformes": 0, "manquantes": 0, "fausses": 0,
           "manque_total": 0.0, "detail": []}
    for r in rows:
        lignes = frappe.db.get_all("Purchase Taxes and Charges", filters={"parent": r.name},
                                   fields=["account_head", "tax_amount", "add_deduct_tax"],
                                   order_by="idx")
        c = regles.controle_retenue(r.grand_total, [dict(l) for l in lignes], seuil, _taux())
        if not c["due"]:
            continue
        out["factures"] += 1
        if c["verdict"] == "conforme":
            out["conformes"] += 1
            continue
        out["manquantes" if c["verdict"] == "manquante" else "fausses"] += 1
        out["manque_total"] = round(out["manque_total"] - c["ecart"], 3)
        out["detail"].append({"facture": r.name, "fournisseur": r.supplier,
                              "date": str(r.posting_date), **c})
    return out


@frappe.whitelist()
def inventaire_retenues(annee=None):
    frappe.only_for(["System Manager", "Accounts Manager"])
    return inventaire(annee)


@frappe.whitelist()
def recapitulatif_retenues(depuis=None):
    """Le tableau croise ERPNext ↔ TEJ des retenues d'achat depuis le plancher. -> dict.

    Une ligne par facture CONCERNEE (retenue due ou saisie), avec les deux verites cote a cote :
    ce que la COMPTABILITE retient (ligne de taxe) et ce que le FISC a recu (certificat attache —
    du module ou a la main —, depot en cours, ou certificat vivant dans l'export du portail).
    C'est la confrontation des deux qui fait le controle : une retenue comptabilisee sans
    certificat est une declaration en retard ; un certificat sans ecriture est une comptabilite
    fausse (cas reels ACC-PINV-2026-00042 et 00054).

    ⚠️ L'EXPORT PORTAIL EST UN COMPLEMENT, PAS UN PREREQUIS. Sa lecture passe par le service TEJ ;
    injoignable (dev, panne), le tableau sort quand meme — colonne TEJ fondee sur les seules
    preuves locales, et `export_disponible: False` pour que l'ecran le dise.
    """
    frappe.only_for(["System Manager", "Accounts Manager", "Accounts User", "Purchase Manager",
                     "Purchase User"])
    from bank_retenue_sync.tej import emis, matricule
    from bank_retenue_sync.tej import depot as M_depot

    from bank_retenue_sync.achat.facture import _plancher as _plancher_achat
    depuis = str(depuis or _plancher_achat())
    seuil, taux = _seuil(), _taux()

    export, export_disponible = [], True
    try:
        export = emis.certificats_emis()
    except Exception:
        export_disponible = False

    rows = frappe.db.sql("""select p.name, p.supplier, s.supplier_name, s.tax_id,
                                   p.posting_date, p.bill_no, p.grand_total
                            from `tabPurchase Invoice` p
                            join `tabSupplier` s on s.name = p.supplier
                            where p.docstatus = 1 and s.country = %(pays)s
                              and p.posting_date >= %(depuis)s
                            order by p.posting_date""",
                         {"pays": regles.PAYS_LOCAL, "depuis": depuis}, as_dict=1)

    lignes, totaux = [], {"due": 0.0, "saisie": 0.0, "manque": 0.0}
    compte = {"factures": 0, "conformes": 0, "manquantes": 0, "fausses": 0,
              "tej_emis": 0, "tej_en_cours": 0, "tej_manquants": 0}
    for r in rows:
        taxes = frappe.db.get_all("Purchase Taxes and Charges", filters={"parent": r.name},
                                  fields=["account_head", "tax_amount", "add_deduct_tax"],
                                  order_by="idx")
        c = regles.controle_retenue(r.grand_total, [dict(t) for t in taxes], seuil, taux)
        if not c["due"] and not c["saisie"]:
            continue

        # La verite TEJ, par force de preuve decroissante : le PDF attache (module ou manuel),
        # le depot en cours, l'export du portail. « manquant » = aucune des trois.
        tej = {"statut": "manquant", "detail": ""}
        cert = emis._certificat_attache(r.name)
        if cert:
            tej = {"statut": "emis",
                   "detail": _("certificat attaché à la main") if cert.get("manuel")
                   else cert.get("reference") or "",
                   "file_url": cert.get("file_url")}
        else:
            en_cours = M_depot.en_cours(r.name)
            if en_cours:
                vue = M_depot.vue(en_cours)
                tej = {"statut": vue.get("statut") or "en_analyse",
                       "detail": vue.get("numero") or ""}
            elif export:
                mat = matricule.normaliser(r.tax_id)
                vivant = next((x for x in export
                               if x["numero"] == (r.bill_no or "").strip()
                               and x["beneficiaire"] == mat
                               and x["etat"] in emis.ETATS_VIVANTS), None)
                if vivant:
                    tej = {"statut": "emis",
                           "detail": _("portail : {0} (PDF non attaché)").format(
                               vivant.get("reference") or vivant.get("etat"))}

        compte["factures"] += 1
        compte["conformes" if c["verdict"] == "conforme"
               else "manquantes" if c["verdict"] == "manquante" else "fausses"] += 1
        if tej["statut"] == "emis":
            compte["tej_emis"] += 1
        elif tej["statut"] == "manquant":
            compte["tej_manquants"] += 1
        else:
            compte["tej_en_cours"] += 1
        totaux["due"] = round(totaux["due"] + c["due"], 3)
        totaux["saisie"] = round(totaux["saisie"] + c["saisie"], 3)
        if c["verdict"] != "conforme":
            totaux["manque"] = round(totaux["manque"] - c["ecart"], 3)

        lignes.append({"facture": r.name, "fournisseur": r.supplier_name or r.supplier,
                       "date": str(r.posting_date), "bill_no": r.bill_no or "",
                       "ttc": c["ttc_avant_retenue"], "due": c["due"], "saisie": c["saisie"],
                       "verdict": c["verdict"], "tej": tej})

    return {"depuis": depuis, "seuil": seuil, "export_disponible": export_disponible,
            "lignes": lignes, "totaux": totaux, "compte": compte}
