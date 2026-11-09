"""Bilan Raven du passage TEJ de nuit (tej/notification) — les fonctions pures.

Le message part chez les utilisateurs : il doit dire la meme chose que les tuiles de la page, ne
jamais montrer un mot de passe de relais, et rester lisible quand le service est eteint.
"""
import unittest

from bank_retenue_sync.tej import notification as N


def bilan(**extra):
    base = {
        "quand": "10/11/2026 04:07", "passage": "synchronise", "erreur": None, "crees": 3, "revus": 66,
        "kpis": {"sans_piece": 2, "retenue_sans_piece": 52.215, "ambigus": 0, "non_identifies": 0,
                 "ecarts": 30, "anomalies": 1, "total": 69},
        "relais": {"serveur": "http://USER:***@thehub.proxy-cheap.com:8080", "joignable": True,
                   "detail": "CONNECT 200", "type": "session", "session": "a56283f4", "tirages": 2},
        "url": "https://erp.example.com/app/retenue-source-vente",
    }
    base.update(extra)
    return base


class TestComposer(unittest.TestCase):
    def test_passage_reussi(self):
        t = N.composer(bilan())
        self.assertIn("passage TEJ du 10/11/2026 04:07", t)
        self.assertIn("✅ terminé — export du portail renouvelé", t)
        self.assertIn("Nouveaux certificats : **3** (revus : 66)", t)
        self.assertIn("Sans écriture : **2** (52,215 DT) → à régulariser", t)
        self.assertIn("À trancher : 0 · Écarts de montant : 30 · Anomalies : 1 · Certificats de l'année : 69", t)
        self.assertIn("Relais TEJ : http://USER:***@thehub.proxy-cheap.com:8080 — ✅ joignable, session a56283f4, 2 tirages", t)
        self.assertIn("[Ouvrir la page](https://erp.example.com/app/retenue-source-vente)", t)

    def test_export_inchange_et_rien_a_faire(self):
        t = N.composer(bilan(passage="export inchange", crees=0, revus=0,
                             kpis={"sans_piece": 0, "retenue_sans_piece": 0, "ambigus": 0, "non_identifies": 0,
                                   "ecarts": 0, "anomalies": 0, "total": 69}))
        self.assertIn("le portail n'a rien de nouveau", t)
        self.assertIn("Nouveaux certificats : **0**", t)
        self.assertIn("Sans écriture : **0** (0,000 DT)", t)
        self.assertNotIn("à régulariser", t)

    def test_echec_dit_pourquoi_sans_compteurs_de_passage(self):
        t = N.composer(bilan(passage="echec", erreur="relais TEJ injoignable (…)"))
        self.assertIn("❌ échec — voir Error Log « brs scheduler » : relais TEJ injoignable", t)
        self.assertNotIn("Nouveaux certificats", t)            # rien n'a ete lu : on ne l'invente pas
        self.assertIn("Sans écriture", t)                       # l'etat de la base, lui, reste vrai

    def test_relais(self):
        self.assertIn("Relais TEJ : non configuré", N.composer(bilan(relais=None)))
        self.assertIn("⚠️ service injoignable (Connection refused)", N.composer(bilan(relais={"erreur": "Connection refused"})))
        t = N.composer(bilan(relais={"serveur": "http://100.127.209.10:3128", "joignable": False,
                                     "detail": "aucun relais ne repond : …", "type": "fixe", "tirages": 1}))
        self.assertIn("❌ aucun relais ne répond", t)
        t = N.composer(bilan(relais={"serveur": "http://100.127.209.10:3128", "joignable": True, "type": "fixe", "tirages": 1}))
        self.assertIn("http://100.127.209.10:3128 — ✅ joignable", t)
        self.assertNotIn("session", t.split("Relais TEJ")[1])

    def test_montants(self):
        self.assertEqual(N._dt(2348.688), "2 348,688 DT")
        self.assertEqual(N._dt(None), "0,000 DT")
        self.assertEqual(N._dt("abc"), "0,000 DT")


class TestRelaisDepuisHealth(unittest.TestCase):
    def test_health_du_service(self):
        health = {"tej_proxy": {"configure": True, "joignable": True, "serveur": "http://U:***@h:8080",
                                "detail": "…", "relais": [
                                    {"serveur": "http://U:***@h:8080", "type": "session", "joignable": True,
                                     "detail": "CONNECT 200 — session abc, tirage 1", "tirages": 1, "session": "abc"},
                                    {"serveur": "http://100.127.209.10:3128", "type": "fixe", "joignable": False}]}}
        r = N.relais_depuis_health(health)
        self.assertEqual((r["serveur"], r["joignable"], r["type"], r["session"], r["tirages"]),
                         ("http://U:***@h:8080", True, "session", "abc", 1))
        self.assertIsNone(N.relais_depuis_health({"tej_proxy": {"configure": False}}))
        self.assertIsNone(N.relais_depuis_health({}))
        r = N.relais_depuis_health({"tej_proxy": {"configure": True, "joignable": False, "serveur": "http://a:1",
                                                  "detail": "aucun relais ne repond", "relais": []}})
        self.assertEqual((r["serveur"], r["joignable"], r["detail"]), ("http://a:1", False, "aucun relais ne repond"))


if __name__ == "__main__":
    unittest.main()
