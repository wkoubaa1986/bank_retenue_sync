"""Tests des ecarts residuels sur pieces deja rapprochees (expenses/residus.py).

Meme convention que les tests existants : `unittest.TestCase` pur, entrees injectees, aucun acces
reseau ni base.
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.bank import classify as C
from bank_retenue_sync.expenses import residus as RS


def cls(cle, montant, ecart=0.0, jour=4, document="ACC-JV-1", groupe=None, credit=False):
    c = C.Classification(cle=cle, date=date(2026, 8, jour), operation="PAIEMENT",
                         reference="FT26216ABCDE",
                         debit=0.0 if credit else montant, credit=montant if credit else 0.0,
                         statut=C.STATUT_IDENTIFIE, groupe=groupe, document_name=document)
    c.montant_document = round(montant - ecart, 3)
    c.ecart = ecart
    return c


def ctx_echeance(ecart=1.0, total=1197.957, voucher="ACC-JV-531", nb=5):
    return C.LinkContext(echeances={
        ("LD2227700127", "04-08-2026"): {"voucher": voucher, "montant": round(total - ecart, 3),
                                         "ecart": ecart, "nb": nb, "total": total}})


class TestResidusDuReleve(unittest.TestCase):
    def test_un_residu_par_GROUPE_et_non_par_ligne(self):
        """Une echeance de leasing est eclatee en 5 debits : l'ecart porte sur le groupe.
        Le reporter sur chaque ligne le multiplierait par 5."""
        lignes = [cls("m%d" % i, 240.0, groupe="echeance-LD2227700127-04-08-2026")
                  for i in range(5)]
        out = RS.residus_du_releve([], context=ctx_echeance(), classifications=lignes)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].ecart, 1.0)
        self.assertEqual(out[0].document_name, "ACC-JV-531")
        self.assertEqual(out[0].date, date(2026, 8, 4))

    def test_echeance_sans_ecart_ignoree(self):
        out = RS.residus_du_releve([], context=ctx_echeance(ecart=0.0), classifications=[])
        self.assertEqual(out, [])

    def test_echeance_sans_piece_ignoree(self):
        """Sans ecriture en face il n'y a pas de residu mais une echeance non comptabilisee."""
        out = RS.residus_du_releve([], context=ctx_echeance(voucher=None), classifications=[])
        self.assertEqual(out, [])

    def test_ligne_hors_groupe_retenue(self):
        out = RS.residus_du_releve([], context=C.LinkContext(),
                                   classifications=[cls("m1", 703.5, ecart=0.5)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].ecart, 0.5)
        self.assertEqual(out[0].banque, 703.5)
        self.assertEqual(out[0].document, 703.0)

    def test_credit_retenu_et_SON_EFFET_EST_INVERSE(self):
        """Une perte de non paiement : la banque a verse 2,380 de moins que la piece ne porte.
        Le compte bancaire ERPNext est donc trop HAUT de 2,380 — il faut le crediter d'autant,
        alors que l'ecart, lui, vaut −2,380."""
        out = RS.residus_du_releve([], context=C.LinkContext(),
                                   classifications=[cls("m1", 2242.22, ecart=-2.38, credit=True)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].ecart, -2.38)
        self.assertEqual(out[0].effet, 2.38)
        self.assertEqual(out[0].sens, "Credit")

    def test_debit_son_effet_suit_l_ecart(self):
        out = RS.residus_du_releve([], context=C.LinkContext(),
                                   classifications=[cls("m1", 703.5, ecart=0.5)])
        self.assertEqual((out[0].ecart, out[0].effet, out[0].sens), (0.5, 0.5, "Debit"))

    def test_sans_piece_aucun_residu(self):
        out = RS.residus_du_releve([], context=C.LinkContext(),
                                   classifications=[cls("m1", 703.5, ecart=0.5, document=None)])
        self.assertEqual(out, [])

    def test_au_dela_de_la_tolerance_ce_n_est_plus_un_residu(self):
        """100 DT d'ecart n'est pas un residu a comptabiliser mais une erreur a corriger."""
        out = RS.residus_du_releve([], context=C.LinkContext(),
                                   classifications=[cls("m1", 703.5, ecart=100.0)])
        self.assertEqual(out, [])

    def test_ecart_infime_ignore(self):
        out = RS.residus_du_releve([], context=C.LinkContext(),
                                   classifications=[cls("m1", 703.5, ecart=0.001)])
        self.assertEqual(out, [])


class TestCumulMensuel(unittest.TestCase):
    def _r(self, jour, ecart, effet, mois=7):
        return RS.Residu(date=date(2026, mois, jour), cle="x", reference="", libelle="",
                         banque=0.0, document=0.0, ecart=ecart, document_name="X", effet=effet)

    def test_somme_ALGEBRIQUE_les_residus_se_compensent(self):
        """Juillet porte +0,500 (recharge Total) et −1,000 (virement JEGHAM) : le signe compte,
        les additionner en valeur absolue creerait une charge fictive."""
        self.assertEqual(
            RS.cumul_mensuel([self._r(20, 0.5, 0.5), self._r(13, -1.0, -1.0)], "2026-07"), -0.5)

    def test_le_cumul_somme_l_EFFET_et_non_l_ecart(self):
        """Sur un credit les deux sont opposes : sommer l'ecart inverserait la correction."""
        self.assertEqual(RS.cumul_mensuel([self._r(14, -3.18, 3.18)], "2026-07"), 3.18)

    def test_autre_mois_exclu(self):
        lignes = [self._r(4, 1.0, 1.0, mois=8)]
        self.assertEqual(RS.cumul_mensuel(lignes, "2026-07"), 0.0)
        self.assertEqual(RS.cumul_mensuel(lignes, "2026-08"), 1.0)


class TestAbsorptionParLeCumul(unittest.TestCase):
    """Le mouvement passe a « identifie » des lors que l'ecriture mensuelle du mois existe —
    meme convention que `_frais_du_mois`, dont c'est le prolongement."""

    def _ctx(self, avec_ecriture=True):
        return C.LinkContext(
            cheque_no_index={"Frais bancaire 08-2026": "ACC-JV-562"} if avec_ecriture else {})

    def test_ecart_repris_quand_l_ecriture_du_mois_existe(self):
        c = cls("m1", 703.5, ecart=0.5)
        c.statut = C.STATUT_A_VERIFIER
        self.assertTrue(C._absorber_ecart(c, {"date": date(2026, 8, 4)}, self._ctx()))
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertIn("ACC-JV-562", c.raison)

    def test_sans_ecriture_du_mois_le_mouvement_reste_en_attente(self):
        c = cls("m1", 703.5, ecart=0.5)
        c.statut = C.STATUT_A_VERIFIER
        self.assertFalse(C._absorber_ecart(c, {"date": date(2026, 8, 4)}, self._ctx(False)))
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)

    def test_au_dela_de_la_tolerance_rien_n_est_absorbe(self):
        c = cls("m1", 703.5, ecart=100.0)
        c.statut = C.STATUT_A_VERIFIER
        self.assertFalse(C._absorber_ecart(c, {"date": date(2026, 8, 4)}, self._ctx()))
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)

    def test_ecart_de_GROUPE_passe_explicitement(self):
        """Les colonnes de la ligne restent vides pour ne pas compter l'ecart N fois : c'est
        l'appelant qui fournit l'ecart du groupe."""
        c = cls("m1", 240.0, groupe="echeance-LD2227700127-04-08-2026")
        c.statut = C.STATUT_A_VERIFIER
        self.assertTrue(C._absorber_ecart(c, {"date": date(2026, 8, 4)}, self._ctx(),
                                          ecart=1.0, montant=1197.957))
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual(c.ecart, 0.0)


if __name__ == "__main__":
    unittest.main()
