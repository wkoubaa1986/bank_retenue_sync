# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class RetenueCertificate(Document):
    """Certificat de retenue a la source recu du portail TEJ (le client retient, nous subissons).

    Nomme par `reference` (unique) : re-ingerer une periode chevauchante ne cree pas de doublon.

    Le document n'est PAS soumettable, et c'est voulu : un certificat n'est pas une piece
    comptable, c'est une preuve. Sa piece, c'est la Payment Entry qu'il justifie. Le figer par une
    soumission interdirait l'arbitrage humain — or corriger le client d'un certificat des mois
    apres reste legitime, et cette correction est justement ce qui apprend a la machine.
    """

    def validate(self):
        self._check_amounts()
        self._calculer_taux()

    def on_update(self):
        self._tracer_arbitrage_humain()

    def _check_amounts(self):
        """La retenue ne peut pas depasser l'assiette. Le certificat est SIGNALE, pas refuse.

        ⚠️ Ce controle levait, et c'etait une erreur de conception : ces montants ne sont pas
        saisis chez nous, ils viennent du portail. L'export reel porte une ligne a 510 DT retenus
        pour 51 DT d'assiette — la faire echouer bloquait l'ingestion des 91 autres certificats.
        Une donnee absurde a la source doit rester visible et rester ecartee de l'automatisme ;
        la refuser reviendrait a ne rien savoir d'elle.
        """
        brut = flt(self.total_brut)
        retenue = flt(self.montant_retenue)
        if brut and retenue and retenue > brut and not self.anomalie:
            self.anomalie = 1
            self.anomalie_raison = _("Montant retenu ({0}) superieur au total TTC ({1}).").format(
                retenue, brut)

    def _calculer_taux(self):
        """Taux = retenue / TTC. VIDE si l'assiette est nulle.

        Un export reel porte une ligne a HT et TVA nuls avec une retenue non nulle : la division
        rendrait 999,98 %, un nombre qui a l'air d'une donnee alors qu'il n'en est pas une. Mieux
        vaut aucun taux et une anomalie signalee qu'un chiffre qu'on croira lire.
        """
        brut = flt(self.total_brut)
        self.taux = round(flt(self.montant_retenue) / brut * 100, 3) if brut else None

    def _tracer_arbitrage_humain(self):
        """Un client pose a la main fait autorite et devient l'alias des certificats suivants.

        C'est la boucle d'apprentissage du flux : la machine ne repassera jamais sur ce champ
        (`upsert` protege les documents `Manually Matched`), et le prochain certificat du meme
        declarant sera rapproche sans fuzzy ni IA.
        """
        if self.flags.in_insert or self.flags.ignore_arbitrage:
            return
        avant = self.get_doc_before_save()
        if not avant or avant.customer == self.customer or not self.customer:
            return
        frappe.db.set_value(self.doctype, self.name, {
            "match_status": "Manually Matched",
            "match_method": "manuel",
            "match_score": 1.0,
            "match_raison": _("Client pose manuellement par {0}.").format(frappe.session.user),
        }, update_modified=False)
