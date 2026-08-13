"""Tests de l'ingestion des certificats de retenue a la source (tej/certificats.py, tej/matricule.py).

Meme convention que les tests existants : `unittest.TestCase` pur, entrees injectees, aucun acces
reseau ni base. Les donnees ci-dessous sont RECOPIEES de l'export reel du 20/07/2026 — espaces
insecables et accents compris : ce sont ces caracteres-la qui cassaient le parsing.
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.tej import certificats as C
from bank_retenue_sync.tej import matricule as MF

# Matricule de la societe (nous, le beneficiaire).
NOUS = "1632013C"

# Formats de tax_id releves en base, tous censes designer un contribuable unique.
FORMATS_REELS = ["974450 / N / A / C / 000", "1648235/B/A/M/000", "1453894 Z",
                 "925641 / W / A / M  / 000", "1511801 C / A / M / 000", "988239/K"]


def ligne(reference="f52865c6-a415-4661-8a8b-9e2fd7c9b0d5", declarant="STE TAURUS",
          mf="1298092B", ht=5376.0, tva=0.0, rs=53.76, etat="REÇUE",
          jour=date(2026, 7, 8), beneficiaire=NOUS):
    return {"reference": reference, "declarant": declarant, "declarant_matricule": mf,
            "numero_chez_declarant": "07065", "date_paiement": jour, "etat_source": etat,
            "type_generation": "F", "total_ht": ht, "total_tva": tva,
            "total_brut": round(ht + tva, 3), "montant_retenue": rs,
            "fichier_declare": "8231605-1298092B-2026-07-1.xml",
            "beneficiaire_matricule": beneficiaire, "beneficiaire": "AQUA WORLD ET   SERVICING",
            "date_creation_certificat": date(2026, 7, 20)}


class StoreFaux:
    """Base simulee : trois methodes, comme le store reel. Aucun acces disque."""

    def __init__(self, docs=None):
        self.docs = docs or {}
        self.inserts, self.updates = [], []

    def get(self, reference):
        return self.docs.get(reference)

    def insert(self, valeurs):
        self.docs[valeurs["reference"]] = dict(valeurs)
        self.inserts.append(valeurs)
        return valeurs["reference"]

    def update(self, reference, valeurs):
        self.docs[reference].update(valeurs)
        self.updates.append((reference, valeurs))


class TestLectureDesMontants(unittest.TestCase):
    """Le piege le plus couteux du fichier : un separateur de milliers invisible."""

    def test_espace_insecable_dans_un_montant(self):
        """« 5 376.000 » avec un insecable : c'est la forme reelle de TOUTES les lignes a
        quatre chiffres de l'export."""
        self.assertEqual(C._num_tej("5\xa0376.000"), 5376.0)

    def test_virgule_decimale(self):
        self.assertEqual(C._num_tej("2\xa0524,417"), 2524.417)

    def test_montant_illisible_leve_au_lieu_de_rendre_zero(self):
        """Un zero silencieux ferait disparaitre la retenue en laissant le certificat valide :
        l'erreur deviendrait invisible. Une ligne illisible doit etre comptee en echec."""
        with self.assertRaises(ValueError):
            C._num_tej("montant absent")

    def test_vide_vaut_zero(self):
        self.assertEqual(C._num_tej(None), 0.0)
        self.assertEqual(C._num_tej(""), 0.0)


class TestTexteEtDate(unittest.TestCase):
    def test_espaces_multiples_internes_reduits(self):
        """« AQUA WORLD ET   SERVICING » et « AQUA WORLD ET SERVICING » doivent donner le meme
        libelle, sinon la meme societe produit deux alias distincts."""
        self.assertEqual(C._texte("AQUA WORLD ET   SERVICING"), "AQUA WORLD ET SERVICING")
        self.assertEqual(C._texte("SOCIETE FM WATER      PLUS"), "SOCIETE FM WATER PLUS")

    def test_date_jour_mois_annee_a_tirets(self):
        from bank_retenue_sync.bank.movements import _to_date

        self.assertEqual(_to_date("08-07-2026"), date(2026, 7, 8))

    def test_le_format_iso_garde_la_priorite(self):
        """Non-regression : les trois autres flux datent leurs lignes en ISO."""
        from bank_retenue_sync.bank.movements import _to_date

        self.assertEqual(_to_date("2026-07-08"), date(2026, 7, 8))
        self.assertEqual(_to_date("08/07/2026"), date(2026, 7, 8))


class TestEtats(unittest.TestCase):
    """LA regle du module : un certificat annule n'est pas une retenue."""

    def test_recue(self):
        self.assertEqual(C.normaliser_etat("REÇUE"), C.ETAT_RECUE)

    def test_annule_accentue(self):
        self.assertEqual(C.normaliser_etat("ANNULÉ"), C.ETAT_ANNULE)

    def test_rectifie(self):
        self.assertEqual(C.normaliser_etat("RECTIFIE"), C.ETAT_RECTIFIE)

    def test_libelle_inconnu_n_est_jamais_devine(self):
        self.assertEqual(C.normaliser_etat("EN COURS DE TRAITEMENT"), C.ETAT_AUTRE)

    def test_un_certificat_annule_n_est_jamais_exploitable(self):
        self.assertFalse(C.est_exploitable(ligne(etat="ANNULÉ")))

    def test_un_certificat_rectifie_reste_exploitable(self):
        """Le montant corrige est le bon : l'ignorer perdrait la retenue. C'est sa revue qui est
        requise, pas son exclusion."""
        self.assertTrue(C.est_exploitable(ligne(etat="RECTIFIE")))


class TestAnomalies(unittest.TestCase):
    def test_assiette_nulle_est_une_anomalie_pas_un_taux_de_999(self):
        """Ligne reelle de l'export : HT et TVA a zero, retenue non nulle."""
        l = ligne(ht=0.0, tva=0.0, rs=53.76)
        self.assertIn("assiette nulle", C.anomalie(l))
        self.assertIsNone(C.taux(l))

    def test_retenue_superieure_a_l_assiette(self):
        self.assertIn("superieure", C.anomalie(ligne(ht=100.0, rs=200.0)))

    def test_taux_inhabituel_signale(self):
        self.assertIn("taux inhabituel", C.anomalie(ligne(ht=1000.0, rs=50.0)))

    def test_un_pour_cent_du_ttc_ne_declenche_rien(self):
        """La regle du marche public : 1 % du TTC, TVA comprise."""
        self.assertIsNone(C.anomalie(ligne(ht=2524.417, tva=365.582, rs=28.899)))

    def test_certificat_d_un_autre_beneficiaire_est_rejete(self):
        """Sans ce controle, un export mal filtre injecterait les certificats d'un tiers."""
        l = ligne(beneficiaire="0438574N")
        self.assertIn("autre beneficiaire", C.anomalie(l, matricule_beneficiaire=NOUS))

    def test_notre_matricule_sous_une_autre_forme_est_accepte(self):
        l = ligne(beneficiaire="1632013 C / A / M / 000")
        self.assertIsNone(C.anomalie(l, matricule_beneficiaire=NOUS))


class TestPerimetre(unittest.TestCase):
    """L'outil suit 2026 : reprendre 2025 rouvrirait des exercices clos."""

    def test_2025_est_hors_perimetre(self):
        self.assertTrue(C.hors_perimetre(ligne(jour=date(2025, 12, 31))))

    def test_2026_est_dans_le_perimetre(self):
        self.assertFalse(C.hors_perimetre(ligne(jour=date(2026, 2, 11))))

    def test_hors_perimetre_n_est_jamais_exploitable(self):
        self.assertFalse(C.est_exploitable(ligne(jour=date(2025, 10, 1))))

    def test_sans_date_on_ne_prend_pas_le_risque(self):
        self.assertTrue(C.hors_perimetre(ligne(jour=None)))


class TestMatricule(unittest.TestCase):
    def test_les_formats_reels_donnent_une_cle_stable(self):
        attendus = ["0974450N", "1648235B", "1453894Z", "0925641W", "1511801C", "0988239K"]
        self.assertEqual([MF.normaliser(v) for v in FORMATS_REELS], attendus)

    def test_la_forme_compacte_du_portail_rejoint_la_saisie_erpnext(self):
        """C'est tout l'objet du module : « 1298092B » au certificat, « 1298092 / B / A / M / 000 »
        en base, et les deux doivent se reconnaitre."""
        self.assertTrue(MF.meme_contribuable("1298092B", "1298092 / B / A / M / 000"))

    def test_un_matricule_sans_lettre_cle_n_est_jamais_indexe(self):
        """La lettre valide les chiffres. Sans elle, deux societes se confondraient."""
        self.assertIsNone(MF.normaliser("1298092"))
        self.assertIsNone(MF.normaliser(""))
        self.assertIsNone(MF.normaliser(None))

    def test_index_conserve_les_homonymes(self):
        """Deux fiches client pour un meme matricule : l'ambiguite doit remonter, pas disparaitre."""
        idx = MF.index([("1298092B", "CLIENT-A"), ("1298092 B / A / M", "CLIENT-B"),
                        ("0005270T", "CLIENT-C")])
        self.assertEqual(sorted(idx["1298092B"]), ["CLIENT-A", "CLIENT-B"])
        self.assertEqual(idx["0005270T"], ["CLIENT-C"])


class TestUpsertIdempotent(unittest.TestCase):
    """« Re-ingerer une periode chevauchante doit etre un no-op » : la propriete la plus
    importante du flux, celle qui autorise un cron quotidien sans surveillance."""

    def test_premiere_ingestion_cree(self):
        store = StoreFaux()
        r = C.upsert_certificats([ligne(), ligne(reference="autre", mf="0438574N")], store=store,
                                 matricule_beneficiaire=NOUS)
        self.assertEqual((r["crees"], r["revus"]), (2, 0))

    def test_reingerer_le_meme_export_ne_cree_rien(self):
        store = StoreFaux()
        lignes = [ligne(), ligne(reference="autre")]
        C.upsert_certificats(lignes, store=store, matricule_beneficiaire=NOUS)
        r = C.upsert_certificats(lignes, store=store, matricule_beneficiaire=NOUS)
        self.assertEqual((r["crees"], r["revus"]), (0, 2))

    def test_un_client_pose_a_la_main_survit_a_une_reingestion(self):
        """Le point ou l'utilisateur perdrait confiance si la machine repassait derriere lui."""
        store = StoreFaux()
        C.upsert_certificats([ligne()], store=store, matricule_beneficiaire=NOUS)
        ref = ligne()["reference"]
        store.docs[ref].update({"customer": "CLIENT-CHOISI", "match_status": "Manually Matched"})
        C.upsert_certificats([ligne(rs=53.77)], store=store, matricule_beneficiaire=NOUS)
        self.assertEqual(store.docs[ref]["customer"], "CLIENT-CHOISI")
        self.assertEqual(store.docs[ref]["match_status"], "Manually Matched")
        self.assertEqual(store.docs[ref]["montant_retenue"], 53.77)   # la source, elle, suit

    def test_un_certificat_annule_est_ingere_mais_ecarte(self):
        store = StoreFaux()
        r = C.upsert_certificats([ligne(etat="ANNULÉ")], store=store, matricule_beneficiaire=NOUS)
        self.assertEqual(r["annules"], 1)
        self.assertEqual(store.docs[ligne()["reference"]]["match_status"], "Ignore")

    def test_une_annulation_posterieure_signale_sans_rien_defaire(self):
        """Retirer un lien comptable automatiquement serait irreversible : on previent, on n'agit
        pas."""
        store = StoreFaux()
        C.upsert_certificats([ligne()], store=store, matricule_beneficiaire=NOUS)
        ref = ligne()["reference"]
        store.docs[ref].update({"match_status": "Auto Matched", "payment_entry": "ACC-PAY-X"})
        r = C.upsert_certificats([ligne(etat="ANNULÉ")], store=store, matricule_beneficiaire=NOUS)
        self.assertEqual(r["signales"], [ref])
        self.assertEqual(store.docs[ref]["revue_requise"], 1)
        self.assertEqual(store.docs[ref]["payment_entry"], "ACC-PAY-X")   # rien n'a ete defait

    def test_une_rectification_demande_une_revue(self):
        store = StoreFaux()
        C.upsert_certificats([ligne()], store=store, matricule_beneficiaire=NOUS)
        C.upsert_certificats([ligne(etat="RECTIFIE")], store=store, matricule_beneficiaire=NOUS)
        self.assertEqual(store.docs[ligne()["reference"]]["revue_requise"], 1)

    def test_une_ligne_sans_reference_est_un_echec_pas_un_silence(self):
        store = StoreFaux()
        r = C.upsert_certificats([ligne(reference="")], store=store, matricule_beneficiaire=NOUS)
        self.assertEqual((r["crees"], r["echecs"]), (0, 1))

    def test_les_compteurs_distinguent_perimetre_et_anomalie(self):
        store = StoreFaux()
        r = C.upsert_certificats(
            [ligne(), ligne(reference="vieux", jour=date(2025, 5, 1)),
             ligne(reference="bizarre", ht=0.0, tva=0.0, rs=10.0)],
            store=store, matricule_beneficiaire=NOUS)
        self.assertEqual(r["hors_perimetre"], 1)
        self.assertEqual(r["anomalies"], 1)
        self.assertEqual(r["crees"], 3)          # tout est ingere, rien n'est perdu


class TestEmpreinte(unittest.TestCase):
    def test_le_meme_fichier_donne_la_meme_empreinte(self):
        """C'est ce qui permet de reconnaitre un export inchange et de ne rien refaire."""
        self.assertEqual(C.empreinte(b"abc"), C.empreinte(b"abc"))
        self.assertNotEqual(C.empreinte(b"abc"), C.empreinte(b"abd"))


class TestTachesPlanifiees(unittest.TestCase):
    """Garde-fou de regression : trois taches referencaient `orchestrator` sans l'importer.

    Le `NameError` etait avale par `_safe()`, si bien que trois crons sur huit — dont les cinq
    verifications bancaires quotidiennes — ne faisaient rien, sans autre trace qu'une Error Log.
    """

    def test_chaque_tache_importe_orchestrator_localement(self):
        """`orchestrator` doit etre une variable de cellule (import local capture par la lambda),
        jamais un global : c'est exactement ce qui distinguait les taches vivantes des inertes."""
        from bank_retenue_sync.tasks import daily

        fautives = []
        for nom in dir(daily):
            fn = getattr(daily, nom)
            code = getattr(fn, "__code__", None)
            if (nom.startswith("_") or not callable(fn) or code is None
                    or getattr(fn, "__module__", "") != daily.__name__):
                continue
            if "orchestrator" in code.co_names and "orchestrator" not in (
                    code.co_cellvars + code.co_varnames):
                fautives.append(nom)
        self.assertEqual(fautives, [], "import local d'orchestrator manquant : la tache est inerte")


if __name__ == "__main__":
    unittest.main()
