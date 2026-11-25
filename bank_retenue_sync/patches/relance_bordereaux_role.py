"""Relance des bordereaux de remise manquants (25/11/2026) : le role qui voit la barre, et ceux qui l'ont.

Decision de l'utilisateur : « l'alerte de bordereaux manquants pour Salma et Nejib ». Le role
« Relance bordereaux » porte la barre (`bank/bordereaux_manquants`) ; pour changer qui la voit, on ajoute ou on
retire ce role sur la fiche Utilisateur. Ce patch ne fait que poser le depart : il ne retire jamais le role."""
import frappe

from bank_retenue_sync.bank.bordereaux_manquants import ROLE

DESTINATAIRES = (
    "salmaaquaworldservicing@gmail.com",    # Salma Ben Saïd
    "aquaworld.servicing@gmail.com",        # Nejib Koubaa, le gerant
)


def execute():
    if not frappe.db.exists("Role", ROLE):
        frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(ignore_permissions=True)
    for user in DESTINATAIRES:
        if frappe.db.get_value("User", user, "enabled") and ROLE not in frappe.get_roles(user):
            frappe.get_doc("User", user).add_roles(ROLE)
