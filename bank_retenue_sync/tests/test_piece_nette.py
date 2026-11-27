"""Le papier dit le vrai montant d'un reglement (27/11/2026) : la traite MH2C de 3 965,000 saisie a 4 005,000,
l'avoir de 40,040 ne de la retenue a la source qu'elle n'a jamais verse. Pur."""
import unittest

from bank_retenue_sync.api import bordereaux as B
from bank_retenue_sync.tej import piece_nette as N

# Le reglement de MH2C apres la regularisation du 17/11/2026 : 4 005,000 dont 40,040 non affectes (l'avoir).
MH2C = {"paid_amount": 4005.0, "unallocated_amount": 40.04, "docstatus": 1, "en_encaissement": False}


class TestDecision(unittest.TestCase):
    def test_le_cas_mh2c(self):
        d = N.decider(3965.0, True, MH2C, 40.04)
        self.assertEqual((d["faire"], d["avant"], d["apres"], d["baisse"], d["reste"]), (True, 4005.0, 3965.0, 40.0, 0.04))

    def test_papier_au_millime_de_la_retenue(self):
        d = N.decider(3964.96, True, MH2C, 40.04)
        self.assertEqual((d["faire"], d["apres"], d["reste"]), (True, 3964.96, 0.0))

    def test_rien_n_est_corrige_sans_preuve(self):
        refus = lambda *a: N.decider(*a)["raison"]  # noqa: E731
        self.assertIn("pas prouvée", refus(3965.0, False, MH2C, 40.04))
        self.assertIn("aucun avoir", refus(3965.0, True, MH2C, 0))
        self.assertIn("pas validé", refus(3965.0, True, {**MH2C, "docstatus": 2}, 40.04))
        self.assertIn("déjà sur un encaissement", refus(3965.0, True, {**MH2C, "en_encaissement": True}, 40.04))
        # L'avoir a deja servi sur une commande : y toucher laisserait cette commande sans paiement.
        self.assertIn("déjà été utilisé", refus(3965.0, True, {**MH2C, "unallocated_amount": 5.0}, 40.04))
        # Le papier vaut le reglement : c'est bien de l'argent compte, l'avoir est juste.
        self.assertIn("ne vaut pas moins", refus(4005.0, True, MH2C, 40.04))
        # Un autre ecart que la retenue (3 900 pour 4 005) : pas notre cas, une personne regarde.
        self.assertIn("ne vaut pas le règlement moins la retenue", refus(3900.0, True, MH2C, 40.04))
        self.assertIn("ne vaut pas le règlement moins la retenue", refus(3966.5, True, MH2C, 40.04))
        self.assertFalse(N.decider(3965.0, True, MH2C, 40.04)["raison"])

    def test_le_papier_ne_descend_pas_sous_ce_qui_est_impute(self):
        # 3 964,200 : dans la tolerance, mais 0,760 de moins que les 3 964,960 imputes a la facture.
        self.assertIn("moins que ce qui est imputé", N.decider(3964.2, True, MH2C, 40.04)["raison"])

    def test_lecture_prouvee(self):
        self.assertTrue(N.lecture_prouvee([{"montant": 3965.0}], 3965.0))
        self.assertTrue(N.lecture_prouvee([{"montant": 270.0}, {"montant": 1045.0}], 1315.0))
        self.assertFalse(N.lecture_prouvee([{"montant": 270.0}, {"montant": 1045.0}], 1316.0))
        self.assertFalse(N.lecture_prouvee([{"montant": 3965.0}], None))
        self.assertFalse(N.lecture_prouvee([{"montant": None}], 3965.0))
        self.assertFalse(N.lecture_prouvee([], 0))


class TestApresLaCorrection(unittest.TestCase):
    """Le reglement repris vaut le papier : le client et le montant le designent, malgre le numero mal lu."""

    TRAITE = {"numero": "0111790279", "montant": 3965.0, "tireur": "Maison Hammami de chauffage et climatisation MH2C"}

    def test_meme_client_meme_montant_trouve(self):
        repris = {"name": "ACC-PAY-NEUF", "numero": "011779021079", "montant": 3965.0, "client": "MH2C", "non_affecte": 0.04}
        r = B.rapprocher([self.TRAITE], [repris, {"name": "X", "numero": "011700000009", "montant": 3965.0, "client": "AUTRE"}])
        self.assertEqual((r[0]["etat"], r[0]["paiement"]), ("Trouvé", "ACC-PAY-NEUF"))
        self.assertEqual(r[0]["remarque"], "même client et même montant ; numéro lu 0111790279, ERPNext 011779021079")
        self.assertEqual(B.bilan(r, {"total": 3965.0, "nombre": 1}, nature="Traites")["signalements"], [])

    def test_deux_pieces_du_meme_client_au_meme_montant_on_ne_choisit_pas(self):
        deux = [{"name": "A", "numero": "011700000001", "montant": 3965.0, "client": "MH2C"},
                {"name": "B", "numero": "011700000002", "montant": 3965.0, "client": "MH2C"}]
        r = B.rapprocher([self.TRAITE], deux)
        self.assertEqual((r[0]["etat"], r[0]["paiement"]), ("À vérifier", None))          # « plusieurs ont ce montant »
