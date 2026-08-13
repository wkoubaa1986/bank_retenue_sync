"""Tests des regles de la facture d'achat locale.

Convention de l'app : `unittest.TestCase` pur, donnees injectees, aucun acces reseau ni base.
Les montants viennent de factures reelles (ELECTROQUIP, ACC-PINV-2026-00088).
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.achat import regles as R


# La table des taxes d'ELECTROQUIP (ACC-PINV-2026-00088), telle qu'elle est saisie.
TAXES = [{"account_head": "TVA 19% - A&S", "tax_amount": 175.311, "add_deduct_tax": "Add"},
         {"account_head": "Retenue a la source achat - A&S", "tax_amount": 11.99,
          "add_deduct_tax": "Deduct"}]


def facture(pays="Tunisia", stock=1, magasin="Magasins - A&S", bill_no="26FA01134",
            bill_date=date(2026, 8, 3), ttc=1099.011, tva=175.311, controle=None):
    return {"pays_fournisseur": pays, "update_stock": stock, "set_warehouse": magasin,
            "bill_no": bill_no, "bill_date": bill_date, "total_ttc": ttc, "total_tva": tva,
            "controle_retenue": controle or {"verdict": "conforme", "due": 10.99}}


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

    def test_une_retenue_manquante_bloque_la_validation(self):
        m = R.manques(facture(controle={"verdict": "manquante", "due": 11.01,
                                        "assiette": 1100.977}), PIECE)
        self.assertTrue(any("retenue" in x for x in m))

    def test_une_retenue_fausse_bloque_la_validation(self):
        m = R.manques(facture(controle={"verdict": "montant faux", "due": 10.99, "saisie": 11.99,
                                        "ecart": 1.0}), PIECE)
        self.assertTrue(any("ne vaut pas celle qui est due" in x for x in m))


class TestConfrontationAuScan(unittest.TestCase):
    """Cas reel ELECTROQUIP : la saisie porte 1 087,021 TTC / 163,321 TVA, deux lectures
    independantes du scan s'accordent sur 1 098,999 / 176,311."""

    def test_la_lecture_du_scan_concorde_avec_la_facture(self):
        """Cas reel ELECTROQUIP : le modele a rendu 1 098,999 la ou la facture porte 1 099,011, et
        la TVA au millime pres. Douze millimes de reconnaissance ne sont pas un ecart comptable."""
        self.assertEqual(R.ecarts(facture(), {"total_ttc": 1098.999, "total_tva": 175.311}), [])

    def test_un_vrai_ecart_de_ttc_est_signale(self):
        m = R.ecarts(facture(), {"total_ttc": 1150.0, "total_tva": 175.311})
        self.assertEqual(len(m), 1)
        self.assertIn("1150.0", m[0])

    def test_un_vrai_ecart_de_tva_est_signale(self):
        m = R.ecarts(facture(), {"total_ttc": 1099.011, "total_tva": 200.0})
        self.assertEqual(len(m), 1)
        self.assertIn("TVA", m[0])

    def test_une_valeur_absente_ne_reproche_rien(self):
        self.assertEqual(R.ecarts(facture(), {"total_ttc": None, "total_tva": None}), [])


class TestLectureDeLaTableDesTaxes(unittest.TestCase):
    """⚠️ NI `grand_total` NI `total_taxes_and_charges` NE DISENT CE QU'ON CROIT quand une retenue
    est deja saisie — c'est l'erreur qui a fausse la premiere version du controle."""

    def test_la_tva_est_la_somme_des_lignes_de_tva(self):
        """`total_taxes_and_charges` vaut 163,321 sur ELECTROQUIP : 175,311 de TVA MOINS 11,990 de
        retenue. Le comparer au scan accusait la facture d'un ecart qui n'existait pas."""
        self.assertEqual(R.tva_facturee(TAXES), 175.311)

    def test_le_ttc_du_scan_est_celui_d_avant_la_retenue(self):
        """`grand_total` (1 087,021) est deja net de la retenue ; le fournisseur, lui, facture
        1 099,011 — et c'est ce nombre qui est imprime sur le scan."""
        self.assertEqual(R.ttc_avant_retenue(1087.021, TAXES), 1099.011)

    def test_la_retenue_saisie_se_lit_sur_les_lignes_en_deduction(self):
        self.assertEqual(R.retenue_saisie(TAXES), 11.99)

    def test_sans_retenue_le_ttc_ne_bouge_pas(self):
        self.assertEqual(R.ttc_avant_retenue(500.0, [TAXES[0]]), 500.0)


class TestAssietteEtRetenue(unittest.TestCase):
    """⚠️ L'ASSIETTE EXCLUT LE TIMBRE. Sur les 17 factures locales de 2026 depassant le seuil,
    « 1 % du TTC hors timbre » tombe au millime sur dix d'entre elles."""

    def test_le_timbre_sort_de_l_assiette(self):
        taxes = [{"account_head": "TVA 7% - A&S", "tax_amount": 253.832, "add_deduct_tax": "Add"},
                 {"account_head": "Timbre Fiscal - A&S", "tax_amount": 1.0, "add_deduct_tax": "Add"},
                 {"account_head": "Retenue a la source achat - A&S", "tax_amount": 41.8,
                  "add_deduct_tax": "Deduct"}]
        # cas reel ACC-PINV-2026-00001 : TTC avant retenue 4 181,0, timbre 1,0
        self.assertEqual(R.assiette_retenue(4139.2, taxes), 4180.0)
        self.assertEqual(R.retenue_due(4180.0, ttc=4181.0), 41.8)

    def test_au_dessus_du_seuil_un_pour_cent(self):
        self.assertEqual(R.retenue_due(1099.011, ttc=1099.011), 10.99)

    def test_sous_le_seuil_aucune_retenue(self):
        self.assertEqual(R.retenue_due(999.999, ttc=999.999), 0.0)

    def test_le_seuil_se_lit_sur_le_ttc_et_l_assiette_sert_de_base(self):
        """Une facture a 1 000,500 TTC dont 1 DT de timbre : le seuil est franchi, la base est
        999,500. Confondre les deux changerait le resultat sur chaque facture timbree."""
        self.assertEqual(R.retenue_due(999.5, ttc=1000.5), 9.995)

    def test_le_seuil_et_le_taux_sont_parametrables(self):
        """La loi de finances les revise : ils ne sont pas ecrits dans le code."""
        self.assertEqual(R.retenue_due(2000.0, seuil=1500.0, taux=1.5, ttc=2000.0), 30.0)


class TestControleDeLaRetenue(unittest.TestCase):
    def test_une_retenue_juste_est_conforme(self):
        taxes = [TAXES[0], {"account_head": "Retenue a la source achat - A&S",
                            "tax_amount": 10.99, "add_deduct_tax": "Deduct"}]
        c = R.controle_retenue(1088.021, taxes)
        self.assertEqual(c["verdict"], "conforme")

    def test_electroquip_retient_un_dinar_de_trop(self):
        """Cas reel : 11,990 saisis pour 10,990 dus."""
        c = R.controle_retenue(1087.021, TAXES)
        self.assertEqual((c["verdict"], c["due"], c["saisie"], c["ecart"]),
                         ("montant faux", 10.99, 11.99, 1.0))

    def test_une_retenue_absente_est_signalee_comme_manquante(self):
        """Quatre factures de 2026 sont dans ce cas : 52,234 DT jamais retenus."""
        c = R.controle_retenue(1100.977, [{"account_head": "TVA 19% - A&S", "tax_amount": 176.627,
                                           "add_deduct_tax": "Add"}])
        self.assertEqual(c["verdict"], "manquante")
        self.assertEqual(c["due"], 11.01)

    def test_une_base_calculee_sur_le_ht_est_signalee(self):
        """Deux factures retiennent 1 % du HT au lieu du TTC : 16,523 au lieu de 19,660."""
        taxes = [{"account_head": "TVA 19% - A&S", "tax_amount": 313.743, "add_deduct_tax": "Add"},
                 {"account_head": "Retenue a la source achat - A&S", "tax_amount": 16.523,
                  "add_deduct_tax": "Deduct"}]
        c = R.controle_retenue(1949.5, taxes)
        self.assertEqual(c["verdict"], "montant faux")
        self.assertEqual(c["due"], 19.66)


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
