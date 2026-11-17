"""Une remise du terminal de paiement (TPE), telle que la banque l'annonce par courriel.

Demande du 17/11/2026 : « la BIAT envoie des releves des operations par email ; ca doit etre utilise pour
la reconnaissance des transactions ». Une fiche par remise : le detail vient du « Relevé Monétique »
(chaque transaction et son n° d'autorisation), les montants de l'« Avis Crédit » (brut − commission −
TVA = net verse). Les deux PDF sont lus sans IA (`bank/monetique.py`), qui remplit aussi cette fiche :
elle ne se saisit pas a la main, et ne comptabilise rien.
"""
import frappe
from frappe.model.document import Document

from bank_retenue_sync.bank import monetique


class BRSRemiseTPE(Document):
    def autoname(self):
        self.name = monetique.nom_remise(frappe.utils.getdate(self.date_traitement), self.terminal, self.remise)
