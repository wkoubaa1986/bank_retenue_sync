"""Alerte des remises a corriger (27/11/2026) : le role qui voit l'alerte, et ceux qui l'ont.

Decision de l'utilisateur : « creer une alerte lorsque les cas de cheque ou traite comme ce cas arrivent, pour
Nejib, pour qu'il puisse corriger », puis « meme en carte de credit, pour Nejib et moi-meme, pour que je sois au
courant ». Le role « Alerte remises à corriger » porte l'alerte (`bank/remises_a_corriger`) ; pour changer qui la
voit, on ajoute ou on retire ce role sur la fiche Utilisateur. Ce patch pose le depart : il ne retire jamais le role."""
import frappe

from bank_retenue_sync.bank.remises_a_corriger import ROLE

DESTINATAIRES = (
    "aquaworld.servicing@gmail.com",        # Nejib Koubaa, le gerant
    "koubaawassim@gmail.com",               # Wassim Koubaa
)


def execute():
    if not frappe.db.exists("Role", ROLE):
        frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(ignore_permissions=True)
    for user in DESTINATAIRES:
        if frappe.db.get_value("User", user, "enabled") and ROLE not in frappe.get_roles(user):
            frappe.get_doc("User", user).add_roles(ROLE)
