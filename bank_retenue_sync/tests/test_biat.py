"""BIAT : reconnaissance des libelles, classement d'une ligne, cle du registre par banque.
Fonctions pures : aucun site. Les libelles reprennent la forme vue le 16/11/2026, numeros inventes."""
import datetime
import unittest

from bank_retenue_sync.bank import biat, registry

REMISE = "Credit Divers Chq Telec A Enc Cre Global/borde : 1234567"
COMMISSION = "Debit Divers Commission CHQ A Enc/Bordereau : 1234567"


def ligne(operation, credit=0.0, debit=0.0, reference="FT26316000012345", date="2026-11-12", cle="k1"):
    return {"cle": cle, "date": date, "operation": operation, "reference": reference, "debit": debit, "credit": credit,
            "montant": debit or credit, "sens": "Debit" if debit else "Credit"}


class TestDetection(unittest.TestCase):
    def test_remise_de_cheques_et_son_bordereau(self):
        vu = biat.detecter(REMISE, "Credit")
        self.assertEqual((vu["regle"], vu["categorie"], vu["sous_categorie"], vu["numero"]),
                         ("enc_cheque", "encaissement", "cheque", "1234567"))
        # variantes d'ecriture du portail : casse, espaces, « bordereau » entier
        self.assertEqual(biat.detecter("credit divers  chq telec a enc cre global/bordereau:7654321", "Credit")["numero"], "7654321")

    def test_commission_reliee_a_la_remise_par_le_bordereau(self):
        vu = biat.detecter(COMMISSION, "Debit")
        self.assertEqual((vu["regle"], vu["categorie"], vu["numero"]), ("com_enc_cheque", "frais_bancaires", "1234567"))

    def test_le_sens_separe_la_remise_de_sa_commission(self):
        # Meme mots (« CHQ », « ENC », « bordereau ») : un credit n'est jamais une commission, un debit jamais une remise.
        self.assertEqual(biat.detecter(COMMISSION, "Credit")["regle"], "")
        self.assertEqual(biat.detecter(REMISE, "Debit")["regle"], "")

    def test_versement_d_especes(self):
        self.assertEqual(biat.detecter("Versement TND", "Credit")["regle"], "versement_especes")
        self.assertEqual(biat.detecter("Versement TND", "Debit")["regle"], "")
        self.assertEqual(biat.detecter("Frais sur versement", "Credit")["regle"], "")      # « versement » en tete seulement

    def test_libelle_inconnu(self):
        for libelle in ("Virement recu STE EXEMPLE", "Cheque 0001234", "", None):
            self.assertEqual(biat.detecter(libelle, "Credit")["regle"], "")


class TestClassement(unittest.TestCase):
    def test_remise_sans_paiement_a_rapprocher(self):
        c = biat.classer_ligne(ligne(REMISE, credit=1500.0), {})
        self.assertEqual((c["statut"], c["regle"], c["groupe"]), ("Orphelin", "enc_cheque", "bordereau:1234567"))
        self.assertIn("BR:1234567", c["raison"])
        self.assertIsNone(c["document_name"])

    def test_remise_portee_par_des_paiements(self):
        ctx = {"paiements_par_bordereau": {"1234567": [{"name": "PE-1", "montant": 1000.0, "docstatus": 1},
                                                       {"name": "PE-2", "montant": 500.0, "docstatus": 1}]}}
        c = biat.classer_ligne(ligne(REMISE, credit=1500.0), ctx)
        self.assertEqual((c["statut"], c["document_type"], c["document_name"], c["montant_document"], c["ecart"]),
                         ("Identifie", "Payment Entry", None, 1500.0, 0.0))                 # plusieurs pieces : pas de nom unique
        seul = biat.classer_ligne(ligne(REMISE, credit=1000.0), {"paiements_par_bordereau": {"1234567": ctx["paiements_par_bordereau"]["1234567"][:1]}})
        self.assertEqual(seul["document_name"], "PE-1")
        # un paiement encore en brouillon : identifie, pas comptabilise ; un total different : l'ecart est dit
        ctx["paiements_par_bordereau"]["1234567"][1]["docstatus"] = 0
        b = biat.classer_ligne(ligne(REMISE, credit=1600.0), ctx)
        self.assertEqual((b["statut"], b["ecart"]), ("Identifie non comptabilise", 100.0))
        self.assertIn("écart", b["raison"])

    def test_commission(self):
        c = biat.classer_ligne(ligne(COMMISSION, debit=0.893), {"remises": {"1234567": 1500.0}})
        self.assertEqual((c["statut"], c["categorie"], c["groupe"]), ("Orphelin", "frais_bancaires", "bordereau:1234567"))
        self.assertIn("1500.000", c["raison"])
        porte = biat.classer_ligne(ligne(COMMISSION, debit=0.893), {"ecritures_par_reference": {"FT26316000012345": [{"name": "JV-1", "docstatus": 1}]}})
        self.assertEqual((porte["statut"], porte["document_type"], porte["document_name"]), ("Identifie", "Journal Entry", "JV-1"))

    def test_versement(self):
        v = ligne("Versement TND", credit=500.0, reference="TT26306000012345", date="2026-11-02")
        par_reference = biat.classer_ligne(v, {"ecritures_par_reference": {"TT26306000012345": [{"name": "JV-9", "docstatus": 1}]}})
        self.assertEqual((par_reference["statut"], par_reference["document_name"]), ("Identifie", "JV-9"))
        jour = datetime.date(2026, 11, 4)
        par_montant = biat.classer_ligne(v, {"ecritures_versement": [{"name": "JV-3", "date": jour, "montant": 500.0, "docstatus": 0},
                                                                     {"name": "JV-4", "date": jour, "montant": 700.0, "docstatus": 1},
                                                                     {"name": "JV-5", "date": datetime.date(2026, 12, 1), "montant": 500.0, "docstatus": 1}]})
        self.assertEqual((par_montant["statut"], par_montant["document_name"]), ("Identifie non comptabilise", "JV-3"))
        deux = biat.classer_ligne(v, {"ecritures_versement": [{"name": "JV-3", "date": jour, "montant": 500.0, "docstatus": 1},
                                                              {"name": "JV-6", "date": jour, "montant": 500.0, "docstatus": 1}]})
        self.assertEqual(deux["statut"], "A verifier")                                      # deux candidates : on ne choisit pas
        self.assertIsNone(deux["document_name"])
        self.assertEqual(biat.classer_ligne(v, {})["statut"], "Orphelin")

    def test_libelle_inconnu_jamais_passe_sous_silence(self):
        c = biat.classer_ligne(ligne("Operation nouvelle 42", debit=10.0), {})
        self.assertEqual((c["statut"], c["regle"], c["categorie"]), ("A verifier", "", ""))
        self.assertIn("inconnu", c["raison"])


class TestCleParBanque(unittest.TestCase):
    """Deux banques peuvent porter le meme jour la meme reference et le meme montant."""

    M = {"date": datetime.date(2026, 11, 12), "reference": "FT26316000012345", "debit": 0.0, "credit": 1500.0}

    def test_la_cle_zitouna_ne_change_pas(self):
        self.assertEqual(registry.movement_key(self.M), registry.movement_key(self.M, "Zitouna"))
        self.assertEqual(registry.movement_key(self.M), registry.movement_key(self.M, None))

    def test_la_biat_a_ses_propres_cles(self):
        self.assertNotEqual(registry.movement_key(self.M), registry.movement_key(self.M, "BIAT"))
        paires = registry.assign_keys([self.M, dict(self.M)], "BIAT")
        self.assertEqual(paires[1][0], paires[0][0] + "-2")                                 # deux lignes strictement identiques

    def test_requetes_sql_ecrites_a_la_main_bornees_a_zitouna(self):
        self.assertEqual(registry.SQL_BANQUE_DEFAUT, "`banque` = 'Zitouna'")
        self.assertEqual(registry.BANQUES, ("Zitouna", "BIAT"))


if __name__ == "__main__":
    unittest.main()
