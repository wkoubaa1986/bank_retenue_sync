"""Facturation mensuelle : la case « Écarter les sorties de charge » (05/11/2026) est enregistrée cochée, pour que le
formulaire du réglage montre ce que fait déjà le code (absente = active). Ne touche pas une valeur déjà posée."""
import frappe

REGLAGE = "Bank Retenue Sync Settings"
CHAMP = "ecarter_sorties_de_charge"


def execute():
    if frappe.db.sql("select 1 from tabSingles where doctype = %s and field = %s", (REGLAGE, CHAMP)):
        return
    frappe.db.set_single_value(REGLAGE, CHAMP, 1)
