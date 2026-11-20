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


CREDIT_TPE = "Reglement Affilie BIAT TPE : 0010010006\\Remise : 20261116-0800000001-1990010006"
COMMISSION_TPE = "COM TVA REF REMISE AFF 0010010006"
R6B = dict(R6, bordereau="1990010006")
R7B = dict(R7, bordereau="1990010007")


class TestLibellesBancaires(unittest.TestCase):
    """Les libelles vus au releve BIAT du 17/11/2026 : credit du brut, commission prelevee a part."""

    def test_le_credit_nomme_la_remise(self):
        self.assertEqual(M.lire_libelle(CREDIT_TPE), {"terminal": "0010010006", "jour_remise": "2026-11-16",
                                                      "affiliation": "0800000001", "bordereau": "1990010006"})
        self.assertEqual(M.lire_libelle(COMMISSION_TPE), {"terminal": "0010010006"})
        self.assertEqual(M.lire_libelle("Versement TND"), {})

    def test_regles_biat(self):
        self.assertEqual(biat.detecter(CREDIT_TPE, "Credit")["regle"], "remise_tpe")
        self.assertEqual(biat.detecter(COMMISSION_TPE, "Debit")["regle"], "com_tpe")
        self.assertEqual(biat.detecter(COMMISSION_TPE, "Debit")["categorie"], "frais_bancaires")
        self.assertEqual(biat.detecter(CREDIT_TPE, "Debit")["regle"], "")
        self.assertEqual(biat.detecter(COMMISSION_TPE, "Credit")["regle"], "")
        # la commission d'une remise de cheques garde sa regle
        self.assertEqual(biat.detecter("Debit Divers Commission CHQ A Enc/Bordereau : 1234567", "Debit")["regle"], "com_enc_cheque")

    def test_le_bordereau_du_libelle_designe_la_remise(self):
        # deux remises du meme brut : sans le libelle la plus ancienne serait prise ; le bordereau tranche
        jumelle = dict(R7B, name="TPE-BIAT-20261116-01-0008", remise="0008", bordereau="1990010008")
        ligne = {"cle": "L1", "date": "2026-11-17", "montant": 381.0, "bordereau": "1990010008", "jour_remise": "2026-11-16",
                 "terminal": "0010010008"}
        out = M.affecter([ligne], [R7B, jumelle])
        self.assertEqual((out["L1"]["remises"], out["L1"]["nature"], out["L1"]["terminal"]), ([jumelle["name"]], "brut", "0010010008"))
        self.assertEqual(out["L1"]["par_remise"], {jumelle["name"]: 381.0})
        # le bordereau nomme une remise, mais le montant ne la confirme pas : on ne force rien
        self.assertEqual(M.affecter([dict(ligne, montant=380.0)], [R7B, jumelle]), {})

    def test_candidat_avec_ce_que_dit_le_libelle(self):
        ligne = {"cle": "A", "date": "2026-11-17", "operation": CREDIT_TPE, "credit": 1500.0, "sens": "Credit"}
        self.assertEqual(biat.candidats_tpe([ligne]), [{"cle": "A", "date": "2026-11-17", "montant": 1500.0, "bordereau": "1990010006",
                                                        "jour_remise": "2026-11-16", "terminal": "0010010006"}])
        debit = {"cle": "B", "date": "2026-11-17", "operation": COMMISSION_TPE, "debit": 45.0, "sens": "Debit"}
        self.assertEqual(biat.prelevements_tpe([ligne, debit]), [{"cle": "B", "date": "2026-11-17", "montant": 45.0, "terminal": "0010010006"}])

    def test_remise_reconnue_au_libelle_sans_avis(self):
        ligne = {"cle": "A", "date": "2026-11-17", "operation": CREDIT_TPE, "credit": 1500.0, "sens": "Credit", "montant": 1500.0}
        c = biat.classer_ligne(ligne, {"tpe": {}})
        self.assertEqual((c["regle"], c["categorie"], c["statut"]), ("remise_tpe", "encaissement", biat.STATUT_ORPHELIN))
        self.assertIn("avis de la banque", c["raison"])


class TestFraisEtTaux(unittest.TestCase):
    """« A chaque fois que tu identifies un frais bancaire relatif au TPE, calcule son taux par rapport a la
    transaction » (17/11/2026)."""

    def test_taux(self):
        self.assertEqual(M.taux(45.0, 1500.0), 3.0)
        self.assertEqual(M.taux(53.55, 1500.0), 3.57)
        self.assertEqual(M.taux(11.43, 381.0), 3.0)
        self.assertEqual(M.taux(10.0, 0), 0.0)

    def test_ce_que_la_banque_preleve(self):
        par_terminal = {"0010010006": R6B["name"]}
        for montant, quoi in ((45.0, "commission"), (53.55, "commission et TVA"), (8.55, "TVA"), (12.0, None)):
            t = M.remise_du_prelevement({"date": "2026-11-17", "montant": montant, "terminal": "0010010006"}, [R6B, R7B], par_terminal)
            self.assertEqual((t["name"], t["remise"], t["quoi"]), (R6B["name"], "0006", quoi), montant)

    def test_sans_le_credit_le_bordereau_finit_comme_la_reference(self):
        t = M.remise_du_prelevement({"date": "2026-11-17", "montant": 11.43, "terminal": "0010010007"}, [R6B, R7B])
        self.assertEqual((t["name"], t["quoi"]), (R7B["name"], "commission"))
        self.assertIsNone(M.remise_du_prelevement({"date": "2026-11-17", "montant": 11.43, "terminal": "0010019999"}, [R6B, R7B]))
        self.assertIsNone(M.remise_du_prelevement({"date": "2027-03-01", "montant": 11.43, "terminal": "0010010007"}, [R6B, R7B]))

    def test_la_phrase_porte_le_taux(self):
        t = M.remise_du_prelevement({"date": "2026-11-17", "montant": 45.0, "terminal": "0010010006"}, [R6B], {})
        phrase = M.raison_prelevement(45.0, t, 1500.0)
        for morceau in ("commission du terminal", "n° 0006", "45,000", "3,00 %", "1 500,000"):
            self.assertIn(morceau, phrase)
        inattendu = M.remise_du_prelevement({"date": "2026-11-17", "montant": 12.0, "terminal": "0010010006"}, [R6B], {})
        self.assertIn("l’avis de la banque annonçait", M.raison_prelevement(12.0, inattendu, 1500.0))
        self.assertIn("0,80 %", M.raison_prelevement(12.0, inattendu, 1500.0))
        self.assertIn("non retrouvée", M.raison_prelevement(12.0, None))

    def test_ligne_de_commission_dans_les_frais_du_mois(self):
        ligne = {"cle": "B", "date": "2026-11-17", "operation": COMMISSION_TPE, "debit": 45.0, "montant": 45.0, "sens": "Debit",
                 "reference": "CHG000000000001"}
        t = M.remise_du_prelevement({"date": "2026-11-17", "montant": 45.0, "terminal": "0010010006"}, [R6B], {})
        contexte = {"prelevements_tpe": {"B": dict(t, raison=M.raison_prelevement(45.0, t, 1500.0))}}
        c = biat.classer_ligne(ligne, contexte)
        self.assertEqual((c["regle"], c["categorie"], c["statut"], c["groupe"]),
                         ("com_tpe", "frais_bancaires", biat.STATUT_ORPHELIN, "tpe:" + R6B["name"]))
        self.assertIn("3,00 %", c["raison"])
        contexte["frais_mensuels"] = {"2026-11": {"name": "JV-1", "docstatus": 1}}
        c = biat.classer_ligne(ligne, contexte)
        self.assertEqual((c["statut"], c["document_type"], c["document_name"], c["montant_document"]),
                         (biat.STATUT_IDENTIFIE, "Journal Entry", "JV-1", 45.0))
        # elle entre dans le cumul mensuel des frais BIAT, comme toute commission
        cumul = biat.cumul_frais([dict(ligne, categorie=c["categorie"], statut=c["statut"])], "2026-11")
        self.assertEqual((cumul["nb"], cumul["frais"], cumul["total"]), (1, 45.0, 45.0))

    def test_reference_ecrite_par_un_autre_ecran(self):
        self.assertEqual(M.autorisations_de("Ticket TPE 012345 - BIAT"), {"12345"})
        transactions = [{"autorisation": "012345", "montant": 100.0, "date_achat": "2026-11-12"}]
        reponses = M.rapprocher_transactions(transactions, [paiement("PE-1", "Ticket TPE 012345 - BIAT", 100.0)])
        self.assertEqual((reponses[0]["etat"], reponses[0]["paiement"]), (M.TROUVE, "PE-1"))


class TestEcritureDeLaRemise(unittest.TestCase):
    """Option A (17/11/2026) : compte d'attente des cartes, solde quand la banque credite la remise."""

    ATTENTE, BANQUE = "Cartes à encaisser - A&S", "BIAT - A&S"

    def remise(self, credite=1500.0, **plus):
        return dict({"name": R6["name"], "brut": 1500.0, "montant_credite": credite, "controle": M.BON,
                     "transactions": [{"paiement": "PE-1", "montant": 100.0, "etat": M.TROUVE},
                                      {"paiement": "PE-2", "montant": 1400.0, "etat": M.TROUVE}]}, **plus)

    def paiements(self, **plus):
        base = {"PE-1": {"docstatus": 1, "compte": self.ATTENTE, "montant": 100.0},
                "PE-2": {"docstatus": 1, "compte": self.ATTENTE, "montant": 1400.0}}
        for nom, valeurs in plus.items():
            base[nom.replace("_", "-")].update(valeurs)
        return base

    def test_credit_du_brut(self):
        d = M.decider_ecriture(self.remise(), self.paiements(), self.ATTENTE, self.BANQUE)
        self.assertTrue(d["faire"])
        self.assertEqual(d["lignes"], [{"account": self.BANQUE, "debit": 1500.0}, {"account": self.ATTENTE, "credit": 1500.0}])
        self.assertEqual(d["frais"], 0.0)

    def test_credit_du_net_les_frais_sont_dans_l_ecriture(self):
        d = M.decider_ecriture(self.remise(credite=1446.45), self.paiements(), self.ATTENTE, self.BANQUE)
        self.assertEqual(d["lignes"], [{"account": self.BANQUE, "debit": 1446.45}, {"account": M.COMPTE_FRAIS, "debit": 53.55},
                                       {"account": self.ATTENTE, "credit": 1500.0}])
        self.assertEqual(round(sum(l.get("debit", 0) for l in d["lignes"]), 3), 1500.0)

    def test_refus_qui_disent_pourquoi(self):
        cas = (
            (self.remise(), self.paiements(), "", "n’est pas réglé"),
            (self.remise(controle=M.A_SIGNALER, transactions=[{"paiement": None, "montant": 100.0, "etat": M.INTROUVABLE}]),
             self.paiements(), self.ATTENTE, "1 transaction(s) sans paiement"),
            (self.remise(transactions=[]), self.paiements(), self.ATTENTE, "n’est pas encore arrivé"),
            (self.remise(), self.paiements(PE_2={"docstatus": 2}), self.ATTENTE, "PE-2 n’est plus validé"),
            (self.remise(), self.paiements(PE_1={"compte": "STE - Zitouna"}), self.ATTENTE, "PE-1 n’est pas sur le compte d’attente"),
            (self.remise(), self.paiements(PE_1={"montant": 90.0}), self.ATTENTE, "les paiements font"),
            (self.remise(credite=1600.0), self.paiements(), self.ATTENTE, "incohérent"),
        )
        for remise, paiements, attente, attendu in cas:
            d = M.decider_ecriture(remise, paiements, attente, self.BANQUE)
            self.assertFalse(d["faire"], attendu)
            self.assertIn(attendu, d["raison"])
            self.assertEqual(d["lignes"], [])

    # ── Écart accepté (20/11/2026 : « comment accepter l'écart et le comptabiliser ») ──
    PERTE, PRODUIT = "Perte de non paiement - A&S", "Produits divers - A&S"

    def remise_ecart(self, banque=100.0, erp=100.4, accepte=1, ecart=None, brut=None):
        """PE-1 : `erp` dans ERPNext, `banque` encaissés ; PE-2 : 1 400 des deux côtés."""
        t = {"paiement": "PE-1", "montant": banque, "montant_erp": erp, "etat": M.MONTANT_DIFFERENT,
             "ecart_accepte": accepte, "ecart_montant": round(banque - erp, 3) if ecart is None else ecart}
        brut = round(banque + 1400.0, 3) if brut is None else brut
        return {"name": R6["name"], "brut": brut, "montant_credite": brut,
                "controle": M.verdict([t, {"paiement": "PE-2", "montant": 1400.0, "etat": M.TROUVE}])["controle"],
                "transactions": [t, {"paiement": "PE-2", "montant": 1400.0, "etat": M.TROUVE}]}

    def test_la_banque_a_encaisse_moins_la_difference_va_en_perte(self):
        comptes = {"perte": self.PERTE, "produit": None}
        d = M.decider_ecriture(self.remise_ecart(), self.paiements(PE_1={"montant": 100.4}), self.ATTENTE, self.BANQUE, comptes)
        self.assertTrue(d["faire"], d["raison"])
        # la banque verse 1 500 ; le compte d'attente rend ce que les paiements y ont porte (1 500,400) ; 0,400 en perte
        self.assertEqual(d["lignes"], [{"account": self.BANQUE, "debit": 1500.0}, {"account": self.PERTE, "debit": 0.4},
                                       {"account": self.ATTENTE, "credit": 1500.4}])
        self.assertEqual((d["perte"], d["produit"]), (0.4, 0.0))
        debit = round(sum(l.get("debit", 0) for l in d["lignes"]), 3)
        self.assertEqual(debit, round(sum(l.get("credit", 0) for l in d["lignes"]), 3))

    def test_sans_acceptation_la_remise_reste_a_signaler(self):
        r = self.remise_ecart(accepte=0)
        self.assertEqual(r["controle"], M.A_SIGNALER)
        d = M.decider_ecriture(r, self.paiements(PE_1={"montant": 100.4}), self.ATTENTE, self.BANQUE, {"perte": self.PERTE})
        self.assertFalse(d["faire"])
        self.assertIn("1 transaction(s) sans paiement au bon montant", d["raison"])

    def test_la_banque_a_encaisse_plus_il_faut_un_compte_de_produit(self):
        r = self.remise_ecart(banque=100.0, erp=99.6)                     # la caisse a saisi 99,600, la carte a pris 100
        paiements = self.paiements(PE_1={"montant": 99.6})
        d = M.decider_ecriture(r, paiements, self.ATTENTE, self.BANQUE, {"perte": self.PERTE, "produit": None})
        self.assertFalse(d["faire"])
        self.assertIn("compte à créditer", d["raison"])
        d = M.decider_ecriture(r, paiements, self.ATTENTE, self.BANQUE, {"perte": self.PERTE, "produit": self.PRODUIT})
        self.assertEqual(d["lignes"], [{"account": self.BANQUE, "debit": 1500.0}, {"account": self.ATTENTE, "credit": 1499.6},
                                       {"account": self.PRODUIT, "credit": 0.4}])
        self.assertEqual((d["perte"], d["produit"]), (0.0, 0.4))

    def test_les_deux_sens_sur_le_meme_compte_debit_ou_credit(self):
        """Décision du 20/11/2026 : « ajoute ça dans perte de non paiement mais en crédit »."""
        comptes = {"perte": self.PERTE, "produit": self.PERTE}
        plus = M.decider_ecriture(self.remise_ecart(banque=100.0, erp=99.6), self.paiements(PE_1={"montant": 99.6}),
                                  self.ATTENTE, self.BANQUE, comptes)
        self.assertEqual(plus["lignes"], [{"account": self.BANQUE, "debit": 1500.0}, {"account": self.ATTENTE, "credit": 1499.6},
                                          {"account": self.PERTE, "credit": 0.4}])
        moins = M.decider_ecriture(self.remise_ecart(), self.paiements(PE_1={"montant": 100.4}), self.ATTENTE, self.BANQUE, comptes)
        self.assertEqual(moins["lignes"][1], {"account": self.PERTE, "debit": 0.4})

    def test_l_acceptation_vaut_pour_l_ecart_vu_pas_pour_un_autre(self):
        # accepté à −0,400 ; le paiement est ensuite passé à 101 : ce n'est plus l'écart accepté
        t = {"paiement": "PE-1", "montant": 100.0, "montant_erp": 101.0, "etat": M.MONTANT_DIFFERENT, "ecart_accepte": 1, "ecart_montant": -0.4}
        self.assertFalse(M.ecart_est_accepte(t))
        self.assertTrue(M.ecart_est_accepte(dict(t, montant_erp=100.4)))
        # même si la fiche n'a pas encore été relue, le paiement RÉEL fait foi au moment d'écrire
        d = M.decider_ecriture(self.remise_ecart(brut=1500.0), self.paiements(PE_1={"montant": 101.0}), self.ATTENTE, self.BANQUE, {"perte": self.PERTE})
        self.assertFalse(d["faire"])
        self.assertIn("a changé de montant depuis que son écart a été accepté", d["raison"])
        # un « Introuvable » ne s'accepte pas : aucun paiement n'a débité le compte d'attente
        self.assertFalse(M.ecart_est_accepte({"paiement": None, "montant": 20.0, "etat": M.INTROUVABLE, "ecart_accepte": 1, "ecart_montant": 20.0}))

    def test_ecart_accepte_avec_credit_du_net(self):
        r = dict(self.remise_ecart(), montant_credite=1446.45)
        d = M.decider_ecriture(r, self.paiements(PE_1={"montant": 100.4}), self.ATTENTE, self.BANQUE, {"perte": self.PERTE})
        self.assertEqual(d["lignes"], [{"account": self.BANQUE, "debit": 1446.45}, {"account": M.COMPTE_FRAIS, "debit": 53.55},
                                       {"account": self.PERTE, "debit": 0.4}, {"account": self.ATTENTE, "credit": 1500.4}])

    def test_ligne_identifiee_par_son_ecriture(self):
        ligne = {"cle": "A", "date": "2026-11-17", "operation": CREDIT_TPE, "credit": 1500.0, "sens": "Credit", "montant": 1500.0}
        tpe = M.affecter(biat.candidats_tpe([ligne]), [R6B])
        tpe["A"]["ecritures"], tpe["A"]["refus"] = [{"name": "JV-9", "docstatus": 1}], ""
        c = biat.classer_ligne(ligne, {"tpe": tpe})
        self.assertEqual((c["statut"], c["document_type"], c["document_name"]), (biat.STATUT_IDENTIFIE, "Journal Entry", "JV-9"))
        self.assertIn("écriture JV-9", c["raison"])
        tpe["A"]["ecritures"] = [{"name": "JV-9", "docstatus": 0}]
        self.assertEqual(biat.classer_ligne(ligne, {"tpe": tpe})["statut"], biat.STATUT_BROUILLON)
        tpe["A"]["ecritures"], tpe["A"]["refus"] = [], "le paiement PE-1 n’est plus validé"
        c = biat.classer_ligne(ligne, {"tpe": tpe})
        self.assertEqual((c["statut"], c["document_type"]), (biat.STATUT_BROUILLON, M.DOCTYPE))
        self.assertIn("pas comptabilisée : le paiement PE-1", c["raison"])

    def test_journal_des_verifications(self):
        from bank_retenue_sync.tasks import passages

        res = biat.resume_passage({"robot": True, "crees": 4, "cartes_compta": {"ecritures": [{"je": "JV-1"}, {"je": "JV-2"}], "erreurs": []}})
        self.assertEqual(res["cartes_ecritures"], 2)
        self.assertIn("2 remise(s) carte comptabilisée(s)", passages._detail_de("biat", res))
        res = biat.resume_passage({"robot": True, "cartes_compta": {"ecritures": [], "erreurs": [{"erreur": "remise X : période close"}]}})
        self.assertIn("cartes : remise X : période close", res["erreur"])     # une ecriture impossible se sait

    def test_resume_dit_ce_qui_n_est_pas_comptabilise(self):
        r = M.resume_remises([dict(R6, etat=M.ETAT_CREDITEE, controle=M.BON, ecriture=None, comptabilisation="paiement annulé"),
                              dict(R7, etat=M.ETAT_CREDITEE, controle=M.BON, ecriture="JV-1", comptabilisation="")])
        self.assertEqual(r["non_comptabilisees"], 1)
        self.assertIn("1 créditée(s) pas comptabilisée(s)", r["phrase"])


if __name__ == "__main__":
    unittest.main()
