"""Rattachement automatique des pieces saisies en retard (27/11/2026) : le dossier qui les porte. Pur."""
import unittest

from bank_retenue_sync.facturation import retards as R


class TestProchainDossier(unittest.TestCase):
    def test_le_plus_ancien_mois_non_envoye_apres_l_origine(self):
        # Le cas du 27/11/2026 : facture SCPS d'aout saisie fin novembre ; aout et septembre envoyes, octobre non.
        self.assertEqual(R.prochain_dossier("2026-08", {"2026-08", "2026-09"}, "2026-11"), "2026-10")
        # Octobre marque envoye : elle part avec novembre, le mois en cours.
        self.assertEqual(R.prochain_dossier("2026-08", {"2026-08", "2026-09", "2026-10"}, "2026-11"), "2026-11")
        self.assertEqual(R.prochain_dossier("2026-10", {"2026-10"}, "2026-11"), "2026-11")

    def test_jamais_le_mois_d_origine_ni_un_mois_envoye(self):
        for origine in ("2026-06", "2026-08", "2026-10"):
            envoyes = {"2026-06", "2026-07", "2026-08", "2026-09", "2026-10"}
            porteur = R.prochain_dossier(origine, envoyes, "2026-11")
            self.assertGreater(porteur, origine)
            self.assertNotIn(porteur, envoyes)

    def test_le_mois_en_cours_porte_ce_qui_reste_meme_note_envoye(self):
        # Le mois en cours ne peut pas etre envoye ; s'il l'etait par erreur, on ne va pas plus loin que lui.
        self.assertEqual(R.prochain_dossier("2026-10", {"2026-10", "2026-11"}, "2026-11"), "2026-11")

    def test_passage_d_annee(self):
        self.assertEqual(R.mois_suivant("2026-12"), "2027-01")
        self.assertEqual(R.mois_suivant("2026-09"), "2026-10")
        self.assertEqual(R.prochain_dossier("2026-11", {"2026-11", "2026-12"}, "2027-02"), "2027-01")

    def test_rien_a_porter(self):
        self.assertIsNone(R.prochain_dossier("2026-11", set(), "2026-11"))        # piece du mois en cours
        self.assertIsNone(R.prochain_dossier("2026-12", set(), "2026-11"))        # piece datee du futur
        self.assertIsNone(R.prochain_dossier("", set(), "2026-11"))
        self.assertIsNone(R.prochain_dossier(None, set(), "2026-11"))
        self.assertIsNone(R.prochain_dossier("2026-08", set(), None))
