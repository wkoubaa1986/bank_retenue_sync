"""Tests du controle « vente Economiq = BL de main d'oeuvre seulement ».

Convention : `unittest.TestCase` pur — on eprouve la REGLE, pas les chiffres de la base.
"""
from __future__ import annotations

import unittest

from bank_retenue_sync.partenaire import controle_bl as K

MO = K.GROUPE_MAIN_D_OEUVRE
ADMIS = {MO}


def ligne(code, groupe, qty=1, amount=100.0):
    return {"item_code": code, "item_name": code, "qty": qty, "amount": amount,
            "item_group": groupe}


def bon(nom, *lignes):
    return {"name": nom, "posting_date": "2026-09-30", "lignes": list(lignes)}


VENTE = {"name": "SAL-ORD-2026-03495", "customer": "Mehdi Batbout", "mois": "2026-09",
         "vente": 705}


class TestRegle(unittest.TestCase):

    def test_bl_main_d_oeuvre_seule_est_conforme(self):
        """Le cas reel de toutes les ventes depuis juillet : une ligne M-I-OD."""
        self.assertIsNone(K.verifier(VENTE, [bon("MAT-DN-1", ligne("M-I-OD", MO))], ADMIS))

    def test_plusieurs_bl_tous_main_d_oeuvre(self):
        bons = [bon("MAT-DN-1", ligne("M-I-OD", MO)), bon("MAT-DN-2", ligne("M-R-OD", MO))]
        self.assertIsNone(K.verifier(VENTE, bons, ADMIS))

    def test_un_article_de_stock_est_signale(self):
        a = K.verifier(VENTE, [bon("MAT-DN-1", ligne("M-I-OD", MO),
                                   ligne("OSMO-5", "RO domestique avec pompe", amount=500))], ADMIS)
        self.assertEqual(a["type"], K.HORS_MAIN_D_OEUVRE)
        self.assertEqual([l["item_code"] for l in a["lignes"]], ["OSMO-5"])
        self.assertEqual(a["lignes"][0]["bon"], "MAT-DN-1")
        self.assertIn("OSMO-5", a["detail"])

    def test_la_ligne_fautive_peut_etre_sur_le_second_bl(self):
        a = K.verifier(VENTE, [bon("MAT-DN-1", ligne("M-I-OD", MO)),
                               bon("MAT-DN-2", ligne("CART-1", "Cartouches à charbon"))], ADMIS)
        self.assertEqual(a["bons"], ["MAT-DN-1", "MAT-DN-2"])
        self.assertEqual(a["lignes"][0]["bon"], "MAT-DN-2")

    def test_livraison_n_est_pas_de_la_main_d_oeuvre(self):
        """Le bilan traite « Livraison » comme un passage, mais certains de ses articles sont
        geres en stock : il ne passe pas le controle."""
        a = K.verifier(VENTE, [bon("MAT-DN-1", ligne("LIV", "Livraison"))], ADMIS)
        self.assertEqual(a["type"], K.HORS_MAIN_D_OEUVRE)

    def test_article_sans_groupe_est_signale(self):
        a = K.verifier(VENTE, [bon("MAT-DN-1", ligne("X", None))], ADMIS)
        self.assertIn("sans groupe", a["detail"])

    def test_sous_groupe_admis(self):
        admis = {MO, "Main d’œuvre - Installation"}
        self.assertIsNone(K.verifier(
            VENTE, [bon("MAT-DN-1", ligne("M-I-OD", "Main d’œuvre - Installation"))], admis))

    def test_vente_sans_bl(self):
        a = K.verifier(VENTE, [], ADMIS)
        self.assertEqual(a["type"], K.SANS_BL)
        self.assertEqual(a["detail"], "aucun BL validé")

    def test_bl_en_brouillon_le_dit(self):
        a = K.verifier({**VENTE, "brouillons": ["MAT-DN-9"]}, [], ADMIS)
        self.assertEqual(a["type"], K.SANS_BL)
        self.assertIn("MAT-DN-9", a["detail"])

    def test_l_anomalie_porte_la_vente(self):
        a = K.verifier(VENTE, [], ADMIS)
        self.assertEqual((a["sales_order"], a["customer"], a["mois"], a["vente"]),
                         ("SAL-ORD-2026-03495", "Mehdi Batbout", "2026-09", 705.0))


class TestMois(unittest.TestCase):

    def test_depuis_juillet(self):
        self.assertEqual(K.DEBUT, "2026-07")
        self.assertEqual(K.mois_controles("2026-07", "2026-10"),
                         ["2026-07", "2026-08", "2026-09", "2026-10"])

    def test_passage_d_annee(self):
        self.assertEqual(K.mois_controles("2026-11", "2027-02"),
                         ["2026-11", "2026-12", "2027-01", "2027-02"])

    def test_un_seul_mois(self):
        self.assertEqual(K.mois_controles("2026-07", "2026-07"), ["2026-07"])

    def test_groupe_avec_apostrophe_typographique(self):
        """Le nom en base porte U+2019 et la ligature œ : une apostrophe droite ne matcherait rien."""
        self.assertEqual(K.GROUPE_MAIN_D_OEUVRE, "Main d’œuvre")


if __name__ == "__main__":
    unittest.main()
