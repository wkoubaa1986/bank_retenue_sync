"""L'échéancier de la commande doit suivre la dette reprise par une retenue à la source.

Cas réel : SAL-ORD-2026-02177, dette 331,00 reprise en 327,29 + 3,71 de retenue le 20/08/2026 ;
l'échéancier restait à 331,00 et le virement du 08/09 (497,970) sortait « Orphelin ».
Convention de l'app : `unittest.TestCase` pur, aucun accès base.
"""
from __future__ import annotations

import unittest

from bank_retenue_sync.tej import echeancier as E

DETTE, RAS = "Dette non payée", "Retenue a la source vente"


def ligne(name, mode, montant):
    return {"name": name, "mode_of_payment": mode, "payment_amount": montant}


class TestPlan(unittest.TestCase):

    def test_cas_nominal_la_dette_baisse_et_la_retenue_est_posee(self):
        p = E.plan([ligne("d", DETTE, 331.0)], 331.0, 327.29, 3.71, DETTE, RAS)
        self.assertTrue(p["ok"])
        self.assertEqual(p["dette"], {"name": "d", "avant": 331.0, "apres": 327.29,
                                      "supprimer": False})
        self.assertEqual(p["retenue"], {"name": None, "avant": 0.0, "apres": 3.71})

    def test_une_ligne_de_retenue_existante_est_augmentee_pas_doublee(self):
        p = E.plan([ligne("d", DETTE, 500.0), ligne("r", RAS, 5.0)], 500.0, 490.0, 10.0,
                   DETTE, RAS)
        self.assertTrue(p["ok"])
        self.assertEqual(p["retenue"], {"name": "r", "avant": 5.0, "apres": 15.0})

    def test_une_dette_entierement_couverte_disparait_de_l_echeancier(self):
        p = E.plan([ligne("d", DETTE, 3.71)], 3.71, 0.0, 3.71, DETTE, RAS)
        self.assertTrue(p["ok"])
        self.assertTrue(p["dette"]["supprimer"])
        self.assertEqual(p["retenue"]["apres"], 3.71)

    def test_les_autres_lignes_ne_sont_jamais_touchees(self):
        p = E.plan([ligne("e", "Espèces", 100.0), ligne("d", DETTE, 331.0)], 331.0, 327.29,
                   3.71, DETTE, RAS)
        self.assertTrue(p["ok"])
        self.assertEqual(p["dette"]["name"], "d")

    def test_un_echeancier_sans_ligne_de_dette_est_refuse(self):
        p = E.plan([ligne("e", "Espèces", 331.0)], 331.0, 327.29, 3.71, DETTE, RAS)
        self.assertFalse(p["ok"])
        self.assertIn("composite", p["raison"])

    def test_deux_lignes_de_dette_ne_se_tranchent_pas_au_juge(self):
        p = E.plan([ligne("d1", DETTE, 200.0), ligne("d2", DETTE, 131.0)], 331.0, 327.29, 3.71,
                   DETTE, RAS)
        self.assertFalse(p["ok"])

    def test_une_ligne_qui_ne_porte_pas_la_dette_reprise_est_refusee(self):
        # L'échéancier a déjà été retouché à la main : on ne repasse pas derrière.
        p = E.plan([ligne("d", DETTE, 300.0)], 331.0, 327.29, 3.71, DETTE, RAS)
        self.assertFalse(p["ok"])
        self.assertIn("ne porte pas la dette reprise", p["raison"])

    def test_la_tolerance_absorbe_l_arrondi_au_millime(self):
        p = E.plan([ligne("d", DETTE, 331.001)], 331.0, 327.29, 3.71, DETTE, RAS)
        self.assertTrue(p["ok"])

    def test_sans_part_retenue_rien_ne_change(self):
        self.assertFalse(E.plan([ligne("d", DETTE, 331.0)], 331.0, 331.0, 0.0, DETTE, RAS)["ok"])

    def test_deux_lignes_de_retenue_sont_laissees_a_l_humain(self):
        p = E.plan([ligne("d", DETTE, 331.0), ligne("r1", RAS, 1.0), ligne("r2", RAS, 2.0)],
                   331.0, 327.29, 3.71, DETTE, RAS)
        self.assertFalse(p["ok"])


class TestCommandesDeLaPiece(unittest.TestCase):

    def commandes(self, reference_no, references, existantes=("SAL-ORD-1",), par_facture=None):
        return E.commandes_parmi(reference_no, references, lambda n: n in existantes,
                                 lambda factures: (par_facture or {}).get(tuple(factures), []))

    def test_la_convention_maison_reference_no_prime(self):
        self.assertEqual(self.commandes("SAL-ORD-1", []), ["SAL-ORD-1"])

    def test_un_reference_no_libre_n_est_pas_une_commande(self):
        self.assertEqual(self.commandes("Dette a comprendre", []), [])

    def test_a_defaut_les_commandes_des_factures_soldees(self):
        refs = [{"reference_doctype": "Sales Invoice", "reference_name": "SI-1"}]
        self.assertEqual(self.commandes("", refs, par_facture={("SI-1",): ["SAL-ORD-9"]}),
                         ["SAL-ORD-9"])

    def test_une_commande_referencee_directement(self):
        refs = [{"reference_doctype": "Sales Order", "reference_name": "SAL-ORD-2"}]
        self.assertEqual(self.commandes("", refs), ["SAL-ORD-2"])

    def test_pas_de_doublon_quand_les_sources_concordent(self):
        refs = [{"reference_doctype": "Sales Invoice", "reference_name": "SI-1"}]
        self.assertEqual(self.commandes("SAL-ORD-1", refs, par_facture={("SI-1",): ["SAL-ORD-1"]}),
                         ["SAL-ORD-1"])


if __name__ == "__main__":
    unittest.main()
