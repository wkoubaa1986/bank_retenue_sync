# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSSyncRun(Document):
    """Trace d'une synchronisation (certificats TEJ, mouvements bancaires, remises).

    `payload_hash` est unique : re-ingerer une periode chevauchante doit etre un no-op.
    """

    pass
