"""Cartes (terminal de paiement BIAT) : lecture du releve et de l'avis de credit, paiement de chaque
transaction, reconnaissance du credit sur le releve bancaire. Fonctions pures : aucun site, aucun PDF.
Les lignes reprennent la forme des deux documents recus le 17/11/2026 ; numeros et montants inventes."""
import datetime
import unittest

from bank_retenue_sync.bank import biat, monetique as M

RELEVE = [
    "Relevé des opérations monétique",
    "N° Affiliation 0800000001 SOCIETE EXEMPLE",
    "N° Compte Dvs Adresse RUE EXEMPLE",
    "N100000001 TND",
    "Date Traitement 16/11/2026",
    "Bordereau N° 1990010006 N° Terminal 01 N° Remise 0006",
    "Transaction Date Achat Montant Commission Num Carte Type Carte N° Aut",
    "0000006001 12/11/2026 100.000 3.000 411111xxxx1111 Nat 012345",
    "0000006002 12/11/2026 1,400.000 42.000 422222xxxx2222 Nat 678901",
    "Total Bordéreau : 2 1,500.000 45.000",
    "Bordereau N° 1990010007 N° Terminal 01 N° Remise 0007",
    "Transaction Date Achat Montant Commission Num Carte Type Carte N° Aut",
    "0000007001 13/11/2026 381.000 11.430 433333xxxx3333 Nat A1B2C3",
    "Total Bordéreau : 1 381.000 11.430",
    "Total Journée: 3 1,881.000 56.430",
    "Total Affilié 3 1,881.000 56.430",
]
AVIS = [
    "Edité le : 17-NOV-26 10:13",
    "Avis d'opérations - remises TPE",
    "N° AFFILIATION",
    "0800000001",
    "COMPTE AFFILIE N100000001",
    "N° Terminal Date Telecollecte Remise Date Traitement Mnt Brut Mnt Com Mnt Tva Mnt Net",
    "01 16-NOV-26 0006 16-NOV-26 1,500.000 45.000 8.550 1,446.450",
    "01 16-NOV-26 0007 16-NOV-26 381.000 11.430 2.172 367.398",
    "Total 1,881.000 56.430 10.722 1,813.848",
]


def paiement(nom, reference, montant, date="2026-11-12", carte=True, client="Client"):
    return {"name": nom, "reference": reference, "montant": montant, "date": date, "carte": carte, "client": client}


def remise(nom, numero, traitement="2026-11-16", brut=0.0, commission=0.0, tva=0.0, net=0.0, nombre=1, mouvement=None):
    return {"name": nom, "remise": numero, "date_traitement": traitement, "brut": brut, "commission": commission,
            "tva": tva, "net": net, "nombre": nombre, "mouvement": mouvement}


R6 = remise("TPE-BIAT-20261116-01-0006", "0006", brut=1500.0, commission=45.0, tva=8.55, net=1446.45, nombre=2)
R7 = remise("TPE-BIAT-20261116-01-0007", "0007", brut=381.0, commission=11.43, tva=2.172, net=367.398)


class TestLecture(unittest.TestCase):
    def test_le_titre_dit_le_document(self):
        self.assertEqual(M.nature_de(RELEVE), "releve")
        self.assertEqual(M.nature_de(AVIS), "avis")
        self.assertIsNone(M.nature_de(["Facture n° 12", "Total 10.000"]))
        self.assertIsNone(M.lire_lignes(["Facture n° 12"])["type"])

    def test_releve_bordereaux_transactions_et_totaux(self):
        r = M.lire_releve(RELEVE)
        self.assertEqual((r["affiliation"], r["compte"], r["date_traitement"]),
                         ("0800000001", "N100000001", datetime.date(2026, 11, 16)))
        self.assertEqual([(b["bordereau"], b["terminal"], b["remise"], b["nombre"], b["total"], b["commission"])
                          for b in r["bordereaux"]],
                         [("1990010006", "01", "0006", 2, 1500.0, 45.0), ("1990010007", "01", "0007", 1, 381.0, 11.43)])
        t = r["bordereaux"][0]["transactions"][1]
        self.assertEqual((t["date_achat"], t["montant"], t["commission"], t["carte"], t["type_carte"], t["autorisation"]),
                         (datetime.date(2026, 11, 12), 1400.0, 42.0, "422222xxxx2222", "Nat", "678901"))
        self.assertEqual(r["bordereaux"][1]["transactions"][0]["autorisation"], "A1B2C3")   # une autorisation n'est pas toujours en chiffres
        self.assertEqual((r["nombre"], r["total"], r["commission"], r["anomalies"]), (3, 1881.0, 56.43, []))

    def test_la_virgule_separe_les_milliers(self):
        self.assertEqual(M.montant("1,149.000"), 1149.0)
        self.assertEqual(M.montant("12,345,678.250"), 12345678.25)
        self.assertEqual(M.montant("92.000"), 92.0)

    def test_une_transaction_perdue_se_voit(self):
        # Les totaux imprimes sont recalcules : une ligne que le lecteur n'a pas su lire ne passe pas en silence.
        tronque = [l for l in RELEVE if not l.startswith("0000006002")]
        anomalies = M.lire_releve(tronque)["anomalies"]
        self.assertTrue(any("1990010006" in a and "1500.000" in a for a in anomalies), anomalies)
        self.assertTrue(any("journée" in a for a in anomalies), anomalies)

    def test_type_de_carte_absent(self):
        ligne = "0000006001 12/11/2026 100.000 3.000 411111xxxx1111 012345"
        r = M.lire_releve(RELEVE[:7] + [ligne])
        self.assertEqual((r["bordereaux"][0]["transactions"][0]["autorisation"], r["bordereaux"][0]["transactions"][0]["type_carte"]),
                         ("012345", ""))

    def test_avis_net_par_remise(self):
        a = M.lire_avis(AVIS)
        self.assertEqual((a["affiliation"], a["compte"], a["edite_le"]), ("0800000001", "N100000001", datetime.date(2026, 11, 17)))
        self.assertEqual([(r["terminal"], r["remise"], r["date_traitement"], r["brut"], r["commission"], r["tva"], r["net"])
                          for r in a["remises"]],
                         [("01", "0006", datetime.date(2026, 11, 16), 1500.0, 45.0, 8.55, 1446.45),
                          ("01", "0007", datetime.date(2026, 11, 16), 381.0, 11.43, 2.172, 367.398)])
        self.assertEqual((a["net"], a["anomalies"]), (1813.848, []))

    def test_avis_dont_le_net_ne_retombe_pas(self):
        faux = [l.replace("1,446.450", "1,446.000") for l in AVIS]
        anomalies = M.lire_avis(faux)["anomalies"]
        self.assertTrue(any("0006" in a for a in anomalies), anomalies)
        self.assertTrue(any("annonce" in a for a in anomalies), anomalies)

    def test_mois_de_l_avis_en_anglais_ou_en_francais(self):
        self.assertEqual(M._date_avis("16-NOV-26"), datetime.date(2026, 11, 16))
        self.assertEqual(M._date_avis("03-FEB-27"), datetime.date(2027, 2, 3))
        self.assertEqual(M._date_avis("03-FÉVR.-27"), datetime.date(2027, 2, 3))
        self.assertEqual(M._date_avis("01-AOÛT-2027"), datetime.date(2027, 8, 1))
        self.assertIsNone(M._date_avis("16-XYZ-26"))

    def test_nom_de_la_fiche(self):
        self.assertEqual(M.nom_remise(datetime.date(2026, 11, 16), "01", "0006"), "TPE-BIAT-20261116-01-0006")


class TestPaiements(unittest.TestCase):
    def test_reference_saisie_a_la_caisse(self):
        self.assertEqual(M.autorisation_de("061800-"), "61800")
        self.assertEqual(M.autorisation_de("061800"), M.autorisation_de(" 61800 - BIAT"))
        self.assertEqual(M.autorisation_de("j4l6c5-"), "J4L6C5")
        self.assertEqual(M.autorisation_de(None), "")

    def test_par_l_autorisation(self):
        transactions = M.lire_releve(RELEVE)["bordereaux"][0]["transactions"]
        reponses = M.rapprocher_transactions(transactions, [
            paiement("PE-1", "012345-", 100.0), paiement("PE-2", "678901-", 1400.0), paiement("PE-9", "999999-", 100.0)])
        self.assertEqual([(r["etat"], r["paiement"]) for r in reponses], [(M.TROUVE, "PE-1"), (M.TROUVE, "PE-2")])
        self.assertEqual(M.verdict([dict(t, **r) for t, r in zip(transactions, reponses)])["controle"], M.BON)

    def test_bonne_autorisation_mauvais_montant(self):
        transactions = M.lire_releve(RELEVE)["bordereaux"][0]["transactions"][:1]
        reponses = M.rapprocher_transactions(transactions, [paiement("PE-1", "012345-", 90.0)])
        self.assertEqual((reponses[0]["etat"], reponses[0]["paiement"], reponses[0]["montant_erp"]), (M.MONTANT_DIFFERENT, "PE-1", 90.0))
        avis = M.verdict([dict(transactions[0], **reponses[0])])
        self.assertEqual(avis["controle"], M.A_SIGNALER)
        self.assertIn("100,000", avis["a_signaler"][0])
        self.assertIn("90,000", avis["a_signaler"][0])

    def test_repli_par_le_montant_seulement_si_un_seul_candidat(self):
        transactions = [{"autorisation": "012345", "montant": 100.0, "date_achat": datetime.date(2026, 11, 12)}]
        # reference mal saisie, un seul paiement par carte de ce montant : designe, avec la remarque
        un = M.rapprocher_transactions(transactions, [paiement("PE-1", "CB", 100.0), paiement("PE-2", "VIR", 100.0, carte=False)])
        self.assertEqual((un[0]["etat"], un[0]["paiement"]), (M.TROUVE_MONTANT, "PE-1"))
        self.assertEqual(M.verdict([dict(transactions[0], **un[0])])["controle"], M.BON)
        # deux candidats : on ne choisit pas
        deux = M.rapprocher_transactions(transactions, [paiement("PE-1", "CB", 100.0), paiement("PE-2", "CB2", 100.0)])
        self.assertEqual((deux[0]["etat"], deux[0]["paiement"]), (M.INTROUVABLE, None))
        # trop loin de la date d'achat, ou deja rattache a une autre remise : pas un candidat
        loin = M.rapprocher_transactions(transactions, [paiement("PE-1", "CB", 100.0, date="2026-11-25")])
        self.assertEqual(loin[0]["etat"], M.INTROUVABLE)
        pris = M.rapprocher_transactions(transactions, [paiement("PE-1", "012345-", 100.0)], deja_pris={"PE-1"})
        self.assertEqual(pris[0]["etat"], M.INTROUVABLE)

    def test_un_paiement_ne_sert_qu_une_fois(self):
        transactions = [{"autorisation": "111111", "montant": 50.0, "date_achat": "2026-11-12"},
                        {"autorisation": "222222", "montant": 50.0, "date_achat": "2026-11-12"}]
        reponses = M.rapprocher_transactions(transactions, [paiement("PE-1", "111111-", 50.0)])
        self.assertEqual([r["etat"] for r in reponses], [M.TROUVE, M.INTROUVABLE])

    def test_verdict_sans_releve(self):
        self.assertEqual(M.verdict([])["controle"], M.SANS_RELEVE)
        self.assertEqual(M.verdict([], ["aucun bordereau lu"])["controle"], M.A_SIGNALER)


class TestCreditBancaire(unittest.TestCase):
    """LA DATE N'EST PAS UNE CLE (« le releve ne veut pas dire necessairement date valeur », 17/11/2026)."""

    def test_le_net_de_l_avis_quel_que_soit_le_jour(self):
        for jour in ("2026-11-14", "2026-11-16", "2026-11-17", "2026-11-24"):
            out = M.affecter([{"cle": "L1", "date": jour, "montant": 1446.45}], [R6, R7])
            self.assertEqual((out["L1"]["remises"], out["L1"]["nature"]), ([R6["name"]], "net"), jour)

    def test_hors_fenetre_ou_autre_montant_rien(self):
        self.assertEqual(M.affecter([{"cle": "L1", "date": "2026-12-20", "montant": 1446.45}], [R6]), {})
        self.assertEqual(M.affecter([{"cle": "L1", "date": "2026-11-10", "montant": 1446.45}], [R6]), {})
        self.assertEqual(M.affecter([{"cle": "L1", "date": "2026-11-17", "montant": 1446.0}], [R6]), {})

    def test_un_seul_credit_pour_la_journee(self):
        out = M.affecter([{"cle": "L1", "date": "2026-11-17", "montant": 1813.848}], [R6, R7])
        self.assertEqual((sorted(out["L1"]["remises"]), out["L1"]["nature"], out["L1"]["nombre"]),
                         ([R6["name"], R7["name"]], "net", 3))

    def test_credit_du_brut(self):
        out = M.affecter([{"cle": "L1", "date": "2026-11-17", "montant": 381.0}], [R6, R7])
        self.assertEqual((out["L1"]["remises"], out["L1"]["nature"]), ([R7["name"]], "brut"))
        # sans avis (net inconnu), seul le brut du releve se compare
        sans_avis = dict(R7, net=0.0, tva=0.0)
        self.assertEqual(M.affecter([{"cle": "L1", "date": "2026-11-17", "montant": 367.398}], [sans_avis]), {})
        self.assertEqual(M.affecter([{"cle": "L1", "date": "2026-11-17", "montant": 381.0}], [sans_avis])["L1"]["nature"], "brut")

    def test_une_remise_ne_paie_qu_une_ligne(self):
        jumelle = dict(R7, name="TPE-BIAT-20261117-01-0008", remise="0008", date_traitement="2026-11-17")
        out = M.affecter([{"cle": "L1", "date": "2026-11-17", "montant": 367.398},
                          {"cle": "L2", "date": "2026-11-18", "montant": 367.398},
                          {"cle": "L3", "date": "2026-11-19", "montant": 367.398}], [jumelle, R7])
        self.assertEqual((out["L1"]["remises"], out["L2"]["remises"]), ([R7["name"]], [jumelle["name"]]))   # la plus ancienne d'abord
        self.assertNotIn("L3", out)

    def test_un_rattachement_deja_fait_est_garde(self):
        jumelle = dict(R7, name="TPE-BIAT-20261117-01-0008", remise="0008", date_traitement="2026-11-17", mouvement="L1")
        out = M.affecter([{"cle": "L1", "date": "2026-11-17", "montant": 367.398}], [R7, jumelle])
        self.assertEqual(out["L1"]["remises"], [jumelle["name"]])

    def test_la_phrase_de_la_ligne(self):
        tpe = M.affecter([{"cle": "L1", "date": "2026-11-17", "montant": 1446.45}], [R6])["L1"]
        phrase = M.raison_credit(tpe)
        for morceau in ("0006", "16/11/2026", "2 paiement(s)", "1 500,000", "45,000", "8,550", "1 446,450", "pas encore comptabilisée"):
            self.assertIn(morceau, phrase)

    def test_resume_pour_le_bandeau(self):
        r = M.resume_remises([dict(R6, etat=M.ETAT_ATTENTE, controle=M.BON), dict(R7, etat=M.ETAT_CREDITEE, controle=M.A_SIGNALER)],
                             [{"date": "2026-11-05", "montant": 281.0}, {"date": "2026-11-16", "montant": 50.0}],
                             datetime.date(2026, 11, 17))
        self.assertEqual((r["nb"], r["en_attente"], r["creditees"], r["a_signaler"]), (2, {"nb": 1, "net": 1446.45}, 1, 1))
        self.assertEqual(r["sans_releve"], {"nb": 2, "montant": 331.0, "en_retard": 1})
        self.assertIn("1 remise(s) en attente de crédit", r["phrase"])
        self.assertIn("aucune remise", M.resume_remises([])["phrase"])


class TestClassementBiat(unittest.TestCase):
    """Le branchement dans le classement des lignes BIAT."""

    def ligne(self, operation, credit=0.0, debit=0.0, cle="L1", **plus):
        return dict({"cle": cle, "date": "2026-11-17", "operation": operation, "reference": "FT26321000000001",
                     "debit": debit, "credit": credit, "montant": debit or credit, "sens": "Debit" if debit else "Credit"}, **plus)

    def test_candidats_les_credits_que_rien_ne_reconnait(self):
        lignes = [self.ligne("Credit Divers Operation Inconnue", credit=10.0, cle="A"),
                  self.ligne("Credit Divers Chq Telec A Enc Cre Global/borde : 1234567", credit=20.0, cle="B"),
                  self.ligne("Virement recu STE EXEMPLE", credit=30.0, cle="C"),
                  self.ligne("Virement remise TPE 0006", credit=40.0, cle="D"),
                  self.ligne("Debit Divers Operation Inconnue", debit=50.0, cle="E"),
                  self.ligne("Credit Divers Operation Inconnue", credit=60.0, cle="F", lien_manuel=1)]
        self.assertEqual([c["cle"] for c in biat.candidats_tpe(lignes)], ["A", "D"])
        self.assertEqual(biat.candidats_tpe(lignes)[0], {"cle": "A", "date": "2026-11-17", "montant": 10.0})

    def test_un_credit_du_terminal_n_est_pas_un_virement_client(self):
        self.assertTrue(biat.credit_de_terminal("Virement remise TPE 0006"))
        self.assertTrue(biat.credit_de_terminal("Crédit monétique affilié 0800000001"))
        self.assertFalse(biat.credit_de_terminal("Virement recu STE EXEMPLE"))
        self.assertFalse(biat.credit_de_terminal("Virement recu STE TPEXPRESS"))        # mot entier seulement

    def test_ligne_reconnue_non_comptabilisee(self):
        ligne = self.ligne("Credit Divers Operation Inconnue", credit=1446.45)
        tpe = M.affecter(biat.candidats_tpe([ligne]), [R6])
        c = biat.classer_ligne(ligne, {"tpe": tpe})
        self.assertEqual((c["regle"], c["categorie"], c["statut"], c["groupe"], c["document_type"], c["document_name"]),
                         ("remise_tpe", "encaissement", biat.STATUT_BROUILLON, "tpe:" + R6["name"], M.DOCTYPE, R6["name"]))
        self.assertEqual((c["montant_document"], c["ecart"]), (1446.45, 0.0))
        self.assertIn("net 1 446,450", c["raison"])

    def test_sans_remise_le_libelle_reste_inconnu(self):
        ligne = self.ligne("Credit Divers Operation Inconnue", credit=1446.45)
        for contexte in ({}, {"tpe": {}}, {"tpe": None}):
            c = biat.classer_ligne(ligne, contexte)
            self.assertEqual((c["regle"], c["statut"]), ("", biat.STATUT_A_VERIFIER))

    def test_journal_des_verifications(self):
        from bank_retenue_sync.tasks import passages

        res = biat.resume_passage({"robot": True, "crees": 2, "cartes": {"nouvelles": ["a", "b"], "illisibles": []}})
        self.assertEqual(res["cartes"], 2)
        self.assertNotIn("erreur", res)
        self.assertIn("2 remise(s) carte reçue(s)", passages._detail_de("biat", res))
        # une boite injoignable est dite, sans faire echouer la verification
        res = biat.resume_passage({"robot": True, "cartes": {"erreur": "IMAP timeout", "nouvelles": []}})
        self.assertNotIn("erreur", res)
        self.assertIn("courriels non lus (IMAP timeout)", passages._detail_de("biat", res))
        # sans courriels lus (depot d'un bordereau), la phrase ne change pas
        self.assertNotIn("carte", passages._detail_de("biat", biat.resume_passage({"robot": True, "crees": 0})))


if __name__ == "__main__":
    unittest.main()
