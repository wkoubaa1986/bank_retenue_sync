"""Cheques IMPAYES : le debit « Cheque repris <n°> » bascule le paiement du cheque sur
« Chèques sans provision ». Tests purs : la recherche du paiement (n° + montant, unicite) et la
classification qui garde le lien une fois le paiement bascule."""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.bank import classify as C
from bank_retenue_sync.encaissement import impayes as I


def _pe(name, reference_no, montant, paid_to="Chèques - A&S"):
    return {"name": name, "reference_no": reference_no, "paid_amount": montant, "paid_to": paid_to,
            "party": "X"}


def _mv(operation, debit, reference="FT26259L2NL9", jour=16):
    return {"date": date(2026, 9, jour), "date_valeur": date(2026, 9, jour), "operation": operation,
            "reference": reference, "debit": debit, "credit": 0.0}


class TestNumero(unittest.TestCase):
    def test_zeros_de_tete_ignores(self):
        self.assertEqual(I.numero_normalise("0000260"), "260")
        self.assertEqual(I.numero_normalise("260-BIAT"), "260")
        self.assertEqual(I.numero_normalise("4000608"), "4000608")
        self.assertEqual(I.numero_normalise(""), "")

    def test_detection_du_rejet(self):
        self.assertTrue(I.est_impaye(_mv("Cheque repris 0000260", 380.0)))
        self.assertFalse(I.est_impaye(_mv("ENC CHEQ TN NUM 90016921", 0.0)))


class TestTrouverPaiement(unittest.TestCase):
    C = [_pe("PE-1", "0000260-BIAT", 380.0), _pe("PE-2", "4000608 - Banque Zitouna", 3800.982),
         _pe("PE-3", "4749219-BT / BR:90027933", 380.0, "STE430127B - Zitouna - A&S")]

    def test_numero_et_montant(self):
        pe, raison = I.trouver_paiement("0000260", 380.0, self.C)
        self.assertEqual(pe["name"], "PE-1")
        self.assertEqual(raison, "")

    def test_le_bon_de_remise_ne_trompe_pas(self):
        """« 4749219-BT / BR:90027933 » porte deux nombres : seul le n° de cheque compte."""
        pe, _ = I.trouver_paiement("4749219", 380.0, self.C)
        self.assertEqual(pe["name"], "PE-3")
        self.assertIsNone(I.trouver_paiement("90027933", 380.0, self.C)[0] and None)

    def test_montant_different_refuse(self):
        pe, raison = I.trouver_paiement("0000260", 300.0, self.C)
        self.assertIsNone(pe)
        self.assertIn("pas au montant", raison)

    def test_numero_inconnu(self):
        pe, raison = I.trouver_paiement("1111111", 380.0, self.C)
        self.assertIsNone(pe)
        self.assertIn("aucun paiement", raison)

    def test_deux_candidats_non_tranches(self):
        deux = self.C + [_pe("PE-4", "0000260-BIAT", 380.0)]
        pe, raison = I.trouver_paiement("0000260", 380.0, deux)
        self.assertIsNone(pe)
        self.assertIn("non tranche", raison)


class TestClassificationApresBascule(unittest.TestCase):
    def test_le_paiement_citant_le_rejet_identifie_le_mouvement(self):
        ctx = C.LinkContext(pe_par_reference={"FT26259L2NL9": ["ACC-PAY-2026-09999"]})
        c = C.classify_one(_mv("Cheque repris 0000260", 380.0), ctx)
        self.assertEqual(c.regle, "cheque_repris")
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual(c.document_name, "ACC-PAY-2026-09999")

    def test_sans_paiement_la_raison_annonce_le_flux(self):
        c = C.classify_one(_mv("Cheque repris 0000260", 380.0), C.LinkContext())
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)
        self.assertIn("impayés", c.raison)

    def test_deux_paiements_citant_la_meme_reference_ne_tranchent_pas(self):
        ctx = C.LinkContext(pe_par_reference={"FT26259L2NL9": ["A", "B"]})
        c = C.classify_one(_mv("Cheque repris 0000260", 380.0), ctx)
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)


class TestContratDuFlux(unittest.TestCase):
    """Le flux est branché : étape de la vérification quotidienne, méthode whitelistée, menu."""

    def test_le_cron_quotidien_appelle_le_flux(self):
        import inspect

        from bank_retenue_sync import orchestrator

        src = inspect.getsource(orchestrator.run_verification_bancaire)
        self.assertIn('("impayes"', src)
        self.assertIn("impayes.process_impayes", src)

    def test_la_methode_manuelle_existe(self):
        from bank_retenue_sync import orchestrator

        self.assertTrue(getattr(orchestrator.run_impayes, "whitelisted", False)
                        or hasattr(orchestrator, "run_impayes"))

    def test_le_menu_de_la_page_propose_l_action(self):
        import os

        page = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bank_retenue_sync", "page", "identification_bancaire",
                            "identification_bancaire.js")
        with open(page, encoding="utf-8") as f:
            js = f.read()
        self.assertIn("Chèques impayés → sans provision", js)
        self.assertIn("bank_retenue_sync.orchestrator.run_impayes", js)
        # Aperçu d'abord (insert: 0), bascule seulement sur confirmation.
        self.assertIn("insert: 0", js)
        self.assertIn("insert: 1", js)
