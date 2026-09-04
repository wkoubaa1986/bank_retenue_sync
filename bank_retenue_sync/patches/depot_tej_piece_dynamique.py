"""Declare la nature des depots TEJ existants avant que `facture` ne devienne un lien dynamique.

⚠️ UN LIEN DYNAMIQUE SANS SON CHAMP DE TYPE NE VALIDE PLUS RIEN. `BRS Depot TEJ.facture` pointait
une facture d'achat et rien d'autre ; il pointe desormais une facture OU une ecriture de journal,
parce qu'une retenue se preleve aussi sur une depense de caisse — qui ne produit pas de facture.
Les lignes deja en base n'ont pas de `piece_type` : sans ce patch, elles deviendraient invalides
et le prochain enregistrement de chacune echouerait.

Toutes les lignes anterieures viennent de factures d'achat : c'etait le seul chemin possible.
"""

import frappe


def execute():
    if not frappe.db.exists("DocType", "BRS Depot TEJ"):
        return
    if "piece_type" not in frappe.db.get_table_columns("BRS Depot TEJ"):
        return
    frappe.db.sql("""UPDATE `tabBRS Depot TEJ`
                     SET piece_type = 'Purchase Invoice'
                     WHERE piece_type IS NULL OR piece_type = ''""")
    frappe.db.commit()
