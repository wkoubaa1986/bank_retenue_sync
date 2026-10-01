"""Doublon du 01/10/2026 : un salaire debite le 1er du mois suivant ne doit pas etre comptabilise
deux fois (moteur des depenses recurrentes + confirmation de l'ordre de paiement)."""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.expenses import ordres as O

ORDRES = [
    {"name": "OP-1", "libelle": "Salaire Koubaâ Néjib 09-2026", "montant": 1700.0, "date_prevue": date(2026, 9, 28)},
    {"name": "OP-2", "libelle": "Salaire Akram 09-2026", "montant": 700.0, "date_prevue": date(2026, 9, 28)},
]


def _mvt(debit, jour, ref="FT26273XWCDR"):
    return {"date": jour, "debit": debit, "reference": ref, "operation": "VIR TN AUTRE BQ"}


class TestOrdreEnAttentePour(unittest.TestCase):
    def test_debit_du_1er_du_mois_suivant_reste_a_l_ordre(self):
        o = O.ordre_en_attente_pour(_mvt(1700.0, date(2026, 10, 1)), ordres=ORDRES)
        self.assertEqual(o["name"], "OP-1")

    def test_montant_ou_date_hors_cadre(self):
        self.assertIsNone(O.ordre_en_attente_pour(_mvt(1700.5, date(2026, 10, 1)), ordres=ORDRES))
        self.assertIsNone(O.ordre_en_attente_pour(_mvt(1700.0, date(2026, 10, 9)), ordres=ORDRES))   # > 7 jours
        self.assertIsNone(O.ordre_en_attente_pour(_mvt(0, date(2026, 10, 1)), ordres=ORDRES))
        self.assertIsNone(O.ordre_en_attente_pour(_mvt(1700.0, date(2026, 10, 1)), ordres=[]))

    def test_tolerance_et_bords_de_fenetre(self):
        self.assertEqual(O.ordre_en_attente_pour(_mvt(700.004, date(2026, 10, 5)), ordres=ORDRES)["name"], "OP-2")
        self.assertEqual(O.ordre_en_attente_pour(_mvt(700.0, date(2026, 9, 21)), ordres=ORDRES)["name"], "OP-2")


class TestMoteurLaisseLeMouvementALOrdre(unittest.TestCase):
    """process_rule sur la vraie regle de salaire, sans rien inserer (insert=False)."""

    def setUp(self):
        import frappe
        from bank_retenue_sync.expenses import engine as E
        self.E = E
        rows = E.load_rules(only=["salaire_koubaa_nejib"])
        if not rows:
            self.skipTest("regle salaire_koubaa_nejib absente")
        self.row = rows[0]
        self.m = {"date": date(2026, 10, 1), "debit": float(self.row["montant"]), "credit": 0,
                  "reference": "FT-TEST-DOUBLON", "operation": "VIR TN AUTRE BQ",
                  "operation_norm": "VIR TN AUTRE BQ"}
        frappe.db.savepoint("doublon")

    def tearDown(self):
        import frappe
        frappe.db.rollback(save_point="doublon")

    def _contexte(self, ordres):
        class Ctx:
            je_par_reference = {}
            cheque_no_index = {}
            ordres_en_attente = ordres
        return Ctx()

    def test_skipped_tant_qu_un_ordre_attend_le_debit(self):
        ordres = [{"name": "OP-TEST", "libelle": "Salaire Koubaâ Néjib 09-2026",
                   "montant": self.row["montant"], "date_prevue": date(2026, 9, 28)}]
        out = self.E.process_rule(self.row, [self.m], context=self._contexte(ordres), insert=False)
        self.assertEqual([o["status"] for o in out], ["skipped"], out)
        self.assertIn("OP-TEST", out[0]["raison"])
        # Sans ordre en attente, le moteur ferait son travail (dry-run) : la garde est bien la seule cause.
        out = self.E.process_rule(self.row, [self.m], context=self._contexte([]), insert=False)
        self.assertEqual([o["status"] for o in out], ["created"], out)
