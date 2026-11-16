"""Les premiers mouvements du compte BIAT entrent au registre de production (reprise du dev, 16/11/2026).

Rien de comptable ici : les encaissements faits sur le dev sont rejoues au premier passage de verification
(`bank/biat_reprise.rejouer`) — pas pendant le migrate, ou ERPNext n'execute aucun server script."""
import frappe


def execute():
    if not frappe.db.has_column("BRS Bank Movement", "banque"):
        return
    from bank_retenue_sync.bank import biat_reprise

    print("BIAT, mouvements de reprise :", biat_reprise.amorcer())
