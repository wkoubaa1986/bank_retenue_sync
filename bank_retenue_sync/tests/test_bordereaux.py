"""Collecte des bordereaux de remise : lecture du nom du fichier, numero, nom du document. Pur."""
import unittest

from bank_retenue_sync.api import bordereaux as B


class TestNomDuFichier(unittest.TestCase):
    def test_facon_de_ranger_de_l_entreprise(self):
        self.assertEqual(B.lire_nom_fichier("Ordre de versement Chèques  Bq BIAT-7473685-du 09-11-2026.pdf"),
                         {"banque": "BIAT", "nature": "Chèques", "numero": "7473685", "date_remise": "2026-11-09"})
        self.assertEqual(B.lire_nom_fichier("Ordre de versement chèque Bq Zitouna-900285827-du 20-10-20267c37c5.pdf"),
                         {"banque": "Zitouna", "nature": "Chèques", "numero": "900285827", "date_remise": "2026-10-20"})   # suffixe colle par Frappe
        self.assertEqual(B.lire_nom_fichier("Ordre de versement Traites EGES - Bq Zitouna-2702065-du 17-08-2026.pdf"),
                         {"banque": "Zitouna", "nature": "Traites", "numero": "2702065", "date_remise": "2026-08-17"})
        self.assertEqual(B.lire_nom_fichier("Ordre de versement Espèce Bq Zitouna-3334383-du 12-10-2026.pdf")["nature"], "Espèces")

    def test_nom_quelconque(self):
        self.assertEqual(B.lire_nom_fichier("scan0001.pdf"), {})
        self.assertEqual(B.lire_nom_fichier(""), {})
        self.assertEqual(B.lire_nom_fichier(None), {})
        self.assertEqual(B.lire_nom_fichier("remise BIAT 7473932.jpg"), {"banque": "BIAT", "numero": "7473932"})
        self.assertEqual(B.lire_nom_fichier("bordereau du 45-13-2026.pdf"), {})                # date impossible : pas lue

    def test_numero_et_nom_du_document(self):
        self.assertEqual(B.numero_propre(" 7 473 685 "), "7473685")
        self.assertEqual(B.nom_du_bordereau("BIAT", "7473685"), "BR-BIAT-7473685")
        self.assertEqual(B.nom_du_bordereau("Zitouna", "900 285 827"), "BR-Zitouna-900285827")
        for mauvais in ("", "12", "abc", None):
            with self.assertRaises(ValueError):
                B.numero_propre(mauvais)
        with self.assertRaises(ValueError):
            B.nom_du_bordereau("Attijari", "7473685")


if __name__ == "__main__":
    unittest.main()
