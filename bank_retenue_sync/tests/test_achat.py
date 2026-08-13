"""Tests des regles de la facture d'achat locale.

Convention de l'app : `unittest.TestCase` pur, donnees injectees, aucun acces reseau ni base.
Les montants viennent de factures reelles (ELECTROQUIP, ACC-PINV-2026-00088).
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.achat import regles as R


def facture(pays="Tunisia", stock=1, magasin="Magasins - A&S", bill_no="26FA01134",
            bill_date=date(2026, 8, 3), ttc=1087.021, tva=163.321):
    return {"pays_fournisseur": pays, "update_stock": stock, "set_warehouse": magasin,
            "bill_no": bill_no, "bill_date": bill_date, "total_ttc": ttc, "total_tva": tva}


PIECE = [{"name": "F-1", "file_name": "Erectroquip.pdf"}]


class TestPerimetre(unittest.TestCase):
    def test_le_fournisseur_etranger_est_hors_sujet(self):
        """Une facture chinoise n'a ni retenue a la source ni scan obligatoire : lui appliquer ces
        regles bloquerait des saisies parfaitement legitimes."""
        self.assertEqual(R.manques(facture(pays="China", stock=0, magasin=None, bill_no=None), []),
                         [])

    def test_le_pays_se_lit_sans_se_soucier_de_la_casse(self):
        self.assertTrue(R.est_local("tunisia"))
        self.assertTrue(R.est_local(" Tunisia "))
        self.assertFalse(R.est_local(None))


class TestControlesBloquants(unittest.TestCase):
    def test_une_facture_complete_ne_bloque_rien(self):
        self.assertEqual(R.manques(facture(), PIECE), [])

    def test_sans_scan_joint_on_refuse(self):
        [m] = R.manques(facture(), [])
        self.assertIn("scan", m)

    def test_sans_mise_a_jour_du_stock_on_refuse(self):
        m = R.manques(facture(stock=0), PIECE)
        self.assertTrue(any("stock" in x for x in m))

    def test_sans_magasin_on_refuse(self):
        m = R.manques(facture(magasin=None), PIECE)
        self.assertTrue(any("magasin" in x for x in m))

    def test_sans_numero_ni_date_de_facture_fournisseur_on_refuse(self):
        m = R.manques(facture(bill_no=None, bill_date=None), PIECE)
        self.assertEqual(len(m), 2)

    def test_les_manques_passent_avant_les_ecarts(self):
        """Reprocher un ecart de TVA a quelqu'un qui n'a pas encore joint son scan n'aide
        personne."""
        m = R.manques(facture(), [], extraction={"total_ttc": 999.0, "total_tva": 1.0})
        self.assertEqual(len(m), 1)
        self.assertIn("scan", m[0])


class TestConfrontationAuScan(unittest.TestCase):
    """Cas reel ELECTROQUIP : la saisie porte 1 087,021 TTC / 163,321 TVA, deux lectures
    independantes du scan s'accordent sur 1 098,999 / 176,311."""

    def test_un_ecart_de_ttc_est_signale(self):
        m = R.ecarts(facture(), {"total_ttc": 1098.999, "total_tva": 163.321})
        self.assertEqual(len(m), 1)
        self.assertIn("1098.999", m[0])

    def test_un_ecart_de_tva_est_signale(self):
        m = R.ecarts(facture(), {"total_ttc": 1087.021, "total_tva": 176.311})
        self.assertEqual(len(m), 1)
        self.assertIn("TVA", m[0])

    def test_le_millime_pres_passe(self):
        """Les deux documents portent les MEMES totaux imprimes : la tolerance n'est pas une marge
        de calcul, c'est la marge de lecture d'un PDF."""
        self.assertEqual(R.ecarts(facture(), {"total_ttc": 1087.026, "total_tva": 163.321}), [])

    def test_une_valeur_absente_ne_reproche_rien(self):
        self.assertEqual(R.ecarts(facture(), {"total_ttc": None, "total_tva": None}), [])


class TestRetenueALaSource(unittest.TestCase):
    def test_au_dessus_du_seuil_un_pour_cent_du_ttc(self):
        self.assertEqual(R.retenue_due(1087.021), 10.87)

    def test_sous_le_seuil_aucune_retenue(self):
        self.assertEqual(R.retenue_due(999.999), 0.0)

    def test_le_seuil_exact_declenche_la_retenue(self):
        self.assertEqual(R.retenue_due(1000.0), 10.0)

    def test_le_seuil_et_le_taux_sont_parametrables(self):
        """La loi de finances les revise : ils ne sont pas ecrits dans le code."""
        self.assertEqual(R.retenue_due(2000.0, seuil=1500.0, taux=1.5), 30.0)


class TestDateLueSurLeScan(unittest.TestCase):
    """⚠️ La lecture de l'ANNEE est le point faible du modele : sur une meme facture, trois
    lectures ont rendu 2020, 2023 et 2026. Le numero et les montants, eux, etaient stables."""

    def test_une_date_proche_est_posee(self):
        self.assertTrue(R.date_plausible("2026-08-03", date(2026, 8, 3)))

    def test_une_facture_saisie_avec_retard_reste_plausible(self):
        self.assertTrue(R.date_plausible("2026-06-15", date(2026, 8, 3)))

    def test_une_annee_mal_lue_est_ecartee(self):
        """2020 pour une facture de 2026 : poser cette date deciderait de l'exercice de
        rattachement sans que personne ne la relise."""
        self.assertFalse(R.date_plausible("2020-08-03", date(2026, 8, 3)))
        self.assertFalse(R.date_plausible("2023-08-03", date(2026, 8, 3)))

    def test_une_date_illisible_est_ecartee(self):
        for valeur in (None, "", "pas une date"):
            self.assertFalse(R.date_plausible(valeur, date(2026, 8, 3)))


if __name__ == "__main__":
    unittest.main()
