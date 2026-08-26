"""Tests de l emission d un certificat de retenue vers TEJ — les fonctions pures.

Ce module declare au fisc et ne peut pas se defaire : un certificat soumis se lit chez le
fournisseur et chez l administration. Il etait pourtant le seul du dossier `tej` sans aucun test.

Le plus important tient en une phrase : `en_analyse` est une REUSSITE. Le job de creation rend
`succeeded` avec un numero de depot et sans reference, et le code qui deduisait le succes de la
seule presence d une reference concluait « non soumis » sur ce cas — qui est le cas nominal.
L ecran annonçait alors qu aucun certificat n avait ete cree alors que le depot existait deja chez
le fisc, et invitait au second clic qui declare en double.
"""
import unittest

from bank_retenue_sync.tej import depot


class TestLectureDeLaCreation(unittest.TestCase):
    """`depot.lire_creation` : ce que le job de creation a VRAIMENT produit."""

    def test_en_analyse_est_reconnu_et_porte_son_numero_de_depot(self):
        r = depot.lire_creation({"cert_create": {
            "statut": "en_analyse", "depot": {"numero": "IN260051"},
            "suivi": {"numero_chez_declarant": "FA-4471"}}})
        self.assertEqual(r["statut"], depot.EN_ANALYSE)
        self.assertEqual(r["depot_numero"], "IN260051")
        self.assertEqual(r["reference"], "")
        self.assertEqual(r["suivi"], {"numero_chez_declarant": "FA-4471"})

    def test_en_analyse_n_est_pas_un_echec_malgre_l_absence_de_reference(self):
        """La regression qui produisait les doubles declarations."""
        r = depot.lire_creation({"cert_create": {
            "statut": "en_analyse", "depot": {"numero": "IN260051"}}})
        self.assertFalse(r["reference"])
        self.assertNotEqual(r["statut"], None)
        self.assertEqual(r["statut"], depot.EN_ANALYSE)

    def test_genere_porte_la_reference(self):
        r = depot.lire_creation({"cert_create": {
            "statut": "genere", "submitted": True, "reference": "9f2c-aa11"}})
        self.assertEqual(r["statut"], depot.GENERE)
        self.assertEqual(r["reference"], "9f2c-aa11")
        self.assertIs(r["submitted"], True)

    def test_dry_run_est_rendu_tel_quel(self):
        r = depot.lire_creation({"cert_create": {"statut": "dry_run"}})
        self.assertEqual(r["statut"], depot.DRY_RUN)
        self.assertEqual(r["depot_numero"], "")

    def test_repli_sans_statut_une_reference_vaut_genere(self):
        """Le service d avant l ajout de `statut` ne doit pas rendre le module aveugle."""
        self.assertEqual(depot.lire_creation({"cert_create": {"reference": "x"}})["statut"],
                         depot.GENERE)

    def test_repli_sans_statut_un_depot_vaut_en_analyse(self):
        self.assertEqual(
            depot.lire_creation({"cert_create": {"depot": {"numero": "IN1"}}})["statut"],
            depot.EN_ANALYSE)

    def test_sans_rien_on_ne_conclut_pas(self):
        """Ni reference ni depot : l inconnu se dit, il ne se devine pas."""
        self.assertIsNone(depot.lire_creation({})["statut"])
        self.assertIsNone(depot.lire_creation({"cert_create": {}})["statut"])
        self.assertIsNone(depot.lire_creation(None)["statut"])

    def test_la_reference_a_la_racine_est_acceptee(self):
        self.assertEqual(depot.lire_creation({"reference": "racine"})["reference"], "racine")


class TestLectureDuStatut(unittest.TestCase):
    """`depot.lire_statut` : le retour du job de suivi, sous `cert_statut`."""

    def test_genere_rend_la_reference(self):
        r = depot.lire_statut({"cert_statut": {"statut": "genere", "reference": "abc"}})
        self.assertEqual(r["statut"], depot.GENERE)
        self.assertEqual(r["reference"], "abc")

    def test_toujours_en_analyse(self):
        r = depot.lire_statut({"cert_statut": {"statut": "en_analyse",
                                               "depot": {"numero": "IN260051"}}})
        self.assertEqual(r["statut"], depot.EN_ANALYSE)
        self.assertEqual(r["depot_numero"], "IN260051")
        self.assertEqual(r["reference"], "")

    def test_une_reference_sans_statut_vaut_genere(self):
        self.assertEqual(depot.lire_statut({"cert_statut": {"reference": "z"}})["statut"],
                         depot.GENERE)

    def test_reponse_vide(self):
        self.assertIsNone(depot.lire_statut({})["statut"])


class TestCorpsDeSuivi(unittest.TestCase):
    """`depot.corps_de_suivi` : ce qu on poste sur la route de statut.

    Le corps verbatim rendu par le service prime toujours : le reconstruire, c est risquer de
    suivre un autre depot que celui qui vient d etre cree.
    """

    #: Ce que le service rend vraiment, releve sur DEP-2026-00235 le 17/08/2026. C'est une
    #: ENVELOPPE `{endpoint, body}` : `CertSuivi` au contrat, et non le corps a poster.
    ENVELOPPE_REELLE = ('{\n "endpoint": "/jobs/tej/certificats-emis/statut",\n "body": {\n'
                        '  "numero_chez_declarant": "26FA01134_V2",\n'
                        '  "beneficiaire": "1802542W",\n'
                        '  "date_paiement": "2026-08-13",\n'
                        '  "numero_depot": "IN260052",\n'
                        '  "exercice": 2026\n }\n}')

    def test_l_enveloppe_du_service_est_deballee(self):
        """Le bug qui rendait tout depot eternellement « en analyse ».

        Postee telle quelle, l'enveloppe n'a pas de `numero_chez_declarant` au premier niveau :
        `interroger` la refusait sans appeler le service, et l'ecran annonçait « c'est normal ».
        """
        corps = depot.corps_de_suivi({"suivi": self.ENVELOPPE_REELLE,
                                      "numero_declarant": "26FA01134"})
        self.assertNotIn("endpoint", corps)
        self.assertNotIn("body", corps)
        self.assertEqual(corps["numero_chez_declarant"], "26FA01134_V2")
        self.assertEqual(corps["numero_depot"], "IN260052")

    def test_l_enveloppe_porte_le_numero_soumis_pas_celui_demande(self):
        """Le `_V2` ne vit que dans le corps du service : la ligne, elle, garde le numero demande."""
        corps = depot.corps_de_suivi({"suivi": self.ENVELOPPE_REELLE,
                                      "numero_declarant": "26FA01134"})
        self.assertEqual(corps["numero_chez_declarant"], "26FA01134_V2")

    def test_le_corps_verbatim_du_service_prime(self):
        ligne = {"suivi": '{"numero_chez_declarant": "FA-1", "numero_depot": "IN9"}',
                 "numero_declarant": "AUTRE", "numero_depot": "IN000"}
        self.assertEqual(depot.corps_de_suivi(ligne),
                         {"numero_chez_declarant": "FA-1", "numero_depot": "IN9"})

    def test_une_enveloppe_sans_numero_retombe_sur_la_reconstruction(self):
        """Un corps inexploitable ne doit pas etre poste : la ligne sait encore quoi dire."""
        ligne = {"suivi": '{"endpoint": "/x", "body": {"numero_depot": "IN9"}}',
                 "numero_declarant": "FA-4471"}
        self.assertEqual(depot.corps_de_suivi(ligne), {"numero_chez_declarant": "FA-4471"})

    def test_un_suivi_qui_n_est_pas_un_objet_ne_fait_rien_tomber(self):
        ligne = {"suivi": '["une", "liste"]', "numero_declarant": "FA-4471"}
        self.assertEqual(depot.corps_de_suivi(ligne)["numero_chez_declarant"], "FA-4471")

    def test_reconstruction_quand_le_service_n_a_rien_rendu(self):
        ligne = {"suivi": "", "numero_declarant": "FA-4471", "beneficiaire": "1234567A/M/000",
                 "date_paiement": "2026-07-31", "numero_depot": "IN260051", "exercice": 2026}
        corps = depot.corps_de_suivi(ligne)
        self.assertEqual(corps["numero_chez_declarant"], "FA-4471")
        self.assertEqual(corps["numero_depot"], "IN260051")
        self.assertEqual(corps["exercice"], 2026)

    def test_un_suivi_illisible_ne_fait_pas_tomber_le_cron(self):
        ligne = {"suivi": "{ceci n est pas du json", "numero_declarant": "FA-4471"}
        self.assertEqual(depot.corps_de_suivi(ligne)["numero_chez_declarant"], "FA-4471")

    def test_les_champs_vides_ne_sont_pas_envoyes(self):
        ligne = {"suivi": "", "numero_declarant": "FA-1", "beneficiaire": "",
                 "date_paiement": None, "numero_depot": "", "exercice": 0}
        self.assertEqual(depot.corps_de_suivi(ligne), {"numero_chez_declarant": "FA-1"})


class TestNombreDuPortail(unittest.TestCase):
    """`emis._nombre_tej` : le portail rend des CHAINES, avec une espace insecable."""

    def _n(self, v):
        from bank_retenue_sync.tej.emis import _nombre_tej
        return _nombre_tej(v)

    def test_espace_insecable_comme_separateur_de_milliers(self):
        self.assertEqual(self._n("1 099.203"), 1099.203)

    def test_espace_ordinaire(self):
        self.assertEqual(self._n("1 500,000"), 1500.0)

    def test_virgule_decimale(self):
        self.assertEqual(self._n("12,345"), 12.345)

    def test_illisible_rend_none_au_lieu_de_lever(self):
        for v in (None, "", "n/a", "—"):
            self.assertIsNone(self._n(v))


class TestMontantsCalculesParTej(unittest.TestCase):
    """`emis.montants_calcules` : ce que le portail a calcule, pour le confronter a la facture."""

    def _m(self, reponse):
        from bank_retenue_sync.tej.emis import montants_calcules
        return montants_calcules(reponse)

    def test_lecture_d_une_operation(self):
        r = self._m({"cert_create": {"operations": [{"computed": {
            "tauxRs": "1", "mantantTva": "190,000", "mantantTTC": "1 190,000",
            "mantantRs": "11,900", "mantantNet": "1 178,100"}}]}})
        self.assertEqual(r["retenue"], 11.9)
        self.assertEqual(r["ttc"], 1190.0)
        self.assertEqual(r["taux"], 1.0)

    def test_plusieurs_operations_les_retenues_se_somment(self):
        r = self._m({"cert_create": {"operations": [
            {"computed": {"mantantRs": "10,000"}}, {"computed": {"mantantRs": "5,500"}}]}})
        self.assertEqual(r["retenue"], 15.5)

    def test_aucune_operation_rend_un_dict_vide(self):
        self.assertEqual(self._m({"cert_create": {}}), {})
        self.assertEqual(self._m({}), {})


class TestCleIdempotence(unittest.TestCase):
    """`emis.cle_idempotence` : la repetition ne doit JAMAIS porter la cle de la soumission."""

    def _c(self, dry_run):
        from bank_retenue_sync.tej.emis import cle_idempotence
        return cle_idempotence({"facture": "ACC-PINV-2026-00093"}, dry_run)

    def test_la_repetition_part_sans_cle(self):
        self.assertIsNone(self._c(True))

    def test_la_soumission_porte_la_facture(self):
        self.assertEqual(self._c(False), "PINV-ACC-PINV-2026-00093")

    def test_les_deux_cles_different(self):
        """La panne du 13/08/2026 : cle commune -> le « soumettre » rendait le job du dry_run."""
        self.assertNotEqual(self._c(True), self._c(False))


class TestFraicheurDeLExport(unittest.TestCase):
    """`emis.est_aveugle` : le controle anti-doublon voit-il quelque chose ?

    ⚠️ LA QUESTION EST « QUAND L EXPORT A-T-IL ETE GENERE », PAS « QUE CONTIENT-IL ». L export du
    portail est toujours complet ; son angle mort est ce qui a ete emis DEPUIS sa generation.
    La version precedente comparait la date des lignes a celle de la facture : comme rien n avait
    ete emis depuis le 20/07/2026, elle alertait sur toute facture posterieure — donc toujours.
    """

    def _f(self):
        from bank_retenue_sync.tej.emis import est_aveugle
        return est_aveugle

    def test_un_export_regenere_a_l_instant_voit_tout(self):
        from datetime import datetime
        maintenant = datetime(2026, 8, 15, 14, 30)
        genere = datetime(2026, 8, 15, 14, 29, 50)
        self.assertFalse(self._f()(genere, maintenant))

    def test_un_export_vieux_de_deux_jours_a_un_angle_mort(self):
        from datetime import datetime
        self.assertTrue(self._f()(datetime(2026, 8, 13, 14, 11),
                                  datetime(2026, 8, 15, 14, 30)))

    def test_le_seuil_est_respecte(self):
        from datetime import datetime, timedelta
        maintenant = datetime(2026, 8, 15, 14, 30)
        self.assertFalse(self._f()(maintenant - timedelta(minutes=14), maintenant))
        self.assertTrue(self._f()(maintenant - timedelta(minutes=16), maintenant))

    def test_sans_date_on_avoue_l_ignorance(self):
        """Un garde-fou qui ne sait pas doit le dire, jamais rassurer."""
        from datetime import datetime
        self.assertTrue(self._f()(None, datetime(2026, 8, 15)))
        self.assertTrue(self._f()(datetime(2026, 8, 15), None))

    def test_une_emission_ancienne_ne_declenche_plus_rien(self):
        """Le faux positif du 15/08/2026 : derniere emission au 20/07, export frais."""
        from datetime import datetime
        genere = datetime(2026, 8, 15, 14, 25)
        self.assertFalse(self._f()(genere, datetime(2026, 8, 15, 14, 30)))


class TestRefusDuPortail(unittest.TestCase):
    """`emis.est_un_refus` : distinguer un refus de TEJ d une panne.

    ⚠️ LA DIFFERENCE DECIDE SI LA FACTURE RESTE BLOQUEE. Un refus signifie que TEJ a examine la
    saisie et l a rejetee : rien n est parti, la facture peut etre reprise. Toute autre erreur
    laisse le doute — le clic « Valider » a pu aboutir avant la panne — et la facture doit rester
    bloquee jusqu a verification sur le portail.
    """

    def _f(self):
        from bank_retenue_sync.tej.emis import est_un_refus
        return est_un_refus

    def test_le_refus_reel_du_15_08_2026(self):
        msg = ("job jb_7dc0ef967f6c481a failed : CertCreateError: Soumission refusee par TEJ : "
               "['Un certificat existe avec ce meme contenu']")
        self.assertTrue(self._f()(msg))

    def test_le_refus_accentue_est_reconnu_aussi(self):
        self.assertTrue(self._f()("CertCreateError: Soumission refusée par TEJ : ['doublon']"))

    def test_un_timeout_n_est_pas_un_refus(self):
        self.assertFalse(self._f()("job jb_x toujours 'running' apres 900s (etape : soumission)"))

    def test_une_coupure_reseau_n_est_pas_un_refus(self):
        self.assertFalse(self._f()("ConnectionError: Max retries exceeded"))

    def test_un_worker_tue_n_est_pas_un_refus(self):
        self.assertFalse(self._f()("job jb_x cancelled : sans detail"))

    def test_message_vide(self):
        self.assertFalse(self._f()(None))
        self.assertFalse(self._f()(""))


class TestCertificatManuel(unittest.TestCase):
    """⚠️ LA BARRIERE NE VOYAIT QUE SA PROPRE CONVENTION (`certificat_ras_*`). Au 26/08/2026,
    treize factures 2026 portent un certificat attache a la main, invisible pour elle ET pour
    l'export du portail (ere papier) : rien ne barrait une seconde declaration de la meme
    retenue."""

    def _f(self):
        from bank_retenue_sync.tej.emis import nom_de_certificat_manuel
        return nom_de_certificat_manuel

    def test_les_noms_reels_sont_reconnus(self):
        """Tels qu'ils sont en base — accents, tirets, ordre des mots variables."""
        for nom in ("Retenue à la source -JEGHAM INDUSTRIES - FA N° 2026-0012 du 05-02-2026.pdf",
                    "Retenue a la source Jgham industrie.pdf",
                    "retenue à la source 2503747.pdf",
                    "Fac N° 2403029-AQUA SERVICE-retenue à la source.pdf"):
            self.assertTrue(self._f()(nom), nom)

    def test_le_scan_de_la_facture_n_est_pas_un_certificat(self):
        for nom in ("Erectroquip.pdf", "Fac N°  FA-2026-0012-JEGHAM Industries.pdf",
                    "SOCIÉTÉ TRITECHace8dd.pdf"):
            self.assertFalse(self._f()(nom), nom)

    def test_seul_un_pdf_compte(self):
        """Meme doctrine que la piece justificative d'achat : un JPG ne se lit ni ne s'imprime
        pareil au controle."""
        self.assertFalse(self._f()("retenue à la source.jpg"))

    def test_le_vide_ne_prouve_rien(self):
        self.assertFalse(self._f()(None))
        self.assertFalse(self._f()(""))


class TestSuiviDesDepotsIncertains(unittest.TestCase):
    """`emis.suivre_depot` sur une ligne `incertain` : la route de statut peut conclure seule.

    Le cas fondateur est DEP-2026-00323 (prod, 26/08/2026) : la confirmation post-Valider du
    service a rendu une erreur alors que le certificat etait GENERE. La ligne est passee
    `incertain` — et le cron, qui ne relisait que `en_analyse`, laissait la facture « a
    verifier sur le portail » pour toujours. La route de statut, elle, est en lecture seule :
    la rappeler ne risque rien et rend la verite.
    """

    LIGNE = {"name": "DEP-TEST-1", "facture": "ACC-PINV-TEST", "statut": "incertain",
             "numero_depot": "", "numero_declarant": "2605094", "suivi": "",
             "verifications": 3}

    def _suivre(self, vu, ligne=None):
        from unittest import mock

        from bank_retenue_sync.tej import emis
        appels = {}
        with mock.patch.object(depot, "interroger", return_value=vu), \
             mock.patch.object(depot, "conclure",
                               side_effect=lambda *a, **k: appels.setdefault("conclure", (a, k))), \
             mock.patch.object(depot, "toucher",
                               side_effect=lambda *a, **k: appels.setdefault("toucher", (a, k))), \
             mock.patch.object(emis, "attacher_pdf",
                               side_effect=lambda *a, **k: appels.setdefault("pdf", (a, k)) or {}):
            resultat = emis.suivre_depot(dict(ligne or self.LIGNE))
        return resultat, appels

    def test_un_incertain_que_le_portail_dit_genere_est_conclu_et_son_pdf_attache(self):
        resultat, appels = self._suivre({"statut": depot.GENERE, "reference": "2f8fbad3-x",
                                         "depot_numero": "IN260099", "message": ""})
        self.assertEqual(resultat["statut"], depot.GENERE)
        a, k = appels["conclure"]
        self.assertEqual(a[1], depot.GENERE)
        self.assertEqual(a[2], "2f8fbad3-x")
        # Le numero de depot appris par le suivi est conserve : la conclusion prematuree en
        # `incertain` n'avait jamais pu l'enregistrer.
        self.assertEqual(k.get("numero"), "IN260099")
        self.assertIn("pdf", appels)

    def test_un_incertain_que_le_portail_dit_en_analyse_reprend_le_circuit_nominal(self):
        resultat, appels = self._suivre({"statut": depot.EN_ANALYSE, "reference": "",
                                         "depot_numero": "IN260099", "message": "patienter"})
        self.assertEqual(resultat["statut"], depot.EN_ANALYSE)
        a, k = appels["conclure"]
        self.assertEqual(a[1], depot.EN_ANALYSE)
        self.assertEqual(k.get("numero"), "IN260099")
        self.assertNotIn("toucher", appels)

    def test_un_en_analyse_toujours_en_analyse_est_seulement_touche(self):
        ligne = dict(self.LIGNE, statut="en_analyse")
        resultat, appels = self._suivre({"statut": depot.EN_ANALYSE, "reference": "",
                                         "depot_numero": "", "message": ""}, ligne)
        self.assertEqual(resultat["statut"], depot.EN_ANALYSE)
        self.assertIn("toucher", appels)
        self.assertNotIn("conclure", appels)

    def test_un_incertain_sans_reponse_exploitable_le_reste(self):
        resultat, appels = self._suivre({"statut": None, "reference": "",
                                         "depot_numero": "", "message": "portail muet"})
        self.assertIn("toucher", appels)
        self.assertNotIn("conclure", appels)
