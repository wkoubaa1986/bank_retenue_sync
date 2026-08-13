"""Tests du rapprochement des certificats de retenue (tej/rapprochement.py).

Convention de l'app : `unittest.TestCase` pur, contexte construit a la main, aucun acces reseau ni
base. Les montants et libelles viennent des donnees reelles 2026.
"""
from __future__ import annotations

import json
import unittest
from datetime import date

from bank_retenue_sync.tej import rapprochement as R


def cert(name="CERT-1", reference="ref-1", declarant="STE KAIZEN WATER", mf="1536635P",
         ttc=5304.5, retenue=53.045, jour=date(2026, 7, 15), etat="Recue", **kw):
    d = {"name": name, "reference": reference, "declarant": declarant, "declarant_matricule": mf,
         "date_paiement": jour, "etat_depot": etat, "total_brut": ttc, "total_ht": ttc,
         "total_tva": 0.0, "montant_retenue": retenue, "anomalie": 0, "anomalie_raison": None,
         "hors_perimetre": 0, "match_status": "Unmatched", "customer": None}
    d.update(kw)
    return d


def pe(name, montant, jour=date(2026, 7, 20)):
    return {"name": name, "posting_date": jour, "paid_amount": montant}


def ctx(**kw):
    base = {"index_mf": {"1536635P": ["Kaizen Water"]}, "clients": ["Kaizen Water", "SOCOBAT"]}
    base.update(kw)
    return R.Contexte(**base)


class Compteur:
    """Resolveur IA factice qui compte ses appels — l'IA ne doit servir qu'en dernier recours."""

    def __init__(self, reponse=None):
        self.appels, self.reponse = [], reponse

    def __call__(self, operation, candidats):
        self.appels.append(operation)
        return self.reponse


class TestIdentificationDuClient(unittest.TestCase):
    def test_le_matricule_tranche_seul(self):
        r = R.identifier_client(cert(), ctx())
        self.assertEqual((r["customer"], r["method"], r["score"]), ("Kaizen Water", "matricule", 1.0))

    def test_le_matricule_n_appelle_jamais_l_ia(self):
        """L'etage le plus sur est aussi le moins couteux : le sauter serait payer pour rien."""
        ia = Compteur("SOCOBAT")
        r = R.identifier_client(cert(), ctx(), ai_resolver=ia)
        self.assertEqual(r["customer"], "Kaizen Water")
        self.assertEqual(ia.appels, [])

    def test_forme_compacte_et_forme_saisie_se_rejoignent(self):
        """Le portail donne « 1536635P », ERPNext porte « 1536635 / P / A / M / 000 »."""
        index = R.MF.index([("1536635 / P / A / M / 000", "Kaizen Water")])
        r = R.identifier_client(cert(), ctx(index_mf=index))
        self.assertEqual(r["method"], "matricule")

    def test_deux_clients_pour_un_matricule_ne_tranchent_pas_si_les_noms_se_valent(self):
        """Doublon de fiche client : l'ambiguite doit remonter — c'est aussi le signal qu'il faut
        fusionner les deux fiches."""
        c = ctx(index_mf={"1536635P": ["Kaizen Water", "Kaizen Waters"]})
        r = R.identifier_client(cert(), c)
        self.assertIsNone(r["customer"])
        self.assertEqual(sorted(r["candidats"]), ["Kaizen Water", "Kaizen Waters"])
        self.assertIn("partagent le matricule", r["raison"])

    def test_la_lettre_cle_absente_cote_erpnext_ne_perd_plus_le_client(self):
        """Cas reel BUSINESS HOTEL MANAGEMENT TUNIS - BHM : le portail declare « 1398789L », la
        fiche porte « 1398789 / A / M / 000 » — la lettre cle a saute a la saisie et le A de
        categorie a pris sa place. La cle exacte ne trouve rien, et la raison sociale du portail est
        tronquee ET coupee (« BUSINESS HOTEL MANAG EMENT TUNIS BHM TUNI ») : le certificat restait
        non identifie. Le NUMERO, lui, concorde et ne designe qu'une fiche."""
        paires = [("1398789 / A / M / 000", "BUSINESS HOTEL MANAGEMENT TUNIS - BHM"),
                  ("1145823 / A / M / 000", "AMERICAN COOPERATIVE SCHOOL OF TUNIS ACST")]
        c = ctx(index_mf=R.MF.index(paires), index_num=R.MF.index_par_numero(paires),
                clients=[n for _, n in paires])
        r = R.identifier_client(
            cert(mf="1398789L", declarant="BUSINESS HOTEL MANAG EMENT TUNIS BHM TUNI"), c)
        self.assertEqual(r["customer"], "BUSINESS HOTEL MANAGEMENT TUNIS - BHM")
        self.assertEqual(r["method"], "matricule_numero")
        # Le nom, compare a ce SEUL candidat, concorde (0,92) : deux preuves independantes, donc
        # pas de drapeau « a verifier » — il ne se leve que quand il apprend quelque chose.
        self.assertFalse(r["revue"])
        self.assertIn("raison sociale concordante", r["raison"])
        self.assertIn("tax_id a corriger", r["raison"])

    def test_le_numero_seul_ne_tranche_pas_quand_deux_fiches_le_portent(self):
        """Des chiffres identiques peuvent venir d'une saisie de travers : ce n'est un vivier, pas
        une cle. Deux fiches -> on restreint les candidats, on ne decide pas a leur place."""
        paires = [("1536635 / A / M / 000", "Kaizen Water"),
                  ("1536635 / B / M / 000", "Kaizen Waters")]
        c = ctx(index_mf={}, index_num=R.MF.index_par_numero(paires),
                clients=["Kaizen Water", "Kaizen Waters", "SOCOBAT"])
        r = R.identifier_client(cert(declarant="ETS SANS RAPPORT XYZ"), c)
        self.assertIsNone(r["customer"])
        self.assertIn("partagent le numero", r["raison"])
        self.assertEqual(sorted(r["candidats"]), ["Kaizen Water", "Kaizen Waters"])

    def test_la_cle_exacte_prime_toujours_sur_le_numero(self):
        """Un matricule complet des deux cotes ne doit jamais passer par l'etage affaibli."""
        paires = [("1536635 / P / A / M / 000", "Kaizen Water"),
                  ("1536635 / A / M / 000", "Kaizen Waters")]
        c = ctx(index_mf=R.MF.index(paires), index_num=R.MF.index_par_numero(paires),
                clients=[n for _, n in paires])
        r = R.identifier_client(cert(), c)
        self.assertEqual((r["customer"], r["method"]), ("Kaizen Water", "matricule"))

    def test_un_numero_seul_sans_appui_du_nom_est_marque_pour_revue(self):
        """Une seule preuve, et une preuve dont la lettre cle n'a pas pu etre verifiee : elle
        rapproche, mais elle se relit."""
        paires = [("1398789 / A / M / 000", "Kaizen Water")]
        c = ctx(index_mf={}, index_num=R.MF.index_par_numero(paires), clients=["Kaizen Water"],
                pes_ras={}, factures={})
        d = R.rapprocher_un(cert(mf="1398789L", declarant="INCONNU AU BATAILLON"), c)
        self.assertEqual(d["customer"], "Kaizen Water")
        self.assertEqual(d["revue_requise"], 1)

    def test_la_raison_sociale_departage_deux_homonymes(self):
        c = ctx(index_mf={"1536635P": ["Kaizen Water", "Boulangerie Nord"]})
        r = R.identifier_client(cert(), c)
        self.assertEqual(r["customer"], "Kaizen Water")

    def test_sans_matricule_le_fuzzy_prend_le_relais(self):
        r = R.identifier_client(cert(mf=""), ctx())
        self.assertEqual((r["customer"], r["method"]), ("Kaizen Water", "fuzzy"))

    def test_un_alias_appris_evite_le_fuzzy_et_l_ia(self):
        """Une correction humaine d'hier doit rapprocher tout seul le certificat d'aujourd'hui."""
        ia = Compteur("SOCOBAT")
        c = ctx(index_mf={}, clients=["Regie Nationale des Tabacs", "SOCOBAT"],
                alias={R.party.normalize("REGIE NATIONALE DES TABACS ET ALLUMETTES"):
                       "Regie Nationale des Tabacs"})
        r = R.identifier_client(cert(declarant="REGIE NATIONALE DES TABACS ET ALLUMETTES", mf=""),
                                c, ai_resolver=ia)
        self.assertEqual((r["customer"], r["method"]), ("Regie Nationale des Tabacs", "alias"))
        self.assertEqual(ia.appels, [])

    def test_l_ia_n_est_appelee_que_lorsque_rien_d_autre_ne_tranche(self):
        ia = Compteur("SOCOBAT")
        c = ctx(index_mf={}, clients=["SOCOBAT", "Kaizen Water"])
        r = R.identifier_client(cert(declarant="ETS NON REFERENCE XYZ", mf=""), c, ai_resolver=ia)
        self.assertEqual(len(ia.appels), 1)
        self.assertEqual((r["customer"], r["method"]), ("SOCOBAT", "ai"))

    def test_sans_ia_le_non_tranche_porte_une_raison(self):
        r = R.identifier_client(cert(declarant="ETS NON REFERENCE XYZ", mf=""), ctx(index_mf={}))
        self.assertIsNone(r["customer"])
        self.assertTrue(r["raison"])


class TestAlias(unittest.TestCase):
    def test_un_declarant_renvoyant_vers_deux_clients_n_apprend_rien(self):
        alias = R.alias_appris([{"declarant": "STE TAURUS", "customer": "Taurus"},
                                {"declarant": "STE TAURUS", "customer": "Sté Taurus"}])
        self.assertEqual(alias, {})

    def test_un_declarant_constant_devient_un_alias(self):
        alias = R.alias_appris([{"declarant": "STE TAURUS", "customer": "Taurus"},
                                {"declarant": "STE  TAURUS", "customer": "Taurus"}])
        self.assertEqual(list(alias.values()), ["Taurus"])


class TestAppariementDeLaPiece(unittest.TestCase):
    def test_montant_exact_et_ecriture_unique(self):
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.045)]})
        r = R.apparier_piece(cert(customer="Kaizen Water"), c)
        self.assertEqual((r["payment_entry"], r["mode"], r["ecart"]),
                         ("ACC-PAY-1", "montant_exact", 0.0))

    def test_deux_ecritures_au_meme_montant_ne_tranchent_pas(self):
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.045), pe("ACC-PAY-2", 53.045)]})
        r = R.apparier_piece(cert(customer="Kaizen Water"), c)
        self.assertEqual(r["mode"], "ambigu")
        self.assertEqual(sorted(r["candidats"]), ["ACC-PAY-1", "ACC-PAY-2"])

    def test_ecart_du_timbre_apparie_et_rend_l_ecart(self):
        """53,045 declares contre 53,055 saisis : 1 % du timbre de 1 DT. On apparie, on montre
        l'ecart, on ne corrige pas."""
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.055)]})
        r = R.apparier_piece(cert(customer="Kaizen Water"), c)
        self.assertEqual((r["payment_entry"], r["mode"]), ("ACC-PAY-1", "montant_approchant"))
        self.assertEqual(r["ecart"], 0.01)
        self.assertIn("timbre", r["raison"])

    def test_l_ecart_du_timbre_prime_sur_un_ecart_inexplique(self):
        """Cas reel STE TAURUS : quatre retenues declarees a 53,760, quatre ecritures a 53,770 (le
        timbre) — et une cinquieme a 54,760. `tolerance()` plancher a 1 DT les admet TOUTES, ce qui
        rendait « ambigus » les quatre certificats alors qu'une seule ecriture tombe au millime.
        """
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.055), pe("ACC-PAY-2", 54.045)]})
        r = R.apparier_piece(cert(customer="Kaizen Water"), c)
        self.assertEqual((r["payment_entry"], r["mode"]), ("ACC-PAY-1", "montant_approchant"))

    def test_l_ecart_du_timbre_dans_la_fenetre_bat_un_montant_exact_a_sept_mois(self):
        """⚠️ Cas reel SPH Khamsa. Deux ecritures de 24,400 PILE (septembre et octobre 2025)
        rendaient le certificat « ambigu » et l'analyse s'arretait la — sans jamais regarder celle
        de 24,410 du 13/04/2026, trois jours apres la declaration au portail, et dont l'ecart EST le
        timbre fiscal. Un ecart qui s'explique dans la fenetre vaut mieux qu'un montant exact hors
        d'elle : le second n'est qu'une repetition de montant chez un client qui facture toujours la
        meme prestation."""
        c = ctx(pes_ras={"Kaizen Water": [pe("VIEILLE-A", 24.4, date(2025, 9, 17)),
                                          pe("VIEILLE-B", 24.4, date(2025, 10, 14)),
                                          pe("BONNE", 24.41, date(2026, 4, 13))]})
        r = R.apparier_piece(cert(customer="Kaizen Water", ttc=2440.0, retenue=24.4,
                                  jour=date(2026, 4, 10)), c)
        self.assertEqual(r["payment_entry"], "BONNE")
        self.assertEqual(r["mode"], "montant_approchant")
        self.assertIn("timbre fiscal", r["raison"])

    def test_le_montant_exact_dans_la_fenetre_prime_toujours(self):
        """Le nouvel etage ne passe jamais devant une correspondance exacte a la bonne date."""
        c = ctx(pes_ras={"Kaizen Water": [pe("EXACTE", 24.4, date(2026, 4, 12)),
                                          pe("TIMBRE", 24.41, date(2026, 4, 13))]})
        r = R.apparier_piece(cert(customer="Kaizen Water", ttc=2440.0, retenue=24.4,
                                  jour=date(2026, 4, 10)), c)
        self.assertEqual((r["payment_entry"], r["mode"]), ("EXACTE", "montant_exact"))

    def test_deux_ecarts_aussi_serres_restent_ambigus(self):
        """Le depart ne se fait que si UNE SEULE ecriture tombe au millime."""
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.055), pe("ACC-PAY-2", 53.035)]})
        self.assertEqual(R.apparier_piece(cert(customer="Kaizen Water"), c)["mode"], "ambigu")

    def test_montant_exact_hors_fenetre_est_retenu_mais_signale(self):
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.045, jour=date(2026, 2, 1))]})
        r = R.apparier_piece(cert(customer="Kaizen Water"), c)
        self.assertEqual(r["mode"], "montant_exact_hors_fenetre")

    def test_une_ecriture_prise_par_un_autre_certificat_est_ecartee(self):
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.045)]},
                prises={"ACC-PAY-1": "CERT-AUTRE"})
        r = R.apparier_piece(cert(customer="Kaizen Water"), c)
        self.assertIsNone(r["payment_entry"])

    def test_sa_propre_ecriture_reste_disponible_au_passage_suivant(self):
        """Sans cette regle, le second passage declarait « sans piece » tout ce que le premier
        avait rapproche."""
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.045)]},
                prises={"ACC-PAY-1": "CERT-1"})
        r = R.apparier_piece(cert(name="CERT-1", customer="Kaizen Water"), c)
        self.assertEqual(r["payment_entry"], "ACC-PAY-1")

    def test_aucune_ecriture_du_client(self):
        r = R.apparier_piece(cert(customer="Kaizen Water"), ctx())
        self.assertEqual(r["mode"], "aucune")

    def test_une_ecriture_au_montant_tres_different_n_est_pas_une_preuve(self):
        """Cas reel ABC MED : un certificat de 110,260 DT s'etait approprie l'ecriture de
        15,840 DT au seul motif qu'elle etait la seule du client dans la fenetre — et la volait
        ainsi au certificat de 15,837 DT auquel elle appartenait."""
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 15.84)]})
        r = R.apparier_piece(cert(customer="Kaizen Water", retenue=110.26), c)
        self.assertIsNone(r["payment_entry"])
        self.assertEqual(r["mode"], "ecart_important")
        self.assertEqual(r["candidats"], ["ACC-PAY-1"])

    def test_l_ecart_du_timbre_est_reconnu_a_l_arrondi_pres(self):
        self.assertTrue(R.ecart_timbre(0.010))
        self.assertTrue(R.ecart_timbre(0.011))
        self.assertTrue(R.ecart_timbre(-0.011))
        self.assertFalse(R.ecart_timbre(-0.73))


class TestAppariementParLaFacture(unittest.TestCase):
    """L'ETAGE QUI EXISTE PARCE QUE LA DATE MENT.

    Cas reel CLIMA FILTRI PRO : ecriture de retenue du 25/02 imputee a la facture, certificat
    declare au portail le 03/06 — 98 jours plus tard, trois fois la fenetre. Le certificat sortait
    « sans ecriture » et l'outil proposait alors d'en creer une SECONDE pour la meme retenue.
    """

    def impute(self, montant=53.045, facture="ACC-SINV-1", jour=date(2026, 2, 25), **kw):
        return ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", montant, jour=jour)]},
                   refs={"ACC-PAY-1": [{"doctype": "Sales Invoice", "name": facture,
                                        "montant": montant}]}, **kw)

    def test_l_imputation_apparie_meme_a_98_jours(self):
        r = R.apparier_par_facture(cert(customer="Kaizen Water", sales_invoice="ACC-SINV-1"),
                                   self.impute())
        self.assertEqual((r["payment_entry"], r["mode"]), ("ACC-PAY-1", "impute_a_la_facture"))
        self.assertEqual(r["ecart"], 0.0)

    def test_l_ecart_du_timbre_est_nomme(self):
        r = R.apparier_par_facture(cert(customer="Kaizen Water", sales_invoice="ACC-SINV-1"),
                                   self.impute(montant=53.055))
        self.assertEqual(r["ecart"], 0.01)
        self.assertIn("timbre", r["raison"])

    def test_une_ecriture_imputee_ailleurs_ne_compte_pas(self):
        r = R.apparier_par_facture(cert(customer="Kaizen Water", sales_invoice="ACC-SINV-1"),
                                   self.impute(facture="ACC-SINV-AUTRE"))
        self.assertIsNone(r["payment_entry"])

    def test_un_montant_sans_rapport_n_est_pas_notre_retenue(self):
        """Meme imputee a la bonne facture : ce peut etre une seconde retenue du meme client."""
        r = R.apparier_par_facture(cert(customer="Kaizen Water", sales_invoice="ACC-SINV-1"),
                                   self.impute(montant=980.0))
        self.assertIsNone(r["payment_entry"])
        self.assertEqual(r["mode"], "ecart_important")

    def test_une_ecriture_prise_par_un_autre_certificat_est_ecartee(self):
        r = R.apparier_par_facture(cert(customer="Kaizen Water", sales_invoice="ACC-SINV-1"),
                                   self.impute(prises={"ACC-PAY-1": "CERT-AUTRE"}))
        self.assertIsNone(r["payment_entry"])

    def test_la_decision_le_branche_apres_la_recherche_de_facture(self):
        """Le chemin complet : pas d'ecriture dans la fenetre -> facture trouvee par l'assiette ->
        l'ecriture retrouvee par l'imputation. C'est ce chemin-la qui manquait."""
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.045, jour=date(2026, 2, 25))]},
                refs={"ACC-PAY-1": [{"doctype": "Sales Invoice", "name": "ACC-SINV-3",
                                     "montant": 53.045}]},
                factures={"Kaizen Water": [{"name": "ACC-SINV-3", "posting_date": date(2026, 2, 13),
                                            "grand_total": 5304.5, "outstanding": 0.0}]})
        d = R.rapprocher_un(cert(), c)
        self.assertEqual(d["match_status"], "Auto Matched")
        self.assertEqual((d["payment_entry"], d["sales_invoice"]), ("ACC-PAY-1", "ACC-SINV-3"))

    def test_jamais_au_premier_passage(self):
        """Les appariements au millime servent d'abord : sinon un certificat passant par la facture
        prendrait l'ecriture d'un certificat qui, lui, tombe juste."""
        # Montant approchant ET hors fenetre : `apparier_piece` ne rend rien du tout, seul
        # l'etage par la facture peut trancher — et il doit attendre le second passage.
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.055, jour=date(2026, 2, 25))]},
                refs={"ACC-PAY-1": [{"doctype": "Sales Invoice", "name": "ACC-SINV-3",
                                     "montant": 53.055}]},
                factures={"Kaizen Water": [{"name": "ACC-SINV-3", "posting_date": date(2026, 2, 13),
                                            "grand_total": 5304.5, "outstanding": 0.0}]})
        self.assertEqual(R.rapprocher_un(cert(), c, modes=R.MODES_FORTS)["match_status"],
                         "Sans piece")
        self.assertEqual(R.rapprocher_un(cert(), c)["payment_entry"], "ACC-PAY-1")


class TestDepartageParLEcriture(unittest.TestCase):
    """Quand le nom ne tranche pas, la comptabilite tranche.

    Cas reels : « STE TAURUS » et « Sté TAURUS - 1 » partagent le matricule 1298092B (doublon de
    fiche), « Technolab » et « Sté TEC » sont aussi proches l'un que l'autre du libelle declare.
    Une seule des deux fiches porte l'ecriture de retenue du bon montant a la bonne date.
    """

    def deux_fiches(self, porteur="Kaizen Water", montant=53.055, jour=date(2026, 7, 20)):
        return ctx(index_mf={"1536635P": ["Kaizen Water", "Kaizen Waters"]},
                   pes_ras={porteur: [pe("ACC-PAY-1", montant, jour=jour)]})

    def test_le_porteur_de_l_ecriture_est_le_client(self):
        r = R.identifier_client(cert(), self.deux_fiches())
        self.assertEqual((r["customer"], r["method"]), ("Kaizen Water", "ecriture"))
        self.assertIn("ACC-PAY-1", r["raison"])

    def test_deux_porteurs_ne_tranchent_pas(self):
        c = ctx(index_mf={"1536635P": ["Kaizen Water", "Kaizen Waters"]},
                pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.045)],
                         "Kaizen Waters": [pe("ACC-PAY-2", 53.045)]})
        r = R.identifier_client(cert(), c)
        self.assertIsNone(r["customer"])
        self.assertIn("portent une ecriture", r["raison"])

    def test_la_marge_est_celle_du_millime_pas_celle_de_l_appariement(self):
        """⚠️ `tolerance()` plancherait a 1 DT : sur une retenue de 3,710 DT elle accepterait tout
        entre 2,71 et 4,71 — assez large pour nommer le mauvais client. Quand le montant doit
        NOMMER un tiers, il doit coller au millime, timbre compris."""
        self.assertIsNone(R.identifier_client(cert(retenue=3.710),
                                              self.deux_fiches(montant=4.400))["customer"])
        self.assertEqual(R.identifier_client(cert(retenue=3.710),
                                            self.deux_fiches(montant=3.720))["customer"],
                         "Kaizen Water")

    def test_une_ecriture_hors_fenetre_ne_nomme_personne(self):
        r = R.identifier_client(cert(), self.deux_fiches(jour=date(2026, 1, 5)))
        self.assertIsNone(r["customer"])

    def test_le_client_designe_par_l_ecriture_demande_une_revue(self):
        """La comptabilite a raison quand elle tranche, mais elle tranche par un montant : le
        certificat sort rapproche ET signale."""
        c = ctx(index_mf={"1536635P": ["Kaizen Water", "Kaizen Waters"]},
                pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.045)]},
                refs={"ACC-PAY-1": [{"doctype": "Sales Invoice", "name": "ACC-SINV-1",
                                     "montant": 53.045}]})
        d = R.rapprocher_un(cert(), c)
        self.assertEqual((d["match_status"], d["customer"]), ("Auto Matched", "Kaizen Water"))
        self.assertEqual(d["revue_requise"], 1)


class TestFacture(unittest.TestCase):
    def test_la_commande_a_zero_ne_masque_pas_la_facture(self):
        """Toutes les ecritures generees depuis une commande gardent une ligne « Sales Order » a
        0,000 : la prendre pour la piece rattacherait le certificat a une commande deja facturee."""
        c = ctx(refs={"ACC-PAY-1": [{"doctype": "Sales Order", "name": "SAL-ORD-1", "montant": 0.0},
                                    {"doctype": "Sales Invoice", "name": "ACC-SINV-1",
                                     "montant": 53.045}]})
        r = R.facture_de_piece("ACC-PAY-1", c)
        self.assertEqual(r["sales_invoice"], "ACC-SINV-1")
        self.assertIsNone(r["sales_order"])

    def test_une_ecriture_sur_commande_seule_donne_la_commande(self):
        c = ctx(refs={"ACC-PAY-1": [{"doctype": "Sales Order", "name": "SAL-ORD-1",
                                     "montant": 53.045}]})
        r = R.facture_de_piece("ACC-PAY-1", c)
        self.assertEqual(r["sales_order"], "SAL-ORD-1")

    def test_le_reglement_net_donne_la_facture(self):
        """Le client qui retient 1 % paie TTC - retenue (+ 1 DT de timbre). Ce reglement existe en
        banque et porte deja sa facture."""
        c = ctx(encaissements={"Kaizen Water": [pe("ACC-PAY-NET", 5252.455)]},
                refs={"ACC-PAY-NET": [{"doctype": "Sales Invoice", "name": "ACC-SINV-9",
                                       "montant": 5252.455}]})
        r = R.facture_par_reglement_net(cert(customer="Kaizen Water"), c)
        self.assertEqual(r["sales_invoice"], "ACC-SINV-9")

    def test_le_timbre_ne_fait_pas_rater_le_reglement_net(self):
        """Reglement reel = net + 1 DT de timbre : sans marge, aucun cas ne passerait."""
        c = ctx(encaissements={"Kaizen Water": [pe("ACC-PAY-NET", 5252.455 + 1.0)]},
                refs={"ACC-PAY-NET": [{"doctype": "Sales Invoice", "name": "ACC-SINV-9",
                                       "montant": 5253.455}]})
        r = R.facture_par_reglement_net(cert(customer="Kaizen Water"), c)
        self.assertEqual(r["sales_invoice"], "ACC-SINV-9")

    def test_un_reglement_net_portant_plusieurs_factures_reste_une_piste(self):
        """Cas reel BHM : le client solde DEUX factures d'un seul versement net de 367,29
        (371,00 - 3,71), dont une partiellement — 271,29 sur une facture de 466 et 96,00 sur une
        autre. Aucune facture ne vaut donc l'assiette, et la regle de l'assiette ne peut rien
        trouver. Ce reglement-la, lui, NOMME les creances : l'information doit remonter au lieu
        d'etre jetee."""
        c = ctx(encaissements={"Kaizen Water": [pe("ACC-PAY-NET", 367.29)]},
                refs={"ACC-PAY-NET": [{"doctype": "Sales Invoice", "name": "SINV-A",
                                       "montant": 271.29},
                                      {"doctype": "Sales Invoice", "name": "SINV-B",
                                       "montant": 96.0}]})
        r = R.facture_par_reglement_net(cert(customer="Kaizen Water", ttc=371.0, retenue=3.71), c)
        self.assertIsNone(r["sales_invoice"])
        self.assertEqual(r["factures_du_reglement"], ["SINV-A", "SINV-B"])
        self.assertIn("porte 2 factures", r["raison"])

    def test_la_piste_du_reglement_net_survit_a_la_regle_de_l_assiette(self):
        """Sans cette reprise, `rapprocher_un` remplacait la piste par « aucune facture au TTC de
        371 » — un cul-de-sac, alors que la reponse etait a un pas."""
        c = ctx(encaissements={"Kaizen Water": [pe("ACC-PAY-NET", 367.29)]},
                refs={"ACC-PAY-NET": [{"doctype": "Sales Invoice", "name": "SINV-A",
                                       "montant": 271.29},
                                      {"doctype": "Sales Invoice", "name": "SINV-B",
                                       "montant": 96.0}]})
        d = R.rapprocher_un(cert(customer="Kaizen Water", ttc=371.0, retenue=3.71), c)
        self.assertEqual(d["match_status"], "Sans piece")
        self.assertEqual(json.loads(d["match_candidates"]), ["SINV-A", "SINV-B"])
        self.assertIn("porte 2 factures", d["match_raison"])

    def test_deux_reglements_nets_possibles_ne_tranchent_pas(self):
        c = ctx(encaissements={"Kaizen Water": [pe("A", 5252.455), pe("B", 5252.455)]})
        self.assertIsNone(R.facture_par_reglement_net(cert(customer="Kaizen Water"),
                                                      c)["sales_invoice"])

    def test_facture_au_ttc_egal_a_l_assiette(self):
        c = ctx(factures={"Kaizen Water": [{"name": "ACC-SINV-3", "posting_date": date(2026, 7, 1),
                                            "grand_total": 5304.5, "outstanding": 53.045}]})
        r = R.facture_par_assiette(cert(customer="Kaizen Water"), c)
        self.assertEqual(r["sales_invoice"], "ACC-SINV-3")

    def test_facture_deja_prise_par_un_autre_certificat_ecartee(self):
        c = ctx(factures={"Kaizen Water": [{"name": "ACC-SINV-3", "posting_date": date(2026, 7, 1),
                                            "grand_total": 5304.5, "outstanding": 0.0}]},
                factures_prises={"ACC-SINV-3": "CERT-AUTRE"})
        self.assertIsNone(R.facture_par_assiette(cert(customer="Kaizen Water"),
                                                 c)["sales_invoice"])


class TestDecision(unittest.TestCase):
    """Aucun silence : chaque certificat ressort avec un statut ET une raison."""

    def test_certificat_annule_jamais_apparie(self):
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.045)]})
        d = R.rapprocher_un(cert(etat="Annule"), c)
        self.assertEqual(d["match_status"], "Ignore")
        self.assertIsNone(d.get("payment_entry"))
        self.assertIn("annule", d["match_raison"])

    def test_hors_perimetre_jamais_apparie(self):
        d = R.rapprocher_un(cert(hors_perimetre=1), ctx())
        self.assertEqual(d["match_status"], "Ignore")
        self.assertIn("perimetre", d["match_raison"])

    def test_anomalie_jamais_apparie(self):
        d = R.rapprocher_un(cert(anomalie=1, anomalie_raison="assiette nulle"), ctx())
        self.assertEqual(d["match_status"], "Ignore")
        self.assertIn("assiette nulle", d["match_raison"])

    def test_cas_nominal_client_piece_et_facture(self):
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.045)]},
                refs={"ACC-PAY-1": [{"doctype": "Sales Invoice", "name": "ACC-SINV-1",
                                     "montant": 53.045}]})
        d = R.rapprocher_un(cert(), c)
        self.assertEqual(d["match_status"], "Auto Matched")
        self.assertEqual((d["customer"], d["payment_entry"], d["sales_invoice"]),
                         ("Kaizen Water", "ACC-PAY-1", "ACC-SINV-1"))

    def test_un_ecart_demande_une_revue(self):
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.055)]})
        d = R.rapprocher_un(cert(), c)
        self.assertEqual(d["ecart_piece"], 0.01)
        self.assertEqual(d["revue_requise"], 1)

    def test_sans_ecriture_la_facture_est_quand_meme_cherchee(self):
        """C'est elle qui rendra la creation du paiement possible et sure."""
        c = ctx(encaissements={"Kaizen Water": [pe("ACC-PAY-NET", 5252.455)]},
                refs={"ACC-PAY-NET": [{"doctype": "Sales Invoice", "name": "ACC-SINV-9",
                                       "montant": 5252.455}]})
        d = R.rapprocher_un(cert(), c)
        self.assertEqual(d["match_status"], "Sans piece")
        self.assertEqual(d["sales_invoice"], "ACC-SINV-9")

    def test_client_non_identifie_ne_cherche_aucune_piece(self):
        d = R.rapprocher_un(cert(declarant="ETS INCONNU", mf=""), ctx(index_mf={}))
        self.assertEqual(d["match_status"], "Unmatched")
        self.assertIsNone(d.get("payment_entry"))

    def test_un_appariement_faible_est_reporte_au_second_passage(self):
        """Le premier passage ne sert que les preuves exactes : il empeche un montant approchant
        de prendre une ecriture qu'un certificat au millime revendique."""
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.055)]})
        d = R.rapprocher_un(cert(), c, modes=R.MODES_FORTS)
        self.assertEqual(d["match_status"], "Reporte")
        self.assertIsNone(d["payment_entry"])

    def test_le_meme_certificat_est_apparie_au_second_passage(self):
        c = ctx(pes_ras={"Kaizen Water": [pe("ACC-PAY-1", 53.055)]})
        d = R.rapprocher_un(cert(), c)
        self.assertEqual((d["match_status"], d["payment_entry"]), ("Auto Matched", "ACC-PAY-1"))

    def test_matricule_ambigu_conserve_les_candidats(self):
        c = ctx(index_mf={"1536635P": ["Kaizen Water", "Kaizen Waters"]})
        d = R.rapprocher_un(cert(), c)
        self.assertEqual(d["match_status"], "Ambiguous")
        self.assertEqual(sorted(json.loads(d["match_candidates"])),
                         ["Kaizen Water", "Kaizen Waters"])


if __name__ == "__main__":
    unittest.main()
