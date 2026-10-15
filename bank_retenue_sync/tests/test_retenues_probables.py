"""Retenue deguisee en dette, regle « la dette d'abord », bilan hebdo (15/10/2026) — regles pures."""
from __future__ import annotations

import unittest

import frappe

from bank_retenue_sync.tej import bilan_hebdo as B, paiements as P, retenues_probables as RP


class TestDetteVautRetenue(unittest.TestCase):
    def test_un_pour_cent_du_ttc(self):
        self.assertTrue(RP.dette_vaut_retenue(13051.0, 130.50))        # WWF, ACC-SINV-2026-01225
        self.assertTrue(RP.dette_vaut_retenue(1500.0, 15.01))          # timbre
        self.assertFalse(RP.dette_vaut_retenue(1500.0, 15.50))         # 0,50 DT : autre chose
        self.assertFalse(RP.dette_vaut_retenue(611.0, 6.11))           # sous le seuil de 1 000 DT TTC
        self.assertFalse(RP.dette_vaut_retenue(1500.0, 0))
        self.assertFalse(RP.dette_vaut_retenue(1500.0, 1500.0))        # dette entiere : pas une retenue


class TestLaDetteDAbord(unittest.TestCase):
    """`reglement_a_reprendre` avec des lignes injectees — aucune base."""

    def _lignes(self):
        return [frappe._dict(name="PE-ESP", posting_date="2026-08-21", mode_of_payment="Espèces", paid_amount=12920.5,
                             docstatus=1, allocated_amount=12920.5, idx=1),
                frappe._dict(name="PE-DETTE", posting_date="2026-08-21", mode_of_payment="Dette non payée", paid_amount=130.5,
                             docstatus=1, allocated_amount=130.5, idx=2)]

    def test_la_dette_porte_la_retenue(self):
        r = P.reglement_a_reprendre({"montant_retenue": 130.5, "date_paiement": "2026-08-21"}, lignes=self._lignes(), ttc=13051.0)
        self.assertTrue(r["ok"])
        self.assertEqual((r["reglement"]["name"], r["regle"]), ("PE-DETTE", "dette portee par la facture"))

    def test_une_dette_peut_tomber_a_zero_pas_un_reglement(self):
        seule = [frappe._dict(name="PE-DETTE", posting_date="2026-08-21", mode_of_payment="Dette non payée", paid_amount=130.5,
                              docstatus=1, allocated_amount=130.5, idx=1)]
        self.assertTrue(P.reglement_a_reprendre({"montant_retenue": 130.5}, lignes=seule)["ok"])
        especes = [frappe._dict(name="PE-ESP", posting_date="2026-08-21", mode_of_payment="Espèces", paid_amount=130.5,
                                docstatus=1, allocated_amount=130.5, idx=1)]
        self.assertFalse(P.reglement_a_reprendre({"montant_retenue": 130.5}, lignes=especes)["ok"])   # l'argent compte ne tombe pas a zero

    def test_deux_dettes_font_trancher(self):
        deux = self._lignes() + [frappe._dict(name="PE-DETTE-2", posting_date="2026-09-01", mode_of_payment="Dette non payée",
                                              paid_amount=200.0, docstatus=1, allocated_amount=200.0, idx=3)]
        r = P.reglement_a_reprendre({"montant_retenue": 130.5}, lignes=deux)
        self.assertFalse(r["ok"])
        self.assertEqual([c["name"] for c in r["candidats"]], ["PE-DETTE", "PE-DETTE-2"])

    def test_sans_dette_les_regles_d_avant(self):
        esp = [frappe._dict(name="PE-ESP", posting_date="2026-08-21", mode_of_payment="Espèces", paid_amount=1500.0,
                            docstatus=1, allocated_amount=1500.0, idx=1)]
        r = P.reglement_a_reprendre({"montant_retenue": 15.0}, lignes=esp, ttc=1500.0)
        self.assertEqual(r["regle"], "seul reglement qui la couvre")


class TestBilan(unittest.TestCase):
    def test_rendu(self):
        b = {"date": "2026-10-19", "annee": 2026, "a_faire": 2, "erreurs": [],
             "fraicheur": {"export": "2026-09-08 16:35:24", "dernier_certificat": "2026-08-24", "lignes": 103},
             "sans_certificat": [{"posting_date": "2026-07-09", "customer": "itkkan sa", "montant": 34.14, "sales_invoice": "ACC-SINV-2026-00879", "name": "ACC-PAY-2026-04444"}],
             "sans_piece": [], "ecarts": [],
             "probables": [{"sales_invoice": "ACC-SINV-2026-01225", "customer_name": "WWF", "grand_total": 13051.0, "retenue": 130.5, "dette_pe": "ACC-PAY-2026-05751"}]}
        html = B.rendre(b, "https://x/app/retenue-source-vente")
        self.assertIn("itkkan sa", html)
        self.assertIn("130,500 DT".replace(",", ".") if False else "130.500 DT", html)
        self.assertIn("plus de 14 jours", html)                 # export du 08/09 lu le 19/10
        self.assertIn("<b>2</b> point(s)", html)
        self.assertIn("aucun", html)                             # rubriques vides dites comme telles


class TestPieceDuCertificat(unittest.TestCase):
    def test_facture_sinon_commande(self):
        self.assertEqual(P.piece_du_certificat({"sales_invoice": "SI-1", "sales_order": "SO-1"}), ("Sales Invoice", "SI-1"))
        self.assertEqual(P.piece_du_certificat({"sales_invoice": None, "sales_order": "SO-1"}), ("Sales Order", "SO-1"))
        self.assertEqual(P.piece_du_certificat({}), (None, None))


class TestCommandesRegroupees(unittest.TestCase):
    """Plusieurs commandes regroupées dans une facture : chaque commande a laissé sa dette de 1 %, le
    client retient 1 % de la facture (ACC-SINV-2024-00447 : 3 écritures = 140,14 sur 14 014 DT)."""

    def test_dettes_qui_forment_la_retenue(self):
        lignes = [{"dette_pe": "D1", "dette": 54.66}, {"dette_pe": "D2", "dette": 30.00}, {"dette_pe": "D3", "dette": 55.48}]
        self.assertEqual([l["dette_pe"] for l in RP.dettes_de_la_retenue(14014.0, lignes)], ["D1", "D2", "D3"])
        self.assertEqual([l["dette_pe"] for l in RP.dettes_de_la_retenue(5466.0, [{"dette_pe": "D1", "dette": 54.66}, {"dette_pe": "X", "dette": 500.0}])], ["D1"])
        self.assertEqual(RP.dettes_de_la_retenue(5466.0, [{"dette_pe": "X", "dette": 500.0}, {"dette_pe": "Y", "dette": 20.0}]), [])
        self.assertEqual(RP.dettes_de_la_retenue(5466.0, []), [])

    def test_reglement_a_reprendre_reprend_toutes_les_dettes(self):
        lignes = [frappe._dict(name="ESP", posting_date="2026-03-01", mode_of_payment="Espèces", paid_amount=13873.86, docstatus=1, allocated_amount=13873.86, idx=1),
                  frappe._dict(name="D1", posting_date="2026-03-01", mode_of_payment="Dette non payée", paid_amount=54.66, docstatus=1, allocated_amount=54.66, idx=2),
                  frappe._dict(name="D2", posting_date="2026-03-02", mode_of_payment="Dette non payée", paid_amount=30.0, docstatus=1, allocated_amount=30.0, idx=3),
                  frappe._dict(name="D3", posting_date="2026-03-03", mode_of_payment="Dette non payée", paid_amount=55.48, docstatus=1, allocated_amount=55.48, idx=4)]
        r = P.reglement_a_reprendre({"montant_retenue": 140.14, "date_paiement": "2026-04-01"}, lignes=lignes, ttc=14014.0)
        self.assertTrue(r["ok"])
        self.assertEqual([l["name"] for l in r["reglements"]], ["D1", "D2", "D3"])
        # Deux dettes trop petites qui ne totalisent pas la retenue : elles ne la portent pas ; seul
        # l'argent compté la couvre, et c'est son imputation qui bougera (jamais son montant).
        r2 = P.reglement_a_reprendre({"montant_retenue": 140.14}, lignes=lignes[:3], ttc=14014.0)
        self.assertEqual((r2["ok"], r2["reglement"]["name"], r2["regle"]), (True, "ESP", "seul reglement qui la couvre"))


class TestAReclamer(unittest.TestCase):
    def test_regrouper_par_client_sans_les_relances_recentes(self):
        from bank_retenue_sync.tej import liste_appels_ras as L
        pieces = [{"customer": "A", "piece": "SI-1", "date": "2026-07-09", "montant": 34.14, "motif": "x"},
                  {"customer": "A", "piece": "SI-2", "date": "2026-07-31", "montant": 54.11, "motif": "x"},
                  {"customer": "B", "piece": "SI-3", "date": "2026-08-31", "montant": 20.86, "motif": "x"},
                  {"customer": None, "declarant": "U B C I", "piece": "SI-4", "date": "2026-08-31", "montant": 1.0, "motif": "x"}]
        g = L.regrouper(pieces, relances_recentes={"B"})
        self.assertEqual(sorted(g), ["? U B C I", "A"])                       # sans fiche : regroupé sous le déclarant
        self.assertEqual([p["piece"] for p in g["A"]], ["SI-1", "SI-2"])
        self.assertIn("SI-1 du 09/07/2026 : retenue 34.14 DT", L._texte_piece(pieces[0]))


class TestLaDetteDesigneLaFacture(unittest.TestCase):
    """BHM 08/09/2026 : 497,97 net pour 3 factures, 5,03 de dette sur l'une d'elles = la retenue déclarée."""

    def test_designation(self):
        from bank_retenue_sync.tej import rapprochement as R
        factures = ["SI-1450", "SI-1451", "SI-1452"]
        dettes = [{"dette_pe": "PE-D1", "doctype": "Sales Invoice", "name": "SI-1452", "reste": 5.03},
                  {"dette_pe": "PE-D2", "doctype": "Sales Order", "name": "SO-9", "reste": 5.03},        # une commande : pas une des factures
                  {"dette_pe": "PE-D3", "doctype": "Sales Invoice", "name": "SI-1451", "reste": 40.0}]
        self.assertEqual(R.facture_designee_par_dette(factures, 5.03, dettes)["name"], "SI-1452")
        self.assertEqual(R.facture_designee_par_dette(factures, 5.04, dettes)["name"], "SI-1452")     # timbre
        self.assertIsNone(R.facture_designee_par_dette(factures, 7.0, dettes))
        deux = dettes + [{"dette_pe": "PE-D4", "doctype": "Sales Invoice", "name": "SI-1450", "reste": 5.03}]
        self.assertIsNone(R.facture_designee_par_dette(factures, 5.03, deux))                       # deux dettes égales : on ne devine pas
