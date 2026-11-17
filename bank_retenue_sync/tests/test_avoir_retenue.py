"""L'avoir ne d'une retenue a la source (17/11/2026) : ce que la regularisation note sur le certificat.
Fonction pure : aucun site."""
import unittest

from bank_retenue_sync.tej import paiements as P


class TestAvoirANoter(unittest.TestCase):
    def test_argent_compte_dont_une_part_reste_sans_dette_a_eteindre(self):
        # MH2C : traite de 4 005,000 pour le TTC entier, puis certificat de 40,040 ; aucune autre dette.
        self.assertEqual(P.avoir_a_noter(True, 40.04, True), 40.04)

    def test_tout_a_ete_reaffecte_a_une_autre_dette(self):
        self.assertEqual(P.avoir_a_noter(True, 0.0, True), 0.0)
        self.assertEqual(P.avoir_a_noter(True, 0.0004, True), 0.0)        # un demi-millime n'est pas un avoir

    def test_une_dette_reduite_ne_laisse_aucun_avoir(self):
        # Le reglement repris etait une « Dette non payée » : ce n'est pas de l'argent, rien n'a ete verse en trop.
        self.assertEqual(P.avoir_a_noter(False, 40.04, True), 0.0)

    def test_sans_reglement_repris(self):
        self.assertEqual(P.avoir_a_noter(True, 40.04, False), 0.0)
        self.assertEqual(P.avoir_a_noter(True, None, True), 0.0)


if __name__ == "__main__":
    unittest.main()
