"""Reglement des dettes fournisseur : compte d'attente -> banque au virement emis.

Le rapprochement se fait sur le SEUL montant (le releve abrege le virement emis en
« VIR TN AUTRE BQ », sans nom de beneficiaire). Ces tests portent donc surtout sur ses REFUS :
une ecriture est annulee et supprimee, un faux positif detruirait une piece comptable.
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.expenses import reglement as AR


def _facture(name, montant, jour=31, mois=7):
    return {"name": name, "posting_date": date(2026, mois, jour), "montant": montant,
            "docstatus": 0, "cheque_no": "Facture Aramex %02d-2026" % mois,
            "user_remark": "Facture Aramex 1900531365 (2026-%02d)" % mois}


def _vir(montant, jour, mois=8, reference="FT26220ABCDE", operation="VIR TN AUTRE BQ"):
    return {"date": date(2026, mois, jour), "operation": operation, "reference": reference,
            "debit": montant, "credit": 0.0}


class TestSelectionDesVirements(unittest.TestCase):
    def test_un_credit_n_est_jamais_un_virement_emis(self):
        m = _vir(958.650, 5)
        m["debit"], m["credit"] = 0.0, 958.650
        self.assertEqual(AR.virements_emis([m]), [])

    def test_un_debit_sans_VIR_est_ecarte(self):
        self.assertEqual(AR.virements_emis([_vir(958.650, 5, operation="REGLEMENT CB 0508")]), [])

    def test_le_libelle_ne_nomme_pas_aramex_et_c_est_normal(self):
        """Le releve reel abrege en « VIR TN AUTRE BQ » : exiger 'ARAMEX' ne trouverait rien."""
        self.assertEqual(len(AR.virements_emis([_vir(958.650, 5)])), 1)


class TestAppariement(unittest.TestCase):
    def test_montant_exact_et_posterieur_est_apparie(self):
        paires, diag = AR.apparier([_facture("JV-1", 958.650)], [_vir(958.650, 5)])
        self.assertEqual(len(paires), 1)
        self.assertEqual(paires[0]["facture"]["name"], "JV-1")
        self.assertEqual(diag, [])

    def test_un_millime_d_ecart_suffit_a_refuser(self):
        """Le montant est le seul critere : il ne souffre aucune tolerance metier."""
        paires, diag = AR.apparier([_facture("JV-1", 958.650)], [_vir(958.700, 5)])
        self.assertEqual(paires, [])
        self.assertEqual(diag[0]["status"], "en attente")

    def test_un_virement_anterieur_a_la_facture_ne_la_regle_pas(self):
        paires, diag = AR.apparier([_facture("JV-1", 958.650)], [_vir(958.650, 15, mois=7)])
        self.assertEqual(paires, [])

    def test_un_virement_trop_lointain_est_une_coincidence(self):
        paires, _ = AR.apparier([_facture("JV-1", 958.650, mois=1)], [_vir(958.650, 5, mois=8)])
        self.assertEqual(paires, [])

    def test_deux_virements_du_meme_montant_ne_sont_pas_tranches(self):
        paires, diag = AR.apparier(
            [_facture("JV-1", 958.650)],
            [_vir(958.650, 5, reference="FT-A"), _vir(958.650, 6, reference="FT-B")])
        self.assertEqual(paires, [])
        self.assertEqual(diag[0]["status"], "ambigu")

    def test_deux_factures_du_meme_montant_ne_sont_pas_tranchees(self):
        """Sinon le virement solderait arbitrairement l'une des deux, et l'autre serait perdue."""
        paires, diag = AR.apparier(
            [_facture("JV-1", 958.650, mois=6, jour=30), _facture("JV-2", 958.650)],
            [_vir(958.650, 5)])
        self.assertEqual(paires, [])
        self.assertTrue(all(d["status"] == "ambigu" for d in diag))

    def test_une_reference_deja_consommee_est_ignoree(self):
        paires, _ = AR.apparier([_facture("JV-1", 958.650)], [_vir(958.650, 5, reference="FT-X")],
                                consommes={"FT-X"})
        self.assertEqual(paires, [])

    def test_deux_factures_de_montants_distincts_se_reglent_chacune(self):
        paires, _ = AR.apparier(
            [_facture("JV-1", 958.650), _facture("JV-2", 945.280, mois=6, jour=30)],
            [_vir(958.650, 5, reference="FT-A"), _vir(945.280, 9, mois=7, reference="FT-B")])
        self.assertEqual({p["facture"]["name"] for p in paires}, {"JV-1", "JV-2"})


class TestLibelleDeReglement(unittest.TestCase):
    def test_la_reference_bancaire_est_ajoutee(self):
        """Format des saisies reelles : « Fac ARAMEX au 30-06-2026 | Réf de paiement :FT... »."""
        r = AR._remarque_reglee("Fac ARAMEX au 30-06-2026", "FT26189PFRLK")
        self.assertIn("Fac ARAMEX au 30-06-2026", r)
        self.assertIn("FT26189PFRLK", r)

    def test_une_reference_deja_presente_n_est_pas_dupliquee(self):
        base = "Fac ARAMEX au 30-06-2026 | Réf de paiement : FT26189PFRLK"
        self.assertEqual(AR._remarque_reglee(base, "FT26189PFRLK"), base)

    def test_une_remarque_vide_ne_laisse_pas_de_separateur_orphelin(self):
        self.assertFalse(AR._remarque_reglee("", "FT-X").startswith("|"))


class TestDeuxCycles(unittest.TestCase):
    """Aramex et note d'honoraire suivent le MEME cycle, sur deux comptes d'attente distincts."""

    def test_les_deux_cycles_sont_declares(self):
        self.assertEqual({c["cle"] for c in AR.CYCLES}, {"aramex", "honoraire"})

    def test_chaque_cycle_a_son_compte_d_attente(self):
        self.assertEqual(AR.cycle("aramex")["compte"], "Créditeurs - A&S")
        self.assertEqual(AR.cycle("honoraire")["compte"], "Compte de découvert bancaire - A&S")

    def test_aramex_se_distingue_par_le_TIERS(self):
        """`Créditeurs` porte tous les fournisseurs : sans le tiers, on reglerait n'importe lequel."""
        self.assertEqual(AR.cycle("aramex")["party"], "ARAMEX")

    def test_l_honoraire_se_distingue_par_le_LIBELLE(self):
        """Le compte de decouvert porte aussi les transitaires (SUNLINE, Frotec, Elestar) et
        aucune de ces lignes n'a de tiers : seul le libelle les separe."""
        cyc = AR.cycle("honoraire")
        self.assertIsNone(cyc["party"])
        self.assertEqual(cyc["marqueur"], "honoraire")

    def test_un_cycle_inconnu_echoue_bruyamment(self):
        with self.assertRaises(ValueError):
            AR.cycle("inexistant")

    def test_le_flux_est_nomme_par_son_cycle(self):
        _, diag = AR.apparier([_facture("JV-1", 463.720)], [], cle="honoraire")
        self.assertEqual(diag[0]["flux"], "reglement_honoraire")


class TestCycleAnticipe(unittest.TestCase):
    """Salaires et loyer : l'ecriture est creee AVANT le virement, sur un compte d'attente, puis
    recreee sur la banque quand le releve le confirme."""

    def test_les_regles_calendaires_ont_un_compte_d_attente(self):
        """Sans lui, l'ecriture anticipee crediterait la banque alors que rien n'en est sorti."""
        from bank_retenue_sync.expenses import defaults

        calendaires = [r for r in defaults.DEFAULTS if r.get("declencheur") == "Calendrier"]
        # 4 salaires, le loyer et l'honoraire comptable. On verifie la PRESENCE des flux plutot
        # qu'un compte : ajouter une regle calendaire ne doit pas casser un test dont le sujet
        # est le compte d'attente.
        self.assertEqual({r["cle"] for r in calendaires} >= {"loyer_bureau", "honoraire_comptable"},
                         True)
        self.assertGreaterEqual(len(calendaires), 6)
        for r in calendaires:
            self.assertEqual(r.get("compte_attente"), "Compte de découvert bancaire - A&S", r["cle"])

    def test_l_anticipation_credite_l_attente_et_non_la_banque(self):
        from bank_retenue_sync.expenses import engine

        row = {"compte_banque": "STE430127B - Zitouna - A&S",
               "compte_attente": "Compte de découvert bancaire - A&S",
               "compte_charge": "Salaire - A&S"}
        anticipee = dict(row, compte_banque=row["compte_attente"])
        lignes = engine.build_lines(anticipee, 1700.0)
        self.assertEqual(lignes[0]["account"], "Compte de découvert bancaire - A&S")
        self.assertEqual(lignes[0]["credit"], 1700.0)

    def test_les_salaires_ne_sont_pas_repris_par_le_cycle_honoraire(self):
        """Les deux vivent sur le compte de decouvert : seul le marqueur de libelle les separe."""
        marqueur = AR.cycle("honoraire")["marqueur"]
        self.assertNotIn(marqueur.upper(), "SALAIRE KOUBAÂ NÉJIB 07-2026".upper())
