# Copyright (c) 2026, Wassim koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSClientRapprochementIgnore(Document):
    # Un client dont on a DÉCIDÉ que l'écart ne se corrigerait pas : ancien litige soldé
    # autrement, reprise d'historique, compte de régularisation. La logique vit dans
    # bank_retenue_sync/clients/rapprochement.py — ce DocType ne porte que la décision et
    # son motif, pour qu'on puisse revenir dessus en sachant pourquoi.
    pass
