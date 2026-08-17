"""Tests du rapport mensuel du partenaire — les fonctions pures.

⚠️ CE QUI EST TESTE ICI, C'EST QU'AUCUN CHIFFRE NE PEUT VENIR DU MODELE. Le rapport part chez le
partenaire : il porte l'echeancier qu'on lui reclame. Le rendu doit donc etre reproductible au
millime, et le garde-fou doit rejeter toute phrase citant un montant absent des donnees.

Le jeu de donnees est celui de juin 2026, celui du rapport de reference.
"""
import unittest

from bank_retenue_sync.partenaire import rapport


def donnees_juin():
    """Juin 2026, tel que l'ecran le calcule. Aucun appel : c'est une fixture."""
    return {
        "mois": "2026-06",
        "libelle": "Juin 2026",
        "nom_du_mois": "Juin",
        "mois_suivant": "Juillet 2026",
        "periode": {"debut": "2026-06-01", "fin": "2026-06-30"},
        "client": "ECONOMIQ AQUA SOLUTIONS",
        "enregistre": True,
        "valide": True,
        # Les vrais chiffres de cette commande : une part réglée par une pièce de dette, donc
        # jamais encaissée.
        "commandes": [{"sales_order": "SAL-ORD-2026-01980", "date": "2026-06-04",
                       "statut": "Actif", "total": 9183.720, "encaisse": 5723.811,
                       "non_paye": 3070.459, "restant": 3459.909,
                       "reglements": [{"payment_entry": "ACC-PAY-2026-05503",
                                       "montant": 3070.459, "mode": "Dette non payée",
                                       "paye": False, "date": "2026-08-10"},
                                      {"payment_entry": "ACC-PAY-2026-05502", "montant": 1270.0,
                                       "mode": "Espèces", "paye": True, "date": "2026-08-10"}]}],
        "totaux_commandes": {"nombre": 1, "total": 9183.720, "encaisse": 5723.811,
                             "non_paye": 3070.459, "restant": 3459.909},
        "commandes_en_dette": [{"sales_order": "SAL-ORD-2026-01980", "total": 9183.720,
                                "non_paye": 3070.459,
                                "reglements": [{"payment_entry": "ACC-PAY-2026-05503",
                                                "mode": "Dette non payée", "paye": False}]}],
        "total_commandes": 9183.720,
        "tiers": 3061.240,
        "echeancier_brut": [
            {"date": "2026-06-30", "montant": 3061.240, "note": "M (fin du mois)"},
            {"date": "2026-07-31", "montant": 3061.240, "note": "M+1"},
            {"date": "2026-08-31", "montant": 3061.240, "note": "M+2"}],
        "bilan": {"aqua": {"ventes": 678.0, "achats": 479.8, "benefice": 198.2},
                  "partenaire": {"ventes": 352.0, "achats": 293.25, "benefice": 58.75}},
        # Le DocType nomme ce champ `libelle` — c'est ce que `historique.lire` rend.
        "charges_libres": [{"libelle": "Salaire Fatma", "montant": 250.0}],
        "total_charges": 250.0,
        "solde_net": 139.45,
        "ajustement": 389.45,
        "journal_entry": None,
        "echeancier_corrige": [
            {"date": "2026-06-30", "montant": 2671.790, "deduit": 389.45,
             "note": "M (fin du mois) — partiellement absorbée"},
            {"date": "2026-07-31", "montant": 3061.240, "deduit": 0.0, "note": "M+1"},
            {"date": "2026-08-31", "montant": 3061.240, "deduit": 0.0, "note": "M+2"}],
        "report": 0.0,
        "paiements": [],
        "total_paiements": 0.0,
        "echeances_couvertes": [],
        "total_couvert": 0.0,
        "avance": 0.0,
        "consolide": [
            {"date": "2026-06-30", "montant": 4169.252, "paye": 1470.294, "reste": 2698.958,
             "statut": "partiel",
             "detail": "2026-04 M+2 : +292.086  |  2026-05 M+1 : +1205.376"},
            {"date": "2026-07-31", "montant": 4266.616, "paye": None, "reste": 4266.616,
             "statut": "non_payé", "detail": "2026-05 M+2 : +1205.376"}],
    }


class TestMontant(unittest.TestCase):
    """`rapport.montant` : le SEUL endroit ou un nombre devient du texte."""

    def test_le_format_du_rapport(self):
        self.assertEqual(rapport.montant(9183.72), "9 183,720")

    def test_trois_decimales_toujours(self):
        self.assertEqual(rapport.montant(250), "250,000")
        self.assertEqual(rapport.montant(0), "0,000")

    def test_millions_groupes_par_trois(self):
        self.assertEqual(rapport.montant(1234567.891), "1 234 567,891")

    def test_negatif(self):
        self.assertEqual(rapport.montant(-221.515), "-221,515")

    def test_none_vaut_zero(self):
        self.assertEqual(rapport.montant(None), "0,000")

    def test_l_arrondi_est_au_millime(self):
        self.assertEqual(rapport.montant(3061.2400001), "3 061,240")


class TestFranciser(unittest.TestCase):
    """`rapport.franciser` : le detail du consolide est juste, il n'est pas lisible."""

    def test_les_montants_du_detail_sont_reformates(self):
        self.assertEqual(rapport.franciser("2026-05 M+1 : +1205.376"),
                         "2026-05 M+1 : +1 205,376")

    def test_les_dates_ne_sont_pas_touchees(self):
        self.assertIn("2026-04-30", rapport.franciser("2026-04-30 : 97.865"))

    def test_l_abreviation_ajust_survit(self):
        """« ajust. 2026-05 » a un point, mais pas entre deux chiffres."""
        self.assertEqual(rapport.franciser("[ajust. 2026-05 = 191.120]"),
                         "[ajust. 2026-05 = 191,120]")

    def test_plusieurs_montants_dans_la_meme_phrase(self):
        self.assertEqual(rapport.franciser("1205.376 − 191.120 = +1014.256"),
                         "1 205,376 − 191,120 = +1 014,256")

    def test_un_montant_deja_groupe_mais_au_point(self):
        """Ce que le modèle écrit : « 3 021.000 TND ». Découper sur « 021.000 » donnait « 3 21,000 »."""
        self.assertEqual(rapport.franciser("3 021.000 TND"), "3 021,000 TND")

    def test_le_point_decimal_du_modele_devient_une_virgule(self):
        self.assertEqual(rapport.franciser("un ajustement de 894.000 TND"),
                         "un ajustement de 894,000 TND")

    def test_une_reference_de_piece_n_est_pas_touchee(self):
        self.assertEqual(rapport.franciser("ACC-PAY-2026-05502 du 2026-08-10"),
                         "ACC-PAY-2026-05502 du 2026-08-10")

    def test_une_annee_seule_n_est_pas_un_montant(self):
        self.assertEqual(rapport.franciser("les échéances d’Août 2026"),
                         "les échéances d’Août 2026")


class TestLibellesDeMois(unittest.TestCase):
    """Le mois dans un titre, et son élision. Le rapport est un document, pas un écran."""

    def test_la_majuscule_du_titre(self):
        self.assertEqual(rapport.maj("juillet 2026"), "Juillet 2026")

    def test_le_reste_de_la_chaine_est_intact(self):
        self.assertEqual(rapport.maj("mai 2026"), "Mai 2026")

    def test_l_elision_devant_une_voyelle(self):
        self.assertEqual(rapport.de("Août 2026"), "d’Août 2026")

    def test_pas_d_elision_devant_une_consonne(self):
        self.assertEqual(rapport.de("Juillet 2026"), "de Juillet 2026")


class TestNombresLus(unittest.TestCase):
    """`rapport.nombres` : ce qu'une phrase AFFIRME, quel qu'en soit le format."""

    def test_le_format_du_rapport_est_reconnu(self):
        self.assertIn(9183.72, rapport.nombres("un total de 9 183,720 TND"))

    def test_le_point_decimal_aussi(self):
        self.assertIn(389.45, rapport.nombres("absorption de 389.450"))

    def test_un_texte_sans_chiffre_ne_dit_rien(self):
        self.assertEqual(rapport.nombres("aucune échéance couverte"), set())

    def test_l_espace_fine_insecable_du_modele_est_comprise(self):
        """Le modèle groupe les milliers avec U+202F : « 3 021,000 » est UN montant, pas deux."""
        self.assertEqual(rapport.nombres("3 021,000 TND"), {3021.0})
        self.assertEqual(rapport.nombres("3 021,000 TND"), {3021.0})

    def test_une_date_n_est_pas_un_montant(self):
        self.assertEqual(rapport.nombres("échéance du 2026-07-31"), set())

    def test_une_reference_de_piece_n_est_pas_un_montant(self):
        self.assertEqual(rapport.nombres("ACC-PAY-2026-05502 du 2026-08-10"), set())

    def test_une_note_d_echeance_n_est_pas_un_montant(self):
        self.assertEqual(rapport.nombres("l’échéance M+1 puis M+2"), set())

    def test_une_annee_seule_n_est_pas_un_montant(self):
        self.assertEqual(rapport.nombres("les échéances d’Août 2026"), set())

    def test_une_reference_bancaire_collee_n_est_pas_un_montant(self):
        """« TT2621295QTD », « Aramex N: 51330111766 » : des références, pas des dinars."""
        self.assertEqual(rapport.nombres("TT2621295QTD"), set())
        self.assertEqual(rapport.nombres("Aramex N: 51330111766 Virement recu N: FT26209ZZCTH"),
                         set())

    def test_un_montant_reste_lu_a_cote_d_une_reference(self):
        self.assertEqual(rapport.nombres("TT2621295QTD pour 3 000,000 TND"), {3000.0})

    def test_un_petit_entier_reste_un_montant_a_justifier(self):
        """⚠️ La faille du 17/08/2026 : « 21 » passait comme jour du mois."""
        self.assertEqual(rapport.nombres("un reste de 21,000 TND"), {21.0})


class TestGardeFouDeLaProse(unittest.TestCase):
    """`rapport.prose_sure` : une phrase qui cite un nombre inexistant est rejetee.

    ⚠️ C'EST LA SEULE CHOSE QUI EMPECHE UN CHIFFRE INVENTE DE PARTIR CHEZ LE PARTENAIRE. Le
    modele ne calcule rien ; s'il calcule quand meme, sa phrase ne doit pas survivre.
    """

    def setUp(self):
        self.d = donnees_juin()
        self.autorises = rapport.valeurs_autorisees(self.d)
        self.secours = rapport.prose_deterministe(self.d)

    def test_une_phrase_juste_est_gardee(self):
        propose = {"absorption": "Les 389,450 TND du bilan ont réduit l’échéance de juin."}
        sure = rapport.prose_sure(propose, self.secours, self.autorises)
        self.assertEqual(sure["absorption"], propose["absorption"])

    def test_un_montant_invente_fait_rejeter_la_phrase(self):
        propose = {"absorption": "Un ajustement de 1 234,567 TND a été absorbé."}
        sure = rapport.prose_sure(propose, self.secours, self.autorises)
        self.assertEqual(sure["absorption"], self.secours["absorption"])

    def test_le_rejet_est_champ_par_champ(self):
        """Une projection fantaisiste ne doit pas emporter une absorption correcte."""
        propose = {"absorption": "Les 389,450 TND ont été absorbés.",
                   "projection": "couvrira les 5 000,000 TND de juillet"}
        sure = rapport.prose_sure(propose, self.secours, self.autorises)
        self.assertEqual(sure["absorption"], "Les 389,450 TND ont été absorbés.")
        self.assertEqual(sure["projection"], self.secours["projection"])

    def test_la_prose_du_modele_est_remise_au_format_du_rapport(self):
        """Le modèle écrit « 389.450 TND » ; le rapport écrit « 389,450 TND ». Un seul format."""
        propose = {"absorption": "Un ajustement de 389.450 TND a été absorbé."}
        sure = rapport.prose_sure(propose, self.secours, self.autorises)
        self.assertEqual(sure["absorption"], "Un ajustement de 389,450 TND a été absorbé.")

    def test_une_phrase_sans_chiffre_passe_toujours(self):
        propose = {"echeances_couvertes": "Aucun règlement n’est parvenu ce mois-ci."}
        sure = rapport.prose_sure(propose, self.secours, self.autorises)
        self.assertEqual(sure["echeances_couvertes"], propose["echeances_couvertes"])

    def test_une_annee_n_est_pas_un_montant_invente(self):
        propose = {"projection": "sera imputée sur les échéances de juillet 2026"}
        sure = rapport.prose_sure(propose, self.secours, self.autorises)
        self.assertEqual(sure["projection"], propose["projection"])

    def test_les_puces_de_lecture_sont_filtrees_une_par_une(self):
        propose = {"lecture": ["Une seule commande sur le mois.",
                               "Un chiffre d’affaires de 99 999,000 TND."]}
        sure = rapport.prose_sure(propose, self.secours, self.autorises)
        self.assertEqual(sure["lecture"], ["Une seule commande sur le mois."])

    def test_le_prefixe_recopie_par_le_modele_est_retire(self):
        """Le cas réel du 17/08/2026 : le montant apparaissait deux fois, dont une au point."""
        propose = {"projection": "Avance Reportée : 0.000 TND — aucune avance disponible "
                                 "à mobiliser sur Août 2026."}
        sure = rapport.prose_sure(propose, self.secours, self.autorises)
        self.assertEqual(sure["projection"],
                         "aucune avance disponible à mobiliser sur Août 2026.")

    def test_une_projection_normale_n_est_pas_amputee(self):
        propose = {"projection": "sera imputée sur la première échéance de juillet"}
        sure = rapport.prose_sure(propose, self.secours, self.autorises)
        self.assertEqual(sure["projection"], propose["projection"])

    def test_le_rejet_est_signale_par_prose_sure_elle_meme(self):
        propose = {"absorption": "Un ajustement de 1 234,567 TND a été absorbé.",
                   "projection": "sera imputée sur juillet"}
        sure = rapport.prose_sure(propose, self.secours, self.autorises)
        self.assertEqual(sure["_rejetes"], ["absorption"])

    def test_un_simple_reformatage_n_est_pas_un_rejet(self):
        """« 389.450 » devient « 389,450 » : la phrase est gardée, pas rejetée."""
        propose = {"absorption": "Un ajustement de 389.450 TND a été absorbé."}
        sure = rapport.prose_sure(propose, self.secours, self.autorises)
        self.assertEqual(sure["_rejetes"], [])
        self.assertIn("389,450", sure["absorption"])

    def test_le_nettoyage_ne_vide_jamais_la_phrase(self):
        self.assertEqual(rapport.sans_prefixe_avance("Avance Reportée : 0,000 TND"),
                         "Avance Reportée : 0,000 TND")

    def test_un_modele_muet_laisse_la_prose_deterministe(self):
        sure = rapport.prose_sure({}, self.secours, self.autorises)
        self.assertEqual(sure["absorption"], self.secours["absorption"])


class TestRendu(unittest.TestCase):
    """`rapport.rendre` : le format demandé, au millime, sans base ni reseau."""

    def setUp(self):
        self.d = donnees_juin()
        self.texte = rapport.rendre(self.d)

    def test_le_titre_porte_le_mois(self):
        self.assertTrue(self.texte.startswith("# Rapport Financier Mensuel — Juin 2026"))

    def test_les_six_sections_sont_la_dans_l_ordre(self):
        positions = [self.texte.find("## %d." % n) for n in range(1, 7)]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))

    def test_la_commande_porte_l_encaisse_le_non_paye_et_le_restant(self):
        """⚠️ Le total seul cachait 3 070,459 TND jamais encaissés."""
        self.assertIn("| SAL-ORD-2026-01980 | 2026-06-04 | Actif | 9 183,720 | 5 723,811 "
                      "| 3 070,459 | 3 459,909 |", self.texte)

    def test_la_ligne_de_total_ferme_le_tableau(self):
        self.assertIn("| **1 commande(s)** |  |  | **9 183,720** | **5 723,811** | **3 070,459** "
                      "| **3 459,909** |", self.texte)

    def test_la_dette_non_payee_est_expliquee_et_pas_seulement_chiffree(self):
        """« Non payé » n'est pas « en retard » : aucune pièce ne constate d'encaissement."""
        self.assertIn("*Dette non payée* : 1 commande(s) sur 1 sont réglées par une pièce qui ne "
                      "constate aucun encaissement", self.texte)
        self.assertIn("3 070,459 TND au total.", self.texte)

    def test_chaque_commande_en_dette_est_nommee_avec_son_mode(self):
        self.assertIn("- SAL-ORD-2026-01980 : 3 070,459 TND non encaissés sur 9 183,720 TND "
                      "— Dette non payée", self.texte)

    def test_sans_dette_aucune_mention(self):
        d = donnees_juin()
        d["commandes_en_dette"] = []
        self.assertNotIn("*Dette non payée*", rapport.rendre(d))

    def test_la_division_par_trois(self):
        self.assertIn("9 183,720 ÷ 3 = 3 061,240 TND", self.texte)

    def test_le_solde_net_est_pose_comme_une_soustraction(self):
        self.assertIn("198,200 - 58,750 = 139,450 TND", self.texte)

    def test_le_total_ajustement(self):
        self.assertIn("139,450 + 250,000 = 389,450 TND", self.texte)

    def test_la_charge_porte_son_libelle(self):
        """Le champ s'appelle `libelle` : chercher `label` laissait la colonne vide."""
        self.assertIn("| Salaire Fatma | 250,000 |", self.texte)

    def test_l_echeance_partiellement_absorbee(self):
        self.assertIn("| 2026-06-30 | 2 671,790 | M (fin du mois) — partiellement absorbée "
                      "| 389,450 |", self.texte)

    def test_le_detail_du_consolide_est_francise(self):
        self.assertIn("2026-05 M+1 : +1 205,376", self.texte)
        self.assertNotIn("1205.376", self.texte)

    def test_les_pipes_du_detail_sont_echappes(self):
        """Sans échappement, le séparateur du détail décale toute la ligne du tableau."""
        ligne = next(l for l in self.texte.splitlines() if l.startswith("| 2026-06-30 | 4 169,252"))
        self.assertIn("\\|", ligne)
        # Six colonnes annoncées par l'en-tête, six colonnes réelles. On ne coupe que sur les
        # pipes NON échappés — c'est justement ce que fait le rendu Markdown.
        import re
        self.assertEqual(len(re.split(r"(?<!\\)\|", ligne.strip().strip("|"))), 6)

    def test_les_valeurs_absentes_du_consolide_restent_null(self):
        """Le format du rapport de reference porte « null », ce n'est pas un oubli de rendu."""
        self.assertIn("| 2026-07-31 | 4 266,616 | null | 4 266,616 | non_payé |", self.texte)

    def test_l_absence_de_paiement_se_dit(self):
        self.assertIn("(Aucun paiement reçu ce mois-ci)", self.texte)
        self.assertIn("Aucune échéance couverte ce mois-ci.", self.texte)

    def test_la_formule_de_l_avance(self):
        self.assertIn("0,000 - 0,000 = 0,000 TND", self.texte)
        self.assertIn("*Avance Reportée : 0,000 TND*", self.texte)

    def test_sans_avance_le_rapport_ne_promet_rien(self):
        """« Sera utilisée en priorité… » sous un montant nul est une promesse vide."""
        self.assertIn("aucune avance à reporter", self.texte)
        self.assertNotIn("sera utilisée en priorité", self.texte)

    def test_une_avance_disponible_nomme_le_mois_suivant(self):
        d = donnees_juin()
        d["avance"] = 28.21
        self.assertIn("de Juillet 2026", rapport.rendre(d))

    def test_aucune_section_avance_future_quand_il_n_y_en_a_pas(self):
        self.assertNotIn("5. *", self.texte)

    def test_aucune_section_lecture_sans_prose_du_modele(self):
        self.assertNotIn("## 7.", self.texte)

    def test_la_lecture_du_modele_devient_la_section_sept(self):
        prose = dict(rapport.prose_deterministe(self.d), lecture=["Une seule commande."])
        self.assertIn("## 7.", rapport.rendre(self.d, prose))

    def test_le_rendu_est_reproductible(self):
        self.assertEqual(rapport.rendre(donnees_juin()), rapport.rendre(donnees_juin()))

    def test_tous_les_nombres_du_rapport_viennent_des_donnees(self):
        """⚠️ LE TEST QUI COMPTE : le rendu n'invente aucun montant.

        Les nombres du rapport sont confrontes aux valeurs autorisees par les donnees. Un rendu
        qui calculerait quelque chose au passage — un total, un pourcentage — se ferait prendre
        ici, avant le partenaire.
        """
        import re
        autorises = rapport.valeurs_autorisees(self.d)
        # La numérotation du document (« ## 3. », « 4. *Avance… ») est de la structure, pas de
        # l'arithmétique : on l'enlève avant de confronter. Les dates et références, elles, sont
        # déjà écartées par `nombres()`.
        propre = "\n".join(re.sub(r"^(?:#+\s*)?\d+\.\s", "", l.strip())
                           for l in self.texte.splitlines())
        inconnus = rapport.nombres(propre) - autorises
        self.assertEqual(inconnus, set(), "montants sans source : %s" % sorted(inconnus))


class TestAvanceDejaImputee(unittest.TestCase):
    """§6 : une avance POSEE sur une échéance à venir n'est pas dans la formule.

    ⚠️ LE CAS REEL DU 17/08/2026. Le §5 montrait 52,594 payés sur l'échéance du 31/08 pendant que
    le §6 annonçait « Avance Reportée : 0,000 TND » et promettait de la mobiliser en août. Les deux
    sections du même rapport se contredisaient : `imputer` ne rend en excédent que ce qui n'a trouvé
    AUCUNE échéance, alors qu'un règlement qui déborde sur une échéance future est déjà imputé.
    """

    def setUp(self):
        self.d = donnees_juin()
        self.d["avances_futures"] = [{
            "date": "2026-08-31", "montant": 52.594, "echeance": 3542.610, "reste": 3490.016,
            "reglements": [{"payment_entry": "ACC-PAY-2026-05502", "date": "2026-08-10",
                            "impute": 52.594}]}]
        self.d["total_avances_futures"] = 52.594
        self.texte = rapport.rendre(self.d)

    def test_l_avance_deja_imputee_est_ecrite(self):
        self.assertIn("5. *Avance déjà imputée sur les échéances à venir (à ce jour)* : 52,594 TND",
                      self.texte)

    def test_l_echeance_visee_et_le_reste_sont_dits(self):
        self.assertIn("échéance du 2026-08-31 : 52,594 TND déjà réglés sur 3 542,610 TND",
                      self.texte)

    def test_la_piece_et_sa_date_sont_nommees(self):
        """Dans un rapport de juillet, l'avance peut venir d'un versement d'août : il faut le dire."""
        self.assertIn("ACC-PAY-2026-05502 du 2026-08-10 pour 52,594 TND", self.texte)

    def test_la_projection_ne_promet_plus_de_mobiliser_une_avance_nulle(self):
        self.assertIn("aucun excédent ne reste disponible", self.texte)
        self.assertIn("52,594", self.texte)
        self.assertNotIn("sera utilisée en priorité", self.texte)

    def test_ces_montants_restent_des_montants_autorises(self):
        """Le garde-fou ne doit pas rejeter une phrase qui cite l'avance déjà imputée."""
        self.assertIn(52.594, rapport.valeurs_autorisees(self.d))


class TestBilanCroise(unittest.TestCase):
    """§2 : un bénéfice n'appartient pas à la société qui le dégage.

    Chaque section est l'activité réalisée POUR L'AUTRE : le bénéfice qu'elle montre est DÛ à
    l'autre. C'est ce qui explique qu'il vienne en déduction de ce que le partenaire doit.
    """

    def setUp(self):
        self.texte = rapport.rendre(donnees_juin())

    def test_l_activite_aqua_est_dite_realisee_pour_le_partenaire(self):
        self.assertIn("Tableau Bilan Aqua World — activité réalisée pour ECONOMIQ AQUA SOLUTIONS",
                      self.texte)

    def test_l_activite_du_partenaire_est_dite_realisee_pour_aqua(self):
        self.assertIn("Tableau Bilan Economiq — activité réalisée pour AQUA WORLD", self.texte)

    def test_le_benefice_aqua_est_du_au_partenaire(self):
        self.assertIn("Bénéfice dû à ECONOMIQ AQUA SOLUTIONS : 198,200 TND.", self.texte)

    def test_le_benefice_du_partenaire_est_du_a_aqua(self):
        self.assertIn("Bénéfice dû à AQUA WORLD : 58,750 TND.", self.texte)

    def test_le_solde_net_est_relu_dans_ce_sens(self):
        self.assertIn("Soit le bénéfice dû à ECONOMIQ AQUA SOLUTIONS moins le bénéfice dû à "
                      "AQUA WORLD.", self.texte)

    def test_la_formule_chiffree_est_inchangee(self):
        """Le wording change, l'arithmétique non : ce sont les mêmes nombres, dans le même ordre."""
        self.assertIn("= 198,200 - 58,750 = 139,450 TND", self.texte)


class TestReserves(unittest.TestCase):
    """Un mois de reprise n'est pas un mois mesuré, et le rapport doit le dire en tête.

    ⚠️ C'EST LE CAS DE JUIN 2026 EN BASE. L'amorce l'a enregistré avec un bilan et un total de
    commandes nuls, alors qu'une commande de 9 183,720 TND y figure. Imprimer des zéros sans un mot
    ferait passer une reprise pour un mois sans activité.
    """

    def test_la_reserve_apparait_avant_tout_le_reste(self):
        d = donnees_juin()
        d["reserves"] = ["Le mois enregistré porte un total de commandes nul."]
        texte = rapport.rendre(d)
        self.assertLess(texte.find("> ⚠️"), texte.find("## 1."))

    def test_sans_reserve_aucun_bandeau(self):
        self.assertNotIn("> ⚠️", rapport.rendre(donnees_juin()))


class TestProseDeterministe(unittest.TestCase):
    """La prose de secours : celle qui part si OpenAI est muet."""

    def test_l_absorption_nomme_l_echeance_touchee(self):
        p = rapport.prose_deterministe(donnees_juin())
        self.assertIn("389,450", p["absorption"])
        self.assertIn("2026-06-30", p["absorption"])

    def test_sans_absorption_on_le_dit(self):
        d = donnees_juin()
        d["ajustement"] = 0.0
        d["echeancier_corrige"] = [{**e, "deduit": 0.0} for e in d["echeancier_corrige"]]
        self.assertIn("Aucune absorption", rapport.prose_deterministe(d)["absorption"])

    def test_les_echeances_couvertes_sont_enumerees(self):
        d = donnees_juin()
        d["echeances_couvertes"] = [{"date": "2026-06-30", "montant": 4169.252,
                                     "impute": 1470.294, "statut": "partiel"}]
        p = rapport.prose_deterministe(d)
        self.assertIn("2026-06-30", p["echeances_couvertes"])
        self.assertIn("1 470,294", p["echeances_couvertes"])
