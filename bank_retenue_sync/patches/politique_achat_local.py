"""Pose les reglages du controle des achats locaux sur un site existant.

Meme raison que `politique_regularisation_ras` : « Bank Retenue Sync Settings » est un Single, son
document existe deja, et un `default` ajoute a un champ ne s'applique qu'a la creation. Sans ce
patch, le seuil vaudrait 0 et le taux 0 : aucune retenue ne serait jamais calculee, en silence.
"""
import frappe

DOCTYPE = "Bank Retenue Sync Settings"

REGLAGES = {
    "controle_achat_actif": "1",
    "ras_achat_seuil": "1000",
    "ras_achat_taux": "1",
    "ras_achat_compte": "Retenue a la source achat - A&S",
    "ras_achat_mode": "Retenue a la source achat",
}


def execute():
    for champ, valeur in REGLAGES.items():
        actuel = frappe.db.get_single_value(DOCTYPE, champ)
        if actuel not in (None, "", 0, 0.0, "0"):
            continue                    # deja choisi : on ne repasse pas derriere l'utilisateur
        if champ == "ras_achat_compte" and not frappe.db.exists("Account", valeur):
            continue
        if champ == "ras_achat_mode" and not frappe.db.exists("Mode of Payment", valeur):
            continue
        frappe.db.set_single_value(DOCTYPE, champ, valeur)
