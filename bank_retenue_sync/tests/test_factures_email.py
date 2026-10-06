"""Factures recues par e-mail (06/10/2026) : « pourquoi l'application banque n'a pas detecte la facture
Aramex ? ni la facture Total ? ».

- Aramex : septembre pris pour deja saisi a cause d'une depense de CAISSE « Frais d'expedition
  Aramex » (10 DT, sans periode au libelle) ; et une deuxieme E-INV dans le mois aurait ete ignoree.
  -> idempotence par numero E-INV, repli « saisie manuelle du mois » seulement si le libelle porte
  la periode et ne cite pas une autre facture.
- Total : la facture mensuelle arrive en PDF depuis 09/2026 (plus de ZIP) ; en aout 2026 deux
  factures (ancien et nouveau systeme). -> lecteur PDF, idempotence par numero.

Meme convention que les tests existants : unittest pur, base et boite mail mockees.
"""
from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bank_retenue_sync import orchestrator as O
from bank_retenue_sync.mail import total_invoice as T

# Texte tel que PyMuPDF le sort (facture FP26/716122 du 30/09/2026, abregee).
PDF_MENSUELLE = """Numéro Client
2044
Compte Client
512340
Numéro Facture
FP26/716122
Date du Document
30/09/2026
AQUA WORLD ET SERVICING
Produits et services consommés
SUPER SP
2.044
408.73
835.599
19.00%
158.763
994.362
GASOIL SS
1.880
78.46
147.487
13.00%
19.174
166.661
Total des produits et services consommés
1,100.753
200.296
1,301.049
Timbre Fiscal
1.000
0.000
1.000
Montant HT
Montant TVA
Montant TTC
Total général
TND
1,101.753
200.296
1,302.049
mille trois cent deux tnd et quarante-neuf millimes"""

PDF_RECHARGE = """Numéro Client
2044
Numéro Facture
PM26/822108
Date du Document
16/09/2026
Opérations de crédit
14/09/2026 09:02
Recharge carte
300.000
0.000
300.000"""


class TestLecturePdfTotal(unittest.TestCase):

    def test_facture_mensuelle(self):
        inv = T.parse_invoice_pdf_text(PDF_MENSUELLE)
        self.assertEqual((inv.invoice_no, inv.invoice_date, inv.period), ("FP26/716122", date(2026, 9, 30), "2026-09"))
        self.assertEqual((inv.total_ht, inv.total_tva, inv.total_ttc), (1101.753, 200.296, 1302.049))   # timbre compris
        self.assertTrue(inv.balanced)
        self.assertEqual(inv.client_no, "2044")

    def test_recharge_et_inconnu_ignores(self):
        self.assertIsNone(T.parse_invoice_pdf_text(PDF_RECHARGE))
        self.assertIsNone(T.parse_invoice_pdf_text("Bon de chargement"))
        self.assertIsNone(T.parse_invoice_pdf_text(PDF_MENSUELLE.replace("Total général", "Total")))


class TestNumeros(unittest.TestCase):

    def test_numero_e_inv_du_sujet(self):
        self.assertEqual(O._numero_e_inv("ARAMEX ACCOUNT NO: 60528998 E-INV NO: 1900541394"), "1900541394")
        self.assertEqual(O._numero_e_inv("Account Statement for Customer 0060528998"), "")

    def test_motifs_de_facture(self):
        self.assertTrue(O._E_INV_ARAMEX.search("Facture Aramex 1900536117 (2026-08)"))
        self.assertFalse(O._E_INV_ARAMEX.search("Dépense caisse — Frais d'expédition Aramex — BL n°51080182775"))
        self.assertTrue(O._FACTURE_TOTAL.search("Facture Total FP261248663 (2026-08)"))
        self.assertTrue(O._FACTURE_TOTAL.search("Facture Total FP26/716122 (2026-09)"))


def _ligne(nom, cheque, remarque, jour):
    return SimpleNamespace(voucher_no=nom, cheque_no=cheque, user_remark=remarque, posting_date=jour)


class TestDejaComptabilise(unittest.TestCase):
    """Le repli « ecriture du mois sur le compte » ne prend plus une depense de caisse ni une autre facture."""
    CAISSE = _ligne("ACC-JV-2026-00764", "Dépense caisse — Frais d'expédition Aramex — Aramex — BL n°51080182775",
                    "Frais d'expédition Aramex — Type : Dépense avec facture", date(2026, 9, 21))
    AUTRE = _ligne("ACC-JV-2026-00900", "Facture Aramex 09-2026", "Facture Aramex 1900540919 (2026-09)", date(2026, 9, 30))
    MANUELLE = _ligne("ACC-JV-2026-00482", "Fac ARAMEX au 30-09-2026", "", date(2026, 10, 8))

    def _cherche(self, lignes, **kw):
        with patch.object(O.frappe.db, "sql", return_value=lignes):
            return O._deja_comptabilise("2026-09", "Frais de Fret", "debit", marqueur="ARAMEX", **kw)

    def test_ancien_critere_se_trompait(self):
        self.assertEqual(self._cherche([self.CAISSE]), "ACC-JV-2026-00764")

    def test_la_caisse_ne_bloque_plus(self):
        self.assertIsNone(self._cherche([self.CAISSE], periode_au_libelle=True, autre_facture=O._E_INV_ARAMEX))

    def test_une_autre_facture_du_mois_ne_bloque_pas(self):
        self.assertIsNone(self._cherche([self.AUTRE], periode_au_libelle=True, autre_facture=O._E_INV_ARAMEX))

    def test_la_saisie_manuelle_du_mois_bloque_toujours(self):
        self.assertEqual(self._cherche([self.CAISSE, self.MANUELLE], periode_au_libelle=True, autre_facture=O._E_INV_ARAMEX),
                         "ACC-JV-2026-00482")


def _msg(uid, sujet, pieces):
    return {"uid": uid, "subject": sujet, "date": "Sat, 3 Oct 2026 11:21:15 +0000", "attachments": pieces}


class TestProcessAramex(unittest.TestCase):
    """Deux E-INV de septembre : deux brouillons, le second avec son numero dans la cle ; une
    facture deja citee est sautee AVANT l'extraction OpenAI."""

    def test_deux_factures_dans_le_mois(self):
        msgs = [_msg("1", "ARAMEX ACCOUNT NO: 60528998 E-INV NO: 1900540919", [("a.PDF", b"%PDF")]),
                _msg("2", "ARAMEX ACCOUNT NO: 60528998 E-INV NO: 1900541394", [("b.PDF", b"%PDF")]),
                _msg("3", "ARAMEX ACCOUNT NO: 60528998 E-INV NO: 1900536117", [("c.PDF", b"%PDF")])]
        crees = []

        def creer(data, pdf=None, insert=True, email_date=None, cheque_no=None):
            crees.append(cheque_no)
            return SimpleNamespace(name="JE-%d" % len(crees), cheque_no=cheque_no, posting_date=date(2026, 9, 30))

        deja = {"1900536117": "ACC-JV-2026-00729"}
        extraction = MagicMock(return_value={"invoice_date": "2026-09-30", "invoice_no": ""})
        with patch.object(O.mail_config, "fetch", return_value=msgs), \
             patch.object(O.mail_config, "attachment_of", side_effect=lambda cle, m: m["attachments"][0]), \
             patch.object(O.mail_config, "message_date", return_value=date(2026, 10, 3)), \
             patch.object(O, "_periode_debut_gestion", return_value="2026-07"), \
             patch.object(O, "extract_invoice", extraction), \
             patch.object(O, "_facture_deja_citee", side_effect=lambda n: deja.get(n)), \
             patch.object(O, "_deja_comptabilise", return_value=None) as legacy, \
             patch.object(O, "_exists", side_effect=lambda c: c in crees), \
             patch.object(O, "_total_de", return_value=0), \
             patch.object(O.journal, "create_aramex_journal_entry", side_effect=creer), \
             patch.object(O, "_signaler"):
            out = O.process_aramex()
        self.assertEqual(crees, ["Facture Aramex 09-2026", "Facture Aramex 09-2026 (1900541394)"])
        self.assertEqual(extraction.call_count, 2)                     # la facture d'aout n'est pas relue
        self.assertEqual([r["status"] for r in out], ["created", "created", "skipped"])
        self.assertTrue(all(c.kwargs.get("periode_au_libelle") for c in legacy.call_args_list))


class TestProcessTotal(unittest.TestCase):

    def test_pdf_mensuel_recharge_et_zip_deja_saisi(self):
        msgs = [_msg("1", "Votre Facture Cartes Prépayées TotalEnergies Marketing Tunis", [("Invoice_10022026.pdf", b"P1")]),
                _msg("2", "Votre Facture de recharge Cartes TotalEnergies Marketing Tunis", [("Invoice_09172026.pdf", b"P2")])]
        pdfs = {b"P1": T.parse_invoice_pdf_text(PDF_MENSUELLE), b"P2": None}
        crees = []

        def creer(inv, pdf=None, insert=True, email_date=None, cheque_no=None):
            crees.append((cheque_no, inv.invoice_no, pdf[0]))
            return SimpleNamespace(name="JE-1", cheque_no=cheque_no, posting_date=date(2026, 9, 30))

        with patch.object(O.mail_config, "fetch", return_value=msgs), \
             patch.object(O.mail_config, "attachment_of", return_value=None), \
             patch.object(O.mail_config, "message_date", return_value=date(2026, 10, 2)), \
             patch.object(O, "_periode_debut_gestion", return_value="2026-07"), \
             patch.object(O, "parse_invoice_pdf", side_effect=lambda b: pdfs[b]), \
             patch.object(O, "_facture_deja_citee", return_value=None), \
             patch.object(O, "_deja_comptabilise", return_value=None), \
             patch.object(O, "_exists", return_value=False), \
             patch.object(O, "_total_de", return_value=1302.049), \
             patch.object(O.journal, "create_total_journal_entry", side_effect=creer), \
             patch.object(O, "_signaler") as signaler:
            out = O.process_total()
        self.assertEqual(crees, [("Facture Total 09-2026", "FP26/716122", "Invoice_10022026.pdf")])
        self.assertEqual([r["status"] for r in out], ["created", "ignore (recharge)"])
        signaler.assert_not_called()                                   # une recharge n'est pas une anomalie


class TestSignaler(unittest.TestCase):

    def test_une_trace_par_email_et_par_semaine(self):
        cache = {}
        fake = MagicMock()
        fake.get_value.side_effect = cache.get
        fake.set_value.side_effect = lambda k, v, expires_in_sec=None: cache.__setitem__(k, v)
        with patch.object(O.frappe, "cache", return_value=fake), patch.object(O.frappe, "log_error") as log:
            for _ in range(5):
                O._signaler("Total", {"uid": "9", "subject": "Facture", "date": "x"}, "aucune piece jointe ZIP ni PDF")
        self.assertEqual(log.call_count, 1)
        self.assertLessEqual(len(log.call_args.kwargs["title"]), 140)


if __name__ == "__main__":
    unittest.main()
