"""Analyse du compte « Economiq Aqua Solution - A&S » — le rapprochement pur.

Le jeu de donnees reprend le compte tel qu'il etait sur la base de prod du 04/11/2026 : mai a
octobre 2026, cinq ecritures de bilan passees, celle d'octobre a passer. Le compte etait a
164,700 ; les ventes d'octobre a crediter, 155,000 ; il restait donc 9,700 une fois l'ecriture
passee — quatre timbres fiscaux et 5,700 de TVA d'un prix hors taxe, encaisses par Economiq et
que l'ecriture de bilan ne reprend pas.

`unittest.TestCase` pur, sans site. Lancer depuis apps/bank_retenue_sync :
`python -m unittest bank_retenue_sync.tests.test_compte_partenaire`.
"""
import copy
import unittest

from bank_retenue_sync.partenaire import compte as C
from bank_retenue_sync.partenaire import historique

TIMBRE = [{"libelle": "Timbre Fiscal", "montant": 1.0}]


def _cmd(so, vente, ttc=None, taxes=None, paiements=None, client="Client"):
    return {"sales_order": so, "customer": client, "vente": vente,
            "total_ttc": vente if ttc is None else ttc, "taxes_en_sus": taxes or [],
            "paiements": paiements or []}


def _pe(nom, date, montant, so):
    return {"date": date, "voucher_type": "Payment Entry", "voucher_no": nom, "debit": montant,
            "credit": 0.0, "remarque": "", "commandes": [{"sales_order": so, "montant": montant}],
            "non_affecte": 0.0}


def _je(nom, date, credit, remarque="Ventes Economiq"):
    return {"date": date, "voucher_type": "Journal Entry", "voucher_no": nom, "debit": 0.0,
            "credit": credit, "remarque": remarque, "commandes": [], "non_affecte": 0.0}


def novembre_2026():
    """Le compte au 04/11/2026."""
    mois = [
        {"mois": "2026-05", "libelle": "mai 2026", "ventes": 684.0,
         "ecritures": ["ACC-JV-2026-00429"],
         "commandes": [_cmd("SAL-ORD-2026-01519", 684.0, 685.0, TIMBRE)]},
        {"mois": "2026-06", "libelle": "juin 2026", "ventes": 352.0,
         "ecritures": ["ACC-JV-2026-00474"],
         "commandes": [_cmd("SAL-ORD-2026-01972", 30.0, 31.0, TIMBRE),
                       _cmd("SAL-ORD-2026-01973", 75.0), _cmd("SAL-ORD-2026-02071", 147.0),
                       _cmd("SAL-ORD-2026-02151", 50.0), _cmd("SAL-ORD-2026-02221", 50.0)]},
        {"mois": "2026-07", "libelle": "juillet 2026", "ventes": 118.0,
         "ecritures": ["ACC-JV-2026-00609"],
         "commandes": [_cmd("SAL-ORD-2026-02236", 48.0), _cmd("SAL-ORD-2026-02424", 40.0),
                       _cmd("SAL-ORD-2026-02501", 30.0, 35.7,
                            [{"libelle": "TVA 19%", "montant": 5.7}])]},
        {"mois": "2026-08", "libelle": "août 2026", "ventes": 1475.0,
         "ecritures": ["ACC-JV-2026-00695"],
         "commandes": [_cmd("SAL-ORD-2026-02849", 140.0, 141.0, TIMBRE),
                       _cmd("WEB1-007987", 667.0, 668.0, TIMBRE),
                       _cmd("SAL-ORD-2026-03202", 668.0)]},
        {"mois": "2026-09", "libelle": "septembre 2026", "ventes": 705.0,
         "ecritures": ["ACC-JV-2026-00864"],
         "commandes": [_cmd("SAL-ORD-2026-03495", 705.0), _cmd("SAL-ORD-2026-03807", 0.0)]},
        {"mois": "2026-10", "libelle": "octobre 2026", "ventes": 155.0, "ecritures": [],
         "commandes": [_cmd("SAL-ORD-2026-04209", 115.0), _cmd("SAL-ORD-2026-04515", 40.0)]},
        {"mois": "2026-11", "libelle": "novembre 2026", "ventes": 0.0, "ecritures": [],
         "en_cours": True, "commandes": []},
    ]
    mouvements = [
        _pe("ACC-PAY-2026-02578", "2026-05-11", 685.0, "SAL-ORD-2026-01519"),
        _je("ACC-JV-2026-00429", "2026-05-31", 684.0),
        # Un reglement d'une commande hors bilan, repris par une ligne qui le cite.
        _je("ACC-JV-2026-00429", "2026-05-31", 211.0,
            "Remboursement paiement pour vente Acid Citrique reçu par Economiq "
            "[réf: ACC-PAY-2026-03392]"),
        _pe("ACC-PAY-2026-03392", "2026-06-09", 211.0, "SAL-ORD-2026-01914"),
        _pe("ACC-PAY-2026-04301", "2026-06-10", 75.0, "SAL-ORD-2026-01973"),
        _pe("ACC-PAY-2026-04302", "2026-06-10", 31.0, "SAL-ORD-2026-01972"),
        _pe("ACC-PAY-2026-04303", "2026-06-19", 147.0, "SAL-ORD-2026-02071"),
        _pe("ACC-PAY-2026-04304", "2026-06-22", 50.0, "SAL-ORD-2026-02151"),
        _pe("ACC-PAY-2026-04080", "2026-06-30", 50.0, "SAL-ORD-2026-02221"),
        _je("ACC-JV-2026-00474", "2026-06-30", 352.0),
        _pe("ACC-PAY-2026-05909", "2026-07-01", 48.0, "SAL-ORD-2026-02236"),
        _pe("ACC-PAY-2026-04602", "2026-07-14", 40.0, "SAL-ORD-2026-02424"),
        _pe("ACC-PAY-2026-04746", "2026-07-20", 35.7, "SAL-ORD-2026-02501"),
        _je("ACC-JV-2026-00609", "2026-07-31", 118.0),
        _pe("ACC-PAY-2026-05364", "2026-08-07", 141.0, "SAL-ORD-2026-02849"),
        _pe("ACC-PAY-2026-05817", "2026-08-11", 668.0, "WEB1-007987"),
        _pe("ACC-PAY-2026-06406", "2026-08-31", 668.0, "SAL-ORD-2026-03202"),
        _je("ACC-JV-2026-00695", "2026-08-31", 1475.0),
        _pe("ACC-PAY-2026-06979", "2026-09-14", 705.0, "SAL-ORD-2026-03495"),
        _je("ACC-JV-2026-00864", "2026-09-30", 705.0),
        _pe("ACC-PAY-2026-07708", "2026-10-10", 115.0, "SAL-ORD-2026-04209"),
        # Commande d'octobre (date de la tache), reglee le 2 novembre : elle compte en octobre.
        _pe("ACC-PAY-2026-08201", "2026-11-02", 40.0, "SAL-ORD-2026-04515"),
    ]
    return 0.0, mois, mouvements


class TestProjection(unittest.TestCase):
    def setUp(self):
        self.r = C.rapprocher(*novembre_2026())

    def test_le_solde_et_ce_qui_reste_a_crediter(self):
        self.assertEqual(self.r["solde"], 164.7)
        self.assertEqual(self.r["a_crediter"], 155.0)
        self.assertEqual([m["mois"] for m in self.r["a_passer"]], ["2026-10"])

    def test_le_compte_ne_revient_pas_a_zero(self):
        self.assertEqual(self.r["projete"], 9.7)
        self.assertTrue(self.r["coherent"])

    def test_l_ecart_est_entierement_explique_par_les_taxes(self):
        self.assertEqual(self.r["par_cause"], {C.TAXES: 9.7})
        self.assertEqual([(c["sales_order"], c["ecart"]) for c in self.r["commandes"]], [
            ("SAL-ORD-2026-01519", 1.0), ("SAL-ORD-2026-01972", 1.0),
            ("SAL-ORD-2026-02501", 5.7), ("SAL-ORD-2026-02849", 1.0), ("WEB1-007987", 1.0)])

    def test_la_cause_nomme_la_taxe(self):
        par_so = {c["sales_order"]: c["cause"]["texte"] for c in self.r["commandes"]}
        self.assertIn("Timbre Fiscal 1,000", par_so["SAL-ORD-2026-01519"])
        self.assertIn("TVA 19% 5,700", par_so["SAL-ORD-2026-02501"])

    def test_ecart_par_mois(self):
        self.assertEqual({m["mois"]: m["ecart"] for m in self.r["mois"]}, {
            "2026-05": 1.0, "2026-06": 1.0, "2026-07": 5.7, "2026-08": 2.0, "2026-09": 0.0,
            "2026-10": 0.0, "2026-11": 0.0})

    def test_un_reglement_regle_apres_le_mois_compte_dans_son_mois_de_bilan(self):
        octobre = [m for m in self.r["mois"] if m["mois"] == "2026-10"][0]
        self.assertEqual((octobre["encaisse"], octobre["credite"], octobre["a_passer"]),
                         (155.0, None, True))

    def test_un_mois_en_cours_vide_n_est_pas_a_passer(self):
        novembre = [m for m in self.r["mois"] if m["mois"] == "2026-11"][0]
        self.assertFalse(novembre["a_passer"])

    def test_le_reglement_repris_par_une_ligne_qui_le_cite_est_lettre(self):
        lettres = [h for h in self.r["hors_bilan"] if h["nature"] == C.LETTRE]
        self.assertEqual(sorted(h["montant"] for h in lettres), [-211.0, 211.0])
        self.assertNotIn("hors_bilan", self.r["par_cause"])     # net nul


class TestRetourAZero(unittest.TestCase):
    def test_reprendre_les_taxes_dans_l_ecriture_ramene_a_zero(self):
        """Si l'ecriture credite ce qui a ete encaisse, le compte revient a zero."""
        ouverture, mois, mouvements = novembre_2026()
        mouvements = [dict(m) for m in mouvements]
        for mv in mouvements:
            if mv["voucher_no"] == "ACC-JV-2026-00695":
                mv["credit"] = 1477.0                       # 1 475 + deux timbres
        mouvements.append(_je("ACC-JV-2026-00999", "2026-10-31", 162.7))
        mois[5]["ecritures"] = ["ACC-JV-2026-00999"]       # octobre : 155 + 7,700 d'arrieres
        r = C.rapprocher(ouverture, mois, mouvements)
        self.assertEqual(r["projete"], 0.0)
        self.assertTrue(r["coherent"])
        self.assertEqual(r["a_passer"], [])


class TestCauses(unittest.TestCase):
    def _un_mois(self, commandes, mouvements, ecritures=(), en_cours=False):
        mois = [{"mois": "2026-10", "libelle": "octobre 2026", "en_cours": en_cours,
                 "ventes": sum(c["vente"] for c in commandes), "ecritures": list(ecritures),
                 "commandes": commandes, "aqua_world": ["SAL-ORD-AW"], "ecartees": ["SAL-ORD-OUV"]}]
        return C.rapprocher(0.0, mois, mouvements)

    def test_reglee_ailleurs(self):
        r = self._un_mois([_cmd("SO-1", 300.0, paiements=[
            {"compte": "STE430127B - Zitouna - A&S", "mode": "Virement", "montant": 300.0}])], [])
        (c,) = r["commandes"]
        self.assertEqual((c["ecart"], c["cause"]["code"]), (-300.0, C.AILLEURS))
        self.assertIn("Zitouna", c["cause"]["texte"])
        self.assertEqual(r["projete"], -300.0)

    def test_pas_encore_encaissee_dans_le_mois_en_cours(self):
        r = self._un_mois([_cmd("SO-1", 80.0)], [], en_cours=True)
        self.assertEqual(r["commandes"][0]["cause"]["code"], C.NON_ENCAISSE)
        self.assertIn("Pas encore", r["commandes"][0]["cause"]["texte"])

    def test_trop_percu_sans_taxe(self):
        r = self._un_mois([_cmd("SO-1", 80.0)], [_pe("PE-1", "2026-10-03", 90.0, "SO-1")])
        self.assertEqual(r["commandes"][0]["cause"]["code"], C.TROP_PERCU)

    def test_reglement_d_une_commande_cote_aqua_world(self):
        r = self._un_mois([], [_pe("PE-1", "2026-10-03", 50.0, "SAL-ORD-AW")])
        (h,) = r["hors_bilan"]
        self.assertEqual((h["nature"], h["montant"]), (C.HORS_BILAN, 50.0))
        self.assertIn("côté Aqua World", h["texte"])
        self.assertEqual(r["projete"], 50.0)
        self.assertTrue(r["coherent"])

    def test_commande_ecartee_et_reste_non_affecte(self):
        pe = _pe("PE-1", "2026-10-03", 70.0, "SAL-ORD-OUV")
        pe["commandes"] = [{"sales_order": "SAL-ORD-OUV", "montant": 50.0}]
        pe["non_affecte"] = 20.0
        r = self._un_mois([], [pe])
        self.assertEqual(sorted((h["nature"], h["montant"]) for h in r["hors_bilan"]),
                         [(C.HORS_BILAN, 50.0), (C.NON_AFFECTE, 20.0)])
        self.assertIn("tâche ouverte", r["hors_bilan"][0]["texte"])
        self.assertTrue(r["coherent"])

    def test_ecriture_qui_credite_autre_chose_que_le_bilan(self):
        r = self._un_mois([_cmd("SO-1", 100.0)],
                          [_pe("PE-1", "2026-10-03", 100.0, "SO-1"),
                           _je("JV-1", "2026-10-31", 90.0)], ecritures=["JV-1"])
        (m,) = r["mois"]
        self.assertEqual((m["ecart"], m["ecart_ecriture"]), (10.0, 10.0))
        self.assertEqual(r["par_cause"], {"ecriture": 10.0})
        self.assertEqual(r["projete"], 10.0)


class TestMontant(unittest.TestCase):
    def test_format(self):
        self.assertEqual(C.montant(9183.72), "9 183,720")
        self.assertEqual(C.montant(-9.7), "-9,700")
        self.assertEqual(C.montant(None), "0,000")


class TestMoisProjete(unittest.TestCase):
    """`historique.projete` : le mois affiche, tel qu'il serait enregistre."""

    def tableau(self, **modifs):
        t = {"disponible": True, "enregistre": False, "mois": "2026-10",
             "libelle": "octobre 2026", "ajustement": 425.07, "report": 425.07,
             "echeancier": [{"date": "2026-10-31", "montant": 0.0, "deduit": 0.0,
                             "note": "M (fin du mois) — néant"},
                            {"date": "2026-11-30", "montant": 0.0, "deduit": 0.0,
                             "note": "M+1 — néant"}]}
        t.update(modifs)
        return t

    def test_un_mois_sans_commande_porte_son_report(self):
        p = historique.projete(self.tableau())
        self.assertEqual((p["mois"], p["report"], p["projete"]), ("2026-10", 425.07, True))
        self.assertEqual([e["statut"] for e in p["echeances"]], ["non_payé", "non_payé"])

    def test_une_echeance_absorbee_l_est_aussi_en_projection(self):
        p = historique.projete(self.tableau(echeancier=[
            {"date": "2026-10-31", "montant": 0.0, "deduit": 50.0, "note": "M — absorbée"},
            {"date": "2026-11-30", "montant": 70.0, "deduit": 0.0, "note": "M+1"}]))
        self.assertEqual([(e["statut"], e["montant_brut"], e["reste"]) for e in p["echeances"]],
                         [("absorbé", 50.0, 0.0), ("non_payé", 70.0, 70.0)])

    def test_un_mois_enregistre_ne_se_projette_pas(self):
        self.assertIsNone(historique.projete(self.tableau(enregistre=True)))
        self.assertIsNone(historique.projete(self.tableau(disponible=False)))
        self.assertIsNone(historique.projete(None))

    def test_ne_modifie_pas_le_tableau(self):
        t = self.tableau()
        avant = copy.deepcopy(t)
        historique.projete(t)
        self.assertEqual(t, avant)


if __name__ == "__main__":
    unittest.main()
