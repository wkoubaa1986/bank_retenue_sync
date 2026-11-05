"""Facturation mensuelle : écritures hors dossier (sortie de charge, dépense financée par un compte exclu) — règles pures."""
import unittest

from bank_retenue_sync.facturation import charges as C

EUROS = "Compte Wassim Euros - A&S"


def je(nom, credites, ttc=10.0):
    return {"document_type": "Journal Entry", "document_name": nom, "date": "2026-11-05", "ref": nom,
            "comptes_credites": credites, "ttc": ttc}


class TestHorsDossier(unittest.TestCase):
    def test_sortie_de_charge(self):
        # ACC-JV-2026-01034 : Dr Compte Wassim Euros / Cr Charges Diverses 29 902,400
        self.assertEqual(C.raison_hors_dossier(je("JV-1034", ["Charges Diverses - A&S"]), -29902.4, set()), C.RAISON_SORTIE)
        self.assertEqual(C.raison_hors_dossier(je("JV-0", ["X"]), 0, set()), C.RAISON_SORTIE)
        self.assertEqual(C.raison_hors_dossier(je("JV-1033", ["STE430127B - Zitouna - A&S"]), 29902.4, {EUROS}), "")

    def test_case_du_reglage_decochee(self):
        self.assertEqual(C.raison_hors_dossier(je("JV-1034", ["Charges Diverses - A&S"]), -29902.4, set(), sorties=False), "")
        lignes = [je("JV-1034", ["Charges Diverses - A&S"]), je("JV-PUB", [EUROS])]
        gardees, ecartees = C.ecarter(lignes, {("Journal Entry", "JV-1034"): -29902.4}, {EUROS}, sorties=False)
        self.assertEqual(([l["document_name"] for l in gardees], [e["document_name"] for e in ecartees]), (["JV-1034"], ["JV-PUB"]))

    def test_financee_par_un_compte_exclu(self):
        self.assertEqual(C.raison_hors_dossier(je("JV-PUB", [EUROS]), 300.0, {EUROS}), "financée par " + EUROS)
        self.assertEqual(C.raison_hors_dossier(je("JV-PUB", [EUROS]), 300.0, set()), "")      # réglage vide
        self.assertEqual(C.raison_hors_dossier(je("JV-X", ["Espèces - A&S"]), None, {EUROS}), "")

    def test_seules_les_ecritures_de_journal(self):
        pinv = {"document_type": "Purchase Invoice", "document_name": "PINV-1", "comptes_credites": [EUROS]}
        self.assertEqual(C.raison_hors_dossier(pinv, -5, {EUROS}), "")

    def test_ecarter_et_nets(self):
        nets = C.nets_par_voucher([
            {"voucher_type": "Journal Entry", "voucher_no": "JV-1033", "debit": 29902.4, "credit": 0},
            {"voucher_type": "Journal Entry", "voucher_no": "JV-1034", "debit": 0, "credit": 29902.4},
            {"voucher_type": "Journal Entry", "voucher_no": "JV-MIX", "debit": 100, "credit": 30},
        ])
        self.assertEqual(nets[("Journal Entry", "JV-MIX")], 70)
        lignes = [je("JV-1033", ["STE430127B - Zitouna - A&S"], 29902.4), je("JV-1034", ["Charges Diverses - A&S"], 29902.4),
                  je("JV-MIX", ["Espèces - A&S"], 70), je("JV-PUB", [EUROS], 300)]
        gardees, ecartees = C.ecarter(lignes, nets, {EUROS})
        self.assertEqual([l["document_name"] for l in gardees], ["JV-1033", "JV-MIX"])
        self.assertEqual([(e["document_name"], e["raison"]) for e in ecartees],
                         [("JV-1034", C.RAISON_SORTIE), ("JV-PUB", "financée par " + EUROS)])
        self.assertEqual(C.totaux_du_bloc([dict(l, ht=0, tva=0, retenue=0, manque=False, exemption="", justificatifs=[])
                                           for l in gardees])["ttc"], 29972.4)
