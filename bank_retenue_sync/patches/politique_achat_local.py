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
    # Seul un ecart important bloque, et chaque grandeur a son seuil (cf. `achat/regles.py`).
    # HT : le plus grand de 1 DT et de 1 % du TTC — large, c'est la que vit le bruit de lecture.
    "ecart_achat_minimal": "1",
    "ecart_achat_taux": "1",
    # TTC : 1,5 DT en absolu. TVA : 0,1 % de la TVA lue.
    #
    # ⚠️ SANS CES DEUX LIGNES, L'ECRAN DES REGLAGES AFFICHE 0 ET 0. Le code, lui, retombe sur ses
    # constantes — `_reglage(...) or ECART_TTC`, et zero est faux au sens booleen — donc 1,5 et 0,1
    # s'appliquent bel et bien. Mais un reglage qui MONTRE zero la ou il en vaut 1,5 est pire qu'un
    # reglage vide : il annonce une tolerance nulle, l'exact contraire de ce qui est en vigueur.
    "ecart_achat_ttc": "1.5",
    "ecart_achat_tva_taux": "0.1",
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
