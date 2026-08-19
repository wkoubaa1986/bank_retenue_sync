# Copyright (c) 2026, Aquaworld and contributors
# For license information, please see license.txt

"""Ecart constate entre un Payment Advice Aramex et les pieces ERPNext, rattache au brouillon
Encaissement Paiement qui porte le lot. Un ecart `bloquant=1` et `statut='À traiter'` empeche
la soumission du brouillon (hook before_submit, cf. encaissement/ecarts.py) : c'est le point
d'intervention humaine voulu — la resolution (perte / ajustement / avoir) se fait par les
boutons du formulaire Encaissement Paiement, jamais automatiquement."""

from frappe.model.document import Document


class BRSEcartEncaissement(Document):
    pass
