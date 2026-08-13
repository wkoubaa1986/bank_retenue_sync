"""Echeances de leasing generees depuis le releve.

Les montants sont ceux des ecritures manuelles de reference (ACC-JV-2026-00472/00473 pour la forme
amortie, 00531/00532 pour la forme simple), recoupes au centime avec les groupes du releve.
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.expenses import contrats


def mv(operation, debit, reference, jour=19, mois=6):
    return {"date": date(2026, mois, jour), "date_valeur": date(2026, mois, jour),
            "operation": operation, "reference": reference, "debit": debit, "credit": 0.0}


# Echeance reelle du Cenntro Logistar 100 au 19/06/2026 :
#   324,838 + 281,190 + 136,245 + 1 = 743,273 HT ; + 115,145 de TVA = 858,418 TTC
ECHEANCE_CENNTRO = [
    ("PAIEMENT PRINCIPAL IJARA TAMOUIL MOUADDET ENNAKL", 324.838),
    ("PAIEMENT PROFIT IJARA TAMOUIL MOUADDET ENNAKL", 281.190),
    ("PRIME TAKAFUL IJARA", 136.245),
    ("DROIT DE TIMBRE", 1.0),
    ("TVA", 115.145),
]

CONTRAT_CENNTRO = {
    "cle": "leasing_cenntro", "libelle": "Leasing Cenntro Logistar 100", "type": "Leasing",
    "reference_bancaire": "LD2614000071", "actif": 1,
    "compte_banque": "STE430127B - Zitouna - A&S", "compte_tva": "TVA 7% - A&S",
    "compte_charge": "Frais de Déplacement - A&S",
    "compte_amortissement": "Amortissement Cumulé - A&S", "montant_amortissement": 70.028,
}

CONTRAT_FIAT = {
    "cle": "leasing_fiat", "libelle": "Leasing FIAT", "type": "Leasing",
    "reference_bancaire": "LD2227700127", "actif": 1,
    "compte_banque": "STE430127B - Zitouna - A&S", "compte_tva": "TVA 19% - A&S",
    "compte_charge": "Charges remboursement véhicules - A&S",
}


def _mouvements(lignes=ECHEANCE_CENNTRO, reference="LD2614000071", jour=19, mois=6):
    return [mv(op, montant, reference, jour, mois) for op, montant in lignes]


class TestRegroupementDesEcheances(unittest.TestCase):
    def test_les_cinq_debits_du_jour_forment_une_echeance(self):
        ech = contrats.echeances_leasing(_mouvements())
        self.assertEqual(len(ech), 1)
        self.assertEqual(ech[0]["ttc"], 858.418)
        self.assertEqual(ech[0]["tva"], 115.145)
        self.assertEqual(ech[0]["ht"], 743.273)

    def test_le_timbre_et_l_assurance_sont_dans_le_HT(self):
        """Ils ne sont ni des frais bancaires ni de la TVA : ils appartiennent a la charge."""
        ech = contrats.echeances_leasing(_mouvements())[0]
        self.assertAlmostEqual(ech["ht"], 324.838 + 281.190 + 136.245 + 1.0, places=3)

    def test_deux_contrats_le_meme_jour_restent_separes(self):
        m = _mouvements() + _mouvements(reference="LD2613900139")
        ech = contrats.echeances_leasing(m)
        self.assertEqual({e["reference"] for e in ech}, {"LD2614000071", "LD2613900139"})

    def test_une_TVA_isolee_n_est_pas_une_echeance(self):
        """Les references 'CHG…' portent une TVA sur commission bancaire, seule de son groupe."""
        self.assertEqual(contrats.echeances_leasing([mv("TVA", 0.475, "CHG2614110081")]), [])

    def test_un_credit_est_ignore(self):
        m = _mouvements()
        for x in m:
            x["credit"], x["debit"] = x["debit"], 0.0
        self.assertEqual(contrats.echeances_leasing(m), [])


class TestIdentificationDuContrat(unittest.TestCase):
    def test_par_reference_et_non_par_montant(self):
        """Le premier loyer du Changan vaut 20 223,172 contre 680,232 ensuite : un critere de
        total mensuel l'aurait manque, la reference non."""
        c = contrats.contrat_par_reference("LD2614000071", [CONTRAT_CENNTRO, CONTRAT_FIAT])
        self.assertEqual(c["cle"], "leasing_cenntro")

    def test_insensible_a_la_casse_et_aux_espaces(self):
        self.assertIsNotNone(
            contrats.contrat_par_reference(" ld2614000071 ", [CONTRAT_CENNTRO]))

    def test_reference_inconnue_ne_rend_rien(self):
        self.assertIsNone(contrats.contrat_par_reference("LD9999", [CONTRAT_CENNTRO]))


class TestFormeDeLEcriture(unittest.TestCase):
    def test_forme_amortie_a_cinq_lignes(self):
        ech = contrats.echeances_leasing(_mouvements())[0]
        lignes = contrats.build_lines_leasing(CONTRAT_CENNTRO, ech)
        self.assertEqual(len(lignes), 5)
        self.assertEqual(lignes[0], {"account": "STE430127B - Zitouna - A&S", "credit": 858.418})
        self.assertEqual(sum(l.get("debit", 0) for l in lignes),
                         sum(l.get("credit", 0) for l in lignes))

    def test_la_dotation_est_reprise_du_contrat(self):
        ech = contrats.echeances_leasing(_mouvements())[0]
        lignes = contrats.build_lines_leasing(CONTRAT_CENNTRO, ech)
        amort = [l for l in lignes if l["account"] == "Amortissement Cumulé - A&S"]
        self.assertEqual(amort[0]["credit"], 70.028)

    def test_forme_simple_a_trois_lignes_sans_amortissement(self):
        """FIAT et CHERY sont traites en charge de location : aucune dotation."""
        ech = contrats.echeances_leasing(
            _mouvements(reference="LD2227700127"))[0]
        lignes = contrats.build_lines_leasing(CONTRAT_FIAT, ech)
        self.assertEqual(len(lignes), 3)
        self.assertFalse([l for l in lignes if "Amortissement" in l["account"]])

    def test_une_TVA_nulle_ne_cree_pas_de_ligne_a_zero(self):
        ech = contrats.echeances_leasing(_mouvements(lignes=[
            ("PAIEMENT PRINCIPAL IJARA", 500.0), ("PRIME TAKAFUL IJARA", 100.0)]))[0]
        lignes = contrats.build_lines_leasing(dict(CONTRAT_FIAT), ech)
        self.assertEqual(len(lignes), 2)
        self.assertEqual(ech["tva"], 0.0)

    def test_l_ecriture_est_equilibree_pour_les_deux_formes(self):
        for contrat, ref in ((CONTRAT_CENNTRO, "LD2614000071"), (CONTRAT_FIAT, "LD2227700127")):
            ech = contrats.echeances_leasing(_mouvements(reference=ref))[0]
            lignes = contrats.build_lines_leasing(contrat, ech)
            self.assertAlmostEqual(sum(l.get("debit", 0) for l in lignes),
                                   sum(l.get("credit", 0) for l in lignes), places=3)
