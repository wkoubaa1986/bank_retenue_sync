"""Chaque mouvement du registre porte sa banque (16/11/2026 : la BIAT rejoint Zitouna).

Tout ce qui est au registre avant ce jour vient du compte Zitouna. Le champ a « Zitouna » pour
valeur par defaut ; ce patch le pose sur les lignes ou la colonne serait restee vide."""
import frappe


def execute():
    if not frappe.db.has_column("BRS Bank Movement", "banque"):
        return
    frappe.db.sql("update `tabBRS Bank Movement` set `banque` = 'Zitouna' where ifnull(`banque`, '') = ''")
