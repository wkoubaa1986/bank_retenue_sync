"""Tests du rapprochement SYMETRIQUE (bank/ecarts.py) et de l'appariement de dernier recours.

Meme convention que les tests existants : `unittest.TestCase` pur, entrees injectees, aucun acces
reseau ni base.
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.bank import classify as C, ecarts as E


def piece(voucher_no, jour, montant, sens="Debit", texte="", vtype="Journal Entry"):
    return {"voucher_type": vtype, "voucher_no": voucher_no, "posting_date": date(2026, 5, jour),
            "montant": montant, "sens": sens, "texte": texte,
            "entree": montant if sens == "Credit" else 0.0,
            "sortie": montant if sens == "Debit" else 0.0}


def mvt(cle, jour, montant, sens="Debit", reference="FT26131LZ2FB", operation="REGLEMENT CB"):
    return {"cle": cle, "date": date(2026, 5, jour), "montant": montant, "sens": sens,
            "reference": reference, "operation": operation,
            "debit": montant if sens == "Debit" else 0.0,
            "credit": montant if sens == "Credit" else 0.0}


class TestClesEtCitations(unittest.TestCase):
    def test_cle_reference_et_numero(self):
        """La reference bancaire ET le n° de cheque du libelle sont deux cles valides."""
        m = {"date": date(2026, 5, 13), "operation": "REGLEMENT CHEQUE 4000968 LE CHAUFFAGE",
             "reference": "FT26133C7GMQ", "debit": 492.963, "credit": 0.0}
        cles = E.cles_du_mouvement(m)
        self.assertIn("FT26133C7GMQ", cles)
        self.assertIn("4000968", cles)

    def test_cle_trop_courte_ecartee(self):
        """Une cle de moins de 6 caracteres se retrouverait par hasard dans n'importe quel texte."""
        m = {"date": date(2026, 5, 4), "operation": "FRAIS", "reference": "FT12",
             "debit": 1.0, "credit": 0.0}
        self.assertEqual(E.cles_du_mouvement(m, rule=None), set())

    def test_piece_citant_une_reference(self):
        p = piece("ACC-JV-1", 4, 33.0, texte="Paiement Orange ref FT261249MVZ0 du 04/05")
        self.assertEqual(E.piece_cite(p, {"FT261249MVZ0"}), {"FT261249MVZ0"})

    def test_numero_fautif_non_rattrape(self):
        """« 400966 » n'est PAS le cheque 4000966 : l'egalite reste stricte, l'ecart doit ressortir."""
        p = piece("ACC-PAY-1", 11, 492.963, texte="400966-Bq Zitouna")
        self.assertEqual(E.piece_cite(p, {"4000966"}), set())

    def test_piece_sans_texte(self):
        self.assertEqual(E.piece_cite(piece("ACC-JV-2", 4, 10.0), {"FT261249MVZ0"}), set())


class TestApparierParMontant(unittest.TestCase):
    def test_paire_unique_des_deux_cotes(self):
        paires = E.apparier_par_montant([mvt("m1", 4, 33.0)], [piece("ACC-JV-1", 4, 33.0)])
        self.assertEqual(paires["m1"]["voucher_no"], "ACC-JV-1")

    def test_deux_mouvements_du_meme_montant_ne_sont_jamais_tranches(self):
        """Cas reel : deux transferts d'especes de 20 000 DT le meme jour."""
        paires = E.apparier_par_montant(
            [mvt("m1", 6, 20000.0, sens="Credit"), mvt("m2", 6, 20000.0, sens="Credit")],
            [piece("ACC-JV-1", 6, 20000.0, sens="Credit")])
        self.assertEqual(paires, {})

    def test_deux_pieces_du_meme_montant_ne_sont_jamais_tranchees(self):
        paires = E.apparier_par_montant(
            [mvt("m1", 6, 20000.0)],
            [piece("ACC-JV-1", 6, 20000.0), piece("ACC-JV-2", 6, 20000.0)])
        self.assertEqual(paires, {})

    def test_sens_oppose_jamais_apparie(self):
        """Un debit du releve ne peut pas correspondre a une entree d'argent."""
        paires = E.apparier_par_montant([mvt("m1", 4, 33.0, sens="Debit")],
                                        [piece("ACC-JV-1", 4, 33.0, sens="Credit")])
        self.assertEqual(paires, {})

    def test_hors_fenetre_de_dates(self):
        paires = E.apparier_par_montant([mvt("m1", 4, 33.0)], [piece("ACC-JV-1", 20, 33.0)])
        self.assertEqual(paires, {})

    def test_montant_different_jamais_apparie(self):
        """Par defaut l'appariement reste EXACT : un ecart releve d'un autre verdict."""
        paires = E.apparier_par_montant([mvt("m1", 20, 703.5)], [piece("ACC-JV-1", 18, 703.0)])
        self.assertEqual(paires, {})

    def test_marge_toleree_rattrape_une_saisie_fausse(self):
        """Cas reel : recharge Total prelevee 703,500 et comptabilisee 703,000."""
        paires = E.apparier_par_montant([mvt("m1", 20, 703.5)], [piece("ACC-JV-1", 18, 703.0)],
                                        marge=None)
        self.assertEqual(paires["m1"]["voucher_no"], "ACC-JV-1")

    def test_marge_toleree_bornee_par_la_tolerance(self):
        """Au-dela de la tolerance, aucun rattrapage : ce serait deux operations differentes."""
        paires = E.apparier_par_montant([mvt("m1", 20, 703.5)], [piece("ACC-JV-1", 18, 600.0)],
                                        marge=None)
        self.assertEqual(paires, {})


class TestTolerance(unittest.TestCase):
    def test_plancher_et_plafond(self):
        self.assertEqual(E.tolerance(10.0), 1.0)          # plancher
        self.assertEqual(E.tolerance(400.0), 2.0)         # 0,5 %
        self.assertEqual(E.tolerance(100000.0), 10.0)     # plafond


class TestApparierRestants(unittest.TestCase):
    """La passe de dernier recours branchee dans `classify`."""

    def _classification(self, cle, montant, jour=4, statut=C.STATUT_A_VERIFIER,
                        reference="FT261249MVZ0", groupe=None, document_name=None):
        return C.Classification(cle=cle, date=date(2026, 5, jour), operation="REGLEMENT CB",
                                reference=reference, debit=montant, credit=0.0, statut=statut,
                                groupe=groupe, document_name=document_name)

    def test_apparie_et_pose_le_document(self):
        cls = [self._classification("m1", 33.0)]
        ctx = C.LinkContext(pieces=[piece("ACC-JV-1", 4, 33.0)])
        self.assertEqual(C.apparier_restants(cls, ctx), 1)
        self.assertEqual(cls[0].statut, C.STATUT_IDENTIFIE)
        self.assertEqual(cls[0].document_name, "ACC-JV-1")
        self.assertIn("apparie par le montant", cls[0].raison)

    def test_montant_approchant_rattache_mais_reste_a_verifier(self):
        """Le lien est probable, le montant ne l'est pas : la piece est posee, le statut alerte."""
        cls = [self._classification("m1", 703.5, jour=20)]
        ctx = C.LinkContext(pieces=[piece("ACC-JV-511", 18, 703.0)])
        self.assertEqual(C.apparier_restants(cls, ctx), 1)
        self.assertEqual(cls[0].statut, C.STATUT_A_VERIFIER)
        self.assertEqual(cls[0].document_name, "ACC-JV-511")
        self.assertEqual(cls[0].ecart, 0.5)
        self.assertIn("montant approchant", cls[0].raison)

    def test_la_passe_exacte_prime_sur_la_passe_toleree(self):
        """Une piece au montant EXACT ne doit jamais etre soufflee par une piece approchante."""
        cls = [self._classification("m1", 703.5, jour=20)]
        ctx = C.LinkContext(pieces=[piece("ACC-JV-511", 18, 703.0),
                                    piece("ACC-JV-528", 20, 703.5)])
        C.apparier_restants(cls, ctx)
        self.assertEqual(cls[0].document_name, "ACC-JV-528")
        self.assertEqual(cls[0].statut, C.STATUT_IDENTIFIE)

    def test_piece_citant_une_cle_du_releve_est_exclue(self):
        """Garde-fou n°1 : la piece appartient au mouvement dont elle cite la reference, meme
        quand ce mouvement a ete rapproche autrement (cas des ecritures groupees)."""
        cls = [self._classification("m1", 33.0)]
        ctx = C.LinkContext(pieces=[piece("ACC-JV-1", 4, 33.0,
                                          texte="3 paiements Orange FT261249MVZ0 et autres")])
        self.assertEqual(C.apparier_restants(cls, ctx), 0)
        self.assertEqual(cls[0].statut, C.STATUT_A_VERIFIER)

    def test_piece_deja_citee_par_une_autre_classification_est_exclue(self):
        cls = [self._classification("m1", 33.0),
               self._classification("m2", 33.0, jour=12, statut=C.STATUT_IDENTIFIE,
                                    reference="FT26131AAAAA", document_name="ACC-JV-1")]
        ctx = C.LinkContext(pieces=[piece("ACC-JV-1", 4, 33.0)])
        self.assertEqual(C.apparier_restants(cls, ctx), 0)

    def test_ligne_de_groupe_exclue(self):
        """Frais journaliers et echeances ne se comptabilisent JAMAIS a l'unite."""
        cls = [self._classification("m1", 33.0, groupe="frais-04-05-2026")]
        ctx = C.LinkContext(pieces=[piece("ACC-JV-1", 4, 33.0)])
        self.assertEqual(C.apparier_restants(cls, ctx), 0)

    def test_mouvement_deja_identifie_non_retouche(self):
        cls = [self._classification("m1", 33.0, statut=C.STATUT_IDENTIFIE,
                                    document_name="ACC-JV-9")]
        ctx = C.LinkContext(pieces=[piece("ACC-JV-1", 4, 33.0)])
        self.assertEqual(C.apparier_restants(cls, ctx), 0)
        self.assertEqual(cls[0].document_name, "ACC-JV-9")

    def test_sans_pieces_aucun_effet(self):
        cls = [self._classification("m1", 33.0)]
        self.assertEqual(C.apparier_restants(cls, C.LinkContext()), 0)

    def test_classify_appelle_la_passe(self):
        """Le branchement lui-meme : `classify` doit terminer par l'appariement de dernier recours."""
        movements = [{"date": date(2026, 5, 4), "operation": "REGLEMENT CB 0405MYTEK INFORM",
                      "reference": "FT261249MVZ0", "debit": 33.0, "credit": 0.0}]
        ctx = C.LinkContext(pieces=[piece("ACC-JV-1", 4, 33.0)])
        out = C.classify(movements, ctx)
        self.assertEqual(out[0].statut, C.STATUT_IDENTIFIE)
        self.assertEqual(out[0].document_name, "ACC-JV-1")


class TestIndicesHorsFenetre(unittest.TestCase):
    def test_meme_montant_hors_fenetre_signale(self):
        """Une recharge Total est comptabilisee au dernier jour du mois PRECEDENT : le montant
        existe, mais a trois semaines de la."""
        indices = E._indices_hors_fenetre(
            603.0, "Debit", date(2026, 6, 22), [piece("ACC-JV-1", 31, 603.0)], "posting_date",
            lambda a: a["voucher_no"])
        self.assertEqual(indices, ["ACC-JV-1 du 2026-05-31"])

    def test_dans_la_fenetre_non_signale(self):
        """Ce qui est dans la fenetre releve de l'appariement, pas de l'indice."""
        self.assertEqual(
            E._indices_hors_fenetre(603.0, "Debit", date(2026, 5, 4),
                                    [piece("ACC-JV-1", 4, 603.0)], "posting_date",
                                    lambda a: a["voucher_no"]),
            [])


class TestEcrituresDeFraisEcartees(unittest.TestCase):
    """L'ecriture mensuelle de frais cite les references de TOUTES les commissions du mois, et
    une commission porte la meme reference que l'operation qui l'a generee. Sans ce filtre, les
    4 salaires de juillet et la facture Aramex ressortaient « plusieurs ecritures citent cette
    reference » alors qu'ils etaient comptabilises."""

    def test_ecriture_de_frais_retiree_de_lindex(self):
        index = {"FT2618214DY4": ["ACC-JV-2026-00456", "ACC-JV-2026-00561"]}
        cheques = {"Frais bancaire 07-2026": "ACC-JV-2026-00561",
                   "Declaration comptable 06-2026": "ACC-JV-2026-00400"}
        self.assertEqual(C._sans_ecritures_de_frais(index, cheques),
                         {"FT2618214DY4": ["ACC-JV-2026-00456"]})

    def test_reference_citee_uniquement_par_les_frais_disparait(self):
        """Aucune entree vide : une reference qui n'est plus citee par personne sort de l'index."""
        index = {"CHG2619506074": ["ACC-JV-2026-00561"]}
        cheques = {"Frais bancaire 07-2026": "ACC-JV-2026-00561"}
        self.assertEqual(C._sans_ecritures_de_frais(index, cheques), {})

    def test_sans_ecriture_de_frais_index_intact(self):
        index = {"FT1": ["ACC-JV-1"]}
        self.assertIs(C._sans_ecritures_de_frais(index, {}), index)


class TestGroupesDeRapprochement(unittest.TestCase):
    """L'unite d'analyse de l'ecart : ni la ligne, ni la piece, mais la composante."""

    def test_une_remise_et_ses_cheques_font_un_seul_groupe(self):
        """La banque groupe (un credit), ERPNext eclate (une piece par cheque)."""
        mvts = [dict(mvt("m1", 15, 4418.940, sens="Credit",
                         reference="FT26196ST3TN", operation="ENC CHEQ TN NUM 90028077"),
                     document_name=None)]
        pcs = [piece("ACC-PAY-1", 13, 1552.320, sens="Credit", texte="0000797 / BR:90028077"),
               piece("ACC-PAY-2", 13, 2866.620, sens="Credit", texte="8377782 / BR:90028077")]
        groupes = E._groupes_de_rapprochement(mvts, pcs)
        self.assertEqual(len(groupes), 1)
        self.assertEqual(len(groupes[0]["pieces"]), 2)

    def test_une_ecriture_groupee_absorbe_ses_trois_mouvements(self):
        """Le cas inverse : ERPNext groupe (une ecriture pour trois paiements Orange)."""
        refs = ["FT262165WY1Q", "FT2621604KYR", "FT26216B4VP4"]
        mvts = [dict(mvt("m%d" % i, 4, 25.928, reference=r), document_name=None)
                for i, r in enumerate(refs)]
        pcs = [piece("ACC-JV-535", 4, 77.784, texte="Orange " + " ".join(refs))]
        groupes = E._groupes_de_rapprochement(mvts, pcs)
        self.assertEqual(len(groupes), 1)
        self.assertEqual(len(groupes[0]["mouvements"]), 3)

    def test_le_document_pose_par_la_classification_relie(self):
        """Un appariement par montant ne laisse aucune trace textuelle : seul `document_name` lie."""
        mvts = [dict(mvt("m1", 4, 33.0, reference="FT261249MVZ0"),
                     document_name="ACC-JV-318")]
        groupes = E._groupes_de_rapprochement(mvts, [piece("ACC-JV-318", 4, 33.0)])
        self.assertEqual(len(groupes), 1)

    def test_les_echeances_d_un_meme_contrat_ne_fusionnent_pas(self):
        """Une reference `LD…` est un numero de CONTRAT : sans precaution, juin, juillet et aout
        formeraient une seule composante et l'ecart mensuel deviendrait illisible."""
        mvts = []
        for mois, doc in ((6, "ACC-JV-393"), (7, "ACC-JV-468")):
            for i in range(2):
                mvts.append(dict(mvt("m%d%d" % (mois, i), 4, 600.0,
                                     reference="LD2227700127"),
                                 groupe="echeance-LD2227700127-04-0%d-2026" % mois,
                                 document_name=doc))
        pcs = [piece("ACC-JV-393", 4, 1200.0, texte="Leasing LD2227700127"),
               piece("ACC-JV-468", 4, 1200.0, texte="Leasing LD2227700127")]
        groupes = E._groupes_de_rapprochement(mvts, pcs)
        self.assertEqual(len(groupes), 2)
        for g in groupes:
            self.assertEqual(len(g["mouvements"]), 2)
            self.assertEqual(len(g["pieces"]), 1)

    def test_sans_lien_deux_groupes_distincts(self):
        groupes = E._groupes_de_rapprochement(
            [dict(mvt("m1", 4, 33.0, reference="FT261249MVZ0"), document_name=None)],
            [piece("ACC-JV-999", 4, 33.0)])
        self.assertEqual(len(groupes), 2)


class TestDoublons(unittest.TestCase):
    """Cas reel : la recharge Total du 30/04 comptabilisee deux fois, alors que le releve ne
    porte qu'un debit de 603 ce jour-la."""

    def test_deux_pieces_identiques_se_designent(self):
        d = E.doublons_de_pieces([piece("ACC-JV-304", 30, 603.0), piece("ACC-JV-466", 30, 603.0)])
        self.assertEqual(d["ACC-JV-304"], ["ACC-JV-466"])
        self.assertEqual(d["ACC-JV-466"], ["ACC-JV-304"])

    def test_dates_differentes_ne_sont_pas_un_doublon(self):
        """Date EXACTE : deux jours d'ecart designent deux operations, pas une saisie en double."""
        self.assertEqual(
            E.doublons_de_pieces([piece("ACC-JV-1", 30, 603.0), piece("ACC-JV-2", 28, 603.0)]), {})

    def test_sens_opposes_ne_sont_pas_un_doublon(self):
        self.assertEqual(
            E.doublons_de_pieces([piece("ACC-JV-1", 30, 603.0),
                                  piece("ACC-JV-2", 30, 603.0, sens="Credit")]), {})

    def test_piece_seule_jamais_signalee(self):
        self.assertEqual(E.doublons_de_pieces([piece("ACC-JV-1", 30, 603.0)]), {})


class TestSynthese(unittest.TestCase):
    def test_cumul_par_nature(self):
        s = E._synthese([{"nature": "a", "solde": 10.0}, {"nature": "a", "solde": -3.0},
                         {"nature": "b", "solde": 20.0}])
        self.assertEqual(s[0], {"nature": "b", "nb": 1, "solde": 20.0})
        self.assertEqual(s[1], {"nature": "a", "nb": 2, "solde": 7.0})


class TestProjection(unittest.TestCase):
    """L'effet, sur l'ecart, de ce qui n'est rapproche d'aucun cote."""

    def _rapport(self, pieces=(), mouvements=()):
        return {"erpnext_sans_banque": list(pieces), "banque_sans_erpnext": list(mouvements)}

    def _p(self, nom, montant, sens, verdict, jour=8):
        return dict(piece(nom, jour, montant, sens=sens), statut_ecart=verdict)

    def test_depot_en_circulation_rapproche_l_ecart_de_zero(self):
        """136 DT deposes non credites : que la banque credite ou que la piece soit annulee,
        `banque − ERPNext` gagne +136 dans les deux cas."""
        r = self._rapport([self._p("ACC-PAY-1", 136.0, "Credit", E.STATUT_TROP_RECENT)])
        postes, delai, correction = E.effets_projection(r)
        self.assertEqual(delai, 136.0)
        self.assertEqual(correction, 0.0)
        self.assertEqual(postes[0]["nature"], "delai")

    def test_un_doublon_ne_compte_que_pour_k_moins_1(self):
        """Les DEUX pieces d'une paire portent le verdict, mais une seule est en trop."""
        r = self._rapport([self._p("ACC-JV-304", 603.0, "Debit", E.STATUT_DOUBLON),
                           self._p("ACC-JV-466", 603.0, "Debit", E.STATUT_DOUBLON)])
        postes, delai, correction = E.effets_projection(r)
        self.assertEqual(correction, -603.0)
        self.assertEqual(delai, 0.0)
        self.assertEqual(postes[0]["nb"], 1)

    def test_UN_DOUBLON_N_EST_PAS_UN_DELAI(self):
        """Il ne s'efface pas avec le temps : il se decide, comme une ecriture a creer. Le ranger
        avec les depots en circulation annoncait une degradation de 603 DT qui n'existe pas,
        puisque la recharge non comptabilisee, cote banque, la compense exactement."""
        r = self._rapport(
            [self._p("ACC-JV-304", 603.0, "Debit", E.STATUT_DOUBLON),
             self._p("ACC-JV-466", 603.0, "Debit", E.STATUT_DOUBLON)],
            [{"reference": "FT26173R1XLF", "sens": "Debit", "montant": 603.0,
              "statut_ecart": E.STATUT_SANS_TRACE}])
        _, delai, correction = E.effets_projection(r)
        self.assertEqual(delai, 0.0)
        self.assertEqual(correction, 0.0)      # −603 du doublon + 603 de la recharge manquante

    def test_verdicts_dont_la_contrepartie_existe_sont_exclus(self):
        """Leur mouvement est au releve, donc deja dans le solde bancaire : les projeter
        compterait deux fois le meme argent."""
        r = self._rapport([self._p("ACC-JV-1", 20000.0, "Credit", E.STATUT_PROBABLE),
                           self._p("ACC-JV-2", 703.0, "Debit", E.STATUT_ECART_MONTANT),
                           self._p("ACC-JV-3", 603.0, "Debit", E.STATUT_HORS_REGISTRE)])
        self.assertEqual(E.effets_projection(r), ([], 0.0, 0.0))

    def test_cote_banque_deplace_l_ecart_en_sens_INVERSE(self):
        """Comptabiliser un prelevement fait BAISSER ERPNext, donc monter `banque − ERPNext`."""
        r = self._rapport(mouvements=[{"reference": "FT1", "sens": "Debit", "montant": 350.124,
                                       "statut_ecart": E.STATUT_SANS_TRACE}])
        postes, delai, correction = E.effets_projection(r)
        self.assertEqual(delai, 0.0)
        self.assertEqual(correction, 350.124)
        self.assertEqual(postes[0]["nature"], "correction")

    def test_un_encaissement_non_comptabilise_creuse_l_ecart(self):
        r = self._rapport(mouvements=[{"reference": "FT2", "sens": "Credit", "montant": 61700.0,
                                       "statut_ecart": E.STATUT_SANS_TRACE}])
        _, _, correction = E.effets_projection(r)
        self.assertEqual(correction, -61700.0)

    def test_rapport_vide(self):
        self.assertEqual(E.effets_projection({}), ([], 0.0, 0.0))


class TestEcartOuverture(unittest.TestCase):
    """L'ecart d'ouverture est un solde REPORTE, pas une mesure : il se soustrait du brut."""

    def setUp(self):
        self._vrai = E.ecart_ouverture

    def tearDown(self):
        E.ecart_ouverture = self._vrai

    def test_net_soustrait_l_ouverture(self):
        E.ecart_ouverture = lambda: {"date": "2026-06-30", "montant": -414.977}
        self.assertEqual(E.ecart_net(-201.858), 213.119)

    def test_sans_ouverture_le_net_vaut_le_brut(self):
        E.ecart_ouverture = lambda: {"date": None, "montant": 0.0}
        self.assertEqual(E.ecart_net(-201.858), -201.858)

    def test_ecart_absent_reste_absent(self):
        E.ecart_ouverture = lambda: {"date": None, "montant": 0.0}
        self.assertIsNone(E.ecart_net(None))


class TestTotaux(unittest.TestCase):
    def test_ventilation_par_sens_et_par_statut(self):
        pieces = [dict(piece("ACC-JV-1", 4, 100.0), statut_ecart=E.STATUT_SANS_TRACE),
                  dict(piece("ACC-JV-2", 4, 50.0, sens="Credit"),
                       statut_ecart=E.STATUT_PROBABLE)]
        t = E._totaux(pieces, [])["erpnext_sans_banque"]
        self.assertEqual(t["nb"], 2)
        self.assertEqual(t["montant"], 150.0)
        self.assertEqual(t["debit"]["montant"], 100.0)
        self.assertEqual(t["credit"]["nb"], 1)
        self.assertEqual(t["sans_trace"]["nb"], 1)
        self.assertEqual(t["probable"]["montant"], 50.0)


if __name__ == "__main__":
    unittest.main()
