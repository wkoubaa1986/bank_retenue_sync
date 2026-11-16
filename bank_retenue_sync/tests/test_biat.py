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
        for libelle in ("Prelevement STE EXEMPLE", "Cheque 0001234", "", None):   # (un virement recu est reconnu depuis le 16/11/2026)
            self.assertEqual(biat.detecter(libelle, "Credit")["regle"], "")


class TestClassement(unittest.TestCase):
    def test_remise_sans_paiement_a_rapprocher(self):
        c = biat.classer_ligne(ligne(REMISE, credit=1500.0), {})
        self.assertEqual((c["statut"], c["regle"], c["groupe"]), ("Orphelin", "enc_cheque", "bordereau:1234567"))
        self.assertIn("BR:1234567", c["raison"])
        self.assertIn("bordereau à collecter", c["raison"])
        self.assertIsNone(c["document_name"])
        collecte = biat.classer_ligne(ligne(REMISE, credit=1500.0), {"bordereaux": {"1234567": "BR-BIAT-1234567"}})
        self.assertIn("bordereau collecté (BR-BIAT-1234567)", collecte["raison"])
        self.assertEqual(collecte["statut"], "Orphelin")                                   # collecter ne rapproche pas
        self.assertIn("chèques à rapprocher", collecte["raison"])
        lu = biat.classer_ligne(ligne(REMISE, credit=1500.0), {"bordereaux": {"1234567": "BR-BIAT-1234567"},
                                                              "lectures": {"1234567": "3 chèques : tous trouvés"}})
        self.assertIn("bordereau collecté (BR-BIAT-1234567), lu par l’IA — 3 chèques : tous trouvés", lu["raison"])
        self.assertEqual(lu["statut"], "Orphelin")                                         # lire ne comptabilise pas

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


class TestEncaissementParBordereau(unittest.TestCase):
    """La BIAT ne publie pas le detail de ses remises : le bordereau collecte en tient lieu."""

    LIGNE = {"date": "2026-11-12", "credit": 3010.127, "reference": "REF1"}
    BORDEREAU = {"name": "BR-BIAT-7473685", "nature": "Chèques", "lecture_statut": "Lue", "a_signaler": None, "pieces": [
        {"numero": "4001767", "montant": 270.0, "paiement": "PE-1", "client": "Ideale eau"},
        {"numero": "4001066", "montant": 1045.0, "paiement": "PE-2", "client": "FM WATER PLUS"},
        {"numero": "8965167", "montant": 1695.127, "paiement": "PE-3", "client": "Agro Techno Service"}]}
    PAIEMENTS = {"PE-1": {"montant": 270.0, "compte": "Chèques - A&S", "docstatus": 1},
                 "PE-2": {"montant": 1045.0, "compte": "Chèques - A&S", "docstatus": 1},
                 "PE-3": {"montant": 1694.88, "compte": "Chèques - A&S", "docstatus": 1}}

    def decider(self, ligne=None, bordereau=None, paiements=None, **change):
        b = dict(self.BORDEREAU if bordereau is None else bordereau, **change)
        return biat.decider_encaissement(ligne or self.LIGNE, "7473685", b, self.PAIEMENTS if paiements is None else paiements)

    def test_cas_reel_encaisse_avec_son_petit_ecart(self):
        d = self.decider()
        self.assertTrue(d["faire"])
        self.assertEqual((d["total"], d["ecart"]), (3009.88, 0.247))
        self.assertEqual([(l["ref_paiement"], l["n_cheque"], l["valeur"], l["bon_remise"], l["date"], l["statut"]) for l in d["lignes"]],
                         [("PE-1", "4001767", 270.0, "7473685", "2026-11-12", "Versé"),
                          ("PE-2", "4001066", 1045.0, "7473685", "2026-11-12", "Versé"),
                          ("PE-3", "8965167", 1694.88, "7473685", "2026-11-12", "Versé")])
        # L'ecart est trace sur le cheque qui le porte, sans bloquer.
        self.assertEqual([(e["sous_type"], e["bloquant"], e["suivi"], e["ref_paiement"], e["montant_piece"], e["montant_advice"], e["ecart"])
                          for e in d["ecarts"]], [("Toléré", 0, "8965167", "PE-3", 1694.88, 1695.127, -0.247)])

    def test_lecture_non_prouvee_pas_d_ecart_par_cheque(self):
        # Un montant mal lu (somme lue != credit) : on encaisse sur les paiements, sans accuser un cheque.
        pieces = [dict(p) for p in self.BORDEREAU["pieces"]]
        pieces[0]["montant"] = 270.5
        d = self.decider(pieces=pieces)
        self.assertTrue(d["faire"])
        self.assertEqual(d["ecarts"], [])

    def test_refus_et_leur_raison(self):
        self.assertIn("bordereau à collecter", biat.decider_encaissement(self.LIGNE, "7473685", None, {})["raison"])
        self.assertIn("n’est pas une remise de chèques", self.decider(nature="Traites")["raison"])
        self.assertIn("pas encore lu", self.decider(lecture_statut="En attente")["raison"])
        self.assertIn("n’a pas pu lire", self.decider(lecture_statut="Échec")["raison"])
        signale = self.decider(a_signaler="Chèque n° 1 : introuvable dans ERPNext.\nAutre chose.")
        self.assertEqual((signale["faire"], signale["raison"]),
                         (False, "à signaler sur le bordereau — Chèque n° 1 : introuvable dans ERPNext. (+1)"))
        self.assertIn("aucun chèque", self.decider(pieces=[])["raison"])
        sans = [dict(self.BORDEREAU["pieces"][0], paiement=None)]
        self.assertIn("pas de paiement dans ERPNext", self.decider(pieces=sans)["raison"])
        double = [self.BORDEREAU["pieces"][0], self.BORDEREAU["pieces"][0]]
        self.assertIn("cité deux fois", self.decider(pieces=double)["raison"])

    def test_cheque_deja_sorti_du_portefeuille(self):
        ailleurs = dict(self.PAIEMENTS, **{"PE-2": {"montant": 1045.0, "compte": "STE430127B - Zitouna - A&S", "docstatus": 1}})
        self.assertIn("n’est plus dans le portefeuille (« STE430127B - Zitouna - A&S »)", self.decider(paiements=ailleurs)["raison"])
        annule = dict(self.PAIEMENTS, **{"PE-2": {"montant": 1045.0, "compte": "Chèques - A&S", "docstatus": 2}})
        self.assertFalse(self.decider(paiements=annule)["faire"])
        pris = dict(self.PAIEMENTS, **{"PE-2": {"montant": 1045.0, "compte": "Chèques - A&S", "docstatus": 1, "en_encaissement": True}})
        self.assertIn("déjà sur un autre encaissement", self.decider(paiements=pris)["raison"])

    def test_credit_qui_ne_tombe_pas(self):
        # 1 DT par cheque de tolerance : 3 cheques -> 3 DT. Au-dela, on n'encaisse pas, et on dit de combien.
        juste = self.decider(ligne=dict(self.LIGNE, credit=3012.5))
        self.assertTrue(juste["faire"])
        trop = self.decider(ligne=dict(self.LIGNE, credit=3110.127))
        self.assertFalse(trop["faire"])
        self.assertIn("diffèrent de 100.247", trop["raison"])

    def test_ligne_encaissee_ouvre_l_encaissement(self):
        ctx = {"paiements_par_bordereau": {"1234567": [{"name": "PE-1", "montant": 1000.0, "docstatus": 1},
                                                       {"name": "PE-2", "montant": 499.753, "docstatus": 1}]},
               "encaissements": {"1234567": "ENC-16-11-2026-00002"}}
        c = biat.classer_ligne(ligne(REMISE, credit=1500.0), ctx)
        self.assertEqual((c["statut"], c["document_type"], c["document_name"], c["ecart"]),
                         ("Identifie", "Encaissement Paiement", "ENC-16-11-2026-00002", 0.247))
        self.assertIn("(encaissement ENC-16-11-2026-00002) — petit écart de 0.247", c["raison"])

    def test_la_raison_du_refus_se_lit_sur_la_ligne(self):
        ctx = {"bordereaux": {"1234567": "BR-BIAT-1234567"}, "lectures": {"1234567": "2 chèques : 1 trouvé, 1 introuvable"},
               "refus": {"1234567": "à signaler sur le bordereau — Chèque n° 9 : introuvable dans ERPNext."}}
        c = biat.classer_ligne(ligne(REMISE, credit=1500.0), ctx)
        self.assertEqual(c["statut"], "A verifier")
        self.assertIn("pas encaissé : à signaler sur le bordereau — Chèque n° 9", c["raison"])


class TestTableauDeBord(unittest.TestCase):
    """Le cas reel du 16/11/2026, apres l'encaissement du bordereau 7473685."""

    LIGNES = [
        {"date": "2026-11-02", "sens": "Credit", "credit": 500.0, "statut": "Identifie", "ecart": 0.0},
        {"date": "2026-11-12", "sens": "Debit", "debit": 2.678, "statut": "Identifie", "ecart": 0.0},
        {"date": "2026-11-12", "sens": "Credit", "credit": 3010.127, "statut": "Identifie", "ecart": 0.247},
        {"date": "2026-11-16", "sens": "Debit", "debit": 0.893, "statut": "Identifie", "ecart": 0.0},
        {"date": "2026-11-16", "sens": "Debit", "debit": 0.893, "statut": "Identifie", "ecart": 0.0},
        {"date": "2026-11-16", "sens": "Credit", "credit": 581.0, "statut": "Orphelin", "operation": "remise 7473932"},
        {"date": "2026-11-16", "sens": "Credit", "credit": 1523.125, "statut": "Orphelin", "operation": "remise 7473933"}]

    def test_sans_robot_le_solde_est_le_cumul_du_registre(self):
        t = biat.resume_solde(self.LIGNES, 3505.416)
        self.assertEqual((t["solde_banque"], t["source"], t["cumul_registre"]), (5609.788, "registre", 5609.788))
        self.assertEqual((t["erpnext"], t["ecart"]), (3505.416, 2104.372))
        self.assertEqual((t["en_attente"]["nb"], t["en_attente"]["net"], t["en_attente"]["credits"]), (2, 2104.125, 2104.125))
        self.assertEqual((t["petits_ecarts"]["nb"], t["petits_ecarts"]["total"]), (1, 0.247))
        self.assertEqual((t["ecart_projete"], t["hors_registre"], t["inexplique"]), (0.247, 0.0, 0.0))
        self.assertEqual((t["premiere_operation"], t["derniere_operation"]), ("2026-11-02", "2026-11-16"))

    def test_avec_le_solde_lu_par_le_robot(self):
        t = biat.resume_solde(self.LIGNES, 3505.416, {"solde": 5609.788, "date": "2026-11-16"})
        self.assertEqual((t["source"], t["solde_banque"], t["hors_registre"], t["inexplique"]), ("portail", 5609.788, 0.0, 0.0))
        # Le portail en sait plus que le registre : 100 de mouvements pas encore importes.
        t = biat.resume_solde(self.LIGNES, 3505.416, {"solde": 5709.788, "date": "2026-11-17"})
        self.assertEqual((t["ecart"], t["hors_registre"], t["inexplique"]), (2204.372, 100.0, 0.0))

    def test_debit_en_attente_et_ligne_ignoree(self):
        lignes = [{"date": "2026-11-20", "sens": "Debit", "debit": 40.0, "statut": "A verifier"},
                  {"date": "2026-11-20", "sens": "Credit", "credit": 10.0, "statut": "Orphelin", "ignore_manuel": 1},
                  {"date": "2026-11-21", "sens": "Debit", "debit": 5.0, "statut": "Identifie", "ecart": 0.5}]
        t = biat.resume_solde(lignes, -4.5)             # ERPNext : seul le debit identifie, pour 4,5
        self.assertEqual((t["solde_banque"], t["ecart"]), (-35.0, -30.5))
        self.assertEqual((t["en_attente"]["nb"], t["en_attente"]["net"], t["en_attente"]["debits"]), (1, -40.0, 40.0))
        self.assertEqual(t["petits_ecarts"]["total"], -0.5)
        # La ligne ecartee a la main pese sur l'ecart sans etre « en attente » : elle a sa propre case.
        self.assertEqual((t["ecart_projete"], t["ignores"], t["inexplique"]), (9.5, {"nb": 1, "net": 10.0}, 0.0))
        self.assertEqual(biat.resume_solde([], 0.0)["solde_banque"], 0.0)


class TestVirementRecu(unittest.TestCase):
    """Meme logique que Zitouna : le virement solde les dettes du client et se reconnait a sa reference."""

    def test_le_libelle_est_reconnu(self):
        for libelle in ("Virement Recu STE EXEMPLE", "VIR RECU DE AHMED BEN SALAH", "Credit Divers Virt Recu Ste X",
                        "Virement reçu\nSTE EXEMPLE"):
            self.assertEqual(biat.detecter(libelle, "Credit")["regle"], "vir_recu", libelle)
        self.assertEqual(biat.detecter("Virement Recu STE EXEMPLE", "Credit")["categorie"], "encaissement")
        # Ni un virement EMIS, ni le reversement d'Aramex, ni un mot qui contient « VIR ».
        self.assertEqual(biat.detecter("Virement emis FOURNISSEUR", "Debit")["regle"], "")
        self.assertEqual(biat.detecter("Virement Recu ARAMEX TUNISIE", "Credit")["regle"], "")
        self.assertEqual(biat.detecter("Credit Divers ENVIRONNEMENT", "Credit")["regle"], "")
        # Les regles precises gardent la main.
        self.assertEqual(biat.detecter("Versement TND", "Credit")["regle"], "versement_especes")
        self.assertEqual(biat.detecter(REMISE, "Credit")["regle"], "enc_cheque")

    def test_identifie_par_le_paiement_qui_cite_la_reference(self):
        l = ligne("Virement Recu STE EXEMPLE", credit=1500.0)
        l["reference"] = "ft26320abc"
        ctx = {"paiements_par_reference": {"FT26320ABC": [{"name": "PE-9", "montant": 1499.0, "docstatus": 1, "client": "STE EXEMPLE"}]},
               "encaissements_par_reference": {"FT26320ABC": "ENC-17-11-2026-00001"}}
        c = biat.classer_ligne(l, ctx)
        self.assertEqual((c["statut"], c["document_type"], c["document_name"], c["montant_document"], c["ecart"]),
                         ("Identifie", "Encaissement Paiement", "ENC-17-11-2026-00001", 1499.0, 1.0))
        self.assertIn("virement reçu de STE EXEMPLE", c["raison"])
        self.assertIn("écart de 1.000", c["raison"])

    def test_pas_encore_encaisse(self):
        l = ligne("Virement Recu STE EXEMPLE", credit=1500.0)
        l["reference"] = "FT1"
        self.assertEqual(biat.classer_ligne(l, {})["statut"], "Orphelin")
        self.assertIn("pas encore rapproché", biat.classer_ligne(l, {})["raison"])
        refuse = biat.classer_ligne(l, {"refus_virements": {"FT1": "client non identifie : aucun candidat"}})
        self.assertEqual(refuse["statut"], "A verifier")
        self.assertIn("pas encaissé — client non identifie", refuse["raison"])
        brouillon = biat.classer_ligne(l, {"brouillons_par_reference": {"FT1": "ENC-X"}})
        self.assertEqual((brouillon["statut"], brouillon["document_name"]), ("Identifie non comptabilise", "ENC-X"))


class TestRemiseSansBordereau(unittest.TestCase):
    CHEQUES = [{"name": "PE-088", "montant": 581.0}, {"name": "PE-259", "montant": 1523.125},
               {"name": "PE-A", "montant": 3000.0}, {"name": "PE-B", "montant": 3000.0}, {"name": "PE-C", "montant": 798.0}]

    def test_le_montant_designe_le_cheque(self):
        self.assertEqual(biat.proposer_cheques(581.0, self.CHEQUES), ["PE-088"])
        self.assertEqual(biat.proposer_cheques(1523.125, self.CHEQUES), ["PE-259"])

    def test_deux_cheques_au_meme_montant_on_ne_choisit_pas(self):
        self.assertEqual(biat.proposer_cheques(3000.0, self.CHEQUES), [])

    def test_combinaison_unique(self):
        self.assertEqual(sorted(biat.proposer_cheques(2104.125, self.CHEQUES)), ["PE-088", "PE-259"])
        self.assertEqual(sorted(biat.proposer_cheques(2902.125, self.CHEQUES)), ["PE-088", "PE-259", "PE-C"])
        # 3 581 = PE-A + PE-088 ou PE-B + PE-088 : deux combinaisons, pas de proposition.
        self.assertEqual(biat.proposer_cheques(3581.0, self.CHEQUES), [])

    def test_rien_ne_tombe(self):
        self.assertEqual(biat.proposer_cheques(123.456, self.CHEQUES), [])
        self.assertEqual(biat.proposer_cheques(581.0, []), [])


class TestEcartsAbsorbesDansLesFrais(unittest.TestCase):
    """Comme pour Zitouna : les petits ecarts des encaissements rejoignent l'ecriture mensuelle des frais."""

    LIGNES = [
        {"date": "2026-11-12", "sens": "Debit", "debit": 2.678, "categorie": "frais_bancaires", "statut": "Identifie"},
        {"date": "2026-11-16", "sens": "Debit", "debit": 0.893, "categorie": "frais_bancaires", "statut": "Identifie"},
        {"date": "2026-11-16", "sens": "Debit", "debit": 0.893, "categorie": "frais_bancaires", "statut": "Identifie"},
        {"date": "2026-11-12", "sens": "Credit", "credit": 3010.127, "categorie": "encaissement", "statut": "Identifie", "ecart": 0.247},
        {"date": "2026-11-16", "sens": "Credit", "credit": 581.0, "categorie": "encaissement", "statut": "Identifie", "ecart": 0.0}]

    def test_cas_reel_novembre(self):
        c = biat.cumul_frais(self.LIGNES, "2026-11")
        # 4,464 de commissions, moins 0,247 que la banque a credite en plus : 4,217 a crediter au compte BIAT.
        self.assertEqual((c["frais"], c["ecarts"], c["nb_ecarts"], c["total"], c["nb"]), (4.464, 0.247, 1, 4.217, 3))

    def test_sens_et_limites(self):
        f = biat.ecart_a_absorber
        self.assertEqual(f({"statut": "Identifie", "sens": "Credit", "ecart": 0.247}), 0.247)       # la banque a plus
        self.assertEqual(f({"statut": "Identifie", "sens": "Credit", "ecart": -0.8}), -0.8)         # frais retenus
        self.assertEqual(f({"statut": "Identifie", "sens": "Debit", "ecart": 1.0}), -1.0)           # debitee de plus
        self.assertEqual(f({"statut": "Identifie", "sens": "Credit", "ecart": 12.0}), 0.0)          # au-dela du seuil
        self.assertEqual(f({"statut": "A verifier", "sens": "Credit", "ecart": 0.247}), 0.0)        # pas identifiee
        self.assertEqual(f({"statut": "Identifie", "sens": "Credit", "ecart": 0.247, "ignore_manuel": 1}), 0.0)
        self.assertEqual(f({"statut": "Identifie", "sens": "Debit", "ecart": 0.5, "categorie": "frais_bancaires"}), 0.0)

    def test_les_ecarts_peuvent_depasser_les_frais(self):
        lignes = [{"date": "2026-12-03", "sens": "Credit", "credit": 100.9, "statut": "Identifie", "ecart": 0.9}]
        c = biat.cumul_frais(lignes, "2026-12")
        self.assertEqual((c["frais"], c["ecarts"], c["total"], c["jour"]), (0.0, 0.9, -0.9, "2026-12-03"))

    def test_tableau_de_bord_une_fois_l_ecriture_soumise(self):
        lignes = [dict(l) for l in self.LIGNES] + [{"date": "2026-11-02", "sens": "Credit", "credit": 500.0, "statut": "Identifie"},
                                                   {"date": "2026-11-16", "sens": "Credit", "credit": 1523.125, "statut": "Identifie"}]
        # Ecriture de novembre soumise a 4,217 : ERPNext = 500 + 3009,880 + 581 + 1523,125 − 4,217 = 5 609,788.
        t = biat.resume_solde(lignes, 5609.788, mois_comptabilises={"2026-11"})
        self.assertEqual((t["solde_banque"], t["ecart"], t["ecart_projete"], t["inexplique"]), (5609.788, 0.0, 0.0, 0.0))
        self.assertEqual((t["petits_ecarts"]["nb"], t["absorbes"]["nb"], t["absorbes"]["total"]), (0, 1, 0.247))
        # Tant que l'ecriture n'est pas soumise, l'ecart reste a expliquer par ce petit ecart.
        t = biat.resume_solde(lignes, 5609.541)
        self.assertEqual((t["ecart"], t["petits_ecarts"]["total"], t["absorbes"]["nb"], t["inexplique"]), (0.247, 0.247, 0, 0.0))


class TestJournalDuPassage(unittest.TestCase):
    """Une panne du robot BIAT doit se SAVOIR : etape en erreur dans le journal, donc notification."""

    def test_connexion_refusee(self):
        from bank_retenue_sync.tasks import passages

        out = biat.resume_passage({"robot": True, "erreur": "Connexion BIAT refusee : Invalid username or password."})
        self.assertIn("relevé du portail : Connexion BIAT refusee", out["erreur"])
        r = passages.resume({"refresh": "ok", "crees": 2, "revus": 5, "biat": out})
        self.assertEqual(r["statut"], "Partial")
        self.assertEqual(r["erreurs"], ["BIAT (relevé, remises, virements) — relevé du portail : Connexion BIAT refusee : Invalid username or password."])
        etape = [e for e in r["etapes"] if e["cle"] == "biat"][0]
        self.assertFalse(etape["ok"])

    def test_code_sms_et_encaissement_non_soumis(self):
        out = biat.resume_passage({"robot": True, "erreur": "MyBIAT demande un code SMS : ce navigateur n'est pas reconnu.",
                                   "encaissements": {"encaisses": [], "erreurs": [{"encaissement": "ENC-1", "erreur": "soumission refusée"}]}})
        self.assertIn("code SMS", out["erreur"])
        self.assertIn("remises : soumission refusée", out["erreur"])

    def test_passage_sans_histoire(self):
        from bank_retenue_sync.tasks import passages

        out = biat.resume_passage({"robot": True, "crees": 3, "encaissements": {"encaisses": [{"soumis": True}, {"soumis": False}]},
                                   "virements": {"encaisses": [{"soumis": True}]}})
        self.assertNotIn("erreur", out)
        r = passages.resume({"refresh": "ok", "biat": out})
        self.assertEqual(r["statut"], "Success")
        self.assertEqual([e["detail"] for e in r["etapes"] if e["cle"] == "biat"],
                         ["relevé : 3 nouveau(x) ; 1 remise(s) et 1 virement(s) encaissés"])
        sans_robot = passages.resume({"biat": biat.resume_passage({"robot": False})})
        self.assertEqual(sans_robot["etapes"][0]["detail"], "relevé automatique non activé ; 0 remise(s) et 0 virement(s) encaissés")


class TestRepriseDuDev(unittest.TestCase):
    """Les cheques designes sur le dev se retrouvent en production par numero ET montant, jamais a peu pres."""

    def setUp(self):
        from bank_retenue_sync.bank import biat_reprise
        self.R = biat_reprise
        self.portefeuille = [{"name": "PE-1", "numero": "4001767", "montant": 270.0}, {"name": "PE-2", "numero": "4001066", "montant": 1045.0},
                             {"name": "PE-3", "numero": "8965167", "montant": 1694.88}, {"name": "PE-4", "numero": "0000088", "montant": 581.0},
                             {"name": "PE-5", "numero": "1259", "montant": 1523.125}, {"name": "PE-6", "numero": "4001763", "montant": 798.0}]

    def test_les_trois_remises_du_dev(self):
        self.assertEqual(self.R.trouver_cheques(self.R.REMISES["7473685"], self.portefeuille), {"paiements": ["PE-1", "PE-2", "PE-3"], "manquants": []})
        self.assertEqual(self.R.trouver_cheques(self.R.REMISES["7473932"], self.portefeuille)["paiements"], ["PE-4"])
        self.assertEqual(self.R.trouver_cheques(self.R.REMISES["7473933"], self.portefeuille)["paiements"], ["PE-5"])   # zeros de tete

    def test_cheque_absent_ou_montant_different(self):
        sans = [p for p in self.portefeuille if p["name"] != "PE-2"]
        r = self.R.trouver_cheques(self.R.REMISES["7473685"], sans)
        self.assertEqual(r["manquants"], ["chèque n° 4001066 (1045.000) : absent du portefeuille"])
        change = [dict(p, montant=1046.0) if p["name"] == "PE-2" else p for p in self.portefeuille]
        self.assertTrue(self.R.trouver_cheques(self.R.REMISES["7473685"], change)["manquants"])
        double = self.portefeuille + [{"name": "PE-9", "numero": "0000088", "montant": 581.0}]
        self.assertIn("2 paiements possibles", self.R.trouver_cheques(self.R.REMISES["7473932"], double)["manquants"][0])

    def test_les_donnees_de_reprise_tiennent_debout(self):
        # Chaque remise designee a sa ligne bancaire dans les mouvements, et son credit tombe sur ses cheques a 1 DT pres par cheque.
        for numero, designes in self.R.REMISES.items():
            credits = [m["credit"] for m in self.R.MOUVEMENTS
                       if biat.detecter(m["operation"], "Credit")["regle"] == "enc_cheque" and biat.detecter(m["operation"], "Credit")["numero"] == numero]
            self.assertEqual(len(credits), 1, numero)
            self.assertLessEqual(abs(credits[0] - sum(m for _n, m in designes)), biat.ECART_PAR_PIECE * len(designes), numero)
        self.assertEqual(round(sum(m["credit"] - m["debit"] for m in self.R.MOUVEMENTS), 3), 5609.788)       # le solde lu par le robot


class TestActivationDuRobot(unittest.TestCase):
    def test_code_sms(self):
        from bank_retenue_sync.api import biat_robot as R

        self.assertEqual(R.code_propre(" 549 865 "), "549865")
        self.assertEqual(R.code_propre("007421"), "007421")                 # zeros de tete gardes
        for mauvais in ("", "12", "abc", "123456789", None):
            with self.assertRaises(ValueError):
                R.code_propre(mauvais)

    def test_lecture_du_job(self):
        from bank_retenue_sync.api import biat_robot as R

        self.assertEqual(R.lire_job({"status": "running", "progress": {"step": "connexion, attente du code SMS"}})["statut"], "en_cours")
        ok = R.lire_job({"status": "succeeded", "result": {"details": {"code_saisi": True, "comptes": ["x"]}}})
        self.assertEqual((ok["statut"], ok["code_saisi"], ok["comptes"], ok["erreur"]), ("reussi", True, 1, ""))
        ko = R.lire_job({"status": "failed", "error": "Aucun code SMS depose a temps."})
        self.assertEqual((ko["statut"], ko["erreur"]), ("echec", "Aucun code SMS depose a temps."))
        self.assertEqual(R.lire_job(None)["statut"], "en_cours")


if __name__ == "__main__":
    unittest.main()


class TestFraisMensuels(unittest.TestCase):
    """16/11/2026 : les frais BIAT dans une ecriture mensuelle a part, tout en « Frais bancaire »."""

    def lignes(self):
        frais = lambda jour, montant, ref, **plus: {"date": jour, "debit": montant, "reference": ref, "categorie": "frais_bancaires", **plus}
        return [frais("2026-11-12", 0.893, "FT1"), frais("2026-11-16", 0.893, "FT2"), frais("2026-11-16", 0.893, "FT3"),
                frais("2026-11-20", 5.0, "FT4", ignore_manuel=1),                       # ecartee a la main
                frais("2026-12-01", 0.893, "FT5"),                                       # autre mois
                {"date": "2026-11-12", "credit": 3010.127, "debit": 0, "reference": "FT6", "categorie": "encaissement"}]

    def test_cumul_du_mois(self):
        c = biat.cumul_frais(self.lignes(), "2026-11")
        self.assertEqual((c["total"], c["nb"], c["jour"], c["references"]), (2.679, 3, "2026-11-16", ["FT1", "FT2", "FT3"]))
        self.assertEqual(biat.cumul_frais(self.lignes(), "2026-12")["total"], 0.893)
        self.assertEqual(biat.cumul_frais(self.lignes(), "2026-10")["total"], 0.0)

    def test_reference_separee_de_celle_de_zitouna(self):
        from bank_retenue_sync.expenses import fees

        self.assertEqual(biat.cle_frais("2026-11"), "BIAT frais bancaires 11-2026")
        self.assertEqual(biat.periode_de("2026-11-16"), "2026-11")
        # Le classement Zitouna indexe les references qui commencent par « Frais bancaire » : jamais celle de la BIAT.
        self.assertFalse(biat.cle_frais("2026-11").startswith(fees.PREFIXE_MENSUEL))
        self.assertNotEqual(biat.cle_frais("2026-11"), fees.cle_mensuelle("2026-11"))

    def test_la_commission_rejoint_l_ecriture_de_son_mois(self):
        ctx = {"frais_mensuels": {"2026-11": {"name": "JV-F", "docstatus": 1}}}
        c = biat.classer_ligne(ligne(COMMISSION, debit=0.893, date="2026-11-12"), ctx)
        self.assertEqual((c["statut"], c["document_type"], c["document_name"]), ("Identifie", "Journal Entry", "JV-F"))
        self.assertIn("BIAT frais bancaires 11-2026", c["raison"])
        brouillon = biat.classer_ligne(ligne(COMMISSION, debit=0.893, date="2026-11-12"), {"frais_mensuels": {"2026-11": {"name": "JV-F", "docstatus": 0}}})
        self.assertEqual(brouillon["statut"], "Identifie non comptabilise")
        autre_mois = biat.classer_ligne(ligne(COMMISSION, debit=0.893, date="2026-12-02"), ctx)
        self.assertEqual(autre_mois["statut"], "Orphelin")
