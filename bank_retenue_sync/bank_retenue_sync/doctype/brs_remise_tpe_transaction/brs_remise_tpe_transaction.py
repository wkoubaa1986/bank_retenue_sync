"""Une transaction par carte d'une remise du terminal : ce que la banque a releve, et le paiement
d'ERPNext qui lui correspond. Voir `bank/monetique.py`."""
from frappe.model.document import Document


class BRSRemiseTPETransaction(Document):
    pass
