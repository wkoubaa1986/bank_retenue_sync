"""Tests du rapprochement des encaissements recus (cheques / traites / Aramex).

Aucun appel externe : mouvements bancaires, remise et advices sont injectes. Le matching et le
mapping des champs de l'Encaissement Paiement sont valides sur des donnees representatives des
formats reels (reference_no des PE, libelles bancaires).
"""
import types
import unittest
from datetime import date, timedelta

from bank_retenue_sync.bank import movements as mv
from bank_retenue_sync.encaissement import matching, builder, pending


def _adv(net, nums, payment_date=None):
    """Fabrique un faux PaymentAdvice minimal (net_total + lignes avec document_number)."""
    lines = [types.SimpleNamespace(document_number=n, reference="") for n in nums]
    return types.SimpleNamespace(net_total=net, lines=lines, payment_date=payment_date)


class TestPendingNumberExtraction(unittest.TestCase):
    def test_cheque_number_from_reference_no(self):
        self.assertEqual(pending._extract_number("4001710-Banque Zitouna"), "4001710")
        self.assertEqual(pending._extract_number("011289089405"), "011289089405")

    def test_aramex_number_requires_explicit_pattern(self):
        self.assertEqual(pending._extract_aramex_number("Aramex N: 48812240654"), "48812240654")
        # commande web sur le meme compte d'attente : surtout PAS '007803'
        self.assertIsNone(pending._extract_aramex_number("WEB1-007803"))
        self.assertIsNone(pending._extract_aramex_number(""))


class TestBankFreshness(unittest.TestCase):
    def _mv(self, d):
        return [{"date": d, "operation": "X", "reference": "R", "debit": 0, "credit": 1}]

    def test_recent_export_is_fresh(self):
        movements = self._mv(date.today())
        self.assertEqual(mv.movements_asof(movements), date.today())
        self.assertFalse(mv.is_stale(movements))

    def test_old_export_is_stale(self):
        self.assertTrue(mv.is_stale(self._mv(date.today() - timedelta(days=12))))

    def test_empty_export_is_stale(self):
        self.assertIsNone(mv.movements_asof([]))
        self.assertTrue(mv.is_stale([]))

    def test_weekend_does_not_age_the_export(self):
        """Le portail ne produit rien le week-end : un export arrete au vendredi est encore frais
        le lundi. En calendaire (3 jours) il paraissait perime -> faux positif tous les lundis."""
        vendredi, lundi = date(2026, 7, 31), date(2026, 8, 3)
        self.assertEqual(vendredi.weekday(), 4)
        self.assertEqual(mv.business_days_between(vendredi, lundi), 1)
        self.assertEqual((lundi - vendredi).days, 3)          # ce que comptait l'ancienne version

    def test_business_days_ignore_only_weekends(self):
        vendredi = date(2026, 7, 31)
        self.assertEqual(mv.business_days_between(vendredi, date(2026, 8, 5)), 3)   # lun+mar+mer
        self.assertEqual(mv.business_days_between(vendredi, vendredi), 0)
        self.assertEqual(mv.business_days_between(date(2026, 8, 5), vendredi), 0)   # fin < debut


class TestWaitJob(unittest.TestCase):
    """`wait_job` ne doit jamais rendre la main en silence sur un job rate ni boucler sans fin."""

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def _patch_get(self, payload):
        original = mv.requests.get
        mv.requests.get = lambda *a, **k: self._Resp(payload)
        self.addCleanup(lambda: setattr(mv.requests, "get", original))

    def test_succeeded_returns_job(self):
        self._patch_get({"status": "succeeded", "result": {"artifacts": ["x.xlsx"]}})
        self.assertEqual(mv.wait_job("jb_1")["status"], "succeeded")

    def test_failed_raises_with_service_error(self):
        self._patch_get({"status": "failed", "error": "portail indisponible"})
        with self.assertRaises(RuntimeError) as ctx:
            mv.wait_job("jb_2")
        self.assertIn("portail indisponible", str(ctx.exception))

    def test_timeout_raises(self):
        self._patch_get({"status": "running", "progress": {"step": "export", "pct": 40}})
        with self.assertRaises(TimeoutError):
            mv.wait_job("jb_3", timeout=0, interval=0)


class TestTraiteMatching(unittest.TestCase):
    def test_effet_matched_by_number(self):
        movements = [
            {"credit": 990.425, "debit": 0, "operation": "ENCAISSEMENT EFFET 011289089405",
             "reference": "FT001", "date": None},
        ]
        pending = [{"name": "ACC-PAY-1", "numero": "011289089405", "paid_amount": 990.425,
                    "party": "SBCOM"}]
        rows, diag = matching.match_traites(movements, pending, consumed=set())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ref_paiement"], "ACC-PAY-1")
        self.assertEqual(rows[0]["statut"], "Versé")
        self.assertEqual(rows[0]["n_cheque"], "011289089405")

    def test_effet_no_pending_is_diagnostic(self):
        # depuis la politique d'ecart du 2026-08-18, le credit sans PE produit le diagnostic
        # historique ET un ecart bloquant « Sans pièce » (flux traite).
        movements = [{"credit": 100, "debit": 0, "operation": "ENCAISSEMENT EFFET 999999999999",
                      "reference": "FT", "date": None}]
        rows, diag = matching.match_traites(movements, [], consumed=set())
        self.assertEqual(rows, [])
        self.assertTrue(any("aucune PE" in (d.get("reason") or "") for d in diag))
        ecarts = [d for d in diag if d.get("type") == "ecart"]
        self.assertEqual(len(ecarts), 1)
        self.assertEqual((ecarts[0]["flux"], ecarts[0]["sous_type"], ecarts[0]["bloquant"]),
                         ("traite", "Sans pièce", 1))

    def test_already_encaissed_reference_is_skipped_silently(self):
        """Un effet deja encaisse ne doit produire ni ligne ni diagnostic : la fenetre d'export
        couvre 8 semaines, on ne re-signale pas tous les depots deja traites."""
        movements = [{"credit": 100, "debit": 0, "operation": "ENCAISSEMENT EFFET 999999999999",
                      "reference": "FT26198BPZR0", "date": None}]
        rows, diag = matching.match_traites(movements, [], consumed={"FT26198BPZR0"})
        self.assertEqual((rows, diag), ([], []))


class TestChequeMatching(unittest.TestCase):
    def test_cheque_matched_via_remise(self):
        movements = [{"credit": 1191.95, "debit": 0, "operation": "ENC CHEQ REMISE 90028032",
                      "reference": "FT", "date": None}]
        pending = [{"name": "ACC-PAY-9", "numero": "0000254", "paid_amount": 1191.95,
                    "party": "STE BEN AISSA WATER"}]

        def fake_loader(bon):
            self.assertEqual(bon, "90028032")
            return {"date_remise": "2026-07-07", "total": 1191.95,
                    "cheques": [{"numero_cheque": "0000254", "banque": "WIB",
                                 "emetteur": "STE BEN AISSA WATER", "montant": 1191.95}]}

        rows, diag = matching.match_cheques(movements, pending, remise_loader=fake_loader,
                                            consumed=set())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ref_paiement"], "ACC-PAY-9")
        self.assertEqual(rows[0]["bon_remise"], "90028032")
        self.assertEqual(rows[0]["statut"], "Versé")

    def test_cheque_in_remise_without_pending_is_diagnostic(self):
        movements = [{"credit": 50, "debit": 0, "operation": "ENC CHEQ 12345678",
                      "reference": "FT", "date": None}]
        rows, diag = matching.match_cheques(
            movements, [], consumed=set(),
            remise_loader=lambda bon: {"cheques": [{"numero_cheque": "0000254", "montant": 50}]})
        self.assertEqual(rows, [])
        self.assertTrue(any(d["reason"].startswith("aucune PE") for d in diag))

    def test_consumed_bon_never_reaches_the_extractor(self):
        """Garde-fou de COUT : un bordereau deja encaisse ne doit jamais etre telecharge ni passe
        a OpenAI. Sur donnees reelles, 13 des 15 depots de la fenetre etaient dans ce cas."""
        movements = [{"credit": 601.0, "debit": 0, "operation": "ENC CHEQ TN NUM 90028133",
                      "reference": "FT26204X2BHC", "date": None}]
        appels = []

        def loader(bon):
            appels.append(bon)
            return {"cheques": [{"numero_cheque": "0000516", "montant": 601.0}]}

        rows, diag = matching.match_cheques(movements, [], remise_loader=loader,
                                            consumed={"90028133"})
        self.assertEqual(appels, [])                  # aucune extraction declenchee
        self.assertEqual((rows, diag), ([], []))


class TestAramexMatching(unittest.TestCase):
    def test_bank_first_credit_then_advice(self):
        movements = [{"credit": 137.4, "debit": 0, "operation": "VIR TN AUTRE BQ ARAMEX TUNISIE",
                      "reference": "FT777", "date": None}]
        pending = [
            {"name": "PE-A", "numero": "48812240654", "paid_amount": 106.4, "party": "Ridha"},
            {"name": "PE-B", "numero": "48812240643", "paid_amount": 31.0, "party": "Ayadi"},
        ]
        advices = [_adv(137.4, ["48812240654", "48812240643"])]
        rows, diag = matching.match_aramex(movements, pending, advices, consumed=set())
        self.assertEqual({r["ref_paiement"] for r in rows}, {"PE-A", "PE-B"})
        self.assertTrue(all(r["type"] == "Virement" for r in rows))
        self.assertTrue(all(r["n_virement"] == "FT777" for r in rows))

    def test_guard_sum_mismatch_blocks_batch(self):
        movements = [{"credit": 137.4, "debit": 0, "operation": "VIR TN AUTRE BQ ARAMEX TUNISIE",
                      "reference": "FT", "date": None}]
        # une seule PE en attente (31.0) alors que le credit = 137.4 -> garde-fou
        pending = [{"name": "PE-B", "numero": "48812240643", "paid_amount": 31.0, "party": "X"}]
        advices = [_adv(137.4, ["48812240654", "48812240643"])]
        rows, diag = matching.match_aramex(movements, pending, advices, consumed=set())
        self.assertEqual(rows, [])
        self.assertTrue(any("garde-fou" in d["reason"] for d in diag))

    def test_pe_without_number_never_matches(self):
        # PE 'WEB1-007803' -> numero None : elle ne doit s'apparier a AUCUNE ligne d'advice, meme
        # quand l'advice porte des references vides (les deux valaient '0' apres normalisation).
        movements = [{"credit": 40.0, "debit": 0, "operation": "VIR TN AUTRE BQ ARAMEX TUNISIE",
                      "reference": "FT", "date": None}]
        pending_pes = [{"name": "PE-WEB", "numero": None, "paid_amount": 40.0, "party": "X"}]
        advices = [_adv(40.0, ["48812240654"])]
        rows, diag = matching.match_aramex(movements, pending_pes, advices, consumed=set())
        self.assertEqual(rows, [])
        self.assertTrue(any("aucune PE" in d["reason"] for d in diag))

    def test_guard_diagnostic_names_unmatched_advice_numbers(self):
        movements = [{"credit": 137.4, "debit": 0, "operation": "VIR TN AUTRE BQ ARAMEX TUNISIE",
                      "reference": "FT", "date": None}]
        pending_pes = [{"name": "PE-B", "numero": "48812240643", "paid_amount": 31.0, "party": "X"}]
        advices = [_adv(137.4, ["48812240654", "48812240643"])]
        rows, diag = matching.match_aramex(movements, pending_pes, advices, consumed=set())
        guard = [d for d in diag if "garde-fou" in d["reason"]][0]
        self.assertEqual(guard["pe_appariees"], ["PE-B"])
        self.assertIn("48812240654", guard["numeros_advice_sans_pe"])
        self.assertAlmostEqual(guard["ecart"], 106.4)

    def test_credit_without_advice_ignored(self):
        # virement client direct (pas d'advice) -> ignore en v1 (ni row ni diagnostic bloquant)
        movements = [{"credit": 500, "debit": 0, "operation": "VIR TN CLIENT",
                      "reference": "FT", "date": None}]
        rows, diag = matching.match_aramex(movements, [], [], consumed=set())
        self.assertEqual(rows, [])

    def test_consumed_virement_is_skipped_silently(self):
        """Un virement Aramex deja encaisse ne produit plus le diagnostic 'advice trouve mais
        aucune PE appariee' : sur donnees reelles, les 8 credits de la fenetre etaient dans ce cas
        et noyaient les diagnostics vraiment actionnables."""
        movements = [{"credit": 2195.92, "debit": 0, "operation": "VIR TN AUTRE BQ ARAMEX TUNISIE",
                      "reference": "FT26209ZZCTH", "date": None}]
        advices = [_adv(2195.92, ["51330111722"])]
        rows, diag = matching.match_aramex(movements, [], advices,
                                           consumed={"FT26209ZZCTH"})
        self.assertEqual((rows, diag), ([], []))


class TestBuilder(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(builder.build_encaissement([], [], []))

    def test_field_mapping_in_memory(self):
        doc = builder.build_encaissement(
            cheque_rows=[{"ref_paiement": "PE1", "n_cheque": "0000254", "valeur": 100.0,
                          "bon_remise": "90028032", "statut": "Versé", "emmeteur": "X"}],
            traite_rows=[{"ref_paiement": "PE2", "n_cheque": "011289089405", "valeur": 990.4,
                          "bon_remise": "FT", "statut": "Versé"}],
            aramex_rows=[{"ref_paiement": "PE3", "type": "Virement", "n_virement": "FT777",
                          "valeur": 106.4, "n_aramex": "48812240654"}],
            insert=False)
        self.assertEqual(len(doc.chèques_a_encaisser), 1)
        self.assertEqual(doc.chèques_a_encaisser[0].ref_paiement, "PE1")
        self.assertEqual(doc.chèques_a_encaisser[0].statut, "Versé")
        self.assertEqual(len(doc.traite_bancaire_a_encaissées), 1)
        self.assertEqual(len(doc.livraison_aramex_a_encaisser), 1)
        self.assertEqual(doc.livraison_aramex_a_encaisser[0].type, "Virement")
        self.assertAlmostEqual(doc.total_des_chèques, 100.0)
        self.assertAlmostEqual(doc.total_virement_a, 106.4)


def _adv_lignes(net, lignes, frais=None):
    """Fabrique un PaymentAdvice AVEC montants par ligne (chemin nominal de match_aramex).
    `lignes` : [(numero, montant), ...] ; `frais` : [(description, montant_negatif), ...]."""
    ls = [types.SimpleNamespace(document_number=n, reference="", invoice_amount=m,
                                withholding_tax=0.0, description="")
          for n, m in lignes]
    for desc, m in (frais or []):
        ls.append(types.SimpleNamespace(document_number="", reference="", invoice_amount=m,
                                        withholding_tax=0.0, description=desc))
    return types.SimpleNamespace(net_total=net, lines=ls, payment_date=None)


class TestAramexEcarts(unittest.TestCase):
    """Politique d'ecart du 2026-08-18 : tolerance <= 1 DT, frais absorbes, deltas et lignes
    sans piece INCLUS/traces au lieu de refuser le lot (cas reel FT26224F6S40)."""

    MOVE = {"credit": 0, "debit": 0, "operation": "VIR TN AUTRE BQ ARAMEX TUNISIE",
            "reference": "FT26224F6S40", "date": None}

    def _run(self, credit, advice, pes):
        m = dict(self.MOVE, credit=credit)
        return matching.match_aramex([m], pes, [advice], consumed=set())

    def test_frais_et_tolerance_ne_bloquent_plus(self):
        # Fillali : PE 106,4 / advice 106,0 (+0,4 <= 1 DT) ; frais TRFREVENUE -2,38.
        adv = _adv_lignes(103.62, [("48812240654", 106.0)], frais=[("TRFREVENUE", -2.38)])
        pes = [{"name": "PE-FIL", "numero": "48812240654", "paid_amount": 106.4, "party": "Fillali"}]
        rows, diag = self._run(103.62, adv, pes)
        self.assertEqual([r["ref_paiement"] for r in rows], ["PE-FIL"])
        self.assertAlmostEqual(rows[0]["valeur"], 106.4)
        ecarts = [d for d in diag if d.get("type") == "ecart"]
        self.assertEqual({e["sous_type"] for e in ecarts}, {"Toléré", "Frais"})
        self.assertTrue(all(not e["bloquant"] for e in ecarts))
        self.assertFalse(any("garde-fou" in (d.get("reason") or "") for d in diag))

    def test_delta_superieur_a_1dt_inclut_mais_bloque(self):
        # Baccari : PE 440, Aramex reverse 265 (retour article defectueux) -> ligne INCLUSE
        # a 440 + ecart BLOQUANT ; l'autre ligne exacte passe sans ecart.
        adv = _adv_lignes(296.0, [("48812240606", 265.0), ("48812240643", 31.0)])
        pes = [{"name": "PE-BAC", "numero": "48812240606", "paid_amount": 440.0, "party": "Baccari"},
               {"name": "PE-B", "numero": "48812240643", "paid_amount": 31.0, "party": "Ayadi"}]
        rows, diag = self._run(296.0, adv, pes)
        self.assertEqual({r["ref_paiement"] for r in rows}, {"PE-BAC", "PE-B"})
        ecarts = [d for d in diag if d.get("type") == "ecart"]
        self.assertEqual(len(ecarts), 1)
        self.assertEqual(ecarts[0]["sous_type"], "Delta paiement")
        self.assertEqual(ecarts[0]["bloquant"], 1)
        self.assertEqual(ecarts[0]["ref_paiement"], "PE-BAC")
        self.assertAlmostEqual(ecarts[0]["ecart"], 175.0)

    def test_ligne_advice_sans_pe_devient_ecart_bloquant(self):
        # suivi 51330112142 (36 DT) paye par Aramex mais jamais saisi -> ecart « Sans pièce ».
        adv = _adv_lignes(67.0, [("51330112142", 36.0), ("48812240643", 31.0)])
        pes = [{"name": "PE-B", "numero": "48812240643", "paid_amount": 31.0, "party": "Ayadi"}]
        rows, diag = self._run(67.0, adv, pes)
        self.assertEqual([r["ref_paiement"] for r in rows], ["PE-B"])
        ecarts = [d for d in diag if d.get("type") == "ecart"]
        self.assertEqual(len(ecarts), 1)
        self.assertEqual(ecarts[0]["sous_type"], "Sans pièce")
        self.assertEqual(ecarts[0]["bloquant"], 1)
        self.assertAlmostEqual(ecarts[0]["montant_advice"], 36.0)

    def test_lot_entier_sans_pe_reste_un_diagnostic(self):
        # rien d'encaissable -> pas de brouillon possible, donc pas d'ecarts : diagnostic
        # historique conserve (le lot reste visible).
        adv = _adv_lignes(1.0, [("51330112186", 1.0)])
        rows, diag = self._run(1.0, adv, [])
        self.assertEqual(rows, [])
        self.assertFalse([d for d in diag if d.get("type") == "ecart"])
        self.assertTrue(any("aucune PE" in (d.get("reason") or "") for d in diag))

    def test_fake_advice_sans_montants_suit_l_ancien_garde_fou(self):
        # les advices sans montants par ligne (parse degrade) gardent le refus strict.
        adv = _adv(137.4, ["48812240654", "48812240643"])
        pes = [{"name": "PE-B", "numero": "48812240643", "paid_amount": 31.0, "party": "X"}]
        rows, diag = matching.match_aramex(
            [dict(self.MOVE, credit=137.4)], pes, [adv], consumed=set())
        self.assertEqual(rows, [])
        self.assertTrue(any("garde-fou" in (d.get("reason") or "") for d in diag))


class TestChequeTraiteEcarts(unittest.TestCase):
    """Extension de la politique d'ecart aux flux cheques et traites (decision 2026-08-18) :
    ligne de bordereau sans PE / montant PE != bordereau -> ecart persiste, lot conserve."""

    def test_cheque_sans_pe_emet_un_ecart_bloquant(self):
        movements = [{"credit": 1506.0, "debit": 0, "operation": "ENC CHEQ TN NUM 90028317",
                      "reference": "FT26230HK6Z5", "date": None}]
        loader = lambda bon: {"cheques": [
            {"numero_cheque": "0001111", "montant": 506.0, "emetteur": "A"},
            {"numero_cheque": "0002222", "montant": 1000.0, "emetteur": "B"}]}
        pes = [{"name": "PE-A", "numero": "0001111", "paid_amount": 506.0, "party": "A"}]
        rows, diag = matching.match_cheques(movements, pes, remise_loader=loader, consumed=set())
        self.assertEqual([r["ref_paiement"] for r in rows], ["PE-A"])
        ecarts = [d for d in diag if d.get("type") == "ecart"]
        self.assertEqual(len(ecarts), 1)
        self.assertEqual((ecarts[0]["flux"], ecarts[0]["sous_type"], ecarts[0]["bloquant"]),
                         ("cheque", "Sans pièce", 1))
        self.assertEqual(ecarts[0]["bon"], "90028317")
        self.assertAlmostEqual(ecarts[0]["montant_advice"], 1000.0)

    def test_cheque_delta_montant_bordereau(self):
        movements = [{"credit": 1053.001, "debit": 0, "operation": "ENC CHEQ TN NUM 90028321",
                      "reference": "FT", "date": None}]
        loader = lambda bon: {"cheques": [{"numero_cheque": "0003333", "montant": 1053.001}]}
        pes = [{"name": "PE-C", "numero": "0003333", "paid_amount": 1052.0, "party": "C"}]
        rows, diag = matching.match_cheques(movements, pes, remise_loader=loader, consumed=set())
        self.assertEqual(len(rows), 1)
        ecarts = [d for d in diag if d.get("type") == "ecart"]
        self.assertEqual(len(ecarts), 1)
        self.assertEqual(ecarts[0]["sous_type"], "Delta paiement")
        self.assertEqual(ecarts[0]["bloquant"], 1)
        self.assertAlmostEqual(ecarts[0]["ecart"], -1.001)

    def test_traite_toleree_et_sans_pe(self):
        movements = [
            {"credit": 990.0, "debit": 0, "operation": "ENCAISSEMENT EFFET 011289089405",
             "reference": "FT-T1", "date": None},
            {"credit": 500.0, "debit": 0, "operation": "ENCAISSEMENT EFFET 011289089999",
             "reference": "FT-T2", "date": None},
        ]
        pes = [{"name": "PE-T", "numero": "011289089405", "paid_amount": 990.4, "party": "T"}]
        rows, diag = matching.match_traites(movements, pes, consumed=set())
        self.assertEqual([r["ref_paiement"] for r in rows], ["PE-T"])
        ecarts = {d["sous_type"]: d for d in diag if d.get("type") == "ecart"}
        self.assertEqual(ecarts["Toléré"]["bloquant"], 0)
        self.assertAlmostEqual(ecarts["Toléré"]["ecart"], 0.4)
        self.assertEqual(ecarts["Sans pièce"]["bloquant"], 1)
        self.assertAlmostEqual(ecarts["Sans pièce"]["montant_advice"], 500.0)
