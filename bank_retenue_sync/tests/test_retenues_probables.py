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
