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


class TestChequeAnnuleParLaBanque(unittest.TestCase):
    """La banque corrige un cheque mal debite en ANNULANT son debit puis en redebitant (18/11/2026).

    Cas reel, cheque 4001056 (MAYAR DE COMMERCE) emis pour 4 841,100 :
        28/10  REGLEMENT CHEQUE 4001056                 debit   841,100   (erreur de la banque)
        18/11  CREDIT DIVERS REGLEMENT CHEQUE 4001056   credit  841,100   (elle reprend son debit)
        18/11  DEBIT DIVERS REGLEMENT CHEQUE 4001056    debit 4 841,100   (le vrai montant)
    Avant : le credit ressortait « libelle inconnu », et rien ne disait que le report en credit
    fournisseur pose le 30/10 n'avait plus lieu d'etre."""

    from datetime import date as _date

    ERRONE = {"date": _date(2026, 10, 28), "operation": "REGLEMENT CHEQUE 4001056",
              "reference": "FT26301G4X18", "debit": 841.1, "credit": 0.0}
    REPRISE = {"date": _date(2026, 11, 18), "operation": "CREDIT DIVERS REGLEMENT CHEQUE 4001056",
               "reference": "DC263226034402001", "debit": 0.0, "credit": 841.1}
    REDEBIT = {"date": _date(2026, 11, 18), "operation": "DEBIT DIVERS REGLEMENT CHEQUE 4001056",
               "reference": "DC263226034402003", "debit": 4841.1, "credit": 0.0}
    PAIEMENT = {"name": "ACC-PAY-2026-08192", "posting_date": _date(2026, 10, 30), "paid_amount": 4841.0,
                "party": "MAYAR DE COMMERCE", "reference_no": "4001056", "sens": "debit"}

    def lignes(self):
        return [dict(self.ERRONE), dict(self.REPRISE), dict(self.REDEBIT)]

    def classer(self, movements=None, **ctx):
        movements = movements if movements is not None else self.lignes()
        cs = C.classify(movements, C.LinkContext(pe_bancaires=[dict(self.PAIEMENT)], **ctx))
        return {c.operation: c for c in cs}

    # ---- la regle
    def test_le_credit_est_reconnu_et_le_debit_reste_un_reglement_de_cheque(self):
        self.assertEqual(R.find_rule(self.REPRISE).key, R.REGLE_ANNULATION_CHEQUE)
        self.assertEqual(R.find_rule(self.REDEBIT).key, "reglement_cheque")
        self.assertEqual(R.extract_numero(R.find_rule(self.REPRISE), self.REPRISE), "4001056")

    def test_une_remise_de_cheques_n_est_pas_une_annulation(self):
        m = {"operation": "CREDIT DIVERS ENC CHEQ TN NUM 00358832", "debit": 0.0, "credit": 3800.982}
        self.assertEqual(R.find_rule(m).key, "enc_cheque")

    # ---- la paire
    def test_la_paire_se_forme_sur_le_numero_et_le_montant(self):
        ann = C.apparier_annulations(self.lignes())
        self.assertEqual(len(ann), 2)
        errone, reprise = ann[registry_key(self.ERRONE)], ann[registry_key(self.REPRISE)]
        self.assertEqual((errone["role"], errone["date"]), ("annule", self.REPRISE["date"]))
        self.assertEqual((reprise["role"], reprise["date"]), ("annulation", self.ERRONE["date"]))
        self.assertEqual(errone["redebit"], {"date": self.REDEBIT["date"], "montant": 4841.1})
        self.assertNotIn(registry_key(self.REDEBIT), ann)

    def test_pas_de_paire_si_le_montant_differe(self):
        m = self.lignes()
        m[1]["credit"] = 841.0
        self.assertEqual(C.apparier_annulations(m), {})

    def test_pas_de_paire_si_le_numero_differe(self):
        m = self.lignes()
        m[1]["operation"] = "CREDIT DIVERS REGLEMENT CHEQUE 4001057"
        self.assertEqual(C.apparier_annulations(m), {})

    def test_un_credit_ne_peut_pas_annuler_un_debit_posterieur(self):
        m = [dict(self.REPRISE), dict(self.ERRONE, date=self._date(2026, 11, 20))]
        self.assertEqual(C.apparier_annulations(m), {})

    def test_un_debit_n_est_annule_qu_une_fois(self):
        m = self.lignes() + [dict(self.REPRISE, reference="DC-BIS", date=self._date(2026, 11, 19))]
        ann = C.apparier_annulations(m)
        self.assertEqual(len(ann), 2)
        self.assertNotIn(registry_key(m[3]), ann)

    def test_a_montants_egaux_le_plus_ancien_est_annule(self):
        """Annulation puis nouveau debit du MEME montant : c'est l'ancien qui est repris."""
        redebit = dict(self.REDEBIT, debit=841.1)
        ann = C.apparier_annulations([dict(self.ERRONE), dict(self.REPRISE), redebit])
        self.assertEqual(ann[registry_key(self.ERRONE)]["role"], "annule")
        self.assertNotIn(registry_key(redebit), ann)

    # ---- la classification
    def test_les_deux_lignes_se_compensent_et_le_paiement_va_au_nouveau_debit(self):
        cs = self.classer()
        errone, reprise, redebit = (cs[m["operation"]] for m in self.lignes())
        for c in (errone, reprise):
            self.assertEqual((c.statut, c.document_name, c.ecart), (C.STATUT_IDENTIFIE, None, 0.0))
            self.assertIn("rien à comptabiliser", c.raison)
        self.assertIn("débit annulé par la banque le 18/11/2026 (crédit de 841,100 sur le chèque 4001056)",
                      errone.raison)
        self.assertIn("la banque annule son débit du 28/10/2026", reprise.raison)
        self.assertIn("redébité 4 841,100 le 18/11/2026", reprise.raison)
        self.assertEqual((redebit.statut, redebit.document_type, redebit.document_name),
                         (C.STATUT_IDENTIFIE, "Payment Entry", "ACC-PAY-2026-08192"))
        self.assertEqual((redebit.montant_document, redebit.ecart), (4841.0, 0.1))

    def test_le_debit_annule_ne_prend_plus_le_paiement_du_cheque(self):
        """Sans la paire, le debit errone s'accrochait au paiement par le n° de cheque."""
        seul = self.classer([dict(self.ERRONE)])[self.ERRONE["operation"]]
        self.assertEqual(seul.document_name, "ACC-PAY-2026-08192")
        self.assertIsNone(self.classer()[self.ERRONE["operation"]].document_name)

    def test_le_report_en_credit_fournisseur_est_a_defaire(self):
        lien = {"par": "wassim", "motif": "écart bancaire de 3999.9 reporté en crédit fournisseur MAYAR (JE-CORR)",
                "pieces": [{"doctype": "Journal Entry", "name": "JE-ORIG", "docstatus": None, "montant": None},
                           {"doctype": "Journal Entry", "name": "JE-CORR", "docstatus": 1, "montant": -3999.9}]}
        c = self.classer(liens_manuels={registry_key(self.ERRONE): lien})[self.ERRONE["operation"]]
        self.assertEqual((c.statut, c.ecart, c.montant_document), (C.STATUT_A_VERIFIER, 0.0, 0.0))
        self.assertTrue(c.raison.startswith(
            "la banque a corrigé : ce débit de 841,100 a été annulé le 18/11/2026 et le chèque 4001056 "
            "redébité 4 841,100"))
        self.assertIn("le report en crédit fournisseur (JE-CORR) n'a plus lieu d'être — « ↩ Défaire »", c.raison)
        self.assertNotIn("JE-ORIG", c.raison)

    def test_un_autre_rattachement_manuel_est_a_detacher(self):
        lien = {"par": "wassim", "motif": "même fournisseur",
                "pieces": [{"doctype": "Payment Entry", "name": "PE-X", "docstatus": 1, "montant": 841.1}]}
        c = self.classer(liens_manuels={registry_key(self.ERRONE): lien})[self.ERRONE["operation"]]
        self.assertEqual(c.statut, C.STATUT_A_VERIFIER)
        self.assertIn("« Détacher » le retire", c.raison)

    def test_un_rattachement_manuel_sans_annulation_n_est_pas_touche(self):
        lien = {"par": "wassim", "motif": "écart bancaire reporté en crédit fournisseur",
                "pieces": [{"doctype": "Journal Entry", "name": "JE-ORIG", "docstatus": 1, "montant": 4841.0},
                           {"doctype": "Journal Entry", "name": "JE-CORR", "docstatus": 1, "montant": -3999.9}]}
        c = self.classer([dict(self.ERRONE)],
                         liens_manuels={registry_key(self.ERRONE): lien})[self.ERRONE["operation"]]
        self.assertEqual((c.statut, c.ecart), (C.STATUT_IDENTIFIE, 0.0))

    def test_une_annulation_sans_debit_reste_a_verifier_et_ne_prend_aucune_piece(self):
        """Un encaissement client du meme montant ne doit pas s'y accrocher."""
        recu = {"name": "PE-CLIENT", "posting_date": self._date(2026, 11, 18), "paid_amount": 841.1,
                "party": "CLIENT", "reference_no": "X", "sens": "credit"}
        cs = C.classify([dict(self.REPRISE)], C.LinkContext(pe_bancaires=[recu]))
        self.assertEqual((cs[0].statut, cs[0].document_name), (C.STATUT_A_VERIFIER, None))
        self.assertIn("aucun débit de 841,100 ne figure au relevé", cs[0].raison)

    # ---- les branchements
    def test_la_page_propose_de_defaire_avec_le_motif_tout_pret(self):
        with open(PAGE, encoding="utf-8") as f:
            js = f.read()
        self.assertIn('(r.raison || "").startsWith("la banque a corrigé")', js)
        self.assertIn("default: banque_a_corrige ?", js)

    def test_une_fenetre_de_dates_cherche_la_paire_dans_tout_le_registre(self):
        src = inspect.getsource(C.run)
        self.assertIn("context.annulations = apparier_annulations(registry.registry_as_movements())", src)


def registry_key(m):
    from bank_retenue_sync.bank import registry
    return registry.movement_key(m)
