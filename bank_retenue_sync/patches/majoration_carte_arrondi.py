"""Majoration carte : l'arrondi devient un réglage — au dixième de dinar supérieur par défaut (18/11/2026).

« La majoration de la carte est arrondie au 0,1 près supérieur, c'est plus simple », puis « est-ce que ça se
change dans le réglage ? » : le pas (0,1) et le sens (Supérieur) de l'arrondi sont deux champs du réglage, à
côté du taux. Un réglage « Single » ne rend pas la valeur par défaut d'un champ jamais enregistré : on les
écrit. Ne touche pas une valeur déjà posée (0 compris : « pas d'arrondi »)."""
import frappe

REGLAGE = "Bank Retenue Sync Settings"
VALEURS = {"majoration_carte_arrondi": 0.1, "majoration_carte_arrondi_sens": "Supérieur"}


def execute():
    for champ, valeur in VALEURS.items():
        if frappe.db.sql("select 1 from tabSingles where doctype = %s and field = %s", (REGLAGE, champ)):
            continue
        frappe.db.set_single_value(REGLAGE, champ, valeur)
