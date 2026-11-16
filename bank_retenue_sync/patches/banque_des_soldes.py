"""Chaque releve de solde porte sa banque (16/11/2026 : la BIAT a son tableau de bord).

Tout ce qui est archive avant ce jour est un solde Zitouna. Le champ a « Zitouna » pour valeur par
defaut ; ce patch le pose sur les lignes ou la colonne serait restee vide."""
import frappe


def execute():
    if not frappe.db.has_column("BRS Solde Bancaire", "banque"):
        return
    frappe.db.sql("update `tabBRS Solde Bancaire` set `banque` = 'Zitouna' where ifnull(`banque`, '') = ''")
