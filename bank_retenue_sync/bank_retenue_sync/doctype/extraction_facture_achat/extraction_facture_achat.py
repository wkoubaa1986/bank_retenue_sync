"""Ce que le scan de la facture fournisseur dit, garde a cote de ce qui a ete saisi.

Un document par facture d'achat : c'est lui qui rend le controle des totaux VERIFIABLE six mois
plus tard, quand personne ne se souviendra de ce que la lecture automatique avait compris.
"""

from frappe.model.document import Document


class ExtractionFactureAchat(Document):
    pass
