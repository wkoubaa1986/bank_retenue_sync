"""Journal des passages de vérification bancaire (14/10/2026) — résumé et détection des interruptions, sans base."""
import unittest
from datetime import datetime, timedelta

from bank_retenue_sync.tasks import passages as P


class TestResume(unittest.TestCase):
    def test_passage_reussi(self):
        out = {"refresh": "ok", "crees": 8, "revus": 920, "solde": {"solde_banque": 195092.851},
               "reglements": [{"flux": "reglement_aramex", "status": "regle", "je": "ACC-JV-1"},
                              {"flux": "reglement_honoraire", "status": "en attente"}],
               "ecritures": [{"periode": "10-2026"}]}
        r = P.resume(out)
        self.assertEqual(r["statut"], "Success")
        self.assertEqual(r["erreurs"], [])
        etapes = {e["cle"]: e for e in r["etapes"]}
        self.assertIn("8 nouveau(x)", etapes["refresh"]["detail"])
        self.assertEqual(etapes["reglements"]["detail"], "1 réglée(s), 1 en attente")
        self.assertNotIn("crees", etapes)               # les compteurs ne sont pas des étapes

    def test_etape_en_erreur_rend_le_passage_partiel(self):
        r = P.resume({"refresh": "ok", "declarations": {"erreur": "boîte mail injoignable"}})
        self.assertEqual(r["statut"], "Partial")
        self.assertEqual(r["erreurs"], ["Déclaration fiscale — boîte mail injoignable"])

    def test_reglement_en_echec(self):
        """Un règlement qui lève (rendu en status « error » par process_reglements) ne passe plus inaperçu."""
        r = P.resume({"reglements": [{"flux": "reglement_aramex", "je": "ACC-JV-2026-00858", "status": "error",
                                      "error": "Cannot cancel"}]})
        self.assertEqual(r["statut"], "Partial")
        self.assertIn("ACC-JV-2026-00858 : Cannot cancel", r["erreurs"][0])

    def test_export_illisible(self):
        r = P.resume({"refresh": "export illisible : timeout"})
        self.assertEqual(r["statut"], "Partial")

    def test_etape_non_lancee(self):
        r = P.resume({"refresh": "ok", "carte": None})
        self.assertEqual({e["cle"]: e["detail"] for e in r["etapes"]}["carte"], "non lancée")


class TestInterrompus(unittest.TestCase):
    def test_reste_en_cours_trop_longtemps(self):
        maintenant = datetime(2026, 10, 14, 17, 0)
        passages = [{"name": "A", "status": "Running", "started_at": datetime(2026, 10, 14, 15, 0)},
                    {"name": "B", "status": "Running", "started_at": datetime(2026, 10, 14, 16, 50)},
                    {"name": "C", "status": "Success", "started_at": datetime(2026, 10, 14, 13, 0)}]
        self.assertEqual(P.interrompus(passages, maintenant), ["A"])
        self.assertEqual(P.interrompus(passages, maintenant, delai_min=5), ["A", "B"])
        self.assertEqual(P.interrompus([], maintenant + timedelta(days=1)), [])


if __name__ == "__main__":
    unittest.main()
