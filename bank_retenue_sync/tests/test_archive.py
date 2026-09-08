"""Ce que le comptable a recu, et ce qui manquait : le manifeste, sa lecture, la comparaison.

`facturation/archive.py` est pur — il recoit des octets et des listes, il rend des listes. Ces
tests le prennent au mot : ni site, ni base, ni fichier sur disque. Les ZIP sont construits en
memoire, les classeurs aussi.

Ce qui se joue ici est un compte remis a un tiers : une manquante inventee envoie chercher une
piece qui est deja partie, une manquante oubliee laisse un trou chez le comptable. D ou les cas
limites — doublons, lignes TOTAL, sous-blocs de retards, archive sans manifeste.
"""
import io
import json
import unittest
import zipfile

from bank_retenue_sync.facturation import archive as A


def _ligne(nom, date="2026-07-05", tiers="Sté ALPHA", ttc=120.0, ref="FA-1",
           categorie="Fournitures", dt="Purchase Invoice"):
    return {"document_type": dt, "document_name": nom, "date": date, "tiers": tiers,
            "ttc": ttc, "reference_export": ref, "categorie": categorie}


def _donnees(lignes_par_bloc):
    return {"blocs": [{"cle": cle, "titre": titre, "lignes": lignes}
                      for cle, titre, lignes in lignes_par_bloc]}


def _zip(membres):
    flux = io.BytesIO()
    with zipfile.ZipFile(flux, "w", zipfile.ZIP_DEFLATED) as z:
        for nom, contenu in membres.items():
            z.writestr(nom, contenu)
    return flux.getvalue()


def _classeur(lignes):
    """Un « Liste des Charges … .xlsx » aux colonnes de `dossier._feuille_charges`."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Charges"
    for ligne in lignes:
        ws.append(list(ligne))
    flux = io.BytesIO()
    wb.save(flux)
    return flux.getvalue()


def _rang(ref, date, tiers, categorie, ttc):
    """Une ligne de charge du classeur : douze colonnes, TTC en dixieme."""
    return [ref, date, tiers, categorie, "Virement", 100.0, 0.0, 20.0, 20.0, ttc, 0.0, "piece.pdf"]


class TestManifeste(unittest.TestCase):
    """Le manifeste doit etre serialisable, complet, et tenir les retards a part."""

    def _manifeste(self):
        donnees = _donnees([
            ("depenses", "Dépenses", [_ligne("JV-1", dt="Journal Entry", ref="CHQ-1")]),
            ("achats", "Achats", [_ligne("PI-1")]),
            ("retenues", "Retenues / Ventes", [_ligne("PE-1", dt="Payment Entry", ref="RAS-1")]),
        ])
        retards = [{"mois": "2026-06",
                    "lignes": [_ligne("PI-JUIN", date="2026-06-12")]}]
        return A.manifeste("2026-07", donnees, retards, genere_le="2026-08-01 10:00:00")

    def test_les_trois_blocs_sont_nommes_piece_par_piece(self):
        m = self._manifeste()
        self.assertEqual([(e["document_type"], e["document_name"], e["bloc"])
                          for e in m["charges"]],
                         [("Journal Entry", "JV-1", "depenses"),
                          ("Purchase Invoice", "PI-1", "achats"),
                          ("Payment Entry", "PE-1", "retenues")])

    def test_les_retards_portent_leur_mois_d_origine_et_restent_hors_des_charges(self):
        m = self._manifeste()
        self.assertEqual([e["document_name"] for e in m["retards"]], ["PI-JUIN"])
        self.assertEqual(m["retards"][0]["mois_origine"], "2026-06")
        self.assertNotIn("PI-JUIN", [e["document_name"] for e in m["charges"]])

    def test_le_manifeste_est_serialisable_et_se_relit_a_l_identique(self):
        m = self._manifeste()
        relu = json.loads(A.serialiser(m).decode("utf-8"))
        self.assertEqual(relu, m)
        self.assertEqual(relu["version"], A.VERSION)
        self.assertEqual(relu["mois"], "2026-07")

    def test_le_chemin_est_celui_du_dossier_racine_du_zip(self):
        self.assertEqual(A.chemin_manifeste("2026-07"), "2026-07/manifeste.json")


class TestValidationDuManifeste(unittest.TestCase):
    """Un manifeste incomplet est PIRE qu absent : il se lit comme une archive vide.

    `{"version": 1}` sans cle `charges` rendrait « zero charge dans le ZIP » — toutes les charges
    du mois ressortiraient manquantes, et l ecran enverrait rattraper un dossier complet. On exige
    donc la structure entiere avant de s y fier ; a defaut, le classeur reprend la main.
    """

    def _bon(self, **remplace):
        m = {"version": A.VERSION, "mois": "2026-07",
             "charges": [{"document_type": "Purchase Invoice", "document_name": "PI-1",
                          "date": "2026-07-05", "tiers": "A", "ttc": 10.0}],
             "retards": []}
        m.update(remplace)
        return m

    def test_un_manifeste_complet_est_accepte(self):
        self.assertIsNone(A.defaut_du_manifeste(self._bon(), "2026-07"))

    def test_un_manifeste_sans_charge_est_accepte_un_mois_peut_etre_vide(self):
        self.assertIsNone(A.defaut_du_manifeste(self._bon(charges=[]), "2026-07"))

    def test_version_seule_sans_charges_est_refusee(self):
        # Le cas qui motive tout ce garde-fou.
        self.assertEqual(A.defaut_du_manifeste({"version": 1}, "2026-07"), A.DEFAUT_MOIS)
        self.assertEqual(A.defaut_du_manifeste({"version": 1, "mois": "2026-07"}, "2026-07"),
                         A.DEFAUT_CHARGES)

    def test_rien_du_tout_est_un_manifeste_absent(self):
        for vide in (None, {}, [], "", 0):
            self.assertEqual(A.defaut_du_manifeste(vide, "2026-07"), A.DEFAUT_ABSENT)

    def test_une_version_inconnue_ou_illisible_est_refusee(self):
        for version in (None, "1", 0, -1, A.VERSION + 1, True, 1.0):
            self.assertEqual(A.defaut_du_manifeste(self._bon(version=version), "2026-07"),
                             A.DEFAUT_VERSION, "version %r" % (version,))

    def test_le_manifeste_d_un_autre_mois_est_refuse(self):
        # Comparer le dossier de juillet a l archive d aout rendrait deux mois entiers en ecart.
        self.assertEqual(A.defaut_du_manifeste(self._bon(mois="2026-08"), "2026-07"),
                         A.DEFAUT_MOIS)
        self.assertEqual(A.defaut_du_manifeste(self._bon(mois=""), "2026-07"), A.DEFAUT_MOIS)

    def test_sans_mois_attendu_seule_la_presence_du_mois_est_exigee(self):
        self.assertIsNone(A.defaut_du_manifeste(self._bon(mois="2026-08")))

    def test_des_charges_qui_ne_sont_pas_une_liste_sont_refusees(self):
        for charges in (None, {}, "PI-1", 3):
            self.assertEqual(A.defaut_du_manifeste(self._bon(charges=charges), "2026-07"),
                             A.DEFAUT_CHARGES, "charges %r" % (charges,))
        self.assertEqual(A.defaut_du_manifeste(self._bon(retards="x"), "2026-07"),
                         A.DEFAUT_CHARGES)

    def test_une_charge_sans_identifiant_de_piece_est_refusee(self):
        # La comparaison par piece ne s appuie que la-dessus : sans nom, elle inventerait.
        for charge in ({"document_type": "Purchase Invoice"}, {"document_name": "PI-1"},
                       {"document_type": "", "document_name": "PI-1"}, {}, "PI-1", None):
            self.assertEqual(A.defaut_du_manifeste(self._bon(charges=[charge]), "2026-07"),
                             A.DEFAUT_PIECES, "charge %r" % (charge,))

    def test_le_manifeste_que_la_constitution_ecrit_est_toujours_accepte(self):
        donnees = _donnees([("achats", "Achats", [_ligne("PI-1")]),
                            ("depenses", "Dépenses", [_ligne("JV-1", dt="Journal Entry")])])
        retards = [{"mois": "2026-06", "lignes": [_ligne("PI-JUIN", date="2026-06-02")]}]
        m = A.manifeste("2026-07", donnees, retards, genere_le="2026-08-01 10:00:00")
        self.assertIsNone(A.defaut_du_manifeste(json.loads(A.serialiser(m)), "2026-07"))


class TestNomDArchive(unittest.TestCase):
    """La seule chose qui protege les fichiers prives du site : le nom attendu, et lui seul."""

    def test_le_dossier_du_mois_demande_est_accepte(self):
        self.assertTrue(A.est_archive_du_mois(
            "Dossier facturation 2026-07 (20260801-1030).zip", "2026-07"))

    def test_le_dossier_d_un_autre_mois_est_refuse(self):
        self.assertFalse(A.est_archive_du_mois(
            "Dossier facturation 2026-08 (20260901-1030).zip", "2026-07"))

    def test_un_prefixe_de_mois_ne_suffit_pas(self):
        # Sans l espace exige apres le mois, « 2026-1 » attraperait le dossier de 2026-10.
        self.assertFalse(A.est_archive_du_mois(
            "Dossier facturation 2026-10 (20261101-1030).zip", "2026-1"))

    def test_tout_autre_fichier_prive_est_refuse(self):
        for nom in ("sauvegarde-site-database.sql.gz", "Bulletin de paie.pdf",
                    "dossier facturation 2026-07 (x).zip",
                    " Dossier facturation 2026-07 (x).zip",
                    "Dossier facturation 2026-07.zip", "", None):
            self.assertFalse(A.est_archive_du_mois(nom, "2026-07"), "nom %r" % (nom,))

    def test_sans_mois_rien_n_est_accepte(self):
        for mois in ("", None):
            self.assertFalse(A.est_archive_du_mois(
                "Dossier facturation 2026-07 (20260801-1030).zip", mois))


class TestLectureDuManifeste(unittest.TestCase):
    """Lire le manifeste d un ZIP en memoire — et repondre None quand il n y en a pas."""

    def test_un_zip_avec_manifeste_le_rend(self):
        m = {"version": 1, "mois": "2026-07", "charges": [], "retards": []}
        octets = _zip({"2026-07/manifeste.json": A.serialiser(m),
                       "2026-07/Facturation 2026-07.xlsx": b"nimporte quoi"})
        self.assertEqual(A.lire_manifeste(octets), m)

    def test_un_zip_sans_manifeste_rend_none(self):
        octets = _zip({"2026-07/Liste des Charges 2026-07.xlsx": b"x"})
        self.assertIsNone(A.lire_manifeste(octets))

    def test_un_manifeste_illisible_vaut_un_manifeste_absent(self):
        # Un JSON tronque ne doit pas faire echouer la comparaison : on retombe sur le classeur.
        octets = _zip({"2026-07/manifeste.json": b'{"charges": ['})
        self.assertIsNone(A.lire_manifeste(octets))

    def test_seules_les_charges_du_mois_sortent_du_manifeste(self):
        m = {"charges": [{"document_type": "Purchase Invoice", "document_name": "PI-1",
                          "date": "2026-07-05", "tiers": "A", "ttc": 10}],
             "retards": [{"document_type": "Purchase Invoice", "document_name": "PI-JUIN",
                          "date": "2026-06-05", "tiers": "B", "ttc": 20}]}
        self.assertEqual([e["document_name"] for e in A.entrees_du_manifeste(m)], ["PI-1"])


class TestLectureDuClasseur(unittest.TestCase):
    """Le repli des archives d avant : relire « Liste des Charges … » sans y prendre un total."""

    def _octets(self, lignes):
        return _zip({"2026-07/Liste des Charges 2026-07.xlsx": _classeur(lignes),
                     "2026-07/Facturation 2026-07.xlsx": _classeur([["Nom Facture"]])})

    def _classeur_complet(self):
        return [
            ["Référence export", "Date", "Tiers", "Catégorie", "Mode", "Valeur HT", "TVA 7%",
             "TVA 19%", "TVA", "Valeur TTC", "Retenue", "Justificatifs"],
            [],
            ["DÉPENSES", "1 ligne(s)"],
            _rang("CHQ-1", "2026-07-03", "Sté ALPHA", "Fournitures", 120.0),
            ["TOTAL Dépenses", "", "", "", "", 100.0, "", "", 20.0, 120.0, 0.0, "0 sans"],
            [],
            ["ACHATS", "1 ligne(s)"],
            _rang("FA-9", "2026-07-11", "Sté BÊTA", "Marchandises", 999.5),
            ["TOTAL Achats", "", "", "", "", 830.0, "", "", 169.5, 999.5, 0.0, "0 sans"],
            [],
            ["TOTAL GÉNÉRAL", "2 ligne(s)", "", "", "", 930.0, "", "", 189.5, 1119.5, 0.0, ""],
            [],
            ["RETARDS DE JUIN 2026", "1 ligne(s) — hors total du mois"],
            _rang("FA-JUIN", "2026-06-20", "Sté GAMMA", "Marchandises", 400.0),
            ["SOUS-TOTAL RETARDS juin 2026", "", "", "", "", 336.0, "", "", 64.0, 400.0, 0.0, ""],
        ]

    def test_seules_les_lignes_de_charge_du_mois_sont_rendues(self):
        lignes = A.lire_classeur_charges(self._octets(self._classeur_complet()))
        self.assertEqual([(e["reference"], e["ttc"]) for e in lignes],
                         [("CHQ-1", 120.0), ("FA-9", 999.5)])

    def test_ni_total_ni_total_general_ne_passent_pour_une_charge(self):
        lignes = A.lire_classeur_charges(self._octets(self._classeur_complet()))
        self.assertNotIn(1119.5, [e["ttc"] for e in lignes])
        self.assertNotIn(400.0, [e["ttc"] for e in lignes])

    def test_le_bloc_retards_est_ecarte_jusqu_a_la_fin_du_classeur(self):
        # Une piece de juin remise avec juillet est dans le ZIP sans etre une charge de juillet :
        # la compter ferait ressortir tout juin en « disparue ».
        lignes = A.lire_classeur_charges(self._octets(self._classeur_complet()))
        self.assertNotIn("Sté GAMMA", [e["tiers"] for e in lignes])

    def test_la_date_et_le_tiers_sont_lus_tels_quels(self):
        lignes = A.lire_classeur_charges(self._octets(self._classeur_complet()))
        self.assertEqual(lignes[0]["date"], "2026-07-03")
        self.assertEqual(lignes[0]["tiers"], "Sté ALPHA")
        self.assertEqual(lignes[0]["categorie"], "Fournitures")

    def test_un_zip_sans_classeur_de_charges_rend_none(self):
        octets = _zip({"2026-07/Caisse espèces 2026-07.xlsx": _classeur([["Indicateur"]])})
        self.assertIsNone(A.lire_classeur_charges(octets))


class TestComparaisonParPiece(unittest.TestCase):
    """Avec un manifeste, la comparaison est une difference d ensembles : exacte."""

    def _comparer(self, mois, archive):
        return A.comparer([A.entree(l) for l in mois], [A.entree(l) for l in archive],
                          A.METHODE_MANIFESTE)

    def test_une_charge_saisie_apres_l_envoi_ressort_manquante(self):
        r = self._comparer([_ligne("PI-1"), _ligne("PI-2")], [_ligne("PI-1")])
        self.assertEqual([e["document_name"] for e in r["manquantes"]], ["PI-2"])
        self.assertEqual(r["disparues"], [])

    def test_une_piece_de_l_archive_disparue_du_mois_sort_a_part(self):
        r = self._comparer([_ligne("PI-1")], [_ligne("PI-1"), _ligne("PI-ANNULEE")])
        self.assertEqual(r["manquantes"], [])
        self.assertEqual([e["document_name"] for e in r["disparues"]], ["PI-ANNULEE"])

    def test_un_montant_corrige_depuis_l_envoi_ne_fait_pas_une_manquante(self):
        # C est tout l interet de la cle : la piece est la meme, seul son montant a bouge.
        r = self._comparer([_ligne("PI-1", ttc=999.0)], [_ligne("PI-1", ttc=120.0)])
        self.assertEqual(r["manquantes"], [])
        self.assertEqual(r["disparues"], [])

    def test_le_total_des_manquantes_est_rendu(self):
        r = self._comparer([_ligne("PI-1", ttc=120.0), _ligne("PI-2", ttc=30.5)], [])
        self.assertEqual(r["totaux_manquantes"], {"nombre": 2, "ttc": 150.5})
        self.assertEqual(r["nb_mois"], 2)
        self.assertEqual(r["nb_archive"], 0)

    def test_comparer_un_zip_a_son_propre_mois_ne_rend_rien(self):
        donnees = _donnees([("achats", "Achats", [_ligne("PI-1"), _ligne("PI-2")])])
        m = A.manifeste("2026-07", donnees)
        r = A.comparer(A.entrees_des_blocs(donnees), A.entrees_du_manifeste(m),
                       A.METHODE_MANIFESTE)
        self.assertEqual((r["manquantes"], r["disparues"]), ([], []))


class TestComparaisonParEmpreinte(unittest.TestCase):
    """Sans manifeste : (date, tiers, TTC), en MULTI-ENSEMBLE — deux lignes jumelles font deux."""

    def _comparer(self, mois, archive):
        return A.comparer([A.entree(l) for l in mois], [A.entree(l) for l in archive],
                          A.METHODE_EMPREINTE)

    def test_la_meme_ligne_sous_un_autre_nom_de_document_est_reconnue(self):
        # Le classeur ne porte aucun nom de document : seule l empreinte peut rapprocher.
        archive = [{"date": "2026-07-05", "tiers": "Sté ALPHA", "ttc": 120.0, "reference": "FA-1"}]
        r = self._comparer([_ligne("PI-1")], archive)
        self.assertEqual((r["manquantes"], r["disparues"]), ([], []))

    def test_deux_lignes_jumelles_et_une_seule_dans_l_archive_laissent_une_manquante(self):
        mois = [_ligne("PI-1"), _ligne("PI-2")]  # meme date, meme tiers, meme TTC
        archive = [{"date": "2026-07-05", "tiers": "Sté ALPHA", "ttc": 120.0}]
        r = self._comparer(mois, archive)
        self.assertEqual(len(r["manquantes"]), 1)
        self.assertEqual(r["disparues"], [])

    def test_trois_dans_l_archive_pour_deux_au_mois_laissent_une_disparue(self):
        mois = [_ligne("PI-1"), _ligne("PI-2")]
        archive = [{"date": "2026-07-05", "tiers": "Sté ALPHA", "ttc": 120.0}] * 3
        r = self._comparer(mois, archive)
        self.assertEqual(r["manquantes"], [])
        self.assertEqual(len(r["disparues"]), 1)

    def test_la_casse_et_les_espaces_du_tiers_ne_font_pas_une_manquante(self):
        archive = [{"date": "2026-07-05", "tiers": "  sté   alpha ", "ttc": 120.0}]
        r = self._comparer([_ligne("PI-1")], archive)
        self.assertEqual(r["manquantes"], [])

    def test_un_montant_corrige_depuis_l_envoi_ressort_manquant_et_disparu(self):
        # La limite assumee du repli : sans cle, une correction ressemble a un echange.
        archive = [{"date": "2026-07-05", "tiers": "Sté ALPHA", "ttc": 120.0}]
        r = self._comparer([_ligne("PI-1", ttc=130.0)], archive)
        self.assertEqual(len(r["manquantes"]), 1)
        self.assertEqual(len(r["disparues"]), 1)

    def test_la_methode_est_rendue_avec_le_resultat(self):
        r = self._comparer([], [])
        self.assertEqual(r["methode"], A.METHODE_EMPREINTE)


if __name__ == "__main__":
    unittest.main()
