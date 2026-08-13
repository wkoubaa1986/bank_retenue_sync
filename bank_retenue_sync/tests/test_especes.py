"""Tests du rapprochement des VERSEMENTS D'ESPECES (caisse Sabah -> banque Zitouna).

Aucun acces base : les ecritures candidates sont injectees via `je_finder`. Les libelles sont
ceux de l'export reel 09/06 -> 31/07.
"""
import unittest
from datetime import date

from bank_retenue_sync.encaissement import especes


def _credit(operation, montant, reference, jour):
    return {"date": date(2026, 7, jour), "date_valeur": date(2026, 7, jour),
            "operation": operation, "reference": reference, "debit": 0.0, "credit": montant}


def _je(name, docstatus=1, user_remark="", cheque_no="Versement espèce"):
    return {"name": name, "posting_date": date(2026, 7, 14), "docstatus": docstatus,
            "cheque_no": cheque_no, "user_remark": user_remark, "remark": "Note : " + user_remark}


class TestDetection(unittest.TestCase):
    def test_versement_especes_reconnu(self):
        self.assertTrue(especes.is_versement_espece(
            _credit("VERSEMENT ESPECES RECETTE AGENCE AOUINA", 34000.0, "TT26166YBG80", 15)))

    def test_virement_client_n_est_pas_un_versement(self):
        self.assertFalse(especes.is_versement_espece(
            _credit("VIR TN AUTRE BQ TAURUS SARL", 5322.24, "FT26211D5NMC", 30)))

    def test_debit_ignore(self):
        m = _credit("VERSEMENT ESPECES RECETTE AGENCE AOUINA", 0.0, "TT1", 15)
        m["debit"] = 34000.0
        self.assertFalse(especes.is_versement_espece(m))


class TestMatchVersements(unittest.TestCase):
    def test_ecriture_portant_deja_la_reference_ne_declenche_rien(self):
        """Cas reel ACC-JV-2026-00499 : le comptable a deja note 'Ref : TT26195DXDH5'."""
        mvts = [_credit("VERSEMENT ESPECES recette AGENCE DAR FADHAL", 22500.0,
                        "TT26195DXDH5", 14)]
        actions, diag = especes.match_versements(
            mvts, je_finder=lambda d, m: [_je("ACC-JV-2026-00499",
                                              user_remark="Versement espèce -Recette \nRef : TT26195DXDH5")])
        self.assertEqual([a["kind"] for a in actions], ["reference"])
        self.assertEqual(diag, [])

    def test_ecriture_sans_reference_est_annotee(self):
        """Cas reel ACC-JV-2026-00432 : l'ecriture existe mais ne porte aucune reference bancaire."""
        mvts = [_credit("VERSEMENT ESPECES RECETTE AGENCE AOUINA", 34000.0, "TT26166YBG80", 15)]
        actions, _ = especes.match_versements(
            mvts, je_finder=lambda d, m: [_je("ACC-JV-2026-00432", user_remark="versement espèces")])
        self.assertEqual(actions[0]["kind"], "annotation")
        self.assertEqual(actions[0]["journal_entry"], "ACC-JV-2026-00432")

    def test_aucune_ecriture_declenche_une_creation(self):
        mvts = [_credit("VERSEMENT ESPECES RECETTE AGENCE AOUINA", 12000.0, "TT-NEW", 20)]
        actions, _ = especes.match_versements(mvts, je_finder=lambda d, m: [])
        self.assertEqual(actions[0]["kind"], "creation")
        self.assertEqual(actions[0]["montant"], 12000.0)

    def test_reglement_client_en_especes_est_ecarte(self):
        """'VERSEMENT ESPECES AGENCE DENDEN' 175 DT = ACC-PAY-2026-03787 (Naturalium) : un client
        qui paie en liquide au guichet, pas un depot de la caisse."""
        mvts = [_credit("VERSEMENT ESPECES AGENCE DENDEN", 175.0, "TT26173FMCY7", 22)]
        actions, diag = especes.match_versements(mvts, booked={"TT26173FMCY7"},
                                                 je_finder=lambda d, m: [])
        self.assertEqual(actions, [])
        self.assertIn("reglement client", diag[0]["reason"])

    def test_plusieurs_ecritures_candidates_ne_tranchent_pas(self):
        mvts = [_credit("VERSEMENT ESPECES RECETTE AGENCE AOUINA", 5000.0, "TT-AMB", 20)]
        actions, diag = especes.match_versements(
            mvts, je_finder=lambda d, m: [_je("ACC-JV-1"), _je("ACC-JV-2")])
        self.assertEqual(actions, [])
        self.assertEqual(diag[0]["ecritures"], ["ACC-JV-1", "ACC-JV-2"])


class TestLibelle(unittest.TestCase):
    def test_libelle_reprend_la_forme_des_saisies_manuelles(self):
        self.assertEqual(especes._libelle("VERSEMENT ESPECES RECETTE AGENCE AOUINA"),
                         "Versement espèce - Recette Agence Aouina")

    def test_libelle_sans_complement(self):
        self.assertEqual(especes._libelle("VERSEMENT ESPECES"), "Versement espèce")


class TestApplyActionsEnModeConstat(unittest.TestCase):
    def test_annotate_false_ne_touche_a_rien(self):
        actions = [{"kind": "annotation", "journal_entry": "ACC-JV-2026-00432",
                    "reference": "TT26166YBG80", "montant": 34000.0, "date": date(2026, 6, 15),
                    "docstatus": 1, "user_remark": "versement espèces"}]
        out = especes.apply_actions(actions, insert=False, annotate=False)
        self.assertEqual(out["annotation"], 0)
        self.assertEqual(out["ecritures"], [])
