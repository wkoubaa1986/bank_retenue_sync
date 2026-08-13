"""Pose les trois reglages de la regularisation RAS sur un site existant.

POURQUOI UN PATCH ET PAS UN `default` DANS LE DOCTYPE
-----------------------------------------------------
« Bank Retenue Sync Settings » est un Single : son document existe DEJA en base. Un `default`
ajoute a un champ ne s'applique qu'a la creation d'un document — sur un Single deja enregistre,
la valeur reste NULLE, donc fausse (decochee). Le site de developpement, lui, a ete regle a la
main : sans ce patch, la production se comporterait autrement que le dev, en silence, et sur des
gestes comptables — ecritures laissees en brouillon, reglements annules conserves, certificats
jamais telecharges.

⚠️ NE FORCE QUE CE QUI N'A JAMAIS ETE CHOISI. Un reglage deja pose (meme a 0) est le choix d'un
humain : le patch ne le rejoue pas. C'est pourquoi il teste la presence de la cle dans `tabSingles`
et non la valeur elle-meme — un 0 volontaire et une absence se ressemblent une fois lus.
"""
import frappe

DOCTYPE = "Bank Retenue Sync Settings"

# Les valeurs demandees par l'utilisateur le 2026-08-12, appliquees sur le site de dev.
REGLAGES = {
    "auto_submit_ras_ajustement": "1",      # creation et ajustement valides d'office
    "supprimer_reglement_annule": "1",      # le reglement annule disparait une fois le remplacant valide
    "pdf_auto_apres_rapprochement": "1",    # le certificat est demande au portail des le rapprochement
    "ras_mode_dette": "Dette non payée",    # le seul mode qui ne represente aucun argent compte
}


def execute():
    poses = {r.field for r in frappe.db.sql(
        """select field from `tabSingles` where doctype = %s""", DOCTYPE, as_dict=1)}
    for champ, valeur in REGLAGES.items():
        if champ in poses:
            continue                        # deja choisi : on ne repasse pas derriere l'utilisateur
        if champ == "ras_mode_dette" and not frappe.db.exists("Mode of Payment", valeur):
            # Le mode peut porter un autre nom sur ce site : mieux vaut laisser vide, le code
            # retombe alors sur sa constante, que pointer un mode qui n'existe pas.
            continue
        frappe.db.set_single_value(DOCTYPE, champ, valeur)
