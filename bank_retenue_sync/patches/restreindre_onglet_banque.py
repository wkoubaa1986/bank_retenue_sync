"""L'onglet « Banque » n'est visible que de deux comptes (demande utilisateur 23/09/2026).

Le role « Banque » et son attache a l'espace sont poses par `install._restreindre_onglet_banque`
a chaque migrate. Ce patch, lui, ne joue QU'UNE FOIS : il donne le role aux comptes nommes. Un
utilisateur retire ensuite du role depuis sa fiche ne le retrouvera pas au deploiement suivant —
c'est le but : l'attribution est une decision, pas une structure.

⚠️ DEUX COMPTES, PAS TROIS. aquaworld.commercial@gmail.com (Nizar Maddouri) porte le role
Accounts User mais n'a PAS acces a l'onglet (decision utilisateur 23/09/2026).
"""

import frappe

from bank_retenue_sync.install import ROLE_BANQUE, _restreindre_onglet_banque

UTILISATEURS = (
    "koubaawassim@gmail.com",
    "aquaworld.servicing@gmail.com",
)


def execute():
    _restreindre_onglet_banque()
    for email in UTILISATEURS:
        if not frappe.db.exists("User", email):
            continue
        if frappe.db.exists("Has Role", {"parent": email, "parenttype": "User",
                                          "role": ROLE_BANQUE}):
            continue
        user = frappe.get_doc("User", email)
        user.append("roles", {"role": ROLE_BANQUE})
        user.flags.ignore_permissions = True
        user.save()
    frappe.clear_cache()
    frappe.db.commit()
