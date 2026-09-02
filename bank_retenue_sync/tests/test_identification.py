"""Tests de l'identification bancaire : cle de mouvement, table de regles, classification,
depenses recurrentes, agregat des frais.

Meme convention que les tests existants : `unittest.TestCase` pur, entrees/sorties injectees,
aucun acces reseau ni base.
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.bank import classify as C, registry, rules as R
from bank_retenue_sync.expenses import engine, fees


def mv(operation, debit=0.0, credit=0.0, reference="FT0000", jour=2):
    return {"date": date(2023, 10, jour), "date_valeur": date(2023, 10, jour),
            "operation": operation, "reference": reference,
            "debit": debit, "credit": credit}


# Libelles REELS du releve Zitouna presents en base (DocType Bank Transaction, octobre 2023).
LIBELLES_REELS = [
    # (libelle, sens, regle attendue)
    ("ENC CHEQ TN NUM 90016921", "credit", "enc_cheque"),
    ("Encaiss cheque preavise 8818030", "credit", "enc_cheque_preavise"),
    ("VIR TN AUTRE BQ ARAMEX TUNISIE", "credit", "vir_aramex"),
    ("VIREMENT TN MM BQ CLIMA FILTRI PRO", "credit", "vir_client"),
    ("COM ENC CHEQUE TN AC-0000657260", "debit", "com_enc_cheque"),
    ("COM VIR TN AUTRE BQ AC-0000657260", "debit", "com_virement"),
    ("COM VIREMENT MM BQ AC-0000657260", "debit", "com_virement"),
    ("COMM VIREMENT RECU EN TND", "debit", "com_virement"),
    ("COMMISSION TAWASSOL", "debit", "commission_diverse"),
    ("TVA", "debit", "tva_bancaire"),
    ("DROIT DE TIMBRE", "debit", "droit_timbre"),
    ("REGLEMENT CHEQUE 4000502", "debit", "reglement_cheque"),
    ("Cheque Emis Preavise 8818030", "debit", "cheque_emis_preavise"),
    ("REGLEMENT CB 1410ORANGE TUNIS", "debit", "reglement_cb"),
    ("PAIEMENT INTERNET 1410STEG TUNIS", "debit", "paiement_internet"),
    ("RETRAIT ESPECES", "debit", "retrait_especes"),
    ("VIREMENT TN MM BQ NIZAR BELGUITH", "debit", "virement_emis"),
    ("PAIEMENT PRINCIPAL IJARA TAMOUIL MOUADDET EN", "debit", "pret_principal"),
    ("PAIEMENT PROFIT IJARA TAMOUIL MOUADDET ENNAK", "debit", "pret_profit"),
    ("PRIME TAKAFUL IJARA", "debit", "pret_assurance"),
    ("PAIEMENT PRELEVEMENT MIN DES FINANCES", "debit", "prelevement_finances"),
    ("PAIEMENT PRELEVEMENT C.N.S.S", "debit", "prelevement_cnss"),
    # Libelles apparus depuis 2023, releves sur l'export reel de juin-juillet 2026.
    ("VIR TN AUTRE BQ", "debit", "virement_emis"),
    ("COMM REMISE EFFET", "debit", "com_effet"),
    ("COMM PERQ DE CHANGE-GARANTIE", "debit", "commission_diverse"),
    ("FRAIS DE TENUE DE COMPTE", "debit", "frais_tenue_compte"),
    ("CHARGEMENT CARTE TECHNOLOGIQUE", "debit", "chargement_carte"),
    ("PAIEMENT PRINCIPAL TAMOUIL CHIRAET", "debit", "pret_principal"),
    ("PAIEMENT PROFIT TAMOUIL CHIRAET", "debit", "pret_profit"),
]


class TestReferenceMoisPrecedent(unittest.TestCase):
    """L'honoraire comptable regle le 25/05 porte la periode « 04-2026 » : sans jeton de mois
    precedent, sa reference designerait le mois du PAIEMENT et deux notes differentes
    partageraient la meme cle d'idempotence."""

    def _ref(self, jour, modele="Note d'honoraire comptable {mm_prec}-{yyyy_prec}"):
        return engine.build_reference({"template_reference": modele, "jour_reference": 25},
                                      {"date": jour, "reference": ""})

    def test_mois_precedent(self):
        self.assertEqual(self._ref(date(2026, 5, 25)), "Note d'honoraire comptable 04-2026")

    def test_bascule_d_annee(self):
        """Janvier renvoie a decembre de l'annee PRECEDENTE."""
        self.assertEqual(self._ref(date(2026, 1, 25)), "Note d'honoraire comptable 12-2025")

    def test_les_jetons_existants_restent_intacts(self):
        self.assertEqual(self._ref(date(2026, 5, 25), "Salaire {mm}-{yyyy}"), "Salaire 05-2026")


class TestMovementKey(unittest.TestCase):
    """La cle doit distinguer une commission de l'operation qui l'a generee."""

    def test_meme_mouvement_meme_cle(self):
        a = mv("ENC CHEQ TN NUM 90017253", credit=2094.0, reference="FT23291KR3ZG")
        self.assertEqual(registry.movement_key(a), registry.movement_key(dict(a)))

    def test_commission_et_operation_partagent_la_reference_mais_pas_la_cle(self):
        # Cas REEL : FT23291KR3ZG porte le credit d'encaissement ET sa commission.
        credit = mv("ENC CHEQ TN NUM 90017253", credit=2094.0, reference="FT23291KR3ZG")
        commission = mv("COM ENC CHEQUE TN AC-0000657260", debit=2.856, reference="FT23291KR3ZG")
        self.assertNotEqual(registry.movement_key(credit), registry.movement_key(commission))

    def test_le_libelle_ne_change_pas_la_cle(self):
        # La casse varie d'un export a l'autre : l'inclure creerait des doublons fantomes.
        a = mv("Encaiss cheque preavise 8818030", credit=2500.0, reference="FT1")
        b = mv("ENCAISS CHEQUE PREAVISE 8818030", credit=2500.0, reference="FT1")
        self.assertEqual(registry.movement_key(a), registry.movement_key(b))

    def test_montant_different_cle_differente(self):
        a = mv("REGLEMENT CHEQUE 4000502", debit=100.0, reference="FT2")
        b = mv("REGLEMENT CHEQUE 4000502", debit=100.001, reference="FT2")
        self.assertNotEqual(registry.movement_key(a), registry.movement_key(b))

    def test_lignes_strictement_identiques_desambiguisees(self):
        m = mv("COM VIR TN AUTRE BQ AC-0000657260", debit=1.785, reference="FT3")
        cles = [c for c, _ in registry.assign_keys([dict(m), dict(m), dict(m)])]
        self.assertEqual(len(set(cles)), 3, "trois lignes identiques doivent rester distinctes")


class TestRules(unittest.TestCase):
    """La table de regles doit couvrir 100 % des libelles reellement observes."""

    def test_chaque_libelle_reel_tombe_dans_la_bonne_regle(self):
        for libelle, sens, attendue in LIBELLES_REELS:
            m = mv(libelle, **{sens: 100.0})
            rule = R.find_rule(m)
            self.assertIsNotNone(rule, "aucune regle pour : %s" % libelle)
            self.assertEqual(rule.key, attendue, "%s -> %s (attendu %s)"
                             % (libelle, rule.key, attendue))

    def test_le_sens_separe_la_commission_de_l_encaissement(self):
        # _norm_op colle les caracteres : 'COM ENC CHEQUE TN' contient ENC et CHEQ.
        # Sans le filtre de sens, la commission serait prise pour un encaissement.
        commission = mv("COM ENC CHEQUE TN AC-0000657260", debit=2.856)
        self.assertEqual(R.find_rule(commission).key, "com_enc_cheque")

    def test_le_cheque_preavise_passe_avant_la_remise(self):
        # 'ENCAISSCHEQUEPREAVISE' contient aussi ENC et CHEQ : c'est la priorite qui tranche.
        m = mv("Encaiss cheque preavise 8818030", credit=2500.0)
        self.assertEqual(R.find_rule(m).key, "enc_cheque_preavise")

    def test_libelle_inconnu_ne_matche_aucune_regle(self):
        self.assertIsNone(R.find_rule(mv("OPERATION EXOTIQUE INCONNUE", debit=10.0)))

    def test_les_frais_ne_sont_jamais_comptabilisables_a_l_unite(self):
        for libelle in ("COM ENC CHEQUE TN AC-1", "COMMISSION TAWASSOL",
                        "COMM REMISE EFFET", "FRAIS DE TENUE DE COMPTE"):
            rule = R.find_rule(mv(libelle, debit=2.0))
            self.assertEqual(rule.action, R.ACTION_AGREGAT, libelle)
            self.assertEqual(rule.groupe, "jour", libelle)

    def test_extraction_des_numeros(self):
        self.assertEqual(R.extract_numero(R.by_key("enc_cheque"),
                                          mv("ENC CHEQ TN NUM 90016921", credit=1.0)), "90016921")
        self.assertEqual(R.extract_numero(R.by_key("enc_cheque_preavise"),
                                          mv("Encaiss cheque preavise 8818030", credit=1.0)),
                         "8818030")
        self.assertEqual(R.extract_numero(R.by_key("reglement_cheque"),
                                          mv("REGLEMENT CHEQUE 4000502", debit=1.0)), "4000502")


class TestClassification(unittest.TestCase):
    """Aucun mouvement ne doit sortir sans statut ni raison."""

    def ctx(self, **kw):
        return C.LinkContext(**kw)

    def test_libelle_inconnu_ressort_a_verifier_avec_une_raison(self):
        c = C.classify_one(mv("OPERATION EXOTIQUE", debit=10.0), self.ctx())
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)
        self.assertIn("inconnu", c.raison)

    def test_depot_de_cheque_deja_encaisse_est_identifie(self):
        m = mv("ENC CHEQ TN NUM 90016921", credit=2285.0)
        ctx = self.ctx(consumed={"cheque": {"90016921"}, "traite": set(),
                                 "aramex": set(), "virement": set()})
        c = C.classify_one(m, ctx)
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual(c.document_type, "Encaissement Paiement")

    def test_depot_non_encaisse_est_orphelin_avec_une_raison(self):
        c = C.classify_one(mv("ENC CHEQ TN NUM 90016921", credit=2285.0), self.ctx())
        self.assertEqual(c.statut, C.STATUT_ORPHELIN)
        self.assertTrue(c.raison)

    def test_reference_deja_saisie_a_la_main_est_identifiee(self):
        m = mv("VIREMENT TN MM BQ CLIMA FILTRI PRO", credit=3481.0, reference="FT23999XX")
        c = C.classify_one(m, self.ctx(booked={"FT23999XX"}))
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual(c.document_type, "Payment Entry")

    def test_depense_avec_ecriture_citant_la_reference_est_identifiee(self):
        m = mv("PAIEMENT PRINCIPAL IJARA TAMOUIL", debit=470.812, reference="FT23AAA111")
        c = C.classify_one(m, self.ctx(je_par_reference={"FT23AAA111": ["ACC-JV-0001"]}))
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual(c.document_name, "ACC-JV-0001")

    def test_plusieurs_ecritures_pour_une_reference_ne_sont_pas_tranchees(self):
        m = mv("PAIEMENT PROFIT IJARA TAMOUIL", debit=326.767, reference="FT23BBB")
        c = C.classify_one(m, self.ctx(je_par_reference={"FT23BBB": ["JV-1", "JV-2"]}))
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)

    def test_les_frais_portent_une_cle_de_groupe_journaliere(self):
        c = C.classify_one(mv("COM ENC CHEQUE TN AC-1", debit=2.856, jour=16), self.ctx())
        self.assertEqual(c.groupe, "frais-16-10-2023")

    def test_tout_mouvement_recoit_un_statut(self):
        ctx = self.ctx()
        for libelle, sens, _ in LIBELLES_REELS:
            c = C.classify_one(mv(libelle, **{sens: 100.0}), ctx)
            self.assertIn(c.statut, (C.STATUT_IDENTIFIE, C.STATUT_ORPHELIN,
                                     C.STATUT_IGNORE, C.STATUT_A_VERIFIER), libelle)

    def test_summarize_compte_tout(self):
        ctx = self.ctx()
        cs = [C.classify_one(mv(l, **{s: 10.0}), ctx) for l, s, _ in LIBELLES_REELS]
        total = sum(v["nb"] for b in C.summarize(cs).values() for v in b.values())
        self.assertEqual(total, len(LIBELLES_REELS))


# ---- Cas REELS du contrat de leasing LD2614000071 (Cenntro Logistar 100), releve du 19/06/2026.
# La banque eclate l'echeance en cinq debits ; l'ecriture ACC-JV-2026-00473 en porte le total.
ECHEANCE_CENNTRO = [
    ("PAIEMENT PRINCIPAL IJARA TAMOUIL MOUADDET ENNAKL", 324.838),
    ("PAIEMENT PROFIT IJARA TAMOUIL MOUADDET ENNAKL", 281.190),
    ("PRIME TAKAFUL IJARA", 136.245),
    ("DROIT DE TIMBRE", 1.0),
    ("TVA", 115.145),
]


def echeance(reference="LD2614000071", jour=19, lignes=ECHEANCE_CENNTRO):
    return [mv(lib, debit=montant, reference=reference, jour=jour)
            for lib, montant in lignes]


class TestApparierEcheances(unittest.TestCase):
    """Une reference `LD…` est un numero de CONTRAT : toutes les echeances la citent.

    Le rapprochement ne peut donc pas se faire ligne a ligne ni sur la seule reference — c'est
    le groupe (reference, jour) qui porte le montant de l'ecriture.
    """

    def ctx(self, ecritures=(), cites=None, **kw):
        return C.LinkContext(
            ecritures_bancaires=[dict(e) for e in ecritures],
            je_par_reference=cites or {}, **kw)

    @staticmethod
    def ecriture(nom, montant, jour=19):
        return {"voucher_no": nom, "posting_date": date(2023, 10, jour), "montant": montant}

    def test_la_somme_du_groupe_retrouve_l_ecriture(self):
        ctx = self.ctx(ecritures=[self.ecriture("ACC-JV-1", 858.418)],
                       cites={"LD2614000071": ["ACC-JV-1", "ACC-JV-VIEUX", "ACC-JV-PLUSVIEUX"]})
        cs = C.classify(echeance(), ctx)
        self.assertEqual({c.statut for c in cs}, {C.STATUT_IDENTIFIE})
        self.assertEqual({c.document_name for c in cs}, {"ACC-JV-1"})

    def test_aucune_ligne_ne_vaut_le_montant_de_l_ecriture(self):
        """Le test qui justifie le groupe : pris isolement, aucun debit n'egale 858,418."""
        self.assertNotIn(858.418, [montant for _, montant in ECHEANCE_CENNTRO])

    def test_l_ecart_est_signale_et_non_absorbe(self):
        """Cas reel LD2227700127 : l'ecriture omet le droit de timbre de 1 DT chaque mois."""
        ctx = self.ctx(ecritures=[self.ecriture("ACC-JV-2", 857.418)],
                       cites={"LD2614000071": ["ACC-JV-2", "ACC-JV-VIEUX"]})
        cs = C.classify(echeance(), ctx)
        self.assertEqual({c.statut for c in cs}, {C.STATUT_A_VERIFIER})
        self.assertIn("ecart de 1.0", cs[0].raison)
        self.assertEqual({c.document_name for c in cs}, {"ACC-JV-2"})

    def test_un_ecart_hors_tolerance_ne_matche_pas(self):
        ctx = self.ctx(ecritures=[self.ecriture("ACC-JV-3", 800.0)],
                       cites={"LD2614000071": ["ACC-JV-3", "ACC-JV-VIEUX"]})
        cs = C.classify(echeance(), ctx)
        self.assertEqual({c.statut for c in cs}, {C.STATUT_ORPHELIN})
        self.assertIn("non comptabilisee", cs[0].raison)

    def test_echeance_manquante_est_orpheline_et_non_ambigue(self):
        """Le cas du Remboursement 7/10 : plusieurs ecritures citent le contrat, aucune ne solde
        CETTE echeance. Annoncer une ambiguite d'appariement serait faux."""
        ctx = self.ctx(ecritures=[self.ecriture("ACC-JV-AUTRE", 12345.0)],
                       cites={"LD2614000071": ["ACC-JV-VIEUX", "ACC-JV-PLUSVIEUX"]})
        cs = C.classify(echeance(), ctx)
        self.assertEqual({c.statut for c in cs}, {C.STATUT_ORPHELIN})
        for c in cs:
            self.assertNotIn(C.RAISON_REFS_MULTIPLES, c.raison)
            self.assertIsNone(c.document_name)

    def test_sans_index_charge_on_ne_conclut_rien(self):
        """Index vide = information absente. Conclure « non comptabilise » serait une invention :
        on retombe sur la resolution par reference."""
        cs = C.classify(echeance(), self.ctx(cites={"LD2614000071": ["JV-1", "JV-2"]}))
        self.assertEqual({c.statut for c in cs}, {C.STATUT_A_VERIFIER})
        self.assertIn(C.RAISON_REFS_MULTIPLES, cs[0].raison)

    def test_une_ecriture_n_est_consommee_qu_une_fois(self):
        """Blocage de provision et acompte du meme montant ne peuvent pas viser la meme piece."""
        mouvements = (echeance(reference="LD-A", jour=19, lignes=[("PRIME TAKAFUL IJARA", 500.0)])
                      + echeance(reference="LD-B", jour=20,
                                 lignes=[("PRIME TAKAFUL IJARA", 500.0)]))
        ctx = self.ctx(ecritures=[self.ecriture("ACC-JV-UNIQUE", 500.0, jour=19)])
        cs = C.classify(mouvements, ctx)
        rattachees = [c for c in cs if c.document_name == "ACC-JV-UNIQUE"]
        self.assertEqual(len(rattachees), 1)

    def test_montant_exact_suffit_quand_l_ecriture_ne_cite_pas_la_reference(self):
        """Les acomptes de financement sont saisis sous un libelle metier (« Avance Cenntro »),
        sans reference bancaire : seul le montant exact les relie."""
        cs = C.classify(echeance(lignes=[("ACOMPTE SUR FINANCEMENT IS26", 5000.0)]),
                        self.ctx(ecritures=[self.ecriture("ACC-JV-4", 5000.0)]))
        self.assertEqual(cs[0].statut, C.STATUT_IDENTIFIE)
        self.assertEqual(cs[0].document_name, "ACC-JV-4")

    def test_sans_citation_un_montant_approchant_est_refuse(self):
        """La tolerance n'est admise QUE si l'ecriture cite la reference : sur le seul montant,
        elle confondrait deux operations voisines."""
        cs = C.classify(echeance(lignes=[("ACOMPTE SUR FINANCEMENT IS26", 5000.0)]),
                        self.ctx(ecritures=[self.ecriture("ACC-JV-5", 5000.5)]))
        self.assertEqual(cs[0].statut, C.STATUT_ORPHELIN)

    def test_une_ecriture_trop_lointaine_est_ignoree(self):
        ctx = self.ctx(ecritures=[self.ecriture("ACC-JV-6", 858.418, jour=25)],
                       cites={"LD2614000071": ["ACC-JV-6", "ACC-JV-VIEUX"]})
        self.assertEqual({c.statut for c in C.classify(echeance(), ctx)}, {C.STATUT_ORPHELIN})

    def test_une_tva_isolee_n_est_pas_une_echeance_manquante(self):
        """Cas reel : 17 lignes 'TVA' de reference 'CHG…' sont des TVA sur commission bancaire.
        Seules dans leur groupe, elles ne peuvent pas etre une echeance de leasing."""
        cs = C.classify(echeance(reference="CHG2614110081", lignes=[("TVA", 0.475)]),
                        self.ctx(ecritures=[self.ecriture("ACC-JV-7", 858.418)]))
        self.assertNotIn("echeance non comptabilisee", cs[0].raison)

    def test_une_tva_accompagnee_reste_un_composant_d_echeance(self):
        cs = C.classify(echeance(), self.ctx(ecritures=[self.ecriture("ACC-JV-8", 858.418)],
                                             cites={"LD2614000071": ["ACC-JV-8", "ACC-JV-VIEUX"]}))
        tva = [c for c in cs if c.regle == "tva_bancaire"][0]
        self.assertEqual(tva.statut, C.STATUT_IDENTIFIE)
        self.assertEqual(tva.document_name, "ACC-JV-8")

    def test_les_regles_d_echeance_sont_declarees(self):
        for cle in ("tva_bancaire", "droit_timbre", "pret_principal", "pret_profit",
                    "pret_assurance", "frais_dossier", "provision_blocage",
                    "acompte_financement"):
            self.assertEqual(R.by_key(cle).groupe, "echeance", cle)


class TestDepensesRecurrentes(unittest.TestCase):
    """Appariement et idempotence des depenses parametrees."""

    SALAIRE = {"cle": "salaire_test", "libelle": "Salaire Test", "actif": 1,
               "montant": 1700.0, "tolerance": 0.01, "motifs_libelle": "KOUBAA",
               "bank_rule": "virement_emis", "compte_charge": "Salaire - A&S",
               "compte_banque": "STE430127B - Zitouna - A&S", "mode_paiement": "Virement",
               "periodicite": "Mensuel", "taux_tva": 0,
               "template_reference": "Salaire Test {mm}-{yyyy}", "idempotence": "Les deux"}

    RECHARGE = dict(SALAIRE, cle="recharge_test", libelle="Recharge", montant=0,
                    motifs_libelle="TOTAL", bank_rule="",
                    compte_charge="Carte Total - A&S", periodicite="Aucune",
                    template_reference="Recharge Carte Total {reference}",
                    idempotence="Reference bancaire")

    SALAIRE_REEL = {"cle": "salaire_reel", "libelle": "Salaire", "actif": 1,
                    "montant": 1700.0, "tolerance": 0.001, "motifs_libelle": "",
                    "bank_rule": "virement_emis", "compte_charge": "Salaire - A&S",
                    "compte_banque": "STE430127B - Zitouna - A&S", "taux_tva": 0,
                    "periodicite": "Mensuel", "mode_paiement": "Virement",
                    "template_reference": "Salaire {mm}-{yyyy}", "idempotence": "Les deux"}

    def test_salaire_reconnu_sur_le_montant_seul(self):
        # Cas REEL : le releve abrege en « VIR TN AUTRE BQ », sans aucun nom de beneficiaire.
        # Le montant est alors le seul discriminant possible.
        m = mv("VIR TN AUTRE BQ", debit=1700.0)
        self.assertTrue(engine.rule_matches(self.SALAIRE_REEL, m)[0])

    def test_salaire_ne_capte_pas_un_virement_voisin(self):
        m = mv("VIR TN AUTRE BQ", debit=1700.5)
        self.assertFalse(engine.rule_matches(self.SALAIRE_REEL, m)[0])

    def test_une_commission_de_virement_n_est_pas_un_salaire(self):
        # 'COM VIR ...' est un debit contenant VIR : sans la priorite des regles de frais,
        # il serait pris pour un virement emis.
        self.assertEqual(R.find_rule(mv("COM VIR TN AUTRE BQ AC-1", debit=1.785)).key,
                         "com_virement")

    def test_les_deux_prets_ne_se_confondent_pas(self):
        ijara = dict(self.SALAIRE_REEL, cle="ijara", montant=0, motifs_libelle="IJARA",
                     bank_rule="pret_principal")
        chiraet = dict(self.SALAIRE_REEL, cle="chiraet", montant=0, motifs_libelle="CHIRAET",
                       bank_rule="pret_principal")
        m_ijara = mv("PAIEMENT PRINCIPAL IJARA TAMOUIL MOUADDET", debit=646.516)
        m_chiraet = mv("PAIEMENT PRINCIPAL TAMOUIL CHIRAET", debit=13504.704)
        self.assertTrue(engine.rule_matches(ijara, m_ijara)[0])
        self.assertFalse(engine.rule_matches(ijara, m_chiraet)[0])
        self.assertTrue(engine.rule_matches(chiraet, m_chiraet)[0])
        self.assertFalse(engine.rule_matches(chiraet, m_ijara)[0])

    def test_montant_et_libelle_se_confirment(self):
        m = mv("VIREMENT TN MM BQ KOUBAA NEJIB", debit=1700.0)
        self.assertTrue(engine.rule_matches(self.SALAIRE, m)[0])

    def test_bon_montant_mauvais_libelle_ne_matche_pas(self):
        m = mv("VIREMENT TN MM BQ AUTRE PERSONNE", debit=1700.0)
        self.assertFalse(engine.rule_matches(self.SALAIRE, m)[0])

    def test_bon_libelle_mauvais_montant_ne_matche_pas(self):
        m = mv("VIREMENT TN MM BQ KOUBAA NEJIB", debit=1650.0)
        self.assertFalse(engine.rule_matches(self.SALAIRE, m)[0])

    def test_un_credit_n_est_jamais_une_depense(self):
        m = mv("VIREMENT TN MM BQ KOUBAA NEJIB", credit=1700.0)
        self.assertFalse(engine.rule_matches(self.SALAIRE, m)[0])

    def test_ligne_sans_critere_est_refusee(self):
        vide = dict(self.SALAIRE, montant=0, motifs_libelle="")
        ok, raison = engine.rule_matches(vide, mv("VIREMENT TN MM BQ X", debit=1.0))
        self.assertFalse(ok)
        self.assertIn("sans critere", raison)

    def test_modele_de_reference_periodise(self):
        m = mv("VIREMENT TN MM BQ KOUBAA NEJIB", debit=1700.0)
        self.assertEqual(engine.build_reference(self.SALAIRE, m), "Salaire Test 10-2023")

    def test_modele_de_reference_par_reference_bancaire(self):
        # La recharge de carte n'a AUCUN identifiant periodise : sans la reference bancaire,
        # deux recharges du meme mois seraient confondues.
        a = engine.build_reference(self.RECHARGE, mv("RECHARGE TOTAL", debit=300.0, reference="FTAAA"))
        b = engine.build_reference(self.RECHARGE, mv("RECHARGE TOTAL", debit=703.5, reference="FTBBB"))
        self.assertNotEqual(a, b)
        self.assertEqual(a, "Recharge Carte Total FTAAA")

    def test_loyer_bimestriel_borne_du_15_au_15(self):
        loyer = dict(self.SALAIRE, cle="loyer", montant=5500.0, motifs_libelle="",
                     periodicite="Bimestriel", jour_reference=15,
                     template_reference="Loyer Local du {jour}-{mm}-{yyyy} au {jour}-{mm2}-{yyyy2}")
        m = mv("VIREMENT TN MM BQ BAILLEUR", debit=5500.0)
        self.assertEqual(engine.build_reference(loyer, m),
                         "Loyer Local du 15-10-2023 au 15-12-2023")

    def test_bimestriel_franchit_l_annee(self):
        loyer = dict(self.SALAIRE, periodicite="Bimestriel", jour_reference=15,
                     template_reference="{mm}-{yyyy} -> {mm2}-{yyyy2}")
        m = dict(mv("X", debit=1.0), date=date(2023, 12, 15))
        self.assertEqual(engine.build_reference(loyer, m), "12-2023 -> 02-2024")

    def test_lignes_equilibrees_sans_tva(self):
        lines = engine.build_lines(self.SALAIRE, 1700.0)
        self.assertAlmostEqual(sum(l.get("debit", 0) for l in lines),
                               sum(l.get("credit", 0) for l in lines), places=3)
        self.assertEqual(lines[0]["account"], "STE430127B - Zitouna - A&S")
        self.assertEqual(lines[-1]["account"], "Salaire - A&S")

    def test_lignes_equilibrees_avec_tva_a_rebours(self):
        regle = dict(self.SALAIRE, taux_tva=19, compte_tva="TVA 19% - A&S")
        lines = engine.build_lines(regle, 119.0)
        self.assertAlmostEqual(sum(l.get("debit", 0) for l in lines), 119.0, places=2)
        tva = [l for l in lines if l["account"] == "TVA 19% - A&S"][0]
        self.assertAlmostEqual(tva["debit"], 19.0, places=2)

    def test_idempotence_par_reference_bancaire(self):
        ctx = C.LinkContext(je_par_reference={"FTAAA": ["ACC-JV-0009"]})
        m = mv("RECHARGE TOTAL", debit=300.0, reference="FTAAA")
        self.assertEqual(
            engine._deja_comptabilise(self.RECHARGE, m, "Recharge Carte Total FTAAA", ctx),
            "ACC-JV-0009")

    def test_idempotence_par_numero_de_reference(self):
        ctx = C.LinkContext(cheque_no_index={"Salaire Test 10-2023": "ACC-JV-0010"})
        m = mv("VIREMENT TN MM BQ KOUBAA NEJIB", debit=1700.0)
        self.assertEqual(
            engine._deja_comptabilise(self.SALAIRE, m, "Salaire Test 10-2023", ctx),
            "ACC-JV-0010")

    def test_le_mode_reference_bancaire_ignore_le_numero_de_reference(self):
        # Deux recharges du meme mois ne doivent pas se neutraliser l'une l'autre.
        ctx = C.LinkContext(cheque_no_index={"Recharge Carte Total FTAAA": "ACC-JV-1"})
        m = mv("RECHARGE TOTAL", debit=300.0, reference="FTBBB")
        self.assertIsNone(
            engine._deja_comptabilise(self.RECHARGE, m, "Recharge Carte Total FTBBB", ctx))


class TestFraisBancaires(unittest.TestCase):
    """Les frais se cumulent sur le MOIS, dans une ecriture refaite a chaque nouveau frais."""

    def mouvements(self):
        return [
            mv("COM ENC CHEQUE TN AC-1", debit=2.856, reference="FT1", jour=16),
            mv("COM VIR TN AUTRE BQ AC-1", debit=1.785, reference="FT2", jour=16),
            mv("TVA", debit=1.52, reference="FT3", jour=16),
            mv("DROIT DE TIMBRE", debit=1.0, reference="FT4", jour=16),
            mv("COM VIR TN AUTRE BQ AC-1", debit=1.785, reference="FT5", jour=17),
            mv("ENC CHEQ TN NUM 90016921", credit=2285.0, reference="FT6", jour=16),
        ]

    def test_la_tva_et_le_timbre_restent_de_categorie_pret(self):
        # Verifie au centime sur les ecritures reelles : accompagnes des autres composants du
        # contrat, ils appartiennent a l'echeance de LEASING du jour. Les compter comme des frais
        # gonflait l'ecriture de 545 DT sur 659 en juin.
        # /!\ Mais SEULS de leur jour, ce sont des TVA sur commission bancaire : elles entrent
        # bien dans le cumul des frais (cf. TestTvaSurCommissionDansLesFrais).
        self.assertEqual(R.find_rule(mv("TVA", debit=115.145)).categorie, "pret")
        self.assertEqual(R.find_rule(mv("DROIT DE TIMBRE", debit=1.0)).categorie, "pret")

    def test_regroupement_par_jour_avec_les_composants_isoles(self):
        """La TVA et le timbre du jeu d'essai sont seuls sur leur reference : ce sont donc des
        frais, comme dans la saisie manuelle reelle « FRais banacaire et TVA »."""
        groupes = fees.group_daily_fees(self.mouvements())
        self.assertEqual(len(groupes), 2)
        g16 = [g for g in groupes if g.jour == date(2023, 10, 16)][0]
        self.assertEqual(len(g16.lignes), 4, "2 commissions + la TVA + le timbre, pas l'encaissement")
        self.assertAlmostEqual(g16.total, 7.161, places=3)
        self.assertAlmostEqual(g16.total_tva, 1.52, places=3)
        self.assertAlmostEqual(g16.total_timbre, 1.0, places=3)

    def test_cumul_mensuel_somme_tous_les_jours(self):
        c = fees.cumul_mensuel(self.mouvements(), "2023-10")
        self.assertAlmostEqual(c.total, 8.946, places=3)
        self.assertEqual(c.cle, "Frais bancaire 10-2023")
        self.assertEqual(c.jour, date(2023, 10, 17), "datee du dernier frais connu du mois")

    def test_cumul_journalier_est_croissant(self):
        suivi = fees.cumul_journalier(self.mouvements(), "2023-10")
        self.assertEqual([s["jour"] for s in suivi],
                         [date(2023, 10, 16), date(2023, 10, 17)])
        self.assertAlmostEqual(suivi[0]["cumul"], 7.161, places=3)
        self.assertAlmostEqual(suivi[1]["cumul"], 8.946, places=3)

    def test_le_cumul_est_recalcule_et_ne_double_jamais(self):
        # Rejouer le meme releve doit donner le meme total : le cumul repart du releve,
        # il ne s'incremente pas a partir de l'ecriture existante.
        m = self.mouvements()
        self.assertAlmostEqual(fees.cumul_mensuel(m, "2023-10").total,
                               fees.cumul_mensuel(m + [], "2023-10").total, places=3)

    def test_un_mois_sans_frais_rend_un_cumul_vide(self):
        c = fees.cumul_mensuel(self.mouvements(), "2023-11")
        self.assertEqual(c.total, 0.0)
        self.assertEqual(c.lignes, [])

    def test_cle_mensuelle_lisible(self):
        self.assertEqual(fees.cle_mensuelle("2026-07"), "Frais bancaire 07-2026")
        self.assertEqual(fees.periode_de(date(2026, 7, 14)), "2026-07")

    def test_references_conservees_pour_le_garde_fou(self):
        c = fees.cumul_mensuel(self.mouvements(), "2023-10")
        self.assertEqual(sorted(c.references), ["FT1", "FT2", "FT3", "FT4", "FT5"])


class TestSourcesEmail(unittest.TestCase):
    """Les defauts de la table doivent reproduire EXACTEMENT ce que l'orchestrateur faisait en
    dur : toute divergence serait un changement de comportement sur des flux valides en prod."""

    def setUp(self):
        from bank_retenue_sync.mail import config as mc
        self.mc = mc
        self.src = {d["cle"]: mc._normalise(dict(d)) for d in mc.DEFAULTS}

    def test_les_six_sources_sont_declarees(self):
        self.assertEqual(set(self.src), {
            "total_invoice", "aramex_invoice", "aramex_payment_advice",
            "comptable_honoraire", "comptable_declaration", "comptable_cnss"})

    def test_valeurs_identiques_a_l_ancien_code_en_dur(self):
        attendu = {
            "total_invoice": ("totalenergies.com", "facture", 4, ".zip", ""),
            "aramex_invoice": ("e.aramex.com", "E-INV", 3, ".pdf", ""),
            "aramex_payment_advice": ("e.aramex.com", "", 20, ".xls", ""),
            "comptable_honoraire": ("belghithayman@gmail.com", "", 6, ".pdf", "honoraire"),
            "comptable_declaration": ("belghithayman@gmail.com", "", 25, ".pdf", "decl.ste"),
            "comptable_cnss": ("belghithayman@gmail.com", "CNSS", 15, ".pdf", ""),
        }
        for cle, (exp, suj, lim, ext, motif) in attendu.items():
            s = self.src[cle]
            self.assertEqual(s["expediteurs"], exp, cle)
            self.assertEqual(s["sujet"], suj, cle)
            self.assertEqual(s["limite"], lim, cle)
            self.assertEqual(s["extension"], ext, cle)
            self.assertEqual(s["motif_nom_piece_jointe"], motif, cle)

    def test_advice_aramex_sans_filtre_de_sujet(self):
        # Volontaire : le sujet varie. Le tri se fait sur l'extension (.xls = advice,
        # .pdf = facture E-INV), pas sur le sujet.
        self.assertEqual(self.src["aramex_payment_advice"]["sujet"], "")
        self.assertNotEqual(self.src["aramex_invoice"]["extension"],
                            self.src["aramex_payment_advice"]["extension"])

    def test_expediteurs_multiples_sont_decoupes(self):
        s = self.mc._normalise({"cle": "x", "expediteurs": "a.com, b.com ,c.com"})
        self.assertEqual(s["expediteurs_liste"], ["a.com", "b.com", "c.com"])

    def test_source_supprimee_retombe_sur_le_defaut(self):
        # Un flux ne doit jamais s'arreter parce qu'une ligne de configuration a ete effacee.
        self.assertEqual(self.mc._DEFAUTS_PAR_CLE["total_invoice"]["sujet"], "facture")

    def test_source_inconnue_echoue_bruyamment(self):
        with self.assertRaises(KeyError):
            self.mc.get_source("source_qui_n_existe_pas")


class TestCalendrier(unittest.TestCase):
    """Declenchement a date fixe : salaires 2 jours avant la fin du mois, loyer le 15."""

    SALAIRE = {"cle": "s", "libelle": "Salaire", "actif": 1, "declencheur": "Calendrier",
               "jours_avant_fin_mois": 2, "jour_declenchement": 0, "mois_ancre": 0,
               "periodicite": "Mensuel", "montant": 1700.0}
    LOYER = {"cle": "l", "libelle": "Loyer", "actif": 1, "declencheur": "Calendrier",
             "jours_avant_fin_mois": 0, "jour_declenchement": 15, "mois_ancre": 6,
             "periodicite": "Bimestriel", "montant": 5500.0}

    def test_deux_jours_avant_la_fin_du_mois(self):
        from bank_retenue_sync.expenses import calendrier
        for annee, mois, attendu in ((2026, 6, 28), (2026, 7, 29), (2026, 2, 26), (2024, 2, 27)):
            self.assertEqual(calendrier.date_declenchement(self.SALAIRE, annee, mois),
                             date(annee, mois, attendu), f"{mois}/{annee}")

    def test_jour_fixe_du_mois(self):
        from bank_retenue_sync.expenses import calendrier
        self.assertEqual(calendrier.date_declenchement(self.LOYER, 2026, 6), date(2026, 6, 15))

    def test_bimestriel_ancre_ne_tombe_qu_un_mois_sur_deux(self):
        from bank_retenue_sync.expenses import calendrier
        self.assertTrue(calendrier.mois_concerne(self.LOYER, 6))
        self.assertTrue(calendrier.mois_concerne(self.LOYER, 8))
        self.assertFalse(calendrier.mois_concerne(self.LOYER, 7))

    def test_sans_ancrage_une_regle_non_mensuelle_ne_se_declenche_pas(self):
        # Mieux vaut ne rien creer que deviner le rythme.
        from bank_retenue_sync.expenses import calendrier
        self.assertFalse(calendrier.mois_concerne(dict(self.LOYER, mois_ancre=0), 6))

    def test_seules_les_regles_calendaires_sont_prises(self):
        from bank_retenue_sync.expenses import calendrier
        banque = dict(self.SALAIRE, cle="b", declencheur="Banque")
        self.assertEqual([r["cle"] for r in calendrier.regles_calendaires([self.SALAIRE, banque])],
                         ["s"])

    def test_echeances_du_jour(self):
        from bank_retenue_sync.expenses import calendrier
        rows = [self.SALAIRE, self.LOYER]
        self.assertEqual([r[0]["cle"] for r in calendrier.echeances_du_jour(date(2026, 6, 28), rows)], ["s"])
        self.assertEqual([r[0]["cle"] for r in calendrier.echeances_du_jour(date(2026, 6, 15), rows)], ["l"])
        self.assertEqual(calendrier.echeances_du_jour(date(2026, 7, 15), rows), [])


class TestContratsFinancement(unittest.TestCase):
    """Le contrat s'identifie par le TOTAL du jour : les deux prets partagent le meme libelle."""

    NANTISSEMENT = {"cle": "nant", "libelle": "Pret nantissement", "actif": 1, "type": "Pret",
                    "total_mensuel": 17705.228, "tolerance": 0.01, "nb_echeances": 10,
                    "date_debut": "2026-01-26", "compte_banque": "STE430127B - Zitouna - A&S",
                    "compte_principal": "Prêts garantis - A&S",
                    "compte_interet": "Frais bancaire Emprunt - A&S",
                    "template_reference": "Remboursement {n}/{total} Pret nantissement"}
    LIGNE = dict(NANTISSEMENT, cle="ligne", libelle="Ligne de credit", total_mensuel=14134.538,
                 nb_echeances=6, date_debut="2026-05-29",
                 template_reference="Remboursement {n}/{total} Pret Ligne de credit")

    def mouvements(self):
        return [
            mv("PAIEMENT PRINCIPAL TAMOUIL CHIRAET", debit=17099.432, reference="FT1"),
            mv("PAIEMENT PROFIT TAMOUIL CHIRAET", debit=605.796, reference="FT2"),
        ]

    def test_paire_principal_profit_du_meme_jour(self):
        from bank_retenue_sync.expenses import contrats
        paires = [p for p in contrats.paires_du_releve(self.mouvements()) if not p.get("incomplet")]
        self.assertEqual(len(paires), 1)
        self.assertAlmostEqual(paires[0]["total"], 17705.228, places=3)
        self.assertAlmostEqual(paires[0]["principal"], 17099.432, places=3)

    def test_le_total_identifie_le_contrat(self):
        from bank_retenue_sync.expenses import contrats
        paire = contrats.paires_du_releve(self.mouvements())[0]
        c = contrats.contrat_de(paire, [self.NANTISSEMENT, self.LIGNE])
        self.assertEqual(c["cle"], "nant")

    def test_deux_contrats_le_meme_jour_s_apparient_par_reference(self):
        # 28/08/2026 : le 29 tombant un samedi, les DEUX prets ont ete preleves le meme jour.
        # Le nantissement (echeance 7/10) a le plus gros principal mais — ses interets fondant —
        # le plus PETIT profit : l'appariement par rang decroissant croisait les couples
        # (17781.034 et 14058.732, inconnus des contrats). La reference LD… tranche.
        from bank_retenue_sync.expenses import contrats
        mvts = [
            mv("PAIEMENT PROFIT TAMOUIL CHIRAET", debit=381.340, reference="LD2613900011"),
            mv("PAIEMENT PRINCIPAL TAMOUIL CHIRAET", debit=13753.198, reference="LD2613900011"),
            mv("PAIEMENT PRINCIPAL TAMOUIL CHIRAET", debit=17399.694, reference="LD2602600081"),
            mv("PAIEMENT PROFIT TAMOUIL CHIRAET", debit=305.534, reference="LD2602600081"),
        ]
        paires = [p for p in contrats.paires_du_releve(mvts) if not p.get("incomplet")]
        self.assertEqual(len(paires), 2)
        self.assertEqual(sorted(round(p["total"], 3) for p in paires),
                         [14134.538, 17705.228])
        cles = {contrats.contrat_de(p, [self.NANTISSEMENT, self.LIGNE])["cle"] for p in paires}
        self.assertEqual(cles, {"nant", "ligne"})

    def test_un_mouvement_sans_jumeau_ne_produit_rien(self):
        # Une echeance a moitie imputee serait pire que pas d'ecriture du tout.
        from bank_retenue_sync.expenses import contrats
        seul = [mv("PAIEMENT PRINCIPAL TAMOUIL CHIRAET", debit=17099.432, reference="FT1")]
        paires = contrats.paires_du_releve(seul)
        self.assertEqual(len(paires), 1)
        self.assertIn("incomplet", paires[0])

    def test_compteur_d_echeance(self):
        from bank_retenue_sync.expenses import contrats
        self.assertEqual(contrats.numero_echeance(self.NANTISSEMENT, date(2026, 6, 26)), 6)
        self.assertEqual(contrats.numero_echeance(self.LIGNE, date(2026, 6, 29)), 2)

    def test_reference_a_l_identique_des_saisies_manuelles(self):
        from bank_retenue_sync.expenses import contrats
        self.assertEqual(contrats.build_reference(self.NANTISSEMENT, date(2026, 6, 26)),
                         "Remboursement 6/10 Pret nantissement")
        self.assertEqual(contrats.build_reference(self.LIGNE, date(2026, 6, 29)),
                         "Remboursement 2/6 Pret Ligne de credit")

    def test_quatre_lignes_avec_deux_credits_bancaires(self):
        from bank_retenue_sync.expenses import contrats
        paire = contrats.paires_du_releve(self.mouvements())[0]
        lines = contrats.build_lines(self.NANTISSEMENT, paire)
        self.assertEqual(len(lines), 4)
        credits = [l for l in lines if l.get("credit")]
        self.assertEqual(len(credits), 2, "deux credits bancaires distincts, comme l'existant")
        self.assertTrue(all(l["account"] == "STE430127B - Zitouna - A&S" for l in credits))
        self.assertAlmostEqual(sum(l.get("debit", 0) for l in lines),
                               sum(l.get("credit", 0) for l in lines), places=3)

    def test_total_inconnu_ne_cree_rien(self):
        from bank_retenue_sync.expenses import contrats
        paire = {"total": 999.999}
        self.assertIsNone(contrats.contrat_de(paire, [self.NANTISSEMENT, self.LIGNE]))


class TestDateFactureFournisseur(unittest.TestCase):
    """Facture recue par email -> ecriture datee du dernier jour du mois precedent."""

    def test_fin_du_mois_precedent(self):
        from bank_retenue_sync.expenses import journal
        self.assertEqual(journal.fin_mois_precedent(date(2026, 7, 14)), date(2026, 6, 30))
        self.assertEqual(journal.fin_mois_precedent(date(2026, 3, 2)), date(2026, 2, 28))
        self.assertEqual(journal.fin_mois_precedent(date(2026, 1, 5)), date(2025, 12, 31))

    def test_absence_de_date_ne_casse_rien(self):
        from bank_retenue_sync.expenses import journal
        self.assertIsNone(journal.fin_mois_precedent(None))

    def test_date_de_message_illisible(self):
        from bank_retenue_sync.mail import config as mc
        self.assertIsNone(mc.message_date({"date": "pas une date"}))
        self.assertEqual(mc.message_date({"date": "Tue, 14 Jul 2026 08:12:00 +0100"}),
                         date(2026, 7, 14))


class TestPertesNonPaiement(unittest.TestCase):
    """Ecart entre le montant attendu et le montant credite : perte, ou impaye a signaler."""

    def setUp(self):
        from bank_retenue_sync.expenses import pertes
        self.p = pertes

    def finder(self, table):
        return lambda ref: table.get(ref)

    def test_tolerance_plancher_plafond(self):
        # Plancher : les frais fixes de virement sur un petit montant.
        self.assertEqual(self.p.tolerance(100.0), 1.0)
        # Proportionnelle au milieu.
        self.assertAlmostEqual(self.p.tolerance(1000.0), 5.0, places=3)
        # Plafond : ne jamais pardonner un impaye significatif sur un gros montant.
        self.assertEqual(self.p.tolerance(100000.0), 10.0)

    def test_ecart_dans_la_tolerance_est_une_perte(self):
        m = [mv("VIREMENT TN MM BQ CLIENT", credit=5322.240, reference="FTA")]
        f = self.finder({"FTA": {"name": "PE-1", "party": "X", "paid_amount": 5323.230}})
        e = self.p.ecarts_du_releve(m, pe_finder=f)
        self.assertEqual(len(e), 1)
        self.assertAlmostEqual(e[0].ecart, 0.990, places=3)
        self.assertTrue(e[0].dans_tolerance)

    def test_ecart_hors_tolerance_n_est_pas_comptabilise(self):
        m = [mv("VIREMENT TN MM BQ CLIENT", credit=4000.0, reference="FTB")]
        f = self.finder({"FTB": {"name": "PE-2", "party": "X", "paid_amount": 5000.0}})
        c = self.p.cumul_mensuel(m, "2023-10", pe_finder=f)
        self.assertEqual(c.total, 0.0, "un impaye ne doit jamais etre efface par une ecriture")
        self.assertEqual(len(c.hors_tolerance), 1)

    def test_credit_superieur_a_l_attendu_n_est_pas_une_perte(self):
        m = [mv("VIREMENT TN MM BQ CLIENT", credit=5100.0, reference="FTC")]
        f = self.finder({"FTC": {"name": "PE-3", "party": "X", "paid_amount": 5000.0}})
        self.assertEqual(self.p.ecarts_du_releve(m, pe_finder=f), [])

    def test_cumul_mensuel_et_cle(self):
        m = [mv("VIREMENT TN MM BQ A", credit=999.0, reference="F1", jour=3),
             mv("VIREMENT TN MM BQ B", credit=499.5, reference="F2", jour=9)]
        f = self.finder({"F1": {"name": "PE-A", "paid_amount": 1000.0},
                         "F2": {"name": "PE-B", "paid_amount": 500.0}})
        c = self.p.cumul_mensuel(m, "2023-10", pe_finder=f)
        self.assertAlmostEqual(c.total, 1.5, places=3)
        self.assertEqual(c.cle, "Perte de non paiement 10-2023")
        self.assertEqual(c.jour, date(2023, 10, 9), "datee du dernier ecart du mois")

    def test_sans_payment_entry_aucun_ecart(self):
        # Un credit sans piece en face n'est pas une perte : c'est un encaissement non saisi.
        m = [mv("VIREMENT TN MM BQ INCONNU", credit=800.0, reference="FTZ")]
        self.assertEqual(self.p.ecarts_du_releve(m, pe_finder=self.finder({})), [])


class TestSolde(unittest.TestCase):
    """Trois valeurs : banque officielle, cumul du registre, ERPNext."""

    def test_le_cumul_part_du_solde_de_depart(self):
        from bank_retenue_sync.bank import solde
        original = solde.flux_registre
        solde.flux_registre = lambda date_min=None, date_max=None: {
            "credits": 1000.0, "debits": 400.0, "net": 600.0, "mouvements": 3}
        try:
            self.assertAlmostEqual(solde.solde_cumule(5000.0, date(2026, 5, 1)), 5600.0, places=3)
        finally:
            solde.flux_registre = original


class TestApparierParNumeroDeCheque(unittest.TestCase):
    """Un reglement fournisseur porte son n° de cheque dans le libelle du releve ET dans la
    reference de la Payment Entry. C'est la preuve la plus forte — plus qu'un montant, qui peut
    etre partage par deux reglements ou diverger de quelques millimes."""

    def pe(self, name, ref, montant, jour=18):
        return {"name": name, "reference_no": ref, "paid_amount": montant, "sens": "debit",
                "posting_date": date(2026, 5, jour), "party": "Fournisseur"}

    def mvt(self, montant, jour=18):
        return {"date": date(2026, 5, jour), "operation": "REGLEMENT CHEQUE 4000968 LE CHAUFFAGE",
                "reference": "FT26138ABCDE", "debit": montant, "credit": 0.0}

    def test_le_numero_apparie_malgre_un_ecart_de_montant(self):
        """Cas reel : banque 237,134 contre 237,150 sur la piece."""
        from bank_retenue_sync.expenses import lookup

        pe, mode, ecart = lookup.apparier_payment_entry(
            self.mvt(237.134), [self.pe("PE-1", "4000968", 237.150)], numero="4000968")
        self.assertEqual(mode, "numero")
        self.assertEqual(pe["name"], "PE-1")
        self.assertAlmostEqual(ecart, 0.016, places=3)

    def test_le_numero_tranche_ou_deux_montants_identiques_bloquaient(self):
        """Cas reel du cheque 4000962 : 400,000 des deux cotes, mais deux PE de ce montant."""
        from bank_retenue_sync.expenses import lookup

        pes = [self.pe("PE-A", "4000962", 400.0), self.pe("PE-B", "4000999", 400.0)]
        sans, _, _ = lookup.apparier_payment_entry(self.mvt(400.0), pes)
        avec, mode, _ = lookup.apparier_payment_entry(self.mvt(400.0), pes, numero="4000962")
        self.assertIsNone(sans, "sans numero, deux montants egaux ne doivent pas etre tranches")
        self.assertEqual((avec["name"], mode), ("PE-A", "numero"))

    def test_un_numero_tronque_ne_matche_pas(self):
        """Reference fautive '400966' pour un cheque 4000966 : l'egalite est stricte."""
        from bank_retenue_sync.expenses import lookup

        pe, mode, _ = lookup.apparier_payment_entry(
            self.mvt(492.963), [self.pe("PE-1", "400966-Bq Zitouna", 492.963)], numero="4000966")
        self.assertNotEqual(mode, "numero")

    def test_deux_pieces_portant_le_meme_numero_ne_sont_pas_tranchees(self):
        from bank_retenue_sync.expenses import lookup

        pes = [self.pe("PE-A", "4000968", 100.0), self.pe("PE-B", "4000968 bis", 200.0)]
        pe, mode, _ = lookup.apparier_payment_entry(self.mvt(100.0), pes, numero="4000968")
        self.assertNotEqual(mode, "numero")

    def test_extraction_des_numeros_d_une_reference(self):
        from bank_retenue_sync.expenses import lookup

        self.assertEqual(lookup.numeros_de("400966-Bq Zitouna"), {"400966"})
        self.assertEqual(lookup.numeros_de("4001004"), {"4001004"})
        self.assertEqual(lookup.numeros_de(None), set())
        self.assertEqual(lookup.numeros_de("Bq 12345"), set(), "moins de 6 chiffres : ignore")

    def test_la_reference_bancaire_reste_prioritaire(self):
        from bank_retenue_sync.expenses import lookup

        pes = [self.pe("PE-REF", "FT26138ABCDE", 999.0), self.pe("PE-NUM", "4000968", 237.134)]
        pe, mode, _ = lookup.apparier_payment_entry(self.mvt(237.134), pes, numero="4000968")
        self.assertEqual((pe["name"], mode), ("PE-REF", "reference"))


class TestReglementSaisiEnEcriture(unittest.TestCase):
    """Tous les reglements ne passent pas par une Payment Entry : certains sont saisis en direct,
    avec le n° de cheque dans la remarque (« Achat quincaillerie … Chq N° 4000969 bq Zitouna »)."""

    def mvt(self, montant=312.757, numero="4000969"):
        return mv("REGLEMENT CHEQUE %s FOURNISSEUR" % numero, debit=montant, reference="FT-X")

    def ctx(self, noms, montant=312.757, voucher="ACC-JV-2026-00369"):
        return C.LinkContext(
            je_par_reference={"4000969": noms},
            ecritures_bancaires=[{"voucher_no": voucher, "posting_date": date(2026, 5, 22),
                                  "montant": montant}])

    def test_l_ecriture_citant_le_numero_identifie_le_mouvement(self):
        c = C.classify_one(self.mvt(), self.ctx(["ACC-JV-2026-00369"]))
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual(c.document_type, "Journal Entry")
        self.assertEqual(c.document_name, "ACC-JV-2026-00369")

    def test_l_ecart_de_montant_est_signale(self):
        """Cas reel du cheque 4001008 : 231,821 preleves pour 232,136 comptabilises."""
        c = C.classify_one(self.mvt(231.821), self.ctx(["ACC-JV-2026-00369"], montant=232.136))
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertIn("ecart", c.raison)
        self.assertAlmostEqual(c.ecart, -0.315, places=3)

    def test_deux_ecritures_citant_le_meme_numero_n_identifient_rien(self):
        c = C.classify_one(self.mvt(), self.ctx(["JV-1", "JV-2"]))
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)
        self.assertIsNone(c.document_name)

    def test_sans_numero_cite_le_mouvement_reste_a_verifier(self):
        c = C.classify_one(self.mvt(), C.LinkContext())
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)

    def test_la_payment_entry_reste_prioritaire_sur_l_ecriture(self):
        ctx = self.ctx(["ACC-JV-2026-00369"])
        ctx.pe_bancaires = [{"name": "PE-1", "reference_no": "4000969", "paid_amount": 312.757,
                             "sens": "debit", "posting_date": date(2026, 5, 22)}]
        c = C.classify_one(self.mvt(), ctx)
        self.assertEqual(c.document_type, "Payment Entry")


class TestSoumissionDeLEcritureDeFrais(unittest.TestCase):
    """Qui decide que l'ecriture mensuelle de frais part au grand livre.

    La regle historique — « re-soumettre seulement si celle qu'on remplace l'etait » — laissait
    le PREMIER frais du mois en brouillon, meme reglage « soumettre automatiquement » coche :
    il n'y a rien a remplacer ce jour-la. L'ecran d'identification affichait alors, chaque debut
    de mois, un ecart « reste a comptabiliser » jusqu'a ce que quelqu'un y pense (02/09/2026,
    ACC-JV-2026-00675, 1,190 DT).
    """

    def decision(self, existant, etait_soumise, auto):
        """La regle telle qu'elle est ecrite dans `sync_ecriture_mensuelle`."""
        return bool(etait_soumise or auto)

    def test_le_premier_frais_du_mois_part_si_le_reglage_est_coche(self):
        self.assertTrue(self.decision(existant=None, etait_soumise=False, auto=True))

    def test_le_premier_frais_reste_en_brouillon_si_le_reglage_est_decoche(self):
        """Reglage decoche : rien ne change, la soumission reste humaine."""
        self.assertFalse(self.decision(existant=None, etait_soumise=False, auto=False))

    def test_remplacer_une_ecriture_soumise_la_re_soumet_toujours(self):
        """Sinon le total du mois sortirait du grand livre a chaque nouveau frais, et y
        resterait jusqu'a validation — sept passages par jour."""
        for auto in (True, False):
            self.assertTrue(self.decision(existant="ACC-JV-1", etait_soumise=True, auto=auto))

    def test_un_brouillon_deja_en_place_est_rattrape(self):
        """Ne soumettre que la PREMIERE ecriture du mois laissait septembre en
        brouillon pour toujours : elle existait deja, donc jamais « premiere ».
        Le reglage du site fait foi (ACC-JV-2026-00685, 02/09/2026)."""
        self.assertTrue(self.decision(existant="ACC-JV-1", etait_soumise=False, auto=True))

    def test_reglage_decoche_rien_ne_part_jamais(self):
        """Le seul cas ou une ecriture reste en brouillon : l'utilisateur n'a pas
        demande la soumission automatique."""
        self.assertFalse(self.decision(existant="ACC-JV-1", etait_soumise=False, auto=False))
        self.assertFalse(self.decision(existant=None, etait_soumise=False, auto=False))


class TestTvaSurCommissionDansLesFrais(unittest.TestCase):
    """Une TVA de reference 'CHG…', seule de son jour, est une TVA sur COMMISSION bancaire et
    entre dans l'ecriture mensuelle de frais. Le reclassement de `tva_bancaire` en categorie
    « pret » (pour le leasing) avait rendu ces lignes invisibles au cumul."""

    def test_une_tva_isolee_entre_dans_le_cumul_des_frais(self):
        mvts = [mv("COM VIR TN AUTRE BQ AC-1", debit=1.190, reference="FT-A", jour=5),
                mv("TVA", debit=0.475, reference="CHG2614110081", jour=5)]
        g = fees.group_daily_fees(mvts)
        self.assertEqual(len(g), 1)
        self.assertAlmostEqual(g[0].total, 1.665, places=3)
        self.assertAlmostEqual(g[0].total_tva, 0.475, places=3)
        self.assertAlmostEqual(g[0].total_commission, 1.190, places=3)

    def test_une_tva_d_echeance_de_leasing_reste_hors_des_frais(self):
        """Le meme libelle, mais accompagne des autres composants du contrat : c'est du leasing."""
        mvts = [mv("PAIEMENT PRINCIPAL IJARA", debit=324.838, reference="LD2614000071", jour=19),
                mv("PRIME TAKAFUL IJARA", debit=136.245, reference="LD2614000071", jour=19),
                mv("TVA", debit=115.145, reference="LD2614000071", jour=19)]
        g = fees.group_daily_fees(mvts)
        self.assertEqual(g, [], "aucune de ces lignes n'est un frais bancaire")

    def test_le_timbre_isole_compte_aussi(self):
        mvts = [mv("DROIT DE TIMBRE", debit=1.0, reference="CHG9999999", jour=7)]
        g = fees.group_daily_fees(mvts)
        self.assertAlmostEqual(g[0].total_timbre, 1.0, places=3)


class TestReferenceBancaireCiteeParUneEcriture(unittest.TestCase):
    """Un flux « sans automatisation prevue » (paiement Orange, recharge) est souvent saisi a la
    main, avec la REFERENCE BANCAIRE dans la remarque. Sans cette passe, l'ecriture existe et le
    mouvement ressort quand meme « a verifier »."""

    def mvt(self, ref="FT262165WY1Q", montant=25.928):
        return mv("PAIEMENT INTERNET 0408ORANGE TUNIS", debit=montant, reference=ref)

    def ctx(self, ref="FT262165WY1Q", voucher="ACC-JV-2026-00563", montant=25.928):
        return C.LinkContext(
            je_par_reference={ref: [voucher]},
            ecritures_bancaires=[{"voucher_no": voucher, "posting_date": date(2026, 8, 4),
                                  "montant": montant}])

    def test_le_mouvement_est_identifie(self):
        c = C.classify_one(self.mvt(), self.ctx())
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual(c.document_name, "ACC-JV-2026-00563")

    def test_deux_ecritures_citant_la_reference_n_identifient_rien(self):
        c = C.classify_one(self.mvt(), self.ctx())
        c2 = C.classify_one(self.mvt(), C.LinkContext(
            je_par_reference={"FT262165WY1Q": ["JV-1", "JV-2"]}))
        self.assertEqual(c2.statut, C.STATUT_A_VERIFIER)

    def test_sans_ecriture_le_mouvement_reste_a_verifier(self):
        c = C.classify_one(self.mvt(), C.LinkContext())
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)
        self.assertIn("aucune automatisation", c.raison)


class TestFraisRattachesAuCumulMensuel(unittest.TestCase):
    """Un frais n'est jamais comptabilise a l'unite, mais des que l'ecriture mensuelle existe il
    EST comptabilise : le laisser « a verifier » gonflait le reste-a-faire de 146 lignes."""

    def ctx(self, avec_ecriture=True):
        idx = {"Frais bancaire 10-2023": "ACC-JV-2023-00999"} if avec_ecriture else {}
        return C.LinkContext(cheque_no_index=idx)

    def test_un_frais_couvert_par_l_ecriture_du_mois_est_identifie(self):
        c = C.classify_one(mv("COM ENC CHEQUE TN AC-1", debit=2.856, jour=16), self.ctx())
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual(c.document_name, "ACC-JV-2023-00999")

    def test_sans_ecriture_du_mois_il_reste_a_verifier(self):
        c = C.classify_one(mv("COM ENC CHEQUE TN AC-1", debit=2.856, jour=16),
                           self.ctx(avec_ecriture=False))
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)
        self.assertIn("agregat journalier", c.raison)

    def test_la_tva_sur_commission_suit_le_meme_cumul(self):
        """Elle entre dans l'ecriture mensuelle : elle doit donc etre identifiee comme les frais."""
        c = C.classify_one(mv("TVA", debit=0.475, reference="CHG2614110081", jour=16), self.ctx())
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual(c.document_name, "ACC-JV-2023-00999")

    def test_la_cle_de_groupe_journaliere_reste_posee(self):
        c = C.classify_one(mv("COM ENC CHEQUE TN AC-1", debit=2.856, jour=16), self.ctx())
        self.assertEqual(c.groupe, "frais-16-10-2023")
