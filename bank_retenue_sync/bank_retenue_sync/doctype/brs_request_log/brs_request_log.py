# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSRequestLog(Document):
    """Journal des appels sortants vers tej-bank-service.

    Calque sur `WooCommerce Request Log` (woocommerce_fusion/tasks/utils.py) : on journalise le
    texte de la reponse et son code, jamais l'objet `Response` — `frappe.enqueue` serialise ses
    arguments.
    """

    pass
