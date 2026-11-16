"""Une piece (cheque ou traite) d'un bordereau de remise collecte : ce que l'IA a lu sur le scan, et ce
qu'ERPNext porte en face. Voir `api/bordereaux.py`."""
from frappe.model.document import Document


class BRSBordereauRemisePiece(Document):
    pass
