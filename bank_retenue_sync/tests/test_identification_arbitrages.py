"""Deux arbitrages a la main depuis la page « Identification bancaire » (30/10/2026).

  1. La banque a debite MOINS que l'ecriture (erreur de saisie bancaire) : le manque part en
     credit fournisseur, et le mouvement est rattache a l'ecriture d'origine ET a son correctif.
     Pour que leur somme fasse le releve, le montant d'une piece rattachee est ORIENTE par le
     sens du mouvement (`classify.montant_oriente`). Cas reel : cheque 4001056, 4 841,000
     comptabilises, 841,100 debites.
  2. Un virement client que le libelle ne permet pas d'identifier (« U S D O SYMBOL » =
     American Embassy Tunis) : l'operateur designe la dette, l'encaissement est construit et
     soumis par le meme chemin que le flux automatique.

Regles pures + presence des branchements (aucune base).
"""
from __future__ import annotations

import inspect
import os
import unittest

from bank_retenue_sync.api import mouvements as API
from bank_retenue_sync.bank import classify as C, rules as R

PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "bank_retenue_sync", "page", "identification_bancaire",
                    "identification_bancaire.js")


class TestMontantOriente(unittest.TestCase):
    def test_sans_sens_la_valeur_absolue_comme_avant(self):
        self.assertEqual(C.montant_oriente(0, 4841.0), 4841.0)
        self.assertEqual(C.montant_oriente(3999.9, 0), 3999.9)

    def test_vu_d_un_debit_du_releve(self):
        """Sortie d'argent : l'ecriture d'origine (banque creditee) compte positif, le correctif
        (banque debitee) compte negatif — ensemble ils font le debit du releve."""
        self.assertEqual(C.montant_oriente(0, 4841.0, "Debit"), 4841.0)
        self.assertEqual(C.montant_oriente(3999.9, 0, "Debit"), -3999.9)
        self.assertEqual(round(4841.0 + C.montant_oriente(3999.9, 0, "Debit"), 3), 841.1)

    def test_vu_d_un_credit_du_releve(self):
        self.assertEqual(C.montant_oriente(2517.595, 0, "Credit"), 2517.595)
        self.assertEqual(C.montant_oriente(0, 10.0, "Credit"), -10.0)


class TestLienManuelAvecCorrectif(unittest.TestCase):
    def test_l_ecriture_et_son_correctif_font_le_releve(self):
        c = C.Classification(cle="x", debit=841.1, credit=0.0)
        m = {"debit": 841.1, "credit": 0.0}
        lien = {"par": "wassim", "motif": "écart bancaire reporté en crédit fournisseur",
                "pieces": [{"doctype": "Journal Entry", "name": "JE-ORIG", "docstatus": 1, "montant": 4841.0},
                           {"doctype": "Journal Entry", "name": "JE-CORR", "docstatus": 1, "montant": -3999.9}]}
        C.resoudre_lien_manuel(c, m, lien)
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)
        self.assertEqual((c.document_name, c.montant_document, c.ecart), ("JE-ORIG", 841.1, 0.0))
        self.assertIn("JE-ORIG + JE-CORR", c.raison)
        self.assertNotIn("écart de", c.raison)


class TestBranchements(unittest.TestCase):
    def setUp(self):
        with open(PAGE, encoding="utf-8") as f:
            self.js = f.read()

    def test_les_pieces_rattachees_sont_orientees_par_le_sens(self):
        src = inspect.getsource(C.liens_manuels_enrichis)
        self.assertIn('frappe.db.get_value(registry.DOCTYPE, cle, "sens")', src)
        self.assertIn('_montant_bancaire(p["doctype"], p["name"], sens)', src)

    def test_la_bascule_cree_le_correctif_et_rattache_les_deux_pieces(self):
        src = inspect.getsource(API.basculer_ecart_credit_fournisseur)
        self.assertIn('{"account": BANK_ACCOUNT, "debit": manque}', src)
        self.assertIn('"party_type": "Supplier", "party": party', src)
        self.assertIn("correctif.submit()", src)
        self.assertIn('[{"doctype": "Journal Entry", "name": je.name},', src)
        self.assertIn('{"doctype": "Journal Entry", "name": correctif.name}]', src)
        self.assertIn("C.run(persist=True)", src)

    def test_la_dette_client_passe_par_le_meme_chemin_que_le_flux_automatique(self):
        src = inspect.getsource(API.rattacher_dette_client)
        self.assertIn("allocation.allocate(credit, choisies)", src)
        self.assertIn("pending.dette_matches_schedule(", src)
        self.assertIn("builder.build_encaissement(virement_lots=[lot], insert=True)", src)
        self.assertIn("enc.submit()", src)
        self.assertIn('pending.consumed_bank_keys()["virement"]', src)

    def test_la_page_porte_les_deux_boutons(self):
        self.assertIn('data-dette-client=', self.js)
        self.assertIn('r.statut === "Orphelin" && r.sens === "Credit" && r.regle === "vir_client"', self.js)
        self.assertIn('data-credit-fournisseur=', self.js)
        self.assertIn('flt(r.ecart || 0, 3) < -ib_tolerance(r.montant)', self.js)
        self.assertIn('this._m("basculer_ecart_credit_fournisseur")', self.js)
        self.assertIn('this._m("rattacher_dette_client")', self.js)
        self.assertIn('this._m("dettes_clients_en_attente")', self.js)
        self.assertIn("function ib_tolerance(montant)", self.js)


class TestDefaire(unittest.TestCase):
    """↩ Défaire : remise en l'état d'un rattachement de virement ou d'un crédit fournisseur."""

    def test_l_echeancier_retrouve_sa_dette(self):
        rows = [{"name": "ps-1", "mode_of_payment": "Virement", "payment_amount": 2517.595,
                 "description": "Represente 100 [%] du virement recu de X dans l'encaissement payement N:ENC-30-10-2026-00002"},
                {"name": "ps-2", "mode_of_payment": "Espèces", "payment_amount": 16.0, "description": "autre"}]
        retirer, ajouter = API.echeancier_restaure(rows, "ENC-30-10-2026-00002", [{"valeur": 2517.595}], "2026-10-28")
        self.assertEqual(retirer, ["ps-1"])
        self.assertEqual(len(ajouter), 1)
        self.assertEqual((ajouter[0]["mode_of_payment"], ajouter[0]["payment_amount"], ajouter[0]["due_date"]),
                         ("Dette non payée", 2517.595, "2026-10-28"))

    def test_une_ligne_virement_d_un_autre_encaissement_reste(self):
        rows = [{"name": "ps-1", "mode_of_payment": "Virement", "payment_amount": 100.0,
                 "description": "… encaissement payement N:ENC-AUTRE"}]
        retirer, _ = API.echeancier_restaure(rows, "ENC-CELUI-CI", [{"valeur": 100.0}])
        self.assertEqual(retirer, [])

    def test_le_serveur_remet_tout_en_l_etat(self):
        src = inspect.getsource(API.defaire_rattachement)
        self.assertIn("pe.cancel()", src)
        self.assertIn("_restaurer_paiement_supprime(l.ref_paiement, redirections)", src)
        self.assertIn("echeancier_restaure(rows, enc.name, dettes, due_date=doc.date)", src)
        self.assertIn("enc.cancel()", src)
        self.assertIn('frappe.db.delete("Liste Dettes"', src)       # encaissement groupé : lot retiré
        self.assertIn('"reference_doctype": "Sales Invoice"', src)  # refus si imputé sur une facture
        self.assertIn("registry.retirer_lien(cle)", src)
        self.assertIn("PREFIXE_ECART_BANCAIRE", src)

    def test_le_correctif_porte_le_prefixe_que_defaire_reconnait(self):
        self.assertIn("PREFIXE_ECART_BANCAIRE + doc.operation", inspect.getsource(API.basculer_ecart_credit_fournisseur))

    def test_la_page_porte_le_bouton(self):
        with open(PAGE, encoding="utf-8") as f:
            js = f.read()
        self.assertIn('data-defaire=', js)
        self.assertIn('this._m("defaire_rattachement")', js)
        self.assertIn('r.document_type === "Encaissement Paiement" && r.regle === "vir_client"', js)
        self.assertIn('(r.lien_motif || "").includes("crédit fournisseur")', js)


class TestChequeManquantSurBordereau(unittest.TestCase):
    """Bordereau 09028865 du 27/10/2026 : 141 + 609 credites, encaissement de 141 seulement."""

    def _ctx(self, portefeuille):
        return C.LinkContext(portefeuille_cheques=portefeuille)

    def test_nomme_le_cheque_en_portefeuille_et_passe_a_verifier(self):
        rule = R.find_rule({"operation": "ENC CHEQ TN NUM 09028865", "credit": 609.0, "debit": 0.0})
        c = C.Classification(cle="x", credit=609.0, statut=C.STATUT_IDENTIFIE,
                             document_name="ENC-27-10-2026-00001", montant_document=141.0, ecart=609.0)
        C._expliquer_manque(c, rule, "09028865", self._ctx(
            [{"name": "ACC-PAY-2026-08039", "party": "WTS", "reference_no": "0000061-BH", "paid_amount": 609.0}]))
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)
        self.assertIn("il manque 609.0", c.raison)
        self.assertIn("WTS n° 0000061-BH (ACC-PAY-2026-08039)", c.raison)
        self.assertIn("à ajouter à l'encaissement", c.raison)

    def test_sans_cheque_en_portefeuille_on_le_dit(self):
        rule = R.find_rule({"operation": "ENC CHEQ TN NUM 09028865", "credit": 609.0, "debit": 0.0})
        c = C.Classification(cle="x", credit=609.0, statut=C.STATUT_IDENTIFIE,
                             document_name="ENC-1", montant_document=141.0, ecart=609.0)
        C._expliquer_manque(c, rule, "09028865", self._ctx([]))
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)
        self.assertIn("jamais saisi", c.raison)

    def test_un_ecart_tolere_reste_identifie(self):
        rule = R.find_rule({"operation": "ENC CHEQ TN NUM 90028879", "credit": 1660.33, "debit": 0.0})
        c = C.Classification(cle="x", credit=1660.33, statut=C.STATUT_IDENTIFIE,
                             document_name="ENC-2", montant_document=1660.32, ecart=0.01)
        C._expliquer_manque(c, rule, "90028879", self._ctx([]))
        self.assertEqual(c.statut, C.STATUT_IDENTIFIE)


class TestCompleterBordereau(unittest.TestCase):
    def test_le_bouton_et_l_api_inscrivent_le_cheque_sur_le_bordereau(self):
        with open(PAGE, encoding="utf-8") as f:
            js = f.read()
        self.assertIn('data-completer=', js)
        self.assertIn('(r.raison || "").includes("il manque")', js)
        self.assertIn('this._m("completer_bordereau")', js)
        src = inspect.getsource(API.completer_bordereau)
        self.assertIn('"statut": "Versé", "bon_remise": bon', src)
        self.assertIn("builder.build_encaissement(cheque_rows=rows, insert=True)", src)
        self.assertIn("enc.submit()", src)


class TestDoublonsEtFactures(unittest.TestCase):
    """Deux factures distinctes, meme fournisseur, meme jour, meme montant : pas un doublon."""

    def test_deux_numeros_de_facture_differents_ne_sont_pas_jumelles(self):
        from bank_retenue_sync.bank import ecarts as E
        pieces = [{"voucher_no": "JE-A", "posting_date": "2026-10-30", "sens": "Debit", "montant": 901.0,
                   "texte": "Dépense caisse — Osmoseur avec pompe — VODA MACHINES — Fact. n°127/2026"},
                  {"voucher_no": "JE-B", "posting_date": "2026-10-30", "sens": "Debit", "montant": 901.0,
                   "texte": "Dépense caisse — osmoseur domestique avec pompe — VODA MACHINES — Fact. n°125/2026"}]
        self.assertEqual(E.doublons_de_pieces(pieces), {})
        self.assertEqual(E.empreinte_piece(pieces[0]["texte"]), "127/2026")

    def test_sans_numero_le_doublon_reste_detecte(self):
        from bank_retenue_sync.bank import ecarts as E
        pieces = [{"voucher_no": "JE-A", "posting_date": "2026-04-30", "sens": "Debit", "montant": 603.0, "texte": "Recharge Carte Total"},
                  {"voucher_no": "JE-B", "posting_date": "2026-04-30", "sens": "Debit", "montant": 603.0, "texte": "Recharge Carte Total (rattrapage)"}]
        self.assertEqual(E.doublons_de_pieces(pieces), {"JE-A": ["JE-B"], "JE-B": ["JE-A"]})
