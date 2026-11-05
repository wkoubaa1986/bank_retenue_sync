# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSCompteHorsDossier(Document):
    """Un compte de FINANCEMENT dont les dépenses restent hors du dossier mensuel (05/11/2026).

    Une dépense dont l'écriture crédite ce compte (ex. « Compte Wassim Euros - A&S » : dépenses payées
    en euros) ne s'affiche pas dans la liste des charges de la facturation mensuelle, ni dans son
    export — cf. facturation/charges.py, `raison_hors_dossier`."""

    pass
