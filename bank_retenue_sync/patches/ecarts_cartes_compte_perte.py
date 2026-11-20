"""Cartes (terminal de paiement) : le compte de perte des ecarts acceptes (20/11/2026).

Decision de l'utilisateur : quand la banque a encaisse MOINS que le paiement saisi et que l'ecart est accepte sur
la remise, la difference va en « Perte de non paiement ». Ce patch pose ce compte dans les Settings s'il existe et
si rien n'est deja regle. Le compte de PRODUIT (la banque a encaisse plus) reste vide : ce cas ne s'accepte pas tant
que l'utilisateur n'a pas choisi ou porter le surplus."""
import frappe

from bank_retenue_sync.bank import monetique


def execute():
    if not frappe.db.exists("DocType", monetique.SETTINGS) \
            or not frappe.get_meta(monetique.SETTINGS).has_field(monetique.CHAMP_COMPTE_PERTE):
        return
    if frappe.db.get_single_value(monetique.SETTINGS, monetique.CHAMP_COMPTE_PERTE):
        return
    if frappe.db.exists("Account", {"name": monetique.COMPTE_PERTE_DEFAUT, "is_group": 0}):
        frappe.db.set_single_value(monetique.SETTINGS, monetique.CHAMP_COMPTE_PERTE, monetique.COMPTE_PERTE_DEFAUT)
