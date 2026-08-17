"""Liberer de la place sur les commandes en dette, pour que l'ajustement puisse les reduire.

⚠️ C'EST LE SEUL MODULE DE L'APPLICATION QUI DETRUIT DES PIECES VALIDEES. Une commande couverte
par une piece de dette a `advance_paid = grand_total` : ERPNext refuse alors toute avance
supplementaire, et l'ecriture de bilan entiere est rejetee. Pour que la reduction passe, la
piece de dette doit d'abord disparaitre, puis etre recreee pour ce qui reste du apres reduction.

LA SEQUENCE, et son ordre n'est pas negociable :

    1. annuler puis supprimer les pieces de dette retenues   -> les commandes se liberent
    2. creer l'ecriture de bilan qui reduit ces commandes    -> l'ajustement s'impute
    3. recreer une piece de dette par commande, pour le reste

⚠️ INVERSER 2 ET 3 REBLOQUE TOUT. Une piece de dette recreee avant l'ecriture ressature la
commande, et l'ecriture est refusee comme avant — sauf qu'entre-temps les pieces d'origine ont
ete supprimees.

⚠️ L'INVARIANT A TENIR : dette avant = montant credite + dette apres. Sur juillet 2026,
1 919,556 de dette sur SAL-ORD-2026-02304 se decompose en 894,000 credites et 1 025,556 de
dette recreee. Le rompre ferait apparaitre ou disparaitre de la dette sans contrepartie.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

from bank_retenue_sync.facturation import periode
from bank_retenue_sync.partenaire.economiq import CLIENT, COMPTE_DETTES, MODE_DETTE

PRECISION = 3

SOCIETE = "Aquaworld & Servicing"
COMPTE_DEBITEURS = "Débiteurs - A&S"

#: Cle du reglage global d'auto-validation. Stocke dans les defauts Frappe, pas dans un DocType :
#: c'est une preference d'usage, pas une donnee metier a historiser.
CLE_AUTO = "brs_economiq_auto_valider"


def auto_validation() -> bool:
    return frappe.utils.cint(frappe.db.get_default(CLE_AUTO) or 0) == 1


def definir_auto_validation(actif) -> bool:
    frappe.db.set_default(CLE_AUTO, 1 if frappe.utils.cint(actif) else 0)
    frappe.db.commit()
    return auto_validation()


def pieces(jusqu_au: str) -> list:
    """Les pieces de dette du partenaire, du plus recent au plus ancien.

    Uniquement celles portees au compte des dettes : une piece « Dette non payee » creditee
    ailleurs (Aramex, par exemple) constate un encaissement par un tiers, pas une dette.
    """
    lignes = frappe.db.sql("""
        select pe.name, pe.posting_date, pe.paid_amount, r.reference_name commande,
               r.allocated_amount, so.transaction_date, so.grand_total
        from `tabPayment Entry` pe
        join `tabPayment Entry Reference` r on r.parent = pe.name and r.docstatus = 1
        join `tabSales Order` so on so.name = r.reference_name
        where pe.party = %s and pe.docstatus = 1 and pe.mode_of_payment = %s
          and pe.paid_to = %s and so.transaction_date <= %s
        order by so.transaction_date desc, pe.posting_date desc
    """, (CLIENT, MODE_DETTE, COMPTE_DETTES, jusqu_au), as_dict=True)
    return [{"payment_entry": l.name, "date": str(l.posting_date or ""),
             "montant": flt(l.paid_amount, PRECISION),
             "sales_order": l.commande, "date_commande": str(l.transaction_date or ""),
             "impute": flt(l.allocated_amount, PRECISION),
             "total_commande": flt(l.grand_total, PRECISION)} for l in lignes]


def choisir(besoin: float, candidates: list, mois: str) -> tuple[list, float]:
    """Les pieces a liberer pour degager `besoin`. Fonction pure. -> (retenues, degage).

    LA REGLE, dans cet ordre :
      1. le mois de l'ecriture d'abord ; s'il suffit a lui seul, on ne touche a rien d'autre ;
      2. dans ce mois, la PLUS PETITE piece qui couvre le besoin a elle seule, s'il en existe une ;
      3. sinon toutes celles du mois, puis on remonte, du mois le plus recent au plus ancien.

    ⚠️ LA PLUS PETITE QUI SUFFIT, PAS LA PREMIERE VENUE. Juillet 2026 offre 1 919,556, 77,000 et
    45,500 pour un besoin de 894,000 : prendre la plus grosse est inevitable ici, mais un mois
    ou une petite piece suffirait ne doit pas voir sa grosse dette detruite pour rien. Chaque
    piece detruite est une piece a recreer, donc une occasion de se tromper.
    """
    besoin = round(float(besoin or 0), PRECISION)
    if besoin <= 0.001:
        return [], 0.0

    du_mois = [c for c in candidates if (c.get("date_commande") or "")[:7] == mois]
    autres = sorted([c for c in candidates if (c.get("date_commande") or "")[:7] != mois],
                    key=lambda c: c.get("date_commande") or "", reverse=True)

    suffisantes = sorted([c for c in du_mois if c["montant"] >= besoin - 0.001],
                         key=lambda c: c["montant"])
    if suffisantes:
        return [suffisantes[0]], suffisantes[0]["montant"]

    retenues, degage = [], 0.0
    for c in sorted(du_mois, key=lambda c: -c["montant"]) + autres:
        if degage >= besoin - 0.001:
            break
        retenues.append(c)
        degage = round(degage + c["montant"], PRECISION)
    return retenues, degage


def supprimer(retenues: list, commit: bool = True) -> list:
    """Annule puis supprime les pieces retenues. IRREVERSIBLE. -> liste des numeros disparus.

    ⚠️ ANNULER NE SUFFIT PAS. Une Payment Entry annulee laisse ses references en place dans
    certaines versions et continue de peser sur `advance_paid` tant qu'elle n'est pas supprimee ;
    la commande resterait saturee et l'ecriture serait refusee malgre l'annulation.

    ⚠️ `commit=False` N'EST PAS UN DETAIL DE PERFORMANCE. Un COMMIT libere TOUS les savepoints de
    la transaction en cours : appelee avec le defaut au milieu d'une sequence protegee, cette
    fonction detruirait le point de reprise avant meme que la piece de remplacement existe, et
    l'atomicite ne serait plus qu'une intention. Seul `paiement.creer` passe False, parce que lui
    seul enveloppe les trois etapes ; `ecriture.executer` garde le commit par etape, sa sequence
    etant deja entrecoupee de creations de pieces validees.
    """
    disparues = []
    for c in retenues or []:
        nom = c["payment_entry"]
        doc = frappe.get_doc("Payment Entry", nom)
        if doc.docstatus == 1:
            doc.flags.ignore_permissions = True
            doc.cancel()
        frappe.delete_doc("Payment Entry", nom, force=1, ignore_permissions=True)
        disparues.append(nom)
    if commit:
        frappe.db.commit()
    return disparues


def recreer(restes: list, valider: bool = False, commit: bool = True) -> list:
    """Recree une piece de dette par commande, pour le montant restant du. -> liste des pieces.

    `restes` : [{sales_order, montant, date, reference}] — `montant` est ce qui reste du APRES
    reduction. Une entree a zero ne cree rien : la commande a ete integralement absorbee.

    `commit=False` : voir `supprimer`. Meme raison, meme unique appelant.
    """
    creees = []
    for r in restes or []:
        montant = flt(r.get("montant"), PRECISION)
        if montant <= 0.001:
            continue
        doc = frappe.new_doc("Payment Entry")
        doc.payment_type = "Receive"
        doc.party_type = "Customer"
        doc.party = CLIENT
        doc.company = SOCIETE
        doc.posting_date = r.get("date") or frappe.utils.nowdate()
        doc.mode_of_payment = MODE_DETTE
        doc.paid_from = COMPTE_DEBITEURS
        doc.paid_to = COMPTE_DETTES
        doc.paid_amount = montant
        doc.received_amount = montant
        doc.source_exchange_rate = 1
        doc.target_exchange_rate = 1
        doc.reference_no = r.get("reference") or r.get("sales_order")
        doc.reference_date = doc.posting_date
        doc.append("references", {"reference_doctype": "Sales Order",
                                  "reference_name": r["sales_order"],
                                  "allocated_amount": montant})
        doc.flags.ignore_permissions = True
        doc.insert()
        if valider:
            doc.submit()
        creees.append({"payment_entry": doc.name, "sales_order": r["sales_order"],
                       "montant": montant, "docstatus": doc.docstatus})
    if commit:
        frappe.db.commit()
    return creees


def plan(mois: str, ajustement: float) -> dict:
    """Ce que la liberation ferait, sans rien detruire. Le dry-run du bouton."""
    mois = periode.normaliser(mois)
    fin = periode.bornes(mois)[1]
    candidates = pieces(fin)
    retenues, degage = choisir(ajustement, candidates, mois)
    return {
        "mois": mois,
        "ajustement": round(float(ajustement or 0), PRECISION),
        "candidates": candidates,
        "a_supprimer": retenues,
        "degage": degage,
        "suffisant": degage >= round(float(ajustement or 0), PRECISION) - 0.001,
        "auto_validation": auto_validation(),
    }
