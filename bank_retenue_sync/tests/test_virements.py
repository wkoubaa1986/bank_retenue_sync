"""Tests du flux VIREMENTS CLIENTS RECUS (identification du payeur + repartition sur les dettes).

Aucun appel externe : mouvements bancaires, dettes en attente et resolveur IA sont injectes.
Les libelles bancaires et les noms de clients sont ceux reellement observes sur l'export
2026-06-09 -> 2026-07-31 (troncature a ~30 caracteres incluse).
"""
import unittest
from datetime import date

from bank_retenue_sync.encaissement import allocation, builder, matching, party

CLIENTS = [
    "STE TAURUS",
    "Sté KEMVET",
    "Ambassade de l'Inde",
    "BUSINESS HOTEL MANAGEMENT TUNIS - BHM",
    "SOCOBAT",
    "BEN Sassi BIOAQUA",
]


def _credit(operation, credit, reference="FT0000000001", jour=15):
    return {"date": date(2026, 7, jour), "date_valeur": date(2026, 7, jour),
            "operation": operation, "reference": reference, "debit": 0.0, "credit": credit}


def _dette(name, party_name, montant, so=None, jour=1, origine=None):
    return {"name": name, "party": party_name, "paid_amount": montant,
            "reference_no": so or name, "sales_order": so, "origine_compte": origine,
            "posting_date": date(2026, 7, jour), "reference_date": date(2026, 7, jour)}


def _ok(sales_order, valeur):
    """Echeance 'Dette non payée' toujours presente (le cas nominal), pour ne pas dependre de la
    base dans les tests de matching."""
    return True


class TestNormalisationLibelle(unittest.TestCase):
    def test_prefixes_bancaires_et_formes_juridiques_disparaissent(self):
        self.assertEqual(party.normalize("VIR TN AUTRE BQ TAURUS SARL"), "TAURUS")
        self.assertEqual(party.normalize("STE TAURUS"), "TAURUS")

    def test_lettres_eclatees_sont_recollees(self):
        """'S O C O B A T' arrive lettre par lettre : sans recollage, chaque lettre devient un
        token d'un caractere et le nom n'est plus reconnaissable."""
        self.assertEqual(party.normalize("VIR TN AUTRE BQ S O C O B A T"), "SOCOBAT")

    def test_initiales_courtes_ne_sont_pas_soudees(self):
        self.assertEqual(party.normalize("A B"), "A B")


class TestScoreLibelleClient(unittest.TestCase):
    def test_identique_apres_normalisation(self):
        self.assertEqual(party.score("VIR TN AUTRE BQ TAURUS SARL", "STE TAURUS"), 1.0)

    def test_libelle_tronque_par_la_banque(self):
        """La banque coupe a longueur fixe : le libelle normalise est un PREFIXE du vrai nom."""
        self.assertGreaterEqual(
            party.score("VIR TN AUTRE BQ BUSINESS HOTEL M",
                        "BUSINESS HOTEL MANAGEMENT TUNIS - BHM"), 0.9)
        self.assertGreaterEqual(
            party.score("VIR TN AUTRE BQ AMBASSADE DE L", "Ambassade de l'Inde"), 0.9)

    def test_clients_sans_rapport_restent_bas(self):
        self.assertLess(party.score("VIR TN AUTRE BQ TAURUS SARL", "BEN Sassi BIOAQUA"), 0.5)


class TestIdentificationClient(unittest.TestCase):
    def test_alias_appris_prime_sur_le_fuzzy(self):
        alias = {party.normalize("VIREMENT TN MM BQ MOHAMED BEJAOUI"): "SOCOBAT"}
        out = party.identify("VIREMENT TN MM BQ MOHAMED BEJAOUI", CLIENTS, aliases=alias)
        self.assertEqual(out["customer"], "SOCOBAT")
        self.assertEqual(out["method"], "alias")

    def test_fuzzy_tranche_sans_appeler_l_ia(self):
        appels = []
        out = party.identify("VIR TN AUTRE BQ TAURUS SARL", CLIENTS,
                             ai_resolver=lambda op, c: appels.append(op))
        self.assertEqual(out["customer"], "STE TAURUS")
        self.assertEqual(out["method"], "fuzzy")
        self.assertEqual(appels, [], "l'IA ne doit pas etre sollicitee quand le fuzzy suffit")

    def test_ia_en_dernier_recours(self):
        out = party.identify("VIREMENT TN MM BQ MOHAMED BEJAOUI", CLIENTS,
                             ai_resolver=lambda op, c: "SOCOBAT")
        self.assertEqual(out["customer"], "SOCOBAT")
        self.assertEqual(out["method"], "ai")

    def test_reponse_ia_hors_liste_est_rejetee(self):
        out = party.identify("VIREMENT TN MM BQ INCONNU", CLIENTS,
                             ai_resolver=lambda op, c: "Client Fantome")
        self.assertIsNone(out["customer"])

    def test_sans_resolveur_le_cas_reste_non_tranche(self):
        out = party.identify("VIREMENT TN MM BQ MOHAMED BEJAOUI", CLIENTS)
        self.assertIsNone(out["customer"])
        self.assertEqual(out["method"], "aucun")


class TestAllocation(unittest.TestCase):
    def test_dette_unique_du_meme_montant(self):
        d = _dette("PE-1", "STE TAURUS", 5323.230, so="SAL-ORD-2026-02413")
        res = allocation.allocate(5323.230, [d])
        self.assertEqual(res["mode"], "exact")
        self.assertEqual(res["lignes"][0]["part"], 5323.230)

    def test_frais_de_virement_dans_la_tolerance_soldent_la_dette(self):
        """Cas reel : 5 322,240 credites pour 5 323,230 dus (0,99 de frais). La dette est soldee
        au montant COMPTABLE, sinon un residu de 0,99 traine sur le Sales Order."""
        d = _dette("PE-1", "STE TAURUS", 5323.230, so="SAL-ORD-2026-02413")
        res = allocation.allocate(5322.240, [d])
        self.assertEqual(res["mode"], "exact")
        self.assertEqual(res["total_alloue"], 5323.230)
        self.assertAlmostEqual(res["ecart"], 0.99, places=3)

    def test_tolerance_plafonnee_sur_les_gros_montants(self):
        """0,5 % d'un virement de 30 000 DT ferait 150 DT : un impaye, pas un frais."""
        self.assertEqual(allocation.tolerance(200.0), 1.0)
        self.assertEqual(allocation.tolerance(30000.0), 10.0)

    def test_virement_groupant_plusieurs_dettes(self):
        dettes = [_dette("PE-1", "C", 100.0, jour=1),
                  _dette("PE-2", "C", 250.0, jour=2),
                  _dette("PE-3", "C", 700.0, jour=3)]
        res = allocation.allocate(350.0, dettes)
        self.assertEqual(res["mode"], "groupe")
        self.assertEqual({l["dette"]["name"] for l in res["lignes"]}, {"PE-1", "PE-2"})

    def test_groupement_ambigu_ne_tranche_pas(self):
        """Deux combinaisons donnent le meme total : choisir au hasard solderait la mauvaise
        commande. On prefere un diagnostic."""
        dettes = [_dette("PE-1", "C", 100.0), _dette("PE-2", "C", 200.0),
                  _dette("PE-3", "C", 150.0), _dette("PE-4", "C", 150.0)]
        res = allocation.allocate(300.0, dettes)
        self.assertEqual(res["mode"], "aucun")
        self.assertIn("combinaisons", res["raison"])

    def test_plusieurs_dettes_du_meme_montant_ne_tranchent_pas(self):
        dettes = [_dette("PE-1", "C", 500.0), _dette("PE-2", "C", 500.0)]
        res = allocation.allocate(500.0, dettes)
        self.assertEqual(res["mode"], "aucun")

    def test_paiement_partiel_impute_la_dette_la_plus_ancienne(self):
        dettes = [_dette("PE-1", "C", 400.0, jour=1), _dette("PE-2", "C", 600.0, jour=5)]
        res = allocation.allocate(500.0, dettes)
        self.assertEqual(res["mode"], "partiel")
        self.assertEqual([(l["dette"]["name"], l["part"]) for l in res["lignes"]],
                         [("PE-1", 400.0), ("PE-2", 100.0)])
        self.assertEqual(res["total_alloue"], 500.0)

    def test_excedent_ne_produit_aucune_ligne(self):
        res = allocation.allocate(5000.0, [_dette("PE-1", "C", 400.0)])
        self.assertEqual(res["mode"], "aucun")
        self.assertIn("superieur", res["raison"])

    def test_aucune_dette_en_attente(self):
        self.assertEqual(allocation.allocate(100.0, [])["mode"], "aucun")


class TestSelectionDesCredits(unittest.TestCase):
    def test_virement_client_retenu(self):
        self.assertTrue(matching.is_virement_credit(_credit("VIR TN AUTRE BQ KEMVET", 126.0)))

    def test_virement_aramex_laisse_a_son_flux(self):
        self.assertFalse(matching.is_virement_credit(
            _credit("VIR TN AUTRE BQ ARAMEX TUNISIE", 1200.0)))

    def test_versement_especes_n_est_pas_un_virement(self):
        """'VERSEMENT ESPECES RECETTE AGENCE' = depot de caisse, pas un reglement client."""
        self.assertFalse(matching.is_virement_credit(
            _credit("VERSEMENT ESPECES RECETTE AGENCE AOUINA", 34000.0)))

    def test_debit_ignore(self):
        m = _credit("VIR TN AUTRE BQ KEMVET", 0.0)
        m["debit"] = 126.0
        self.assertFalse(matching.is_virement_credit(m))


class TestMatchVirements(unittest.TestCase):
    def test_cas_reel_taurus(self):
        mvts = [_credit("VIR TN AUTRE BQ TAURUS SARL", 5322.240, "FT26211D5NMC", jour=30)]
        dettes = [_dette("ACC-PAY-2026-04592", "STE TAURUS", 5323.230,
                         so="SAL-ORD-2026-02413", jour=14)]
        lots, diag = matching.match_virements(mvts, dettes, consumed=set(), booked=set(), schedule_check=_ok)
        self.assertEqual(len(lots), 1)
        lot = lots[0]
        self.assertEqual(lot["client"], "STE TAURUS")
        self.assertEqual(lot["n_virement"], "FT26211D5NMC")
        self.assertEqual(lot["banque"], matching.BANQUE)
        self.assertEqual(lot["total"], 5323.230)
        self.assertEqual(lot["lignes"][0]["bl"], "SAL-ORD-2026-02413")
        self.assertTrue(any(d.get("ecart") for d in diag), "l'ecart de frais doit etre signale")

    def test_reference_deja_encaissee_est_ignoree(self):
        mvts = [_credit("VIR TN AUTRE BQ TAURUS SARL", 5323.230, "FT26211D5NMC")]
        dettes = [_dette("PE-1", "STE TAURUS", 5323.230, so="SAL-ORD-2026-02413")]
        lots, diag = matching.match_virements(mvts, dettes, consumed={"FT26211D5NMC"},
                                              booked=set())
        self.assertEqual(lots, [])
        self.assertEqual(diag, [])

    def test_saisie_manuelle_est_signalee_mais_pas_rejouee(self):
        """Le comptable a saisi le virement contre la facture : l'argent est encaisse, mais la
        dette reste ouverte sur 'Dettes - A&S'. Rejouer creerait un double encaissement."""
        mvts = [_credit("VIR TN AUTRE BQ TAURUS SARL", 5323.230, "FT26190LPH3V")]
        dettes = [_dette("PE-1", "STE TAURUS", 5323.230, so="SAL-ORD-2026-02413")]
        lots, diag = matching.match_virements(mvts, dettes, consumed=set(),
                                              booked={"FT26190LPH3V"})
        self.assertEqual(lots, [])
        self.assertEqual(len(diag), 1)
        self.assertIn("hors flux dettes", diag[0]["reason"])

    def test_client_non_identifie_produit_un_diagnostic(self):
        mvts = [_credit("VIREMENT TN MM BQ MOHAMED BEJAOUI", 40.0, "FT262047GD1Q")]
        dettes = [_dette("PE-1", "STE TAURUS", 5323.230)]
        lots, diag = matching.match_virements(mvts, dettes, consumed=set(), booked=set(), schedule_check=_ok)
        self.assertEqual(lots, [])
        self.assertIn("client non identifie", diag[0]["reason"])

    def test_echeance_absente_annule_le_lot_entier(self):
        """Le server script apparie l'echeance par egalite stricte et n'ecrit rien s'il ne trouve
        pas : sans ce garde-fou, le brouillon paraitrait complet et la dette resterait ouverte."""
        mvts = [_credit("VIR TN AUTRE BQ KEMVET", 350.0, "FT-X")]
        dettes = [_dette("PE-1", "Sté KEMVET", 100.0, so="SAL-ORD-1", jour=1),
                  _dette("PE-2", "Sté KEMVET", 250.0, so="SAL-ORD-2", jour=2)]
        lots, diag = matching.match_virements(
            mvts, dettes, consumed=set(), booked=set(),
            schedule_check=lambda so, v: so != "SAL-ORD-2")
        self.assertEqual(lots, [])
        self.assertIn("silence", diag[0]["reason"])
        self.assertEqual(diag[0]["dettes"], ["PE-2"])

    def test_deux_virements_du_meme_client_ne_partagent_pas_une_dette(self):
        """Sans retrait des dettes deja prises, le second virement se verrait proposer la meme
        dette et on la solderait deux fois dans le meme brouillon."""
        mvts = [_credit("VIR TN AUTRE BQ KEMVET", 123.0, "FT-A", jour=10),
                _credit("VIR TN AUTRE BQ KEMVET", 123.0, "FT-B", jour=20)]
        dettes = [_dette("PE-1", "Sté KEMVET", 123.0, so="SAL-ORD-1", jour=1),
                  _dette("PE-2", "Sté KEMVET", 123.0, so="SAL-ORD-2", jour=2)]
        lots, _ = matching.match_virements(mvts, dettes, consumed=set(), booked=set(), schedule_check=_ok)
        # 2 dettes identiques : le 1er virement ne tranche pas (ambigu), le 2e non plus.
        # Ce qui compte : aucune dette n'est allouee deux fois.
        prises = [l["ref_paiement"] for lot in lots for l in lot["lignes"]]
        self.assertEqual(len(prises), len(set(prises)))


class TestReliquatSurCompteAramex(unittest.TestCase):
    """Cas reel Mohamed Bejaoui : commande WEB1-007803 de 46 DT livree en COD par Aramex, dont
    Aramex ne remet que 6 DT. Les 40 DT restants sont reparques sur 'Livraison Aramex - A&S'
    (mode « Dette non payée »), puis le client les vire lui-meme.

    Ce reliquat n'etait atteignable par AUCUN flux : `match_aramex` apparie par numero d'advice
    (absent ici) et `match_virements` ne regardait que 'Dettes - A&S'.
    """

    def _mvt(self):
        return [_credit("VIREMENT TN MM BQ MOHAMED BEJAOUI", 40.0, "FT262047GD1Q", jour=23)]

    def _reliquat(self, montant=40.0):
        # `sales_order` volontairement None : l'encaissement Aramex anterieur a remplace le
        # payment_schedule du Sales Order par une ligne unique de 6 DT (cf. get_pending_dettes_aramex).
        return _dette("ACC-PAY-2026-04820", "Mohamed Bejaoui", montant,
                      so=None, jour=22, origine="Livraison Aramex - A&S")

    def test_le_reliquat_est_desormais_apparie(self):
        lots, _ = matching.match_virements(
            self._mvt(), [self._reliquat()], consumed=set(), booked=set(),
            aliases={}, schedule_check=_ok)
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0]["lignes"][0]["ref_paiement"], "ACC-PAY-2026-04820")
        self.assertEqual(lots[0]["client"], "Mohamed Bejaoui")

    def test_il_part_dans_la_table_DETTES_et_non_dans_la_table_aramex(self):
        """C'est un virement client recu qui solde une dette : le compte d'attente d'origine ne
        change pas sa nature. La branche dettes supprime la PE sans regarder son `paid_to`, donc
        elle solde le compte Aramex tout aussi bien — et elle sait faire du partiel."""
        lots, _ = matching.match_virements(
            self._mvt(), [self._reliquat()], consumed=set(), booked=set(),
            aliases={}, schedule_check=_ok)
        doc = builder.build_encaissement(virement_lots=lots, insert=False)
        self.assertEqual(doc.get("livraison_aramex_a_encaisser"), [])
        self.assertEqual(len(doc.get("dette_client")), 1)
        self.assertEqual(len(doc.get("dettes_a_encaisser")), 1)
        self.assertEqual(doc.total_virement_d, 40.0)

    def test_bl_reste_vide_pour_ne_pas_declencher_un_echec_muet(self):
        """Avec `bl`, le script chercherait une echeance « Dette non payée » de 40 DT que
        l'encaissement Aramex anterieur a effacee, et n'ecrirait rien sans le dire."""
        lots, _ = matching.match_virements(
            self._mvt(), [self._reliquat()], consumed=set(), booked=set(),
            aliases={}, schedule_check=_ok)
        detail = builder.build_encaissement(virement_lots=lots,
                                            insert=False).get("dettes_a_encaisser")[0]
        self.assertFalse(detail.get("bl"))
        self.assertEqual(detail.get("n_chèque"), "FT262047GD1Q")
        self.assertEqual(detail.get("date"), date(2026, 7, 23))

    def test_un_paiement_partiel_reste_possible(self):
        """La branche dettes calcule une proportion : contrairement a la branche aramex, elle
        accepte de ne solder qu'une partie de la dette."""
        mvts = [_credit("VIREMENT TN MM BQ MOHAMED BEJAOUI", 25.0, "FT262047GD1Q", jour=23)]
        lots, diag = matching.match_virements(mvts, [self._reliquat()], consumed=set(),
                                              booked=set(), aliases={}, schedule_check=_ok)
        self.assertEqual(len(lots), 1, diag)
        self.assertEqual(lots[0]["lignes"][0]["part"], 25.0)

    def test_un_lot_peut_melanger_dette_classique_et_reliquat_aramex(self):
        mvts = [_credit("VIREMENT TN MM BQ MOHAMED BEJAOUI", 140.0, "FT-MIX", jour=23)]
        dettes = [self._reliquat(),
                  _dette("PE-DETTE", "Mohamed Bejaoui", 100.0, so="SAL-ORD-9", jour=5)]
        lots, diag = matching.match_virements(mvts, dettes, consumed=set(), booked=set(),
                                              aliases={}, schedule_check=_ok)
        self.assertEqual(len(lots), 1, diag)
        doc = builder.build_encaissement(virement_lots=lots, insert=False)
        entete = doc.get("dette_client")[0]
        parts = sum(d.get("valeur_du_cheque") for d in doc.get("dettes_a_encaisser"))
        self.assertEqual(entete.get("valeur_du_cheque"), 140.0)
        self.assertAlmostEqual(parts, 140.0, places=3)
        self.assertEqual(doc.get("livraison_aramex_a_encaisser"), [])


class TestBuilderTablesDettes(unittest.TestCase):
    def _lot(self):
        return {
            "client": "STE TAURUS", "n_virement": "FT26211D5NMC", "banque": matching.BANQUE,
            "date": date(2026, 7, 30), "total": 5323.230, "credit_bancaire": 5322.240,
            "mode": "exact", "methode_client": "fuzzy",
            "lignes": [{"ref_paiement": "ACC-PAY-2026-04592", "bl": "SAL-ORD-2026-02413",
                        "valeur": 5323.230, "part": 5323.230, "emmeteur": "STE TAURUS"}],
        }

    def test_entete_et_detail_partagent_la_cle_de_jointure(self):
        """Le server script relie detail -> en-tete par (n_chèque, banque) : si les deux tables
        divergent, la proportion n'est jamais calculee et la ligne est perdue en silence."""
        doc = builder.build_encaissement(virement_lots=[self._lot()], insert=False)
        entete = doc.get("dette_client")[0]
        detail = doc.get("dettes_a_encaisser")[0]
        self.assertEqual(entete.get("n_chèque"), detail.get("n_chèque"))
        self.assertEqual(entete.get("banque"), detail.get("banque"))
        self.assertEqual(entete.get("client"), detail.get("emmeteur"))
        self.assertEqual(entete.get("type"), "Virement")

    def test_somme_des_parts_egale_le_total_de_l_entete(self):
        """Contrainte ERPNext : les references d'une PE ne peuvent pas allouer plus que son
        montant. L'en-tete porte le total, le detail les parts."""
        doc = builder.build_encaissement(virement_lots=[self._lot()], insert=False)
        entete = doc.get("dette_client")[0]
        parts = sum(d.get("valeur_du_cheque") for d in doc.get("dettes_a_encaisser"))
        self.assertAlmostEqual(parts, entete.get("valeur_du_cheque"), places=3)
        self.assertEqual(doc.total_virement_d, 5323.230)

    def test_aucun_lot_aucun_document(self):
        self.assertIsNone(builder.build_encaissement(virement_lots=[], insert=False))
