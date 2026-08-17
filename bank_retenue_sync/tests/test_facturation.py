"""Tests des regles pures de la cloture mensuelle : periode, ventilation TVA, echeancier.

Ces trois modules n'ont besoin ni de site ni de base : ce sont eux qui portent les arrondis et
les regles de repartition, donc eux qui doivent etre couverts. Le reste de la cloture n'est que
de la lecture.
"""
import glob
import json
import os
import re
import unittest
from datetime import date

from bank_retenue_sync.facturation import periode, tva
from bank_retenue_sync.partenaire import echeancier


class TestGabaritsDePage(unittest.TestCase):
    """Le garde-fou de l'apostrophe : il coute une seconde et a deja sauve deux ecrans.

    `frappe.build.scrub_html_template` pretend echapper le caractere apostrophe mais le remplace
    par lui-meme, puis enveloppe le gabarit dans des apostrophes simples. Une seule suffit a
    fermer la chaine et a casser TOUT le script de la page — qui cesse alors de s afficher, sans
    erreur serveur, sans ligne de log, sans rien. Les commentaires HTML sont retires avant ce
    traitement ; les commentaires CSS, non.
    """

    def test_aucune_apostrophe_dans_les_gabarits(self):
        racine = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "bank_retenue_sync", "page")
        gabarits = glob.glob(os.path.join(racine, "*", "*.html"))
        self.assertTrue(gabarits, "aucun gabarit de page trouve : le test ne verifie rien")
        for chemin in gabarits:
            with open(chemin, encoding="utf-8") as f:
                contenu = f.read()
            # Les commentaires HTML sont retires par Frappe AVANT l echappement : une
            # apostrophe qui y vit ne sort jamais dans le gabarit. On applique le meme
            # retrait, sinon le garde-fou signale des lignes parfaitement sures.
            sans_commentaires = re.sub(r"<!--.*?-->", "", contenu, flags=re.S)
            fautives = [n for n, ligne in enumerate(sans_commentaires.splitlines(), 1)
                        if "'" in ligne]
            self.assertFalse(
                fautives,
                "%s : apostrophe(s) ligne(s) %s — le gabarit casserait le script de la page"
                % (os.path.basename(chemin), fautives))


class TestReferenceDePaiement(unittest.TestCase):
    """Séparer le n° de pièce de la référence bancaire — les quatre formes relevées en base."""

    def _decomposer(self, ref):
        from bank_retenue_sync.facturation.reglement import decomposer_reference
        return decomposer_reference(ref)

    def test_cheque_numero_et_banque_emettrice(self):
        self.assertEqual(self._decomposer("9416665 - BNA / BR:90027975"),
                         {"piece": "9416665 - BNA", "banque": "90027975"})

    def test_traite(self):
        self.assertEqual(self._decomposer("11376605424- / RE:FT26198BPZR0"),
                         {"piece": "11376605424", "banque": "FT26198BPZR0"})

    def test_aramex(self):
        self.assertEqual(
            self._decomposer("Aramex N: 48812240514 Virement recu N: FT26189VC29V"),
            {"piece": "Aramex n° 48812240514", "banque": "FT26189VC29V"})

    def test_virement_la_tete_est_la_reference_bancaire(self):
        """Rien à afficher comme pièce : le numéro EST la référence bancaire."""
        self.assertEqual(self._decomposer("FT26211D5NMC - Banque Zitouna"),
                         {"piece": "", "banque": "FT26211D5NMC"})

    def test_vide(self):
        self.assertEqual(self._decomposer(""), {"piece": "", "banque": ""})
        self.assertEqual(self._decomposer(None), {"piece": "", "banque": ""})

    def test_seuls_cheque_traite_virement_portent_un_numero(self):
        from bank_retenue_sync.facturation.factures import _porte_un_numero
        for mode in ("Chèque", "Traite bancaire LC", "Virement", "cheque"):
            self.assertTrue(_porte_un_numero(mode), mode)
        # Pour ceux-ci, `reference_no` porte un nom de document, pas un n° de pièce : les
        # traiter comme un numéro empêchait de sommer les versements d'une même facture.
        for mode in ("Espèces", "Dette non payée", "Retenue a la source vente", "", None):
            self.assertFalse(_porte_un_numero(mode), mode)


class TestEspecesPresumees(unittest.TestCase):
    """Imputer le reste dû en espèces — sans jamais le confondre avec un règlement réel."""

    def _presumer(self, paiements, reste):
        from bank_retenue_sync.facturation.factures import presumer_especes
        return presumer_especes(paiements, reste)

    def _especes(self, montant, nombre=1):
        return {"mode": "Espèces", "piece": "", "banque": "", "montant": montant,
                "nombre": nombre, "date": "", "payment_entry": "ACC-PAY-1"}

    def test_sans_reste_du_rien_ne_change(self):
        depart = [self._especes(100.0)]
        self.assertEqual(self._presumer(depart, 0), depart)
        self.assertEqual(self._presumer(depart, 0.0005), depart)

    def test_facture_sans_especes_recoit_une_ligne_neuve(self):
        r = self._presumer([{"mode": "Chèque", "piece": "123-BNA", "banque": "90028025",
                             "montant": 400.0, "nombre": 1, "date": "",
                             "payment_entry": "ACC-PAY-2"}], 100.0)
        self.assertEqual(len(r), 2)
        ligne = [l for l in r if l["mode"] == "Espèces"][0]
        self.assertEqual(ligne["montant"], 100.0)
        self.assertEqual(ligne["presume"], 100.0)
        # Aucune piece derriere : ni compteur de versements, ni lien.
        self.assertEqual(ligne["nombre"], 0)
        self.assertIsNone(ligne["payment_entry"])

    def test_especes_existantes_la_presomption_s_y_ajoute(self):
        r = self._presumer([self._especes(500.0, nombre=2)], 120.0)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["montant"], 620.0)
        self.assertEqual(r[0]["presume"], 120.0)
        self.assertEqual(r[0]["nombre"], 2)

    def test_l_original_n_est_pas_modifie(self):
        depart = [self._especes(500.0)]
        self._presumer(depart, 120.0)
        self.assertEqual(depart[0]["montant"], 500.0)
        self.assertNotIn("presume", depart[0])

    def test_le_cheque_ne_capte_jamais_la_presomption(self):
        """Seules des espèces sans pièce absorbent le reste : un chèque a un numéro."""
        r = self._presumer([{"mode": "Chèque", "piece": "123-BNA", "banque": "", "montant": 400.0,
                             "nombre": 1, "date": "", "payment_entry": "ACC-PAY-2"}], 50.0)
        cheque = [l for l in r if l["mode"] == "Chèque"][0]
        self.assertEqual(cheque["montant"], 400.0)
        self.assertNotIn("presume", cheque)


class TestPeriode(unittest.TestCase):
    def test_defaut_est_le_mois_precedent(self):
        self.assertEqual(periode.normaliser(None, date(2026, 8, 13)), "2026-07")
        self.assertEqual(periode.normaliser("", date(2026, 1, 3)), "2025-12")

    def test_cle_illisible_retombe_sur_le_defaut(self):
        self.assertEqual(periode.normaliser("2026-13", date(2026, 8, 13)), "2026-07")
        self.assertEqual(periode.normaliser("juillet", date(2026, 8, 13)), "2026-07")

    def test_bornes_inclusives(self):
        self.assertEqual(periode.bornes("2026-02"), ("2026-02-01", "2026-02-28"))
        self.assertEqual(periode.bornes("2024-02"), ("2024-02-01", "2024-02-29"))
        self.assertEqual(periode.bornes("2026-12"), ("2026-12-01", "2026-12-31"))

    def test_libelle(self):
        self.assertEqual(periode.libelle("2026-07"), "juillet 2026")

    def test_derniers_part_du_mois_clos(self):
        mois = periode.derniers(3, date(2026, 1, 15))
        self.assertEqual([m["cle"] for m in mois], ["2025-12", "2025-11", "2025-10"])


class TestVentilationTVA(unittest.TestCase):
    def test_deux_taux_sur_une_meme_facture(self):
        """Le cas que la division par le taux ne sait pas traiter."""
        lignes = [{"item_code": "A", "net_amount": 100.0},
                  {"item_code": "B", "net_amount": 200.0}]
        taxes = [
            {"rate": 19, "tax_amount": 19.0,
             "item_wise_tax_detail": json.dumps({"A": [19, 19.0]})},
            {"rate": 7, "tax_amount": 14.0,
             "item_wise_tax_detail": json.dumps({"B": [7, 14.0]})},
        ]
        r = tva.ventiler(lignes, taxes, net_total=300.0, total_taxes=33.0)
        self.assertEqual(r["source"], "detail")
        self.assertEqual([(t["taux"], t["base"], t["tva"]) for t in r["taux"]],
                         [(19.0, 100.0, 19.0), (7.0, 200.0, 14.0)])
        self.assertEqual(r["ecart_base"], 0.0)
        self.assertEqual(r["ecart_tva"], 0.0)

    def test_ligne_exoneree_ne_gonfle_aucune_base(self):
        lignes = [{"item_code": "A", "net_amount": 100.0},
                  {"item_code": "EXO", "net_amount": 50.0}]
        taxes = [{"rate": 19, "tax_amount": 19.0,
                  "item_wise_tax_detail": json.dumps({"A": [19, 19.0], "EXO": [0, 0]})}]
        r = tva.ventiler(lignes, taxes, net_total=150.0, total_taxes=19.0)
        self.assertEqual(r["taux"], [{"taux": 19.0, "base": 100.0, "tva": 19.0}])
        self.assertEqual(r["base_exoneree"], 50.0)
        self.assertEqual(r["ecart_base"], 0.0)

    def test_repli_par_division_est_signale(self):
        """Sans detail par article, on divise — mais on le DIT."""
        r = tva.ventiler([{"item_code": "A", "net_amount": 100.0}],
                         [{"rate": 19, "tax_amount": 19.0, "item_wise_tax_detail": ""}],
                         net_total=100.0, total_taxes=19.0)
        self.assertEqual(r["source"], "division")
        self.assertEqual(r["taux"], [{"taux": 19.0, "base": 100.0, "tva": 19.0}])

    def test_timbre_fiscal_n_est_pas_un_ecart(self):
        """Une taxe a montant fixe (timbre) entre dans le total sans se rattacher a une base."""
        lignes = [{"item_code": "A", "net_amount": 100.0}]
        taxes = [
            {"rate": 19, "tax_amount": 19.0,
             "item_wise_tax_detail": json.dumps({"A": [19, 19.0]})},
            {"rate": 0, "tax_amount": 1.0, "description": "Timbre Fiscal",
             "item_wise_tax_detail": json.dumps({"A": [0, 0]})},
        ]
        r = tva.ventiler(lignes, taxes, net_total=100.0, total_taxes=20.0)
        self.assertEqual(r["autres_taxes"], 1.0)
        self.assertEqual(r["autres_taxes_libelles"], ["Timbre Fiscal"])
        self.assertEqual(r["total_tva"], 19.0)
        self.assertEqual(r["ecart_tva"], 0.0)
        self.assertEqual(r["ecart_base"], 0.0)
        self.assertEqual(r["base_exoneree"], 0.0)

    def test_taux_zero_sur_la_ligne_mais_reel_par_article(self):
        """Le cas reel : `rate` vaut 0 sur la ligne, le taux vit dans le detail par article."""
        lignes = [{"item_code": "A", "net_amount": 100.0},
                  {"item_code": "B", "net_amount": 200.0}]
        taxes = [{"rate": 0, "tax_amount": 33.0, "description": "TVA",
                  "item_wise_tax_detail": json.dumps({"A": [19, 19.0], "B": [7, 14.0]})}]
        r = tva.ventiler(lignes, taxes, net_total=300.0, total_taxes=33.0)
        self.assertEqual([(t["taux"], t["base"]) for t in r["taux"]],
                         [(19.0, 100.0), (7.0, 200.0)])
        self.assertEqual(r["autres_taxes"], 0.0)
        self.assertEqual(r["ecart_tva"], 0.0)

    def test_ecart_est_rendu_pas_absorbe(self):
        """Une base qui ne retombe pas sur le HT de la facture doit se voir."""
        lignes = [{"item_code": "A", "net_amount": 100.0}]
        taxes = [{"rate": 19, "tax_amount": 19.0,
                  "item_wise_tax_detail": json.dumps({"A": [19, 19.0]})}]
        r = tva.ventiler(lignes, taxes, net_total=140.0, total_taxes=25.0)
        self.assertEqual(r["ecart_base"], 40.0)
        self.assertEqual(r["ecart_tva"], 6.0)

    def test_detail_illisible_bascule_sans_lever(self):
        r = tva.ventiler([{"item_code": "A", "net_amount": 100.0}],
                         [{"rate": 19, "tax_amount": 19.0,
                           "item_wise_tax_detail": "{ceci n'est pas du json"}])
        self.assertEqual(r["source"], "division")
        self.assertEqual(r["total_tva"], 19.0)

    def test_cumul_compte_les_factures_approchees(self):
        exacte = tva.ventiler([{"item_code": "A", "net_amount": 100.0}],
                              [{"rate": 19, "tax_amount": 19.0,
                                "item_wise_tax_detail": json.dumps({"A": [19, 19.0]})}])
        approchee = tva.ventiler([{"item_code": "B", "net_amount": 200.0}],
                                 [{"rate": 19, "tax_amount": 38.0,
                                   "item_wise_tax_detail": ""}])
        c = tva.cumuler([exacte, approchee])
        self.assertEqual(c["taux"], [{"taux": 19.0, "base": 300.0, "tva": 57.0}])
        self.assertEqual(c["factures_par_division"], 1)


class TestEcheancier(unittest.TestCase):
    def test_trois_versements_resomment_au_total(self):
        e = echeancier.brut(1000.0, 2026, 7)
        self.assertEqual([x["date"] for x in e],
                         ["2026-07-31", "2026-08-31", "2026-09-30"])
        self.assertEqual(round(sum(x["montant"] for x in e), 3), 1000.0)

    def test_les_centimes_vont_sur_le_dernier(self):
        e = echeancier.brut(100.0, 2026, 12)
        self.assertEqual([x["montant"] for x in e], [33.333, 33.333, 33.334])
        self.assertEqual([x["date"] for x in e],
                         ["2026-12-31", "2027-01-31", "2027-02-28"])

    def test_ajustement_absorbe_dans_l_ordre(self):
        e = echeancier.brut(300.0, 2026, 7)
        ajuste, report = echeancier.ajuster(e, 150.0)
        self.assertEqual([x["montant"] for x in ajuste], [0.0, 50.0, 100.0])
        self.assertEqual(report, 0.0)

    def test_ajustement_superieur_descend_en_report(self):
        e = echeancier.brut(300.0, 2026, 7)
        ajuste, report = echeancier.ajuster(e, 500.0)
        self.assertEqual([x["montant"] for x in ajuste], [0.0, 0.0, 0.0])
        self.assertEqual(report, 200.0)

    def test_solde_net_et_ajustement(self):
        """L'ajustement vient EN DÉDUCTION de l'échéancier ; il n'en est pas la base.

        La base, ce sont les COMMANDES du mois. Confondre les deux sortait un échéancier de
        692 DT là où le partenaire en attendait 9 183 — vérifié sur le rapport de juin 2026.
        """
        self.assertEqual(echeancier.solde_net(1000.0, 200.0), 800.0)
        self.assertEqual(echeancier.ajustement(1000.0, 200.0, 50.0), 850.0)
        self.assertEqual(echeancier.ajustement(1000.0, 200.0), 800.0)

    def test_la_chaine_de_juin_2026(self):
        """Rejoue le rapport reçu : commandes 9 183,720 et ajustement 389,450."""
        brut = echeancier.brut(9183.720, 2026, 6)
        self.assertEqual([e["montant"] for e in brut], [3061.24, 3061.24, 3061.24])
        ajuste, report = echeancier.ajuster(brut, 389.450)
        self.assertEqual([e["montant"] for e in ajuste], [2671.79, 3061.24, 3061.24])
        self.assertEqual(report, 0.0)


if __name__ == "__main__":
    unittest.main()


class TestConsolideEconomiq(unittest.TestCase):
    """Le solde inter-mois : ce que le partenaire doit, echeance par echeance."""

    def _mois(self, cle, echeances, report=0.0, ajustement=0.0):
        return {"mois": cle, "report": report, "ajustement": ajustement,
                "echeances": echeances}

    def _e(self, date, montant, **kw):
        return dict({"date": date, "montant": montant, "deduit": 0.0, "note": "",
                     "statut": "non_payé", "paye": 0.0, "reste": montant}, **kw)

    def test_deux_mois_se_cumulent_sur_la_meme_date(self):
        from bank_retenue_sync.partenaire.echeancier import consolider
        r = consolider([
            self._mois("2026-05", [self._e("2026-07-31", 100.0)]),
            self._mois("2026-06", [self._e("2026-07-31", 50.0)]),
        ])
        self.assertEqual([(l["date"], l["montant"]) for l in r], [("2026-07-31", 150.0)])
        self.assertIn("2026-05", r[0]["detail"])
        self.assertIn("2026-06", r[0]["detail"])

    def test_une_echeance_absorbee_ne_compte_pas(self):
        """L'ajustement du mois l'a déjà effacée : la reporter la réclamerait deux fois."""
        from bank_retenue_sync.partenaire.echeancier import consolider
        r = consolider([self._mois("2026-06", [
            self._e("2026-06-30", 0.0, deduit=200.0, statut="absorbé"),
            self._e("2026-07-31", 80.0),
        ])])
        self.assertEqual([l["date"] for l in r], ["2026-07-31"])

    def test_le_report_s_impute_sur_la_plus_ancienne(self):
        from bank_retenue_sync.partenaire.echeancier import consolider
        r = consolider([
            self._mois("2026-05", [self._e("2026-06-30", 100.0),
                                   self._e("2026-07-31", 100.0)]),
            self._mois("2026-06", [], report=150.0),
        ])
        self.assertEqual([(l["date"], l["montant"]) for l in r], [("2026-07-31", 50.0)])

    def test_statut_selon_le_paiement(self):
        from bank_retenue_sync.partenaire.echeancier import consolider
        r = consolider([self._mois("2026-05", [
            self._e("2026-06-30", 100.0, paye=100.0, reste=0.0, statut="payé"),
            self._e("2026-07-31", 100.0, paye=40.0, reste=60.0, statut="partiel"),
            self._e("2026-08-31", 100.0),
        ])])
        self.assertEqual([l["statut"] for l in r], ["payé", "partiel", "non_payé"])

    def test_historique_vide(self):
        from bank_retenue_sync.partenaire.echeancier import consolider
        self.assertEqual(consolider([]), [])
        self.assertEqual(consolider(None), [])


class TestAmorcePartenaire(unittest.TestCase):
    """L'etat de depart repris du partenaire — constantes, pas calcul.

    ⚠️ UNE AMORCE QUI DERIVE NE SE VOIT PAS. Elle est ecrite une fois puis gelee ; si ses
    montants changent en meme temps que le code, le consolide reclame autre chose que ce qui a
    ete communique, des mois plus tard et sans le moindre signal.
    """

    def _echeances(self):
        from bank_retenue_sync.partenaire.amorce import ECHEANCES
        return {e["date"]: e["montant"] for e in ECHEANCES}

    def test_montants_communiques(self):
        self.assertEqual(self._echeances(),
                         {"2026-07-31": 4266.616, "2026-08-31": 3061.240})

    def test_somme_egale_le_reste_du(self):
        self.assertAlmostEqual(sum(self._echeances().values()), 7327.856, places=3)

    def test_composition_de_juillet(self):
        # mai M+2 (3 616,128 / 3) + juin M+1 (9 183,720 / 3)
        self.assertAlmostEqual(self._echeances()["2026-07-31"], 1205.376 + 3061.240, places=3)

    def test_avance_du_surpaiement_de_juin(self):
        from bank_retenue_sync.partenaire.amorce import ECHEANCES
        avances = {e["date"]: e["avance"] for e in ECHEANCES}
        # 2 700,000 verses le 14/07 pour une echeance de 2 671,790.
        self.assertAlmostEqual(avances["2026-07-31"], 2700.0 - 2671.790, places=3)
        self.assertEqual(avances["2026-08-31"], 0.0)

    def test_reste_du_apres_avance(self):
        from bank_retenue_sync.partenaire.amorce import ECHEANCES
        reste = sum(e["montant"] - e["avance"] for e in ECHEANCES)
        self.assertAlmostEqual(reste, 7299.646, places=3)

    def test_notes_tiennent_dans_le_champ(self):
        """Note est un Data(140) : au-dela, la sauvegarde entiere echoue, elle ne tronque pas."""
        from bank_retenue_sync.partenaire.amorce import ECHEANCES, NOTE_MAX
        for e in ECHEANCES:
            self.assertLessEqual(len(e["note"]), NOTE_MAX, e["date"])


class TestEcritureDeBilan(unittest.TestCase):
    """La ligne d equilibre de l ecriture de bilan — extraction pure."""

    def _l(self, compte, party="", debit=0.0, credit=0.0):
        return {"account": compte, "party": party, "debit": debit, "credit": credit}

    def test_juin_2026(self):
        from bank_retenue_sync.partenaire.ecriture import (
            CLIENT, COMPTE_CHARGES, COMPTE_DEBITEURS, COMPTE_PARTENAIRE,
            ajustement_des_lignes)
        lignes = [
            self._l(COMPTE_CHARGES, debit=198.200),
            self._l(COMPTE_CHARGES, debit=293.250),
            self._l(COMPTE_CHARGES, debit=250.000),
            self._l(COMPTE_PARTENAIRE, credit=352.000),
            self._l(COMPTE_DEBITEURS, party=CLIENT, credit=389.450),
        ]
        self.assertAlmostEqual(ajustement_des_lignes(lignes), 389.450, places=3)
        # 3 061,240 (9 183,720 / 3) − 389,450 = l echeance annoncee au partenaire.
        self.assertAlmostEqual(3061.240 - ajustement_des_lignes(lignes), 2671.790, places=3)

    def test_plusieurs_lignes_au_debiteur(self):
        """Avril 2026 en porte trois : n en lire qu une sous-estimerait de 123,000."""
        from bank_retenue_sync.partenaire.ecriture import (
            CLIENT, COMPTE_DEBITEURS, ajustement_des_lignes)
        lignes = [self._l(COMPTE_DEBITEURS, party=CLIENT, credit=c)
                  for c in (376.500, 67.925, 55.075)]
        self.assertAlmostEqual(ajustement_des_lignes(lignes), 499.500, places=3)

    def test_ignore_les_autres_tiers_et_comptes(self):
        from bank_retenue_sync.partenaire.ecriture import (
            CLIENT, COMPTE_CHARGES, COMPTE_DEBITEURS, ajustement_des_lignes)
        lignes = [
            self._l(COMPTE_DEBITEURS, party="AUTRE CLIENT", credit=999.0),
            self._l(COMPTE_CHARGES, party=CLIENT, credit=888.0),
            self._l(COMPTE_DEBITEURS, party=CLIENT, credit=10.0),
        ]
        self.assertAlmostEqual(ajustement_des_lignes(lignes), 10.0, places=3)

    def test_sans_ligne(self):
        from bank_retenue_sync.partenaire.ecriture import ajustement_des_lignes
        self.assertEqual(ajustement_des_lignes([]), 0.0)
        self.assertEqual(ajustement_des_lignes(None), 0.0)


class TestImputationDesReglements(unittest.TestCase):
    """L imputation des reglements sur l echeancier consolide — fonction pure."""

    def _l(self, date, montant, paye=None):
        return {"date": date, "montant": montant, "paye": paye, "reste": None,
                "statut": "non_payé", "detail": ""}

    def _v(self, nom, date, montant):
        return {"payment_entry": nom, "date": date, "montant": montant}

    def test_la_plus_ancienne_s_eteint_la_premiere(self):
        lignes, reste = echeancier.imputer(
            [self._l("2026-08-31", 300.0), self._l("2026-07-31", 100.0)],
            [self._v("PE-1", "2026-08-05", 150.0)])
        self.assertEqual([l["date"] for l in lignes], ["2026-07-31", "2026-08-31"])
        self.assertEqual(lignes[0]["statut"], "payé")
        self.assertEqual(lignes[1]["paye"], 50.0)
        self.assertEqual(lignes[1]["reste"], 250.0)
        self.assertEqual(reste, 0.0)

    def test_l_avance_deja_portee_compte(self):
        """L amorce inscrit 28,210 sur le 31/07 : l ignorer les reclamerait deux fois."""
        lignes, _ = echeancier.imputer(
            [self._l("2026-07-31", 4266.616, paye=28.210)], [])
        self.assertAlmostEqual(lignes[0]["paye"], 28.210, places=3)
        self.assertAlmostEqual(lignes[0]["reste"], 4238.406, places=3)
        self.assertEqual(lignes[0]["statut"], "partiel")

    def test_les_pieces_sont_tracees(self):
        lignes, _ = echeancier.imputer(
            [self._l("2026-07-31", 25.0)],
            [self._v("PE-1", "2026-07-29", 10.0), self._v("PE-2", "2026-07-31", 11.0)])
        self.assertEqual([r["payment_entry"] for r in lignes[0]["reglements"]], ["PE-1", "PE-2"])
        self.assertEqual([r["impute"] for r in lignes[0]["reglements"]], [10.0, 11.0])

    def test_un_versement_se_partage_entre_deux_echeances(self):
        lignes, reste = echeancier.imputer(
            [self._l("2026-07-31", 30.0), self._l("2026-08-31", 30.0)],
            [self._v("PE-1", "2026-08-01", 50.0)])
        self.assertEqual([r["impute"] for r in lignes[0]["reglements"]], [30.0])
        self.assertEqual([r["impute"] for r in lignes[1]["reglements"]], [20.0])
        self.assertEqual(reste, 0.0)

    def test_excedent_quand_plus_rien_a_eteindre(self):
        lignes, reste = echeancier.imputer([self._l("2026-07-31", 10.0)],
                                           [self._v("PE-1", "2026-08-01", 40.0)])
        self.assertEqual(lignes[0]["statut"], "payé")
        self.assertEqual(reste, 30.0)

    def test_sans_versement_rien_ne_bouge(self):
        lignes, reste = echeancier.imputer([self._l("2026-07-31", 10.0)], [])
        self.assertEqual(lignes[0]["statut"], "non_payé")
        self.assertIsNone(lignes[0]["paye"])
        self.assertEqual(reste, 0.0)


class TestEquilibreDeLEcriture(unittest.TestCase):
    """La ligne au debiteur du brouillon — fonction pure."""

    def _eq(self, *a):
        from bank_retenue_sync.partenaire.ecriture import equilibre
        return equilibre(*a)

    def test_juin_2026_reconstitue(self):
        # benefice Aqua 198,200 + achats Economiq 293,250 + charges 250 − ventes 352 = 389,450
        self.assertAlmostEqual(self._eq(198.200, 293.250, 250.0, 352.0), 389.450, places=3)

    def test_mai_2026_reconstitue(self):
        self.assertAlmostEqual(self._eq(308.500, 516.620, 261.0, 895.0), 191.120, places=3)

    def test_equivaut_a_solde_net_plus_charges(self):
        """benefice Economiq = ventes − achats, donc les deux formules coincident."""
        benefice_aqua, achats, ventes, charges = 198.200, 293.250, 352.0, 250.0
        self.assertAlmostEqual(
            self._eq(benefice_aqua, achats, charges, ventes),
            echeancier.ajustement(benefice_aqua, ventes - achats, charges), places=3)

    def test_zeros(self):
        self.assertEqual(self._eq(0, 0, 0, 0), 0.0)
        self.assertEqual(self._eq(None, None, None, None), 0.0)


class TestChoixDesDettesALiberer(unittest.TestCase):
    """La selection des pieces de dette a detruire — fonction pure, et geste irreversible."""

    def _c(self, nom, montant, date_commande):
        return {"payment_entry": nom, "montant": montant, "date_commande": date_commande,
                "sales_order": "SO-" + nom}

    def _choisir(self, besoin, candidates, mois):
        from bank_retenue_sync.partenaire.dette import choisir
        return choisir(besoin, candidates, mois)

    def test_juillet_2026_une_seule_piece_suffit(self):
        candidates = [self._c("PE-A", 1919.556, "2026-07-06"),
                      self._c("PE-B", 77.0, "2026-07-22"),
                      self._c("PE-C", 45.5, "2026-07-31"),
                      self._c("PE-JUIN", 7340.459, "2026-06-04")]
        retenues, degage = self._choisir(894.0, candidates, "2026-07")
        self.assertEqual([r["payment_entry"] for r in retenues], ["PE-A"])
        self.assertAlmostEqual(degage, 1919.556, places=3)

    def test_la_plus_petite_qui_suffit(self):
        """Detruire la grosse quand une petite suffit multiplie les pieces a recreer."""
        candidates = [self._c("GROSSE", 5000.0, "2026-07-01"),
                      self._c("JUSTE", 900.0, "2026-07-02")]
        retenues, _ = self._choisir(894.0, candidates, "2026-07")
        self.assertEqual([r["payment_entry"] for r in retenues], ["JUSTE"])

    def test_remonte_sur_les_mois_anterieurs_si_le_mois_ne_suffit_pas(self):
        candidates = [self._c("PE-JUIL", 100.0, "2026-07-06"),
                      self._c("PE-JUIN", 500.0, "2026-06-04"),
                      self._c("PE-MAI", 900.0, "2026-05-14")]
        retenues, degage = self._choisir(550.0, candidates, "2026-07")
        self.assertEqual([r["payment_entry"] for r in retenues], ["PE-JUIL", "PE-JUIN"])
        self.assertAlmostEqual(degage, 600.0, places=3)

    def test_le_mois_le_plus_recent_est_pris_en_premier_en_remontant(self):
        candidates = [self._c("PE-MARS", 300.0, "2026-03-01"),
                      self._c("PE-JUIN", 300.0, "2026-06-01")]
        retenues, _ = self._choisir(300.0, candidates, "2026-07")
        self.assertEqual([r["payment_entry"] for r in retenues], ["PE-JUIN"])

    def test_besoin_nul_ne_detruit_rien(self):
        candidates = [self._c("PE-A", 100.0, "2026-07-01")]
        self.assertEqual(self._choisir(0, candidates, "2026-07"), ([], 0.0))
        self.assertEqual(self._choisir(None, candidates, "2026-07"), ([], 0.0))

    def test_sans_candidate_rien_n_est_degage(self):
        retenues, degage = self._choisir(500.0, [], "2026-07")
        self.assertEqual(retenues, [])
        self.assertEqual(degage, 0.0)


class TestDialoguesDePage(unittest.TestCase):
    """Fermer un dialogue avant d y repondre annule silencieusement la reponse.

    `frappe.ui.Dialog.hide()` declenche `onhide`, qui resout la promesse au refus. Ecrire
    `d.hide(); repondre(true)` verrouille donc le drapeau sur le refus : le bouton ne fait plus
    rien, sans erreur, sans log. Deux ecrans en sont deja morts — « Constituer le dossier » puis
    « Supprimer et poursuivre ». Le drapeau ne protege pas de l ordre des appels ; ce test si.
    """

    def _sources(self):
        racine = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "bank_retenue_sync", "page")
        return glob.glob(os.path.join(racine, "*", "*.js"))

    def test_aucune_reponse_apres_hide(self):
        sources = self._sources()
        self.assertTrue(sources, "aucun script de page trouve : le test ne verifie rien")
        motif = re.compile(r"\.hide\(\)\s*;\s*(fini|resolve|r)\s*\(")
        for chemin in sources:
            with open(chemin, encoding="utf-8") as f:
                contenu = f.read()
            # Les commentaires DECRIVENT le piege — les scanner ferait echouer le test sur sa
            # propre documentation, et pousserait a effacer l explication pour le faire passer.
            code = re.sub(r"/\*.*?\*/", "", contenu, flags=re.S)
            code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
            fautifs = motif.findall(code)
            self.assertFalse(
                fautifs,
                "%s : reponse donnee APRES hide() — onhide aura deja resolu au refus, le bouton "
                "sera inerte" % os.path.basename(chemin))


class TestSimulationDeLiberation(unittest.TestCase):
    """Verifier AVANT de detruire — la regression qui a coute ACC-PAY-2026-04403."""

    def _sim(self, ordres, detruites):
        from bank_retenue_sync.partenaire.ecriture import simuler_liberation
        return simuler_liberation(ordres, detruites)

    def test_la_commande_liberee_retrouve_sa_capacite(self):
        from bank_retenue_sync.partenaire.ecriture import repartir
        ordres = [{"sales_order": "SO-02304", "date": "2026-07-06", "total": 1919.556,
                   "disponible": 0.0}]
        detruites = [{"sales_order": "SO-02304", "montant": 1919.556}]
        simule = self._sim(ordres, detruites)
        self.assertAlmostEqual(simule[0]["disponible"], 1919.556, places=3)
        plan, non_impute = repartir(894.0, simule)
        self.assertEqual(non_impute, 0.0)
        self.assertAlmostEqual(plan[0]["montant"], 894.0, places=3)

    def test_avant_liberation_rien_ne_passe(self):
        """L etat d avant dit toujours non : c est pour ca qu il ne faut pas s y fier."""
        from bank_retenue_sync.partenaire.ecriture import repartir
        ordres = [{"sales_order": "SO-02304", "date": "2026-07-06", "total": 1919.556,
                   "disponible": 0.0}]
        _, non_impute = repartir(894.0, ordres)
        self.assertAlmostEqual(non_impute, 894.0, places=3)

    def test_plusieurs_pieces_sur_la_meme_commande_s_additionnent(self):
        simule = self._sim([{"sales_order": "SO-1", "disponible": 10.0}],
                           [{"sales_order": "SO-1", "montant": 30.0},
                            {"sales_order": "SO-1", "montant": 5.0}])
        self.assertAlmostEqual(simule[0]["disponible"], 45.0, places=3)

    def test_une_commande_hors_destruction_ne_bouge_pas(self):
        simule = self._sim([{"sales_order": "SO-1", "disponible": 10.0},
                            {"sales_order": "SO-2", "disponible": 0.0}],
                           [{"sales_order": "SO-1", "montant": 30.0}])
        self.assertAlmostEqual(simule[0]["disponible"], 40.0, places=3)
        self.assertAlmostEqual(simule[1]["disponible"], 0.0, places=3)

    def test_sans_destruction_rien_ne_change(self):
        ordres = [{"sales_order": "SO-1", "disponible": 10.0}]
        self.assertEqual(self._sim(ordres, []), ordres)
        self.assertEqual(self._sim(ordres, None), ordres)


class TestOrdreDAffectation(unittest.TestCase):
    """L ordre d affectation d un reglement — il se choisit, il ne se devine pas."""

    def _o(self, nom, date, dispo):
        return {"sales_order": nom, "date": date, "total": dispo, "disponible": dispo}

    def test_anciennes_d_abord_reste_le_defaut(self):
        """Le defaut de `repartir` ne bouge pas : l ajustement du bilan en depend."""
        from bank_retenue_sync.partenaire.ecriture import repartir
        ordres = [self._o("VIEILLE", "2023-11-09", 400.0), self._o("RECENTE", "2026-06-04", 400.0)]
        plan, _ = repartir(300.0, ordres)
        self.assertEqual([p["sales_order"] for p in plan], ["VIEILLE"])

    def test_recentes_d_abord_epargne_les_vieilles_creances(self):
        from bank_retenue_sync.partenaire.ecriture import repartir
        ordres = [self._o("VIEILLE", "2023-11-09", 400.0), self._o("RECENTE", "2026-06-04", 400.0)]
        plan, _ = repartir(300.0, ordres, "recentes")
        self.assertEqual([p["sales_order"] for p in plan], ["RECENTE"])

    def test_le_debordement_suit_le_meme_sens(self):
        from bank_retenue_sync.partenaire.ecriture import repartir
        ordres = [self._o("A", "2026-01-01", 100.0), self._o("B", "2026-02-01", 100.0)]
        plan, reste = repartir(150.0, ordres, "recentes")
        self.assertEqual([(p["sales_order"], p["montant"]) for p in plan], [("B", 100.0),
                                                                           ("A", 50.0)])
        self.assertEqual(reste, 0.0)


class TestReglementSurDettes(unittest.TestCase):
    """Un reglement eteint les plus anciennes dettes de commandes — fonction pure."""

    def _c(self, nom, date, dette):
        return {"sales_order": nom, "date": date, "dette": dette, "pieces": ["PE-" + nom]}

    def _plan(self, montant, cibles):
        from bank_retenue_sync.partenaire.paiement import planifier
        return planifier(montant, cibles)

    def test_la_plus_ancienne_dette_s_eteint_la_premiere(self):
        cibles = [self._c("JUIN", "2026-06-04", 7340.459),
                  self._c("JUILLET", "2026-07-06", 1025.556)]
        plan, avance = self._plan(2700.0, cibles)
        self.assertEqual([p["sales_order"] for p in plan], ["JUIN"])
        self.assertAlmostEqual(plan[0]["dette_apres"], 4640.459, places=3)
        self.assertEqual(avance, 0.0)

    def test_l_invariant_dette_avant_egale_regle_plus_apres(self):
        plan, _ = self._plan(2700.0, [self._c("A", "2026-06-04", 7340.459)])
        p = plan[0]
        self.assertAlmostEqual(p["dette_avant"], p["regle"] + p["dette_apres"], places=3)

    def test_deborde_sur_la_dette_suivante(self):
        cibles = [self._c("A", "2026-06-04", 100.0), self._c("B", "2026-07-06", 500.0)]
        plan, avance = self._plan(300.0, cibles)
        self.assertEqual([(p["sales_order"], p["regle"]) for p in plan], [("A", 100.0),
                                                                          ("B", 200.0)])
        self.assertEqual(plan[0]["dette_apres"], 0.0)
        self.assertEqual(avance, 0.0)

    def test_le_surplus_part_en_avance(self):
        plan, avance = self._plan(500.0, [self._c("A", "2026-06-04", 100.0)])
        self.assertAlmostEqual(avance, 400.0, places=3)
        self.assertEqual(len(plan), 1)

    def test_sans_dette_rien_n_est_touche(self):
        plan, avance = self._plan(500.0, [])
        self.assertEqual(plan, [])
        self.assertAlmostEqual(avance, 500.0, places=3)


class TestEmpreinteDuPlan(unittest.TestCase):
    """L empreinte du plan d affectation — ce que l utilisateur a confirme, nommement.

    L ecran nomme les pieces de dette qui vont etre detruites, puis le serveur recalcule le plan
    de son cote. Entre les deux, un reglement saisi ailleurs peut avoir change les dettes : sans
    empreinte, on detruirait des pieces que personne n a vues, derriere une confirmation qui en
    nommait d autres.
    """

    def _ligne(self, nom, avant, regle, pieces):
        return {"sales_order": nom, "dette_avant": avant, "regle": regle,
                "dette_apres": round(avant - regle, 3), "pieces": list(pieces)}

    def _f(self):
        from bank_retenue_sync.partenaire.paiement import concorde, empreinte
        return empreinte, concorde

    def test_le_meme_plan_donne_la_meme_empreinte(self):
        empreinte, _ = self._f()
        a = [self._ligne("JUIN", 7340.459, 2700.0, ["ACC-PAY-1"])]
        b = [self._ligne("JUIN", 7340.459, 2700.0, ["ACC-PAY-1"])]
        self.assertEqual(empreinte(a), empreinte(b))

    def test_l_ordre_des_pieces_d_une_ligne_n_influe_pas(self):
        """`cibles` les obtient par group_concat : MariaDB n en garantit pas l ordre."""
        empreinte, _ = self._f()
        a = [self._ligne("JUIN", 100.0, 60.0, ["ACC-PAY-1", "ACC-PAY-2"])]
        b = [self._ligne("JUIN", 100.0, 60.0, ["ACC-PAY-2", "ACC-PAY-1"])]
        self.assertEqual(empreinte(a), empreinte(b))

    def test_une_dette_qui_a_bouge_change_l_empreinte(self):
        empreinte, _ = self._f()
        avant = [self._ligne("JUIN", 7340.459, 2700.0, ["ACC-PAY-1"])]
        apres = [self._ligne("JUIN", 6000.000, 2700.0, ["ACC-PAY-1"])]
        self.assertNotEqual(empreinte(avant), empreinte(apres))

    def test_une_piece_de_dette_remplacee_change_l_empreinte(self):
        empreinte, _ = self._f()
        avant = [self._ligne("JUIN", 100.0, 60.0, ["ACC-PAY-1"])]
        apres = [self._ligne("JUIN", 100.0, 60.0, ["ACC-PAY-9"])]
        self.assertNotEqual(empreinte(avant), empreinte(apres))

    def test_une_commande_disparue_change_l_empreinte(self):
        empreinte, _ = self._f()
        avant = [self._ligne("A", 100.0, 100.0, ["PE-A"]),
                 self._ligne("B", 500.0, 200.0, ["PE-B"])]
        apres = [self._ligne("A", 100.0, 100.0, ["PE-A"])]
        self.assertNotEqual(empreinte(avant), empreinte(apres))

    def test_le_plan_vide_a_une_empreinte_stable(self):
        empreinte, concorde = self._f()
        self.assertEqual(empreinte([]), empreinte([]))
        self.assertTrue(concorde([], empreinte([])))

    def test_une_empreinte_absente_vaut_accord(self):
        """Console et scripts de reprise n ont rien confirme a l ecran, donc rien a contredire."""
        _, concorde = self._f()
        plan = [self._ligne("A", 100.0, 100.0, ["PE-A"])]
        self.assertTrue(concorde(plan, None))
        self.assertTrue(concorde(plan, ""))

    def test_le_plan_qui_a_bouge_ne_concorde_plus(self):
        """Le cas reel : un reglement saisi ailleurs entre l apercu et le clic."""
        from bank_retenue_sync.partenaire.paiement import planifier
        empreinte, concorde = self._f()
        cible = {"sales_order": "JUIN", "date": "2026-06-04", "dette": 7340.459,
                 "pieces": ["ACC-PAY-1"]}
        vue, _ = planifier(2700.0, [cible])
        confirmee = empreinte(vue)
        # Entre-temps, une autre piece a reduit la dette de la meme commande.
        cible_apres = dict(cible, dette=3000.0, pieces=["ACC-PAY-7"])
        recalcule, _ = planifier(2700.0, [cible_apres])
        self.assertFalse(concorde(recalcule, confirmee))
