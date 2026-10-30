"""Frais de lettre de credit affectes a un fournisseur (decision utilisateur 30/10/2026).

Cas reel du 29/10/2026, reference TF2629600003 : COMM OUV - ANNUL LCI 2 616,269 + TVA 497,091,
COMM SWIFT 50 + TVA 9,500, COM PROJ LCI 30 + TVA 5,700. Regles pures et branchements.
"""
from __future__ import annotations

import inspect
import os
import unittest
from datetime import date

from bank_retenue_sync.api import mouvements as API
from bank_retenue_sync.bank import classify as C, rules as R

PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "bank_retenue_sync", "page", "identification_bancaire",
                    "identification_bancaire.js")


def _m(operation, debit, ref="TF2629600003", jour=date(2026, 10, 29)):
    return {"date": jour, "operation": operation, "reference": ref, "debit": debit, "credit": 0.0}


class TestRegles(unittest.TestCase):
    def test_les_commissions_de_lc_sortent_du_cumul_des_frais(self):
        for libelle in ("COMM OUV - ANNUL LCI", "COM PROJ LCI"):
            rule = R.find_rule(_m(libelle, 30.0))
            self.assertEqual(rule.key, "frais_lc", libelle)
            self.assertEqual((rule.categorie, rule.groupe), (R.CATEGORIE_LC, "echeance"))
        rule = R.find_rule(_m("COMM SWIFT", 50.0))
        self.assertEqual((rule.key, rule.categorie), ("frais_lc_swift", R.CATEGORIE_LC))

    def test_les_autres_commissions_restent_des_frais_du_mois(self):
        self.assertEqual(R.find_rule(_m("COMM PERQ DE CHANGE-GARANTIE", 5.0)).key, "commission_diverse")
        self.assertEqual(R.find_rule(_m("COM ENC CHEQUE TN", 2.856)).categorie, "frais_bancaires")

    def test_la_tva_garde_sa_regle_mais_rejoint_le_groupe(self):
        self.assertEqual(R.find_rule(_m("TVA", 497.091)).key, "tva_bancaire")


class TestVentilation(unittest.TestCase):
    LIGNES = [{"operation": "COMM OUV - ANNUL LCI", "regle": "frais_lc", "debit": 2616.269},
              {"operation": "TVA", "regle": "tva_bancaire", "debit": 497.091},
              {"operation": "COMM SWIFT", "regle": "frais_lc_swift", "debit": 50.0},
              {"operation": "TVA", "regle": "tva_bancaire", "debit": 9.5},
              {"operation": "COM PROJ LCI", "regle": "frais_lc", "debit": 30.0},
              {"operation": "TVA", "regle": "tva_bancaire", "debit": 5.7}]

    def test_hors_taxe_et_tva(self):
        self.assertEqual(API.ventiler_frais_lc(self.LIGNES), {"ht": 2696.269, "tva": 512.291, "total": 3208.56})

    def test_pourcentages(self):
        lignes = API.pourcentages_frais_lc(
            {"MAYAR DE COMMERCE": {"ht": 2696.269, "tva": 512.291, "nb": 1},
             "SANS ACHAT": {"ht": 10.0, "tva": 1.9, "nb": 1}},
            {"MAYAR DE COMMERCE": 134813.45})
        self.assertEqual(lignes[0]["fournisseur"], "MAYAR DE COMMERCE")
        self.assertEqual(lignes[0]["pourcentage"], 2.0)
        self.assertIsNone(lignes[1]["pourcentage"])


class TestGroupeLC(unittest.TestCase):
    def test_le_groupe_porte_la_categorie_et_l_appel_a_affecter(self):
        c = C.Classification(cle="x", debit=497.091)
        rule = R.find_rule(_m("TVA", 497.091))
        ctx = C.LinkContext(echeances={C.cle_echeance(_m("TVA", 497.091)):
                                       {"voucher": None, "montant": 0.0, "ecart": 0.0, "nb": 6,
                                        "total": 3208.56, "lc": True}})
        C._resoudre_echeance(rule, _m("TVA", 497.091), c, ctx)
        self.assertEqual((c.categorie, c.statut), (R.CATEGORIE_LC, C.STATUT_ORPHELIN))
        self.assertTrue(c.groupe.startswith("lc-TF2629600003"))
        self.assertIn("Affecter à un fournisseur (LC)", c.raison)

    def test_un_groupe_de_leasing_n_est_pas_touche(self):
        c = C.Classification(cle="x", debit=151.54)
        m = _m("TVA", 151.54, ref="LD2227700127")
        rule = R.find_rule(m)
        ctx = C.LinkContext(echeances={C.cle_echeance(m): {"voucher": "ACC-JV-1", "montant": 858.418,
                                                           "ecart": 0.0, "nb": 5, "total": 858.418, "lc": False}})
        C._resoudre_echeance(rule, m, c, ctx)
        self.assertEqual((c.categorie, c.statut), ("", C.STATUT_IDENTIFIE))


class TestBranchements(unittest.TestCase):
    def test_apparier_marque_les_groupes_lc(self):
        src = inspect.getsource(C.apparier_echeances)
        self.assertIn('if rule.categorie == R.CATEGORIE_LC:', src)
        self.assertIn('"lc": g.get("lc", False)', src)

    def test_l_affectation_cree_une_charge_et_sa_tva_qui_citent_la_reference(self):
        src = inspect.getsource(API.affecter_frais_lc)
        self.assertIn('{"account": _compte_frais_lc(), "debit": v["ht"]}', src)
        self.assertIn('{"account": fees.FEE_TVA_ACCOUNT, "debit": v["tva"]}', src)
        self.assertIn('{"account": BANK_ACCOUNT, "credit": v["total"]}', src)
        self.assertIn("Fournisseur : {0}", src)
        self.assertIn("PREFIXE_FRAIS_LC", src)
        self.assertNotIn("Payment Entry", src)        # jamais une avance fournisseur

    def test_le_compte_est_un_actif_en_attente_lu_par_le_dossier(self):
        from bank_retenue_sync.expenses import frais_lc as F
        from bank_retenue_sync.facturation import charges as CH
        self.assertEqual(F.COMPTE_FRAIS_LC, "Frais de LC en attente - A&S")
        self.assertEqual(F.COMPTE_PARENT_FRAIS_LC, "Prêts et avances (actif) - A&S")
        self.assertIn('"root_type": "Asset"', inspect.getsource(F.compte_frais_lc))
        self.assertIn("compte_frais_lc(creer=False)", inspect.getsource(CH.comptes_charges))
        self.assertIn("([frais_lc] if frais_lc else [])", inspect.getsource(CH.liste))
        self.assertIn(F.COMPTE_FRAIS_LC, CH.COMPTES_EXEMPTES)
        self.assertIn("Salaire - A&S", CH.COMPTES_EXEMPTES)      # bulletin de paie + virement
        self.assertIn("Loyer du Bureau - A&S", CH.COMPTES_EXEMPTES)  # bail + virement
        # Échéance de leasing saisie par le flux des contrats : le LD… est dans la remarque.
        self.assertEqual(CH.exemption({"ref": "Echeance Leasing Cenntro 09-2026", "categorie": "Frais de Déplacement - A&S"}),
                         "échéance de leasing : portée par le contrat")
        self.assertEqual(CH.exemption({"ref": "Dépense caisse — recharge", "categorie": "Frais de Déplacement - A&S"}), "")

    def test_la_page_porte_le_bouton_et_le_rapport(self):
        with open(PAGE, encoding="utf-8") as f:
            js = f.read()
        self.assertIn('data-frais-lc=', js)
        self.assertIn('if (r.categorie === "frais_lc") {', js)
        self.assertIn('this._m("affecter_frais_lc")', js)
        self.assertIn('this._m("frais_lc_par_fournisseur")', js)
        self.assertIn("Frais de LC par fournisseur (%)", js)


class TestUnBoutonParOperation(unittest.TestCase):
    def test_le_bouton_est_sur_la_ligne_la_plus_grosse_et_defaire_sait_l_annuler(self):
        with open(PAGE, encoding="utf-8") as f:
            js = f.read()
        self.assertIn("_est_tete_lc(r)", js)
        self.assertIn('if (lc_tete && r.statut !== "Identifie") {', js)
        self.assertIn('(lc_tete && r.statut === "Identifie" && r.document_type === "Journal Entry")', js)
        src = inspect.getsource(API.defaire_rattachement)
        self.assertIn('.startswith(PREFIXE_FRAIS_LC)', src)
        self.assertIn('"defait": "frais_lc"', src)


class TestMoisEnCours(unittest.TestCase):
    """Le sélecteur de la facturation mensuelle propose le mois en cours (consultation), que la
    clôture refuse (demande utilisateur 30/10/2026)."""

    def test_le_mois_en_cours_est_propose_en_tete_et_marque(self):
        from bank_retenue_sync.facturation import periode as P
        mois = P.derniers(2, date(2026, 10, 30), avec_courant=True)
        self.assertEqual([m["cle"] for m in mois], ["2026-10", "2026-09", "2026-08"])
        self.assertEqual(mois[0]["libelle"], "octobre 2026 (en cours)")
        self.assertTrue(mois[0].get("en_cours"))
        self.assertEqual([m["cle"] for m in P.derniers(2, date(2026, 10, 30))], ["2026-09", "2026-08"])

    def test_le_defaut_reste_le_mois_precedent_et_la_cloture_refuse_le_courant(self):
        from bank_retenue_sync.api import cloture as CL
        from bank_retenue_sync.facturation import periode as P
        self.assertEqual(P.normaliser(None, date(2026, 10, 30)), "2026-09")
        self.assertTrue(P.est_en_cours("2026-10", date(2026, 10, 30)))
        self.assertFalse(P.est_en_cours("2026-09", date(2026, 10, 30)))
        for fn in (CL.generer_dossier, CL.marquer_envoye):
            self.assertIn("_refuser_mois_en_cours(mois)", inspect.getsource(fn))


class TestLeasingVersChargesVehicules(unittest.TestCase):
    """Patch leasing_vers_charges_vehicules : contrats et anciennes echeances refaites a leur date."""

    def test_le_patch_est_declare_et_refait_chaque_echeance_a_sa_date(self):
        from bank_retenue_sync.patches import leasing_vers_charges_vehicules as P
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "patches.txt"), encoding="utf-8") as f:
            self.assertIn("bank_retenue_sync.patches.leasing_vers_charges_vehicules", f.read())
        self.assertEqual(P.COMPTE_VEHICULES, "Charges remboursement véhicules - A&S")
        self.assertEqual(P.PLANCHER, "2026-01-01")
        src = inspect.getsource(P)
        self.assertNotIn("Reclassement", src.split('"""', 2)[2])      # plus d'ecriture datee du jour
        self.assertIn("nouvelle.posting_date = ancienne.posting_date", src)
        self.assertIn("ifnull(je.cheque_no, '') not like 'Assurance%%'", src)
        self.assertIn("update `tabBRS Bank Movement` set document_name", src)


class TestSalaireSalma(unittest.TestCase):
    def test_la_regle_suit_le_modele_des_autres_salaires_et_cree_octobre(self):
        from bank_retenue_sync.patches import ajouter_salaire_salma_ben_said as P
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "patches.txt"), encoding="utf-8") as f:
            self.assertIn("bank_retenue_sync.patches.ajouter_salaire_salma_ben_said", f.read())
        r = P.REGLE
        self.assertEqual((r["cle"], r["type"], r["montant"], r["actif"]), ("salaire_salma_ben_said", "Salaire", 700.0, 1))
        self.assertEqual((r["declencheur"], r["jours_avant_fin_mois"], r["periodicite"]), ("Calendrier", 2, "Mensuel"))
        self.assertEqual((r["compte_charge"], r["compte_attente"], r["mode_paiement"]),
                         ("Salaire - A&S", "Compte de découvert bancaire - A&S", "Virement"))
        self.assertEqual(r["template_reference"], "Salaire Salma Ben Saïd {mm}-{yyyy}")
        self.assertEqual(P.PREMIER_MOIS, date(2026, 10, 31))
        self.assertIn("rattrapage=True", inspect.getsource(P.execute))
