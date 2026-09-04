# Copyright (c) 2026, Wassim koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSRetenueAchatAEmettre(Document):
    # La file des certificats de retenue à émettre pour les dépenses de caisse facturées.
    # La logique vit dans bank_retenue_sync/tej/emis_journal.py : ce DocType ne porte que
    # l'état d'une ligne et ce qui lui manque encore.
    pass
