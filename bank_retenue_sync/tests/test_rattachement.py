"""Un prelevement couvert par PLUSIEURS pieces, et le rattachement MANUEL (06/10/2026).

Cas reel : « PAIEMENT INTERNET 0510ORANGE TUNIS » 51,856 (FT262780LGWS) = deux factures Orange de
25,928 saisies chacune avec la reference bancaire (ACC-JV-2026-00845 et 00846). Le moteur exigeait
UNE seule ecriture par reference : le mouvement restait « a verifier ».

Contexte injecte, aucune ecriture en base (meme convention que test_identification).
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.bank import classify as C, registry


def orange(montant=51.856, ref="FT262780LGWS"):
    return {"date": date(2026, 10, 5), "date_valeur": date(2026, 10, 5),
            "operation": "PAIEMENT INTERNET 0510ORANGE TUNIS", "reference": ref,
            "debit": montant, "credit": 0.0}


def ctx_citations(noms_montants, ref="FT262780LGWS", **kw):
    return C.LinkContext(
        je_par_reference={ref: [n for n, _ in noms_montants]},
        ecritures_bancaires=[{"voucher_no": n, "posting_date": date(2026, 10, 5), "montant": v}
                             for n, v in noms_montants if v is not None], **kw)


class TestEcrituresComplementaires(unittest.TestCase):

    def test_deux_factures_qui_font_le_prelevement(self):
        g = C.ecritures_complementaires(["JV-845", "JV-846"], {"JV-845": 25.928, "JV-846": 25.928}, 51.856)
        self.assertEqual(g, {"voucher_no": "JV-845", "montant": 51.856, "pieces": ["JV-845", "JV-846"]})

    def test_total_different_ne_vaut_rien(self):
        self.assertIsNone(C.ecritures_complementaires(
            ["JV-1", "JV-2"], {"JV-1": 25.928, "JV-2": 40.0}, 51.856))

    def test_montant_inconnu_ne_vaut_rien(self):
        self.assertIsNone(C.ecritures_complementaires(["JV-1", "JV-2"], {"JV-1": 25.928}, 51.856))

    def test_reference_de_contrat_citee_par_des_dizaines_d_echeances(self):
        noms = ["JV-%d" % i for i in range(31)]
        self.assertIsNone(C.ecritures_complementaires(noms, {n: 858.418 for n in noms}, 858.418))


class TestPrelevementCouvertParDeuxEcritures(unittest.TestCase):

    def test_le_paiement_orange_est_identifie(self):
        c = C.classify_one(orange(), ctx_citations([("ACC-JV-2026-00845", 25.928),
                                                    ("ACC-JV-2026-00846", 25.928)]))
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual((c.document_type, c.document_name), ("Journal Entry", "ACC-JV-2026-00845"))
        self.assertEqual((c.montant_document, c.ecart), (51.856, 0.0))
        self.assertIn("ACC-JV-2026-00845 + ACC-JV-2026-00846", c.raison)

    def test_deux_ecritures_qui_ne_font_pas_le_montant_restent_a_verifier(self):
        c = C.classify_one(orange(), ctx_citations([("JV-1", 25.928), ("JV-2", 10.0)]))
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)
        self.assertIsNone(c.document_name)

    def test_une_seule_ecriture_reste_identifiee_comme_avant(self):
        c = C.classify_one(orange(25.928), ctx_citations([("JV-1", 25.928)]))
        self.assertEqual((c.statut, c.document_name, c.raison), (C.STATUT_IDENTIFIE, "JV-1", ""))


class TestLienManuel(unittest.TestCase):

    def lien(self, *pieces, motif="un paiement pour deux factures", par="wassim"):
        return {"pieces": [dict(doctype="Journal Entry", name=n, docstatus=s, montant=v)
                           for n, s, v in pieces], "motif": motif, "par": par}

    def classer(self, lien, m=None, **kw):
        m = m or orange()
        ctx = C.LinkContext(liens_manuels={registry.movement_key(m): lien}, **kw)
        return C.classify_one(m, ctx)

    def test_deux_pieces_soumises_font_le_montant(self):
        c = self.classer(self.lien(("JV-A", 1, 25.928), ("JV-B", 1, 25.928)))
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual((c.document_name, c.montant_document, c.ecart), ("JV-A", 51.856, 0.0))
        self.assertIn("rattaché à la main par wassim : un paiement pour deux factures", c.raison)
        self.assertIn("JV-A + JV-B", c.raison)
        # La regle categorise toujours (cumuls, rapports).
        self.assertTrue(c.categorie)

    def test_le_lien_manuel_prime_sur_la_citation(self):
        c = self.classer(self.lien(("JV-MAIN", 1, 51.856)),
                         je_par_reference={"FT262780LGWS": ["JV-AUTO"]},
                         ecritures_bancaires=[{"voucher_no": "JV-AUTO", "montant": 51.856}])
        self.assertEqual(c.document_name, "JV-MAIN")

    def test_ecart_mesure_et_dit(self):
        c = self.classer(self.lien(("JV-A", 1, 50.0)))
        self.assertEqual((c.statut, c.ecart), (C.STATUT_IDENTIFIE, 1.856))
        self.assertIn("écart de 1.856", c.raison)

    def test_piece_en_brouillon(self):
        c = self.classer(self.lien(("JV-A", 1, 25.928), ("JV-B", 0, 25.928)))
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE_BROUILLON)

    def test_piece_annulee_ou_supprimee_redevient_a_verifier(self):
        partielle = self.classer(self.lien(("JV-A", 1, 25.928), ("JV-B", 2, None)))
        self.assertEqual((partielle.statut, partielle.document_name), (C.STATUT_A_VERIFIER, "JV-A"))
        self.assertIn("JV-B", partielle.raison)
        aucune = self.classer(self.lien(("JV-A", None, None)))
        self.assertEqual((aucune.statut, aucune.document_name), (C.STATUT_A_VERIFIER, None))
        self.assertIn("à rattacher de nouveau", aucune.raison)

    def test_une_piece_rattachee_n_est_plus_appariee_par_montant(self):
        rattache = orange()
        orphelin = {"date": date(2026, 10, 5), "date_valeur": date(2026, 10, 5),
                    "operation": "LIBELLE SANS REGLE", "reference": "FTZZZ999", "debit": 25.928,
                    "credit": 0.0}
        piece = {"voucher_type": "Journal Entry", "voucher_no": "JV-B", "posting_date": date(2026, 10, 5),
                 "montant": 25.928, "sens": "Debit", "texte": "facture"}
        ctx = C.LinkContext(pieces=[piece], liens_manuels={registry.movement_key(rattache): self.lien(
            ("JV-A", 1, 25.928), ("JV-B", 1, 25.928))})
        res = {c.reference: c for c in C.classify([rattache, orphelin], ctx)}
        self.assertIsNone(res["FTZZZ999"].document_name)


class TestPiecesDuLien(unittest.TestCase):

    def test_lecture_tolerante(self):
        self.assertEqual(registry.pieces_du_lien(None), [])
        self.assertEqual(registry.pieces_du_lien("pas du json"), [])
        self.assertEqual(registry.pieces_du_lien(
            '[{"doctype": "Journal Entry", "name": " JV-1 "}, {"doctype": "Sales Invoice", "name": "X"}, {}]'),
            [{"doctype": "Journal Entry", "name": "JV-1"}])


if __name__ == "__main__":
    unittest.main()
