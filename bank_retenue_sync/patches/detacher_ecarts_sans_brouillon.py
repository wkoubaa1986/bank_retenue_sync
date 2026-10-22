"""Detache les ecarts poses sur un brouillon qui ne porte aucune ligne de leur mouvement.

Jusqu'au 22/10/2026, l'ecart d'un bordereau dont AUCUN cheque n'avait de piece etait rattache au
brouillon commun du passage. Cas reel : le cheque 9416698 de BIOAQUA (remise 90028827, 3 000 DT,
jamais saisi en caisse) bloquait ENC-21-10-2026-00001, le lot du virement Aramex FT262948VH9P,
et la ligne bancaire de la remise restait « Orphelin » sans dire quel cheque manquait.

Seuls les BROUILLONS sont touches : un lot soumis est de l'histoire. L'ecart detache garde son
statut ; l'identification l'affiche sur la ligne du depot, et il se ferme seul quand le
mouvement entre dans un encaissement (cf. encaissement/ecarts.fermer_sans_brouillon).
"""

import frappe


def execute():
    from bank_retenue_sync.encaissement import ecarts, pending

    if not frappe.db.exists("DocType", ecarts.DOCTYPE):
        return
    par_cle = pending.documents_par_cle_bancaire()
    for e in frappe.get_all(ecarts.DOCTYPE,
                            filters={"encaissement": ("is", "set"),
                                     "statut": ("in", ("À traiter", "Auto"))},
                            fields=["name", "encaissement", "flux", "bon", "reference_bancaire"]):
        if frappe.db.get_value(ecarts.ENCAISSEMENT_DOCTYPE, e.encaissement, "docstatus") != 0:
            continue
        flux = e.flux or "aramex"
        if (par_cle.get(flux) or {}).get(ecarts.cle_bancaire(flux, e.bon, e.reference_bancaire)) \
                == e.encaissement:
            continue
        frappe.db.set_value(ecarts.DOCTYPE, e.name, "encaissement", ecarts.SANS_BROUILLON)
