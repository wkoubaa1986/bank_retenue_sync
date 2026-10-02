"""Un bordereau Aramex porté par PLUSIEURS paiements en attente : le rapprochement s'arrête et avertit.

Cas réel (corrigé à la main le 02/10/2026) : le colis 51330112551 de Mehdi jedidi (85 DT encaissés
par Aramex) était posé sur la commande web WEB1-007972 (81 DT) ET sur SAL-ORD-2026-03100 (85 DT).
L'ancien index « premier trouvé » imputait le virement à la commande web, et 85 DT restaient en
dette fantôme sur le compte Aramex.
"""
import types
import unittest
from unittest import mock

from bank_retenue_sync.encaissement import ecarts, matching

MOVE = {"credit": 0, "debit": 0, "operation": "VIR TN AUTRE BQ ARAMEX TUNISIE",
        "reference": "FT26239V20MN", "date": None}


def _adv_lignes(net, lignes):
    ls = [types.SimpleNamespace(document_number=n, reference="", invoice_amount=m,
                                withholding_tax=0.0, description="") for n, m in lignes]
    return types.SimpleNamespace(net_total=net, lines=ls, payment_date=None)


def _adv_sans_montants(net, nums):
    lines = [types.SimpleNamespace(document_number=n, reference="") for n in nums]
    return types.SimpleNamespace(net_total=net, lines=lines, payment_date=None)


PE_WEB = {"name": "ACC-PAY-2026-05955", "numero": "51330112551", "paid_amount": 81.0, "party": "Mehdi jedidi"}
PE_SO = {"name": "ACC-PAY-2026-05934", "numero": "51330112551", "paid_amount": 85.0, "party": "Mehdi jedidi"}
PE_AUTRE = {"name": "ACC-PAY-2026-06001", "numero": "51330112466", "paid_amount": 53.0, "party": "Mehdi Belguith"}


def _run(credit, advice, pes):
    return matching.match_aramex([dict(MOVE, credit=credit)], pes, [advice], consumed=set())


class TestMatchingBordereauEnDouble(unittest.TestCase):

    def test_cas_reel_rien_n_est_encaisse_et_l_avertissement_nomme_les_deux_paiements(self):
        adv = _adv_lignes(138.0, [("51330112551", 85.0), ("51330112466", 53.0)])
        rows, diag = _run(138.0, adv, [PE_WEB, PE_SO, PE_AUTRE])
        # la ligne saine passe ; AUCUNE des deux pièces du bordereau en double n'est encaissée
        self.assertEqual([r["ref_paiement"] for r in rows], ["ACC-PAY-2026-06001"])
        ecs = [d for d in diag if d.get("type") == "ecart"]
        self.assertEqual(len(ecs), 1)
        e = ecs[0]
        self.assertEqual((e["sous_type"], e["bloquant"], e["suivi"], e["client"]),
                         ("Bordereau en double", 1, "51330112551", "Mehdi jedidi"))
        self.assertAlmostEqual(e["montant_advice"], 85.0)
        self.assertAlmostEqual(e["montant_piece"], 0.0)
        self.assertAlmostEqual(e["ecart"], -85.0)
        self.assertIn("ACC-PAY-2026-05955 (81.0)", e["note"])
        self.assertIn("ACC-PAY-2026-05934 (85.0)", e["note"])
        self.assertEqual((e["flux"], e["reference"]), ("aramex", "FT26239V20MN"))

    def test_l_ordre_des_paiements_ne_change_rien(self):
        for pes in ([PE_WEB, PE_SO, PE_AUTRE], [PE_SO, PE_WEB, PE_AUTRE]):
            rows, diag = _run(138.0, _adv_lignes(138.0, [("51330112551", 85.0), ("51330112466", 53.0)]), pes)
            self.assertNotIn("ACC-PAY-2026-05934", [r["ref_paiement"] for r in rows])
            self.assertNotIn("ACC-PAY-2026-05955", [r["ref_paiement"] for r in rows])

    def test_bordereau_en_double_seul_dans_le_lot_reste_visible_en_diagnostic(self):
        rows, diag = _run(85.0, _adv_lignes(85.0, [("51330112551", 85.0)]), [PE_WEB, PE_SO])
        self.assertEqual(rows, [])
        self.assertTrue(any("bordereau en double (51330112551)" in (d.get("reason") or "") for d in diag))

    def test_un_seul_paiement_par_bordereau_rien_ne_change(self):
        rows, diag = _run(85.0, _adv_lignes(85.0, [("51330112551", 85.0)]), [PE_SO])
        self.assertEqual([r["ref_paiement"] for r in rows], ["ACC-PAY-2026-05934"])
        self.assertFalse([d for d in diag if d.get("type") == "ecart"])

    def test_repli_sans_montants_ne_choisit_pas_non_plus(self):
        # advice sans montants par ligne : la somme 81 + 85 = 166 tombe juste, mais deux pièces
        # portent le même bordereau -> lot non apparié, avertissement explicite.
        adv = _adv_sans_montants(166.0, ["51330112551"])
        rows, diag = _run(166.0, adv, [PE_WEB, PE_SO])
        self.assertEqual(rows, [])
        self.assertTrue(any("bordereau en double" in (d.get("reason") or "") for d in diag))


class _FauxEcart:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def db_set(self, champ, valeur):
        setattr(self, champ, valeur)


class TestRecalculBordereauEnDouble(unittest.TestCase):
    """« Recalculer les écarts » : l'avertissement se ferme seul une fois la pièce en trop supprimée."""

    def _recalculer(self, ecart, pending_pes):
        enc = types.SimpleNamespace(docstatus=0)
        docs = {"ENC-1": enc, "EC-1": ecart}
        ajoutees, resolus = [], []

        def get_doc(doctype, nom=None):
            return docs.get(nom) or types.SimpleNamespace(name=nom)

        def get_all(doctype, filters=None, pluck=None, **kw):
            return ["EC-1"] if doctype == ecarts.DOCTYPE else []

        with mock.patch.object(ecarts.frappe, "only_for"), \
                mock.patch.object(ecarts.frappe, "get_doc", side_effect=get_doc), \
                mock.patch.object(ecarts.frappe, "get_all", side_effect=get_all), \
                mock.patch.object(ecarts.frappe, "db", types.SimpleNamespace(commit=lambda: None)), \
                mock.patch.object(ecarts, "_", side_effect=lambda texte: texte), \
                mock.patch.object(ecarts, "flt", side_effect=lambda v, p=None: round(float(v or 0), p or 9)), \
                mock.patch("bank_retenue_sync.encaissement.pending.get_pending_aramex", return_value=pending_pes), \
                mock.patch("bank_retenue_sync.encaissement.pending.get_pending_cheques", return_value=[]), \
                mock.patch("bank_retenue_sync.encaissement.pending.get_pending_traites", return_value=[]), \
                mock.patch.object(ecarts, "_refs_au_brouillon", return_value=set()), \
                mock.patch.object(ecarts, "_ajouter_ligne_brouillon",
                                  side_effect=lambda enc, pe, *a, **k: ajoutees.append(pe.name)), \
                mock.patch.object(ecarts, "_resoudre",
                                  side_effect=lambda e, res, pieces, note="": resolus.append((res, pieces))):
            # la fonction sous le décorateur whitelist (sa validation de types exige un site)
            out = getattr(ecarts.recalculer, "__wrapped__", ecarts.recalculer)("ENC-1")
        return out, ajoutees, resolus

    def _ecart(self, **kw):
        base = dict(name="EC-1", type_ecart="Bordereau en double", suivi="51330112551", montant_advice=85.0,
                    reference_bancaire="FT26239V20MN", flux="aramex", bon="", note="", ref_paiement="",
                    montant_piece=0.0, ecart=-85.0, client="Mehdi jedidi", bloquant=1)
        base.update(kw)
        return _FauxEcart(**base)

    def test_encore_deux_pieces_l_avertissement_reste(self):
        e = self._ecart()
        out, ajoutees, resolus = self._recalculer(e, [PE_WEB, PE_SO])
        self.assertEqual((ajoutees, resolus), ([], []))
        self.assertEqual(e.type_ecart, "Bordereau en double")

    def test_doublon_supprime_la_ligne_entre_au_brouillon_et_l_ecart_se_ferme(self):
        e = self._ecart()
        out, ajoutees, resolus = self._recalculer(e, [PE_SO])
        self.assertEqual(ajoutees, ["ACC-PAY-2026-05934"])
        self.assertEqual(resolus, [("Ajustement", ["ACC-PAY-2026-05934"])])
        self.assertEqual((e.ref_paiement, e.montant_piece, e.ecart), ("ACC-PAY-2026-05934", 85.0, 0.0))

    def test_piece_restante_au_mauvais_montant_devient_un_delta_paiement(self):
        e = self._ecart()
        out, ajoutees, resolus = self._recalculer(e, [PE_WEB])     # la mauvaise a été gardée : 81 ≠ 85
        self.assertEqual(ajoutees, ["ACC-PAY-2026-05955"])
        self.assertEqual(resolus, [])
        self.assertEqual((e.type_ecart, e.ref_paiement, e.ecart), ("Delta paiement", "ACC-PAY-2026-05955", -4.0))

    def test_sans_piece_qui_trouve_deux_paiements_devient_un_bordereau_en_double(self):
        e = self._ecart(type_ecart="Sans pièce", note="CASH ON DELIVERY COLLECTION")
        out, ajoutees, resolus = self._recalculer(e, [PE_WEB, PE_SO])
        self.assertEqual((ajoutees, resolus), ([], []))
        self.assertEqual((e.type_ecart, e.bloquant), ("Bordereau en double", 1))
        self.assertIn("ACC-PAY-2026-05934", e.note)


if __name__ == "__main__":
    unittest.main()
