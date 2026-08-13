# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSSourceEmail(Document):
    """Une source d'emails declenchant un traitement (facture fournisseur, avis de paiement...).

    Avant cette table, les expediteurs et sujets etaient ecrits DEUX fois : declares dans
    `mail/sources.py` (couche declarative jamais consommee) et reecrits en dur dans
    `orchestrator.py` (la seule reellement utilisee). Les deux pouvaient diverger sans que rien
    ne le signale. La table est desormais la source unique de verite.
    """

    pass
