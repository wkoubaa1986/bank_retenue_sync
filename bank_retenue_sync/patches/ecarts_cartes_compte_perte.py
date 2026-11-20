"""Cartes (terminal de paiement) : le compte des ecarts acceptes (20/11/2026).

Decision de l'utilisateur : un ecart de montant accepte sur une remise va dans « Perte de non paiement » — au DEBIT
quand la banque a encaisse MOINS que le paiement saisi, au CREDIT quand elle a encaisse PLUS (« ajoute ca dans perte
de non paiement mais en credit »). Ce patch pose ce compte dans les deux reglages, la ou rien n'est deja regle."""
import frappe

from bank_retenue_sync.bank import monetique


def execute():
    if not frappe.db.exists("DocType", monetique.SETTINGS) \
            or not frappe.db.exists("Account", {"name": monetique.COMPTE_PERTE_DEFAUT, "is_group": 0}):
        return
    meta = frappe.get_meta(monetique.SETTINGS)
    for champ in (monetique.CHAMP_COMPTE_PERTE, monetique.CHAMP_COMPTE_PRODUIT):
        if meta.has_field(champ) and not frappe.db.get_single_value(monetique.SETTINGS, champ):
            frappe.db.set_single_value(monetique.SETTINGS, champ, monetique.COMPTE_PERTE_DEFAUT)
