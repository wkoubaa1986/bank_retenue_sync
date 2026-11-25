"""Relance des bordereaux de remise manquants (25/11/2026) : remises orphelines, remises de traites, trous dans
les numeros, lignes ecartees. Pur. Les cas reprennent la BIAT au 25/11/2026."""
import unittest

from bank_retenue_sync.bank import bordereaux_manquants as M


def remise(numero, date, montant, statut="Identifie"):
    return {"numero": numero, "date": date, "montant": montant, "statut": statut}


# La BIAT au 25/11/2026 : cinq remises, deux bordereaux photographies, une remise orpheline, un numero saute.
REMISES = [remise("7473685", "2026-11-12", 3010.127), remise("7473932", "2026-11-16", 581.0),
           remise("7473933", "2026-11-16", 1523.125), remise("7473934", "2026-11-19", 701.0),
           remise("7473936", "2026-11-25", 3000.0, "Orphelin")]
BORDEREAUX = [{"numero": "7473685", "nature": "Chèques", "date_remise": "2026-11-09"},
              {"numero": "7473934", "nature": "Chèques", "date_remise": "2026-11-18"}]
EFFETS = [{"numero": "132601", "date": "2026-11-24", "montant": 4.0}]


class TestTrous(unittest.TestCase):
    def test_un_numero_saute(self):
        self.assertEqual(M.trous(["7473932", "7473933", "7473934", "7473936"]), [("7473935", "7473936")])

    def test_un_autre_carnet_n_est_pas_un_trou(self):
        # 7473685 puis 7473932 : 246 numeros d'ecart, un autre carnet.
        self.assertEqual(M.trous(["7473685", "7473932"]), [])
        self.assertEqual(M.trous(["100", "104"]), [("101", "104"), ("102", "104"), ("103", "104")])   # TROU_MAX = 3
        self.assertEqual(M.trous(["100", "105"]), [])

    def test_zeros_de_tete_doublons_et_vides(self):
        self.assertEqual(M.trous(["0012", "0014", "0012", "", None]), [("0013", "0014")])


class TestManquants(unittest.TestCase):
    def test_la_biat_au_25_novembre(self):
        lignes = M.manquants(REMISES, EFFETS, BORDEREAUX, {})
        self.assertEqual([(l["genre"], l["numero"]) for l in lignes],
                         [("remise_traites", "132601"), ("trou", "7473935"), ("remise_cheques", "7473936")])
        cheques = lignes[2]
        self.assertEqual(cheques["cle"], "BIAT|remise_cheques|7473936")
        self.assertIn("3 000,000 DT", cheques["detail"])
        self.assertIn("25/11", cheques["detail"])
        self.assertEqual(lignes[1]["date"], "2026-11-25")        # le trou se date de la remise qui le revele
        self.assertEqual(M.attendu(lignes[0]), {"banque": "BIAT", "numero": "132601", "nature": "Traites"})
        self.assertEqual(M.attendu(cheques), {"banque": "BIAT", "numero": "7473936", "nature": "Chèques"})

    def test_le_bordereau_collecte_eteint_la_relance(self):
        collectes = BORDEREAUX + [{"numero": "7473936", "nature": "Chèques", "date_remise": "2026-11-24"}]
        self.assertNotIn("7473936", [l["numero"] for l in M.manquants(REMISES, [], collectes, {})])
        # Collecte avant meme d'etre credite, il bouche aussi le trou.
        collectes.append({"numero": "7473935", "nature": "Chèques", "date_remise": "2026-11-22"})
        self.assertEqual(M.manquants(REMISES, [], collectes, {}), [])

    def test_seule_une_remise_orpheline_est_relancee(self):
        # Encaissee « sans bordereau » (BR:<n°> pose a la main) ou en brouillon : rien a relancer.
        for statut in ("Identifie", "Identifie non comptabilise", "A verifier", None):
            self.assertEqual(M.manquants([remise("7473936", "2026-11-25", 3000.0, statut)], [], [], {}), [])

    def test_ligne_ecartee(self):
        ecartes = {"BIAT|trou|7473935": {"motif": "Annulé ou abîmé (pas de remise)"}}
        self.assertNotIn("trou", [l["genre"] for l in M.manquants(REMISES, [], BORDEREAUX, ecartes)])

    def test_remise_de_traites_couverte(self):
        # Par son numero…
        self.assertEqual(M.manquants([], EFFETS, [{"numero": "132601", "nature": "Traites", "date_remise": "2026-11-21"}], {}), [])
        # … ou par un bordereau de traites collecte dans les jours qui precedent la commission (autre numero imprime).
        proche = [{"numero": "55501", "nature": "Traites", "date_remise": "2026-11-20"}]
        self.assertEqual(M.manquants([], EFFETS, proche, {}), [])
        loin = [{"numero": "55501", "nature": "Traites", "date_remise": "2026-10-20"}]
        self.assertEqual(len(M.manquants([], EFFETS, loin, {})), 1)
        # Un bordereau ne couvre qu'une seule remise.
        deux = EFFETS + [{"numero": "132602", "date": "2026-11-25", "montant": 4.0}]
        self.assertEqual([l["numero"] for l in M.manquants([], deux, proche, {})], ["132602"])

    def test_commission_sans_numero(self):
        lignes = M.manquants([], [{"numero": "", "date": "2026-11-24", "montant": 4.0}], [], {})
        self.assertEqual((lignes[0]["cle"], lignes[0]["titre"]), ("BIAT|remise_traites|du 2026-11-24", "Remise de traites BIAT"))

    def test_les_traites_ne_comptent_pas_dans_la_serie_des_cheques(self):
        # Un bordereau de traites n'allonge pas la serie : 7473937 (traites) ne fait pas de 7473936… un voisin.
        traites = [{"numero": "7473939", "nature": "Traites", "date_remise": "2026-11-20"}]
        self.assertNotIn("7473937", [l["numero"] for l in M.manquants(REMISES, [], BORDEREAUX + traites, {})])
        # Mais un numero collecte, quelle que soit sa nature, n'est pas perdu.
        mal_lu = [{"numero": "7473935", "nature": "Traites", "date_remise": "2026-11-20"}]
        self.assertNotIn("7473935", [l["numero"] for l in M.manquants(REMISES, [], BORDEREAUX + mal_lu, {})])


if __name__ == "__main__":
    unittest.main()
