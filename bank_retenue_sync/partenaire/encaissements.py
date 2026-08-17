"""Les reglements recus du partenaire, depuis l'ancrage — lecture seule.

⚠️ CE MODULE NE SAISIT AUCUN PAIEMENT. Il LIT les Payment Entry deja passees et les impute sur
l'echeancier. Un pointage saisi a la main a cote des pieces serait une seconde comptabilite : il
divergerait au premier reglement enregistre directement dans ERPNext, et personne ne saurait
lequel des deux a raison. Ici, la piece est la seule source.

⚠️ « DETTE NON PAYEE » N'EST PAS TOUJOURS UNE DETTE. Le mode sert aussi a enregistrer un
reglement encaisse par un tiers — Aramex livre, encaisse, et reverse. C'est le COMPTE credite qui
tranche : porte aux dettes, c'est une dette ; porte ailleurs, l'argent est bien rentre. Le
31/07/2026 en donne les deux cas le meme jour : 45,500 aux dettes, 11,000 chez Aramex.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

from bank_retenue_sync.partenaire.economiq import (CLIENT, COMPTE_DETTES, MODE_DETTE, MODE_PERTE)

PRECISION = 3


def encaisse(mode: str, compte: str) -> bool:
    """L'argent est-il reellement rentre ? Meme regle que `economiq.commandes`."""
    mode, compte = (mode or ""), (compte or "")
    return not (mode == MODE_PERTE or (mode == MODE_DETTE and compte == COMPTE_DETTES))


def recus(depuis: str) -> list:
    """Les reglements du partenaire encaisses a partir de `depuis` (incluse). -> liste triee."""
    lignes = frappe.get_all(
        "Payment Entry",
        filters={"party": CLIENT, "party_type": "Customer", "payment_type": "Receive",
                 "docstatus": 1, "posting_date": [">=", depuis]},
        fields=["name", "posting_date", "paid_amount", "mode_of_payment", "paid_to",
                "reference_no"],
        order_by="posting_date asc, name asc", limit_page_length=0)
    return [{
        "payment_entry": l.name,
        "date": str(l.posting_date or ""),
        "montant": flt(l.paid_amount, PRECISION),
        "mode": l.mode_of_payment or "",
        "compte": l.paid_to or "",
        "reference": l.reference_no or "",
    } for l in lignes if encaisse(l.mode_of_payment, l.paid_to)]


def dettes(depuis: str) -> list:
    """Les pieces portees au compte des dettes : de l'argent qui n'est PAS rentre.

    ⚠️ ELLES NE S'IMPUTENT SUR RIEN. Les compter comme des reglements eteindrait des echeances
    que personne n'a payees — c'est exactement l'inverse de ce qu'elles constatent.
    """
    lignes = frappe.get_all(
        "Payment Entry",
        filters={"party": CLIENT, "party_type": "Customer", "docstatus": 1,
                 "mode_of_payment": MODE_DETTE, "paid_to": COMPTE_DETTES,
                 "posting_date": [">=", depuis]},
        fields=["name", "posting_date", "paid_amount", "reference_no"],
        order_by="posting_date asc", limit_page_length=0)
    return [{"payment_entry": l.name, "date": str(l.posting_date or ""),
             "montant": flt(l.paid_amount, PRECISION), "reference": l.reference_no or ""}
            for l in lignes]
