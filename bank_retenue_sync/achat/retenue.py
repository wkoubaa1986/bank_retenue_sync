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


def _lignes(doc):
    return [{"account_head": t.account_head, "tax_amount": t.tax_amount,
             "add_deduct_tax": t.add_deduct_tax} for t in (doc.get("taxes") or [])]


def controle(doc) -> dict:
    """Ce qui est du, ce qui est saisi, et l'ecart. -> dict (cf. `regles.controle_retenue`)."""
    return regles.controle_retenue(doc.grand_total, _lignes(doc), _seuil(), _taux())


def poser_ligne(doc) -> dict:
    """Ajoute la ligne de retenue si elle manque. -> dict. Ne touche a rien d'autre.

    Une retenue DEJA saisie n'est jamais corrigee d'office, meme fausse : le montant peut avoir ete
    negocie, ou repris d'un accord avec le fournisseur. L'ecart est signale au moment de valider,
    et c'est un humain qui tranche.
    """
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
    })
    # ERPNext recalcule les totaux a partir de la table : sans cet appel, `grand_total` resterait
    # celui d'avant la ligne et la facture se validerait avec un total faux.
    doc.calculate_taxes_and_totals()
    return {"statut": "posee", **c, "compte": compte}


@frappe.whitelist()
def poser_maintenant(facture):
    """Bouton du formulaire, pour une facture ou la ligne n'a pas ete posee."""
    frappe.only_for(["System Manager", "Accounts Manager", "Accounts User", "Purchase Manager",
                     "Purchase User"])
    doc = frappe.get_doc("Purchase Invoice", facture)
    if doc.docstatus != 0:
        frappe.throw(_("La facture est validée : sa table des taxes ne peut plus changer."))
    res = poser_ligne(doc)
    if res["statut"] == "posee":
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
    rows = frappe.db.sql("""select p.name, p.supplier, p.posting_date, p.grand_total
                            from `tabPurchase Invoice` p
                            join `tabSupplier` s on s.name = p.supplier
                            where p.docstatus = 1 and s.country = %(pays)s
                              and year(p.posting_date) = %(annee)s
                            order by p.posting_date""",
                         {"pays": regles.PAYS_LOCAL, "annee": annee}, as_dict=1)
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
