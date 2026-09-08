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
           categorie="Fournitures", dt="Purchase Invoice", pieces=()):
    return {"document_type": dt, "document_name": nom, "date": date, "tiers": tiers,
            "ttc": ttc, "reference_export": ref, "categorie": categorie,
            "justificatifs": [{"file_name": p, "file_url": "/private/files/%s" % p}
                              for p in pieces]}


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


def _rang(ref, date, tiers, categorie, ttc, pieces="piece.pdf"):
    """Une ligne de charge du classeur d AUJOURD HUI : douze colonnes, TTC en dixieme."""
    return [ref, date, tiers, categorie, "Virement", 100.0, 0.0, 20.0, 20.0, ttc, 0.0, pieces]


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


class TestClasseurDUneVersionAnterieure(unittest.TestCase):
    """Le dossier de juillet 2026 : un classeur dont les colonnes ne sont plus a la meme place.

    « Référence » et « Type » sont sortis du fichier depuis. Lues par POSITION, les colonnes d un
    dossier ancien decalent tout : la neuvieme colonne y porte la TVA 19 %, pas le TTC. Les lignes
    etaient donc lues avec une date qui est une reference, un tiers qui est une date et un montant
    qui est une TVA — aucune ne se rapprochait, et le mois entier ressortait « non envoye ».
    """

    ENTETE_ANCIEN = ["Référence", "Référence export", "Date", "Type", "Tiers", "Catégorie",
                     "Mode", "Valeur HT", "TVA 7%", "TVA 19%", "TVA", "Valeur TTC", "Retenue",
                     "Justificatifs"]

    def _rang_ancien(self, ref, date, tiers, categorie, ttc, pieces="piece.pdf"):
        return ["JV-001", ref, date, "Journal Entry", tiers, categorie, "Chèque",
                100.0, 0.0, 20.0, 20.0, ttc, 0.0, pieces]

    def _octets(self, lignes):
        return _zip({"2026-07/Liste des Charges 2026-07.xlsx": _classeur(lignes)})

    def _classeur_ancien(self):
        return [
            self.ENTETE_ANCIEN,
            [],
            ["DÉPENSES", "1 ligne(s)"],
            self._rang_ancien("Facture Aramex 07-2026 2605049", "2026-07-18",
                              "Fournisseurs - A&S", "Transport sur achat", 486.2,
                              "facture_aramex_072026.pdf"),
            ["TOTAL Dépenses", "", "", "", "", "", "", 406.0, "", "", 80.2, 486.2, 0.0, "0 sans"],
        ]

    def test_les_colonnes_sont_retrouvees_par_leur_intitule(self):
        positions = A.positions_des_colonnes(self.ENTETE_ANCIEN)
        # « Référence export » l emporte sur « Référence », qui la precede pourtant.
        self.assertEqual(positions["reference"], 1)
        self.assertEqual(positions["date"], 2)
        self.assertEqual(positions["tiers"], 4)
        self.assertEqual(positions["ttc"], 11)
        self.assertEqual(positions["pieces"], 13)

    def test_une_ligne_de_charge_n_est_pas_prise_pour_un_en_tete(self):
        self.assertIsNone(A.positions_des_colonnes(
            _rang("FA-1", "2026-07-05", "Sté ALPHA", "Fournitures", 120.0)))

    def test_un_classeur_ancien_est_lu_avec_les_bonnes_valeurs(self):
        lignes = A.lire_classeur_charges(self._octets(self._classeur_ancien()))
        self.assertEqual(len(lignes), 1)
        self.assertEqual(lignes[0]["date"], "2026-07-18")
        self.assertEqual(lignes[0]["tiers"], "Fournisseurs - A&S")
        self.assertEqual(lignes[0]["ttc"], 486.2)
        self.assertEqual(lignes[0]["reference"], "Facture Aramex 07-2026 2605049")
        self.assertEqual(lignes[0]["pieces"], ["facture_aramex_072026.pdf"])

    def test_la_charge_du_classeur_ancien_n_est_plus_annoncee_manquante(self):
        # Le cas signale : la facture Aramex de juillet 2026 EST dans le ZIP.
        lignes = A.lire_classeur_charges(self._octets(self._classeur_ancien()))
        mois = [A.entree(_ligne("JV-001", date="2026-07-18", tiers="Fournisseurs - A&S",
                                ttc=486.2, ref="Facture Aramex 07-2026 2605049",
                                dt="Journal Entry", pieces=["facture_aramex_072026.pdf"]))]
        r = A.comparer(mois, lignes, A.METHODE_EMPREINTE)
        self.assertEqual(r["manquantes"], [])
        self.assertEqual(r["disparues"], [])

    def test_un_classeur_illisible_n_est_pas_un_mois_vide(self):
        # Sans en-tete reconnu ni ligne lue, on refuse de conclure : declarer l archive vide
        # ferait annoncer TOUT le mois comme non envoye.
        octets = self._octets([["Compte", "Libellé"], ["606100", "Fournitures de bureau"]])
        self.assertIsNone(A.lire_classeur_charges(octets))


class TestPiecesPresentesDansLArchive(unittest.TestCase):
    """La preuve la plus directe : le fichier est dans le ZIP, donc la charge est partie."""

    def _octets(self):
        return _zip({
            "2026-07/Liste des Charges 2026-07.xlsx": b"x",
            "2026-07/manifeste.json": b"{}",
            "2026-07/Dépenses/facture_aramex_072026.pdf": b"pdf",
            "2026-07/Dépenses/Retenue à la source Vente/certificat_ras_1.pdf": b"pdf",
            "2026-07/Dépenses/Retenue à la source Achat/certificat_ras_2.pdf": b"pdf",
            "2026-07/Dépenses/Retards 2026-06/facture_juin.pdf": b"pdf",
            "2026-07/Chiffre D'affaire Facturé/FA-1 - Client.pdf": b"pdf",
            "2026-07/Relevé bancaire/releve_2026-07.pdf": b"pdf",
        })

    def test_les_justificatifs_des_charges_sont_rendus(self):
        pieces = A.pieces_de_l_archive(self._octets())
        self.assertIn("facture_aramex_072026.pdf", pieces)
        self.assertIn("certificat_ras_1.pdf", pieces)
        self.assertIn("certificat_ras_2.pdf", pieces)

    def test_les_pieces_des_retardataires_sont_ecartees(self):
        # Elles appartiennent a juin : les compter ferait passer une charge de juin pour envoyee
        # avec le dossier de juillet.
        self.assertNotIn("facture_juin.pdf", A.pieces_de_l_archive(self._octets()))

    def test_ni_les_pdf_de_ventes_ni_le_releve_ni_les_classeurs(self):
        pieces = A.pieces_de_l_archive(self._octets())
        for nom in ("FA-1 - Client.pdf", "releve_2026-07.pdf", "manifeste.json",
                    "Liste des Charges 2026-07.xlsx"):
            self.assertNotIn(nom, pieces)

    def test_une_archive_sans_justificatif_rend_un_ensemble_vide(self):
        self.assertEqual(A.pieces_de_l_archive(_zip({"2026-07/manifeste.json": b"{}"})), set())


class TestRepliParLeContenuDuDossier(unittest.TestCase):
    """Ce que le ticket corrige : une charge presente dans le ZIP ne doit plus etre « manquante ».

    L empreinte (date, tiers, TTC) est faite de trois valeurs qui BOUGENT — le compte credite se
    renomme, le montant se corrige, la date se rectifie. Le nom du justificatif, lui, ne bouge pas.
    """

    def _comparer(self, mois, archive, pieces=None):
        return A.comparer([A.entree(l) for l in mois], [A.entree(l) for l in archive],
                          A.METHODE_EMPREINTE, pieces_archive=pieces)

    def test_un_montant_corrige_depuis_l_envoi_ne_fait_plus_une_manquante(self):
        mois = [_ligne("JV-1", ttc=486.2, pieces=["aramex.pdf"])]
        archive = [{"date": "2026-07-05", "tiers": "Sté ALPHA", "ttc": 402.0,
                    "pieces": ["aramex.pdf"]}]
        r = self._comparer(mois, archive)
        self.assertEqual(r["manquantes"], [])
        self.assertEqual(r["disparues"], [])
        self.assertEqual(r["retrouvees_par_piece"], 1)

    def test_un_tiers_renomme_depuis_l_envoi_ne_fait_plus_une_manquante(self):
        mois = [_ligne("JV-1", tiers="Fournisseurs divers - A&S", pieces=["aramex.pdf"])]
        archive = [{"date": "2026-07-05", "tiers": "Fournisseurs - A&S", "ttc": 120.0,
                    "pieces": ["aramex.pdf"]}]
        self.assertEqual(self._comparer(mois, archive)["manquantes"], [])

    def test_une_date_rectifiee_depuis_l_envoi_ne_fait_plus_une_manquante(self):
        mois = [_ligne("JV-1", date="2026-07-31", pieces=["aramex.pdf"])]
        archive = [{"date": "2026-07-05", "tiers": "Sté ALPHA", "ttc": 120.0,
                    "pieces": ["aramex.pdf"]}]
        self.assertEqual(self._comparer(mois, archive)["manquantes"], [])

    def test_la_piece_physiquement_dans_le_zip_sauve_une_ligne_introuvable(self):
        # Le classeur ne dit rien de cette charge — mais son justificatif est dans l archive.
        mois = [_ligne("JV-1", pieces=["aramex.pdf"])]
        r = self._comparer(mois, [], pieces={"aramex.pdf"})
        self.assertEqual(r["manquantes"], [])
        self.assertEqual(r["retrouvees_par_piece"], 1)

    def test_une_piece_absente_du_zip_laisse_la_charge_manquante(self):
        mois = [_ligne("JV-1", pieces=["jamais_envoye.pdf"])]
        r = self._comparer(mois, [], pieces={"autre.pdf"})
        self.assertEqual([e["document_name"] for e in r["manquantes"]], ["JV-1"])
        self.assertEqual(r["retrouvees_par_piece"], 0)

    def test_une_charge_sans_justificatif_reste_jugee_a_l_empreinte(self):
        mois = [_ligne("JV-1", ttc=999.0)]
        archive = [{"date": "2026-07-05", "tiers": "Sté ALPHA", "ttc": 120.0}]
        r = self._comparer(mois, archive, pieces={"aramex.pdf"})
        self.assertEqual(len(r["manquantes"]), 1)
        self.assertEqual(len(r["disparues"]), 1)

    def test_deux_charges_sans_piece_ne_s_apparient_pas_sur_le_vide(self):
        # Une liste de justificatifs vide ne doit rapprocher personne de personne.
        mois = [_ligne("JV-1", ttc=10.0), _ligne("JV-2", ttc=20.0)]
        archive = [{"date": "2026-07-05", "tiers": "Sté ALPHA", "ttc": 30.0}]
        r = self._comparer(mois, archive)
        self.assertEqual(len(r["manquantes"]), 2)

    def test_un_justificatif_n_apparie_qu_une_seule_ligne_d_archive(self):
        mois = [_ligne("JV-1", ttc=10.0, pieces=["commun.pdf"]),
                _ligne("JV-2", ttc=20.0, pieces=["commun.pdf"])]
        archive = [{"date": "2026-07-05", "tiers": "Sté ALPHA", "ttc": 10.0,
                    "pieces": ["commun.pdf"]}]
        r = self._comparer(mois, archive)
        # La seconde ne trouve plus de ligne libre : elle repasse a l empreinte, qui echoue.
        self.assertEqual([e["document_name"] for e in r["manquantes"]], ["JV-2"])

    def test_un_fichier_du_zip_n_absout_qu_une_seule_charge(self):
        # Deux ecritures dont la piece jointe porte le meme nom, et un seul fichier dans le ZIP :
        # une seule est partie, l autre reste a rattraper.
        mois = [_ligne("JV-1", ttc=10.0, pieces=["scan.pdf"]),
                _ligne("JV-2", ttc=20.0, pieces=["scan.pdf"])]
        r = self._comparer(mois, [], pieces={"scan.pdf"})
        self.assertEqual([e["document_name"] for e in r["manquantes"]], ["JV-2"])

    def test_un_nom_deja_consomme_par_le_classeur_ne_sauve_pas_une_seconde_charge(self):
        mois = [_ligne("JV-1", ttc=10.0, pieces=["scan.pdf"]),
                _ligne("JV-2", ttc=20.0, pieces=["scan.pdf"])]
        archive = [{"date": "2026-07-05", "tiers": "Sté ALPHA", "ttc": 10.0,
                    "pieces": ["scan.pdf"]}]
        r = self._comparer(mois, archive, pieces={"scan.pdf"})
        self.assertEqual([e["document_name"] for e in r["manquantes"]], ["JV-2"])

    def test_le_manifeste_ignore_les_pieces_et_reste_exact(self):
        # Avec une cle de document, aucun repli n est necessaire ni souhaitable.
        mois = [A.entree(_ligne("PI-1", pieces=["a.pdf"]))]
        archive = [A.entree(_ligne("PI-2", pieces=["a.pdf"]))]
        r = A.comparer(mois, archive, A.METHODE_MANIFESTE, pieces_archive={"a.pdf"})
        self.assertEqual([e["document_name"] for e in r["manquantes"]], ["PI-1"])
        self.assertEqual(r["retrouvees_par_piece"], 0)


class TestJustificatifsDuClasseur(unittest.TestCase):
    """La colonne « Justificatifs » melange noms de fichiers, motifs d exemption et « AUCUN »."""

    def _lire(self, cellule):
        octets = _zip({"2026-07/Liste des Charges 2026-07.xlsx": _classeur([
            ["Référence export", "Date", "Tiers", "Catégorie", "Mode", "Valeur HT", "TVA 7%",
             "TVA 19%", "TVA", "Valeur TTC", "Retenue", "Justificatifs"],
            _rang("FA-1", "2026-07-05", "Sté ALPHA", "Fournitures", 120.0, cellule),
        ])})
        return A.lire_classeur_charges(octets)[0]["pieces"]

    def test_plusieurs_pieces_sont_separees(self):
        self.assertEqual(self._lire("facture.pdf · avoir.pdf"), ["facture.pdf", "avoir.pdf"])

    def test_la_mention_deja_remise_est_retiree(self):
        self.assertEqual(self._lire("facture.pdf — DÉJÀ REMISE avec le dossier de juin 2026"),
                         ["facture.pdf"])

    def test_aucun_et_les_motifs_d_exemption_ne_sont_pas_des_fichiers(self):
        for cellule in ("AUCUN", "Salaires : pas de justificatif exigible", ""):
            self.assertEqual(self._lire(cellule), [], "cellule %r" % (cellule,))


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
