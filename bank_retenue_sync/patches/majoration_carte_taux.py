"""Paiement par carte : la majoration répercutée sur le client vaut 2 % (décision du 18/11/2026).

La banque prend 3 % sur ce qui passe par le terminal ; 1 % reste à la charge de la société, 2 % sont ajoutés
aux prix de la commande, sur la part réglée par carte (calcul : customization_app.majoration_carte, qui lit ce
réglage). Un réglage « Single » ne rend pas la valeur par défaut d'un champ jamais enregistré : on l'écrit.
Ne touche pas une valeur déjà posée (0 compris : c'est « aucune majoration »)."""
import frappe

REGLAGE = "Bank Retenue Sync Settings"
CHAMP = "majoration_carte_taux"
TAUX = 2


def execute():
    if frappe.db.sql("select 1 from tabSingles where doctype = %s and field = %s", (REGLAGE, CHAMP)):
        return
    frappe.db.set_single_value(REGLAGE, CHAMP, TAUX)
