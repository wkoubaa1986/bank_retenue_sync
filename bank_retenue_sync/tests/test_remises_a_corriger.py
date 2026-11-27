"""Alerte des remises a corriger (27/11/2026) : cheque ou traite d'un bordereau introuvable, remise carte a
signaler. Pur. Les cas reprennent la BIAT au 27/11/2026."""
import unittest

from bank_retenue_sync.bank import remises_a_corriger as R


def bordereau(numero, nature="Chèques", a_signaler=None, statut="Lue", date="2026-11-25", pieces=()):
    return {"name": f"BR-BIAT-{numero}", "nature": nature, "numero": numero, "date_remise": date,
            "lecture_statut": statut, "a_signaler": a_signaler, "pieces": list(pieces)}


INTROUVABLE = "Chèque n° 0000373 (701,000) : introuvable dans ERPNext."
# Le cas du 27/11/2026 : cheque du colis Aramex de KARRAY SARRAH, parti a la banque sans passer par la caisse.
B_9437101 = bordereau("9437101", a_signaler=INTROUVABLE, pieces=[{"numero": "0000373", "montant": 701.0, "etat": "Introuvable"}])
CREDIT = {"9437101": {"statut": "A verifier", "date": "2026-11-27", "montant": 701.0, "ignore": False}}
ARAMEX = [{"client": "KARRAY SARRAH", "montant": 701.0, "colis": "55087934495", "commande": "SAL-ORD-2026-04731"},
          {"client": "Hammadi Mahmoud", "montant": 476.0, "colis": "55088542924", "commande": None}]


def carte(remise, controle="À signaler", ecriture=None, a_signaler="1 transaction sans paiement dans ERPNext.", credit="2026-11-26"):
    return {"name": f"TPE-BIAT-20261125-01-{remise}", "remise": remise, "date_traitement": "2026-11-25", "etat": "Créditée",
            "date_credit": credit, "montant_credite": 2757.2, "brut": 2757.2, "controle": controle,
            "a_signaler": a_signaler, "ecriture": ecriture}


class TestBordereaux(unittest.TestCase):
    def test_le_cas_du_27_11_cheque_introuvable_remise_deja_creditee(self):
        (l,) = R.a_corriger([B_9437101], CREDIT, [], {}, ARAMEX)
        self.assertEqual((l["cle"], l["genre"], l["doctype"], l["name"], l["verifier"]),
                         ("BIAT|bordereau_a_corriger|BR-BIAT-9437101", R.GENRE_BORDEREAU, "BRS Bordereau Remise",
                          "BR-BIAT-9437101", True))
        self.assertEqual(l["titre"], "Remise de chèques BIAT n° 9437101")
        self.assertEqual(l["points"], [INTROUVABLE])
        self.assertIn("La BIAT a crédité 701,000 DT le 27/11", l["detail"])
        self.assertIn("colis Aramex de KARRAY SARRAH (colis 55087934495), commande SAL-ORD-2026-04731", l["piste"])
        self.assertIn("Encaissement Aramex", l["piste"])
        self.assertIn("n° 0000373", l["piste"])

    def test_remise_encaissee_ou_ecartee_plus_rien_a_corriger(self):
        # Le 7473932 : son cheque « introuvable » a ete designe a la main (« Encaisser sans bordereau »).
        encaissee = {"9437101": {**CREDIT["9437101"], "statut": "Identifie"}}
        self.assertEqual(R.a_corriger([B_9437101], encaissee, [], {}), [])
        ignoree = {"9437101": {**CREDIT["9437101"], "ignore": True}}
        self.assertEqual(R.a_corriger([B_9437101], ignoree, [], {}), [])

    def test_pas_encore_creditee_on_previent_quand_meme(self):
        (l,) = R.a_corriger([B_9437101], {}, [], {})
        self.assertIn("pas encore créditée par la BIAT", l["detail"])
        self.assertIsNone(l["piste"])                                    # aucun colis en attente connu

    def test_traites_lecture_ratee_et_bordereaux_sans_souci(self):
        traites = bordereau("0132601", "Traites", "Traite n° 0111790279 (3 965,000) : introuvable dans ERPNext.", date="2026-11-23")
        illisible = bordereau("7473940", statut="Échec")
        bon = bordereau("7473685")                                       # tout est retrouve
        en_cours = bordereau("7473941", statut="En attente", a_signaler="reste d'une ancienne lecture")
        especes = bordereau("5550001", "Espèces", a_signaler="x")
        lignes = R.a_corriger([illisible, bon, traites, en_cours, especes], {}, [], {})
        self.assertEqual([l["name"] for l in lignes], ["BR-BIAT-0132601", "BR-BIAT-7473940"])     # les plus anciennes d'abord
        self.assertEqual(lignes[0]["titre"], "Remise de traites BIAT n° 0132601")
        self.assertIn("une traite du bordereau n’est pas retrouvée", lignes[0]["detail"])
        self.assertIn("n’a pas pu lire le bordereau", lignes[1]["points"][0])
        # Une remise de traites ne se juge pas sur le credit d'une remise de cheques de meme numero.
        self.assertEqual(len(R.a_corriger([traites], {"0132601": {"statut": "Identifie"}}, [], {})), 1)

    def test_ne_plus_signaler(self):
        self.assertEqual(R.a_corriger([B_9437101], CREDIT, [], {"BIAT|bordereau_a_corriger|BR-BIAT-9437101": {"note": "vu"}}), [])


class TestPiste(unittest.TestCase):
    PIECES = [{"numero": "0000373", "montant": 701.0, "etat": "Introuvable"}]

    def test_un_seul_colis_de_ce_montant(self):
        self.assertIn("KARRAY SARRAH", R.piste_aramex(self.PIECES, ARAMEX))

    def test_deux_colis_du_meme_montant_on_ne_devine_pas(self):
        self.assertIsNone(R.piste_aramex(self.PIECES, ARAMEX + [{"client": "Autre", "montant": 701.0}]))

    def test_aucun_colis_ou_piece_retrouvee(self):
        self.assertIsNone(R.piste_aramex(self.PIECES, ARAMEX[1:]))
        self.assertIsNone(R.piste_aramex([{"numero": "1", "montant": 701.0, "etat": "Trouvé"}], ARAMEX))
        self.assertIsNone(R.piste_aramex([{"numero": "1", "montant": None, "etat": "Introuvable"}], ARAMEX))
        self.assertIsNone(R.piste_aramex([], ARAMEX))


class TestCartes(unittest.TestCase):
    def test_remise_carte_a_signaler_non_comptabilisee(self):
        (l,) = R.a_corriger([], {}, [carte("0017")], {})
        self.assertEqual((l["genre"], l["doctype"], l["name"], l["verifier"]),
                         (R.GENRE_CARTE, "BRS Remise TPE", "TPE-BIAT-20261125-01-0017", False))
        self.assertEqual(l["titre"], "Remise carte BIAT n° 0017")
        self.assertEqual(l["points"], ["1 transaction sans paiement dans ERPNext."])
        self.assertIn("La BIAT a crédité 2 757,200 DT le 26/11", l["detail"])

    def test_tout_est_bon_ou_deja_comptabilisee(self):
        self.assertEqual(R.a_corriger([], {}, [carte("0017", controle="Tout est bon")], {}), [])
        self.assertEqual(R.a_corriger([], {}, [carte("0017", ecriture="ACC-JV-2026-01106")], {}), [])
        self.assertEqual(R.a_corriger([], {}, [carte("0017", controle="En attente du relevé")], {}), [])

    def test_pas_encore_creditee_et_sans_phrase(self):
        (l,) = R.a_corriger([], {}, [carte("0018", credit=None, a_signaler=None)], {})
        self.assertIn("pas encore créditée", l["detail"])
        self.assertEqual(len(l["points"]), 1)

    def test_cheques_et_cartes_ensemble_par_date(self):
        lignes = R.a_corriger([B_9437101], CREDIT, [carte("0017")], {}, ARAMEX)
        self.assertEqual([l["genre"] for l in lignes], [R.GENRE_CARTE, R.GENRE_BORDEREAU])        # 26/11 puis 27/11


class TestEcartsZitouna(unittest.TestCase):
    """Sur Zitouna le meme cas prend la forme d'un ecart bloquant d'encaissement (cas reels d'octobre-novembre 2026)."""

    SANS_PIECE = {"name": "u5ug8vs2hb", "date": "2026-11-06 11:02:20", "flux": "cheque", "bon": "90028952", "suivi": "0000008",
                  "reference_bancaire": "FT26310YYLXY", "type_ecart": "Sans pièce", "client": "CLINIQUE DU CENTRE",
                  "montant_advice": 361.0, "encaissement": "ENC-06-11-2026-00002", "banque": "Zitouna"}

    def test_cheque_sans_piece_dans_un_brouillon(self):
        (l,) = R.a_corriger([], {}, [], {}, ecarts=[self.SANS_PIECE])
        self.assertEqual((l["cle"], l["genre"], l["route"]),
                         ("Zitouna|ecart_a_traiter|u5ug8vs2hb", R.GENRE_ECART, ["Form", "Encaissement Paiement", "ENC-06-11-2026-00002"]))
        self.assertEqual(l["titre"], "Remise de chèques Zitouna n° 90028952")
        self.assertEqual(l["points"], ["Chèque n° 0000008 de CLINIQUE DU CENTRE (361,000 DT) : aucun paiement dans ERPNext."])
        self.assertIn("ENC-06-11-2026-00002 reste en brouillon", l["detail"])
        self.assertFalse(l["verifier"])
        self.assertNotIn("pieces", l)
        self.assertNotIn("ecart", l)

    def test_la_piste_aramex_vaut_aussi_pour_zitouna(self):
        e = {**self.SANS_PIECE, "montant_advice": 701.0}
        (l,) = R.a_corriger([], {}, [], {}, ARAMEX, [e])
        self.assertIn("KARRAY SARRAH", l["piste"])
        (sans,) = R.a_corriger([], {}, [], {}, ARAMEX, [self.SANS_PIECE])
        self.assertIsNone(sans["piste"])

    def test_traite_sans_brouillon_delta_et_aramex(self):
        traite = {"name": "t1", "date": "2026-11-03", "flux": "traite", "bon": "FT26307YX9VX", "suivi": "011644580146",
                  "type_ecart": "Sans pièce", "montant_advice": 693.885, "encaissement": None}
        delta = {"name": "d1", "date": "2026-11-04", "flux": "cheque", "bon": "90028700", "suivi": "4001015",
                 "type_ecart": "Delta paiement", "client": "Baccari", "ref_paiement": "ACC-PAY-1", "montant_advice": 265.0,
                 "montant_piece": 440.0, "encaissement": "ENC-1"}
        aramex = {"name": "a1", "date": "2026-11-05", "flux": "aramex", "suivi": "55087934495", "reference_bancaire": "FT2632928ZYQ",
                  "type_ecart": "Sans pièce", "montant_advice": 120.0, "encaissement": "ENC-2"}
        t, d, a = R.a_corriger([], {}, [], {}, ARAMEX, [aramex, traite, delta])                # triees par date
        self.assertEqual((t["titre"], t["route"], t["name"]), ("Remise de traites Zitouna", [R.PAGE_IDENTIFICATION], "t1"))
        self.assertEqual(t["points"], ["Traite n° 011644580146 (693,885 DT) : aucun paiement dans ERPNext."])
        self.assertIn("aucun encaissement ne peut être préparé", t["detail"])
        self.assertEqual(d["points"], ["Chèque n° 4001015 de Baccari : 265,000 DT versés, 440,000 DT dans ERPNext (paiement ACC-PAY-1)."])
        self.assertEqual(a["titre"], "Virement Aramex (FT2632928ZYQ)")
        self.assertIsNone(a["piste"])                                    # la piste ne vaut que pour un cheque ou une traite

    def test_ne_plus_signaler_et_genre_d_une_cle(self):
        self.assertEqual(R.a_corriger([], {}, [], {"Zitouna|ecart_a_traiter|u5ug8vs2hb": {}}, ecarts=[self.SANS_PIECE]), [])
        self.assertEqual(R.genre_de("Zitouna|ecart_a_traiter|u5ug8vs2hb"), R.GENRE_ECART)
        self.assertEqual(R.genre_de("BIAT|bordereau_a_corriger|BR-BIAT-9437101"), R.GENRE_BORDEREAU)
        for autre in ("BIAT|remise_cheques|7473936", "BIAT|trou|7473935", "", None, "x|ecart_a_traiter", "a|b|c|d"):
            self.assertEqual(R.genre_de(autre), "")
