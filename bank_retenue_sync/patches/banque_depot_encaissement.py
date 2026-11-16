"""Champ « Banque de dépôt » (custom_banque_depot) sur Encaissement Paiement.

Le server script « Traitement des encaissement » (customization_app) envoyait toute remise de chèques ou
de traites sur le compte Zitouna. Avec la BIAT (16/11/2026), l'encaissement dit où la remise a été déposée :
vide = Zitouna, comme avant. `bank/biat.encaisser_remises` le pose à « BIAT » ; on peut aussi le choisir à
la main sur un encaissement saisi par un humain.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    if not frappe.db.exists("DocType", "Encaissement Paiement"):
        return
    create_custom_fields({"Encaissement Paiement": [{
        "fieldname": "custom_banque_depot", "fieldtype": "Select", "label": "Banque de dépôt",
        "options": "\nZitouna\nBIAT", "insert_after": "naming_series",
        "description": "Où les chèques et les traites de cet encaissement ont été déposés. Vide : Zitouna.",
    }]}, ignore_validate=True)
    frappe.db.commit()
