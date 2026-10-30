"""Salaire de Salma Ben Saïd (demande utilisateur 30/10/2026).

Embauchee le 28/09/2026 (HR-EMP-00011), en poste depuis le debut d'octobre : 700 DT par mois,
vires avec les autres salaires deux jours avant la fin du mois. La regle est ajoutee aux depenses
recurrentes des reglages, sur le modele des salaires existants, et l'echeance d'OCTOBRE 2026 est
creee tout de suite (ecriture anticipee + ordre de paiement), comme le scheduler l'a fait pour les
autres le 29/10 — le patch peut tourner apres la fin du mois, l'echeance reste celle d'octobre.
Idempotent : la regle n'est ajoutee qu'une fois (cle), l'echeance qu'une fois (numero de reference).
"""
from __future__ import annotations

from datetime import date

import frappe

CLE = "salaire_salma_ben_said"
LIBELLE = "Salaire Salma Ben Saïd"
MONTANT = 700.0
PREMIER_MOIS = date(2026, 10, 31)      # l'echeance du 29/10/2026 est < ce jour : rattrapage

REGLE = {
    "actif": 1, "cle": CLE, "libelle": LIBELLE, "type": "Salaire", "montant": MONTANT,
    "bank_rule": "virement_emis", "motifs_libelle": "", "tolerance": 0.001,
    "declencheur": "Calendrier", "jours_avant_fin_mois": 2, "jour_declenchement": 0,
    "mois_ancre": 0, "periodicite": "Mensuel", "jour_reference": 0,
    "compte_banque": "STE430127B - Zitouna - A&S", "compte_charge": "Salaire - A&S",
    "compte_attente": "Compte de découvert bancaire - A&S", "compte_tva": None, "taux_tva": 0,
    "mode_paiement": "Virement", "template_reference": LIBELLE + " {mm}-{yyyy}",
    "idempotence": "Les deux",
    "notes": "Embauche du 28/09/2026 (HR-EMP-00011), en poste depuis octobre 2026 : premier "
             "virement fin 10/2026, 700 DT. Ajoutee par patch le 30/10/2026.",
}


def execute():
    from bank_retenue_sync.expenses import calendrier, contrats as K

    if not frappe.db.exists("DocType", K.SETTINGS):
        return
    doc = frappe.get_doc(K.SETTINGS)
    table = "depenses_recurrentes" if doc.meta.has_field("depenses_recurrentes") else _table(doc)
    if not table:
        return
    row = next((r for r in (doc.get(table) or []) if r.cle == CLE), None)
    if row is None:
        row = doc.append(table, REGLE)
        doc.flags.ignore_permissions = True
        doc.save()
    # L'echeance d'octobre 2026, comme pour les autres salaires (idempotent par numero de reference).
    res = calendrier.process_calendrier(jour=PREMIER_MOIS, rows=[row.as_dict()], rattrapage=True)
    frappe.db.commit()
    return res


def _table(doc) -> str | None:
    """Le champ-table des depenses recurrentes, quel que soit son nom."""
    for df in doc.meta.get_table_fields():
        if df.options == "BRS Depense Recurrente":
            return df.fieldname
    return None
