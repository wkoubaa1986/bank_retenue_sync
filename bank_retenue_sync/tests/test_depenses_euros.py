"""Dépenses en euros : règles pures (contre-valeur, contrôle des lignes, totaux)."""
from __future__ import annotations

import unittest

from bank_retenue_sync.api.depenses_euros import MAX_LIGNES, montant_tnd, resume, verifier_lignes

CHARGES = {"Frais de Marketing - A&S", "Abonnements - A&S"}


class TestRegles(unittest.TestCase):
    def test_contre_valeur_a_trois_decimales(self):
        self.assertEqual(montant_tnd(185.73, 3.34), 620.338)       # ACC-JV-2026-00608
        self.assertEqual(montant_tnd(100, 3.364158), 336.416)

    def test_lignes_valides(self):
        lignes = [{"date": "2026-11-02", "libelle": "Pub Google", "montant_eur": 120.5, "compte_charge": "Frais de Marketing - A&S"}]
        self.assertEqual(verifier_lignes(lignes, 3.36, CHARGES), [])
        self.assertEqual(resume(lignes, 3.36), {"nb": 1, "eur": 120.5, "tnd": 404.88})

    def test_lignes_refusees(self):
        e = verifier_lignes([{"date": "", "libelle": " ", "montant_eur": 0, "compte_charge": "Inconnu - A&S"}], 0, CHARGES)
        self.assertEqual(len(e), 5)                                  # taux, date, libellé, montant, compte
        self.assertTrue(any("taux" in x.lower() for x in e))
        self.assertTrue(verifier_lignes([], 3.3, CHARGES))
        trop = [{"date": "2026-11-02", "libelle": "x", "montant_eur": 1, "compte_charge": "Frais de Marketing - A&S"}] * (MAX_LIGNES + 1)
        self.assertTrue(any("Au plus" in x for x in verifier_lignes(trop, 3.3, CHARGES)))
