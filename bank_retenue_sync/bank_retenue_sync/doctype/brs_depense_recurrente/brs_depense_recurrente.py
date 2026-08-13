# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSDepenseRecurrente(Document):
    """Une depense recurrente parametree (salaire, loyer, recharge de carte).

    Editable depuis les Settings : un salaire ou un loyer qui change ne doit jamais demander une
    modification de code (le loyer est deja passe de 5000 a 5500 en aout 2025).
    """

    pass
