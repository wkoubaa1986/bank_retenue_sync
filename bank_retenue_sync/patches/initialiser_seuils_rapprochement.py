"""Pose les seuils du rapprochement client a leur valeur par defaut : 1 DT chacun.

⚠️ POURQUOI UN PATCH ET PAS LE `default` DU CHAMP. Sur un Single deja cree, ajouter un champ
Currency ne lui donne PAS sa valeur par defaut : la ligne nait a 0. Or 0 veut dire « signale-moi
le moindre centime » — mesure sur la base reelle, 145 clients en ecart au lieu de 108. L'ecran
serait donc arrive avec un reglage que personne n'a choisi et un tiers de bruit en plus.

⚠️ ET POURQUOI IL NE SE REJOUE PAS. Zero est un choix VALIDE. Un patch ne s'execute qu'une fois :
si l'utilisateur descend ensuite un seuil a 0 deliberement, rien ne viendra le remonter. Une
fonction `after_migrate` idempotente, elle, l'aurait ecrase a chaque deploiement.
"""

import frappe

SETTINGS = "Bank Retenue Sync Settings"
DEFAUT = 1.0
CHAMPS = ("tolerance_ecart_montant", "tolerance_ecart_bl")


def execute():
    if not frappe.db.exists("DocType", SETTINGS):
        return
    for champ in CHAMPS:
        try:
            actuel = frappe.db.get_single_value(SETTINGS, champ)
        except Exception:
            continue
        if not actuel:
            frappe.db.set_single_value(SETTINGS, champ, DEFAUT)
    frappe.clear_cache()
    frappe.db.commit()
