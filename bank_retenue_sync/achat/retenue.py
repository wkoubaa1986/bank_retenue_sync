"""La retenue a la source SUR ACHAT : 1 % du TTC des la barre des 1 000 DT.

CE QUE CETTE ECRITURE DIT
-------------------------
Nous devons 1 087,021 DT au fournisseur, mais nous ne lui en verserons que 1 076,151 : les 10,870
restants, nous les devons au Tresor en son nom. La dette ne disparait pas, elle CHANGE DE
CREANCIER — et c'est exactement ce que l'ecriture ecrit :

    Dr « Crediteurs »                    la dette envers le fournisseur diminue
    Cr « Retenue a la source achat »     une dette envers le Tresor apparait

C'est le miroir exact de la retenue de vente (`tej/paiements.py`), ou le client nous retient 1 %
et ou nous portons un credit d'impot. Ici nous sommes de l'autre cote du guichet.

⚠️ UNE SEULE RETENUE PAR FACTURE. La facture peut etre modifiee, annulee, reprise ; le hook
`on_submit` peut donc repasser. Sans le garde-fou de l'existant, une reprise creerait une seconde
retenue et nous verserions deux fois la meme somme au Tresor.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate

from bank_retenue_sync.achat import regles

MODE_RETENUE_ACHAT = "Retenue a la source achat"
COMPTE_RETENUE_ACHAT = "Retenue a la source achat - A&S"


def _reglage(champ, defaut=None):
    try:
        v = frappe.db.get_single_value("Bank Retenue Sync Settings", champ)
        return defaut if v in (None, "") else v
    except Exception:
        return defaut


def compte_retenue() -> str:
    return _reglage("ras_achat_compte", COMPTE_RETENUE_ACHAT)


def mode_retenue() -> str:
    return _reglage("ras_achat_mode", MODE_RETENUE_ACHAT)


def existante(facture: str):
    """La retenue deja portee par cette facture, s'il y en a une (brouillon compris)."""
    return frappe.db.sql("""select pe.name, pe.docstatus, pe.paid_amount
                            from `tabPayment Entry` pe
                            join `tabPayment Entry Reference` per on per.parent = pe.name
                            where per.reference_doctype = 'Purchase Invoice'
                              and per.reference_name = %(f)s
                              and pe.mode_of_payment = %(mode)s and pe.docstatus < 2
                            limit 1""", {"f": facture, "mode": mode_retenue()}, as_dict=1)


def construire(doc, montant, insert=False, submit=False):
    """L'ecriture, en memoire. `insert`/`submit` la posent et la valident."""
    pe = frappe.new_doc("Payment Entry")
    pe.update({
        "payment_type": "Pay",
        "company": doc.company,
        "posting_date": getdate(doc.posting_date),
        "party_type": "Supplier",
        "party": doc.supplier,
        # Pay : on CREDITE `paid_from` et on DEBITE `paid_to`. La dette envers le Tresor nait au
        # credit du compte de retenue, celle envers le fournisseur s'eteint au debit des Crediteurs.
        "paid_from": compte_retenue(),
        "paid_to": doc.credit_to,
        "paid_amount": montant,
        "received_amount": montant,
        "source_exchange_rate": 1,
        "target_exchange_rate": 1,
        "mode_of_payment": mode_retenue(),
        "reference_no": doc.bill_no or doc.name,
        "reference_date": getdate(doc.bill_date or doc.posting_date),
        "remarks": ("Retenue a la source de %s %% sur achat local\nFacture %s (%s) — TTC %s\n"
                    "Fournisseur %s" % (_taux(), doc.name, doc.bill_no or "sans numero",
                                        flt(doc.grand_total, 3), doc.supplier)),
    })
    pe.append("references", {"reference_doctype": "Purchase Invoice", "reference_name": doc.name,
                             "allocated_amount": montant})
    if insert:
        pe.insert(ignore_permissions=True)
        if submit:
            pe.submit()
    return pe


def creer_pour(doc, insert=True) -> dict:
    """Cree la retenue de cette facture si elle est due. -> dict, ne leve pas.

    Ne leve pas, et c'est deliberé : appelee depuis `on_submit`, une exception annulerait la
    validation de la facture. Or la facture est juste — c'est la retenue qui a echoue. On la
    signale au journal, l'utilisateur la creera d'un bouton.
    """
    montant = regles.retenue_due(flt(doc.grand_total, 3), _seuil(), _taux())
    if not montant:
        return {"statut": "sous le seuil", "seuil": _seuil(), "ttc": flt(doc.grand_total, 3)}
    deja = existante(doc.name)
    if deja:
        return {"statut": "deja creee", "payment_entry": deja[0].name}
    if not insert:
        return {"statut": "a creer", "montant": montant}
    try:
        pe = construire(doc, montant, insert=True, submit=bool(_auto_submit()))
        return {"statut": "creee", "payment_entry": pe.name, "montant": montant,
                "valide": bool(_auto_submit())}
    except Exception as e:
        frappe.log_error(title="Retenue achat %s" % doc.name, message=frappe.get_traceback())
        return {"statut": "erreur", "message": str(e)[:200]}


@frappe.whitelist()
def creer_maintenant(facture):
    """Bouton du formulaire, pour les factures ou la creation automatique a echoue."""
    frappe.only_for(["System Manager", "Accounts Manager"])
    doc = frappe.get_doc("Purchase Invoice", facture)
    res = creer_pour(doc)
    frappe.db.commit()
    return res


def _seuil():
    # ⚠️ Un seuil a zero rendrait TOUTE facture passible de retenue ; un taux a zero n'en calculerait
    # aucune. Ni l'un ni l'autre n'est un reglage voulu : c'est `controle_achat_actif` qui coupe.
    return flt(_reglage("ras_achat_seuil", None) or regles.SEUIL_RETENUE, 3)


def _taux():
    return flt(_reglage("ras_achat_taux", None) or regles.TAUX_RETENUE, 3)


def _auto_submit():
    return frappe.utils.cint(_reglage("auto_submit_ras_ajustement", 0))
