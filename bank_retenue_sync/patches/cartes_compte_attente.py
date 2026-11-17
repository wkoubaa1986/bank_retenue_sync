"""Cartes (terminal de paiement) : le compte d'attente « Cartes à encaisser » (17/11/2026, option A).

Decision de l'utilisateur : un paiement recu par carte attend dans un compte d'attente, comme un cheque ;
quand la banque credite la remise du terminal, l'ecriture le solde. Ce patch cree le compte (a cote de
« Chèques », meme nature), le pose dans les Settings, et demande le rapatriement des paiements par carte
deja saisis sur un compte de banque : il se fera au premier passage de verification (jamais ici : pas
d'ecriture comptable pendant un migrate)."""
import frappe

from bank_retenue_sync.bank import monetique


def execute():
    if not frappe.db.exists("DocType", monetique.SETTINGS) \
            or not frappe.get_meta(monetique.SETTINGS).has_field(monetique.CHAMP_COMPTE_ATTENTE):
        return
    if frappe.db.get_single_value(monetique.SETTINGS, monetique.CHAMP_COMPTE_ATTENTE):
        return
    modele = frappe.db.get_value("Account", {"account_name": "Chèques", "is_group": 0},
                                 ["parent_account", "company", "account_type", "account_currency"], as_dict=True)
    if not modele:
        return                                         # site sans le plan de comptes de la societe : rien a faire
    nom = frappe.db.get_value("Account", {"account_name": monetique.NOM_COMPTE_ATTENTE, "company": modele.company}, "name")
    if not nom:
        compte = frappe.get_doc({"doctype": "Account", "account_name": monetique.NOM_COMPTE_ATTENTE,
                                 "parent_account": modele.parent_account, "company": modele.company,
                                 "account_type": modele.account_type or "Bank",
                                 "account_currency": modele.account_currency, "is_group": 0})
        compte.insert(ignore_permissions=True)
        nom = compte.name
    frappe.db.set_single_value(monetique.SETTINGS, monetique.CHAMP_COMPTE_ATTENTE, nom)
    frappe.db.set_global(monetique.CLE_RAPATRIER, "1")
