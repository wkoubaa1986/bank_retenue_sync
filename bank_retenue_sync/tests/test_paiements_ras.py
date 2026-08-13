"""Tests des garde-fous de creation d'ecriture et du rangement des PDF.

Convention de l'app : `unittest.TestCase` pur, dependances injectees par parametre, aucun acces
reseau ni base.
"""
from __future__ import annotations

import unittest
from datetime import date

import frappe

from bank_retenue_sync.tej import paiements as P
from bank_retenue_sync.tej import pdf as PDF
from bank_retenue_sync.tej import rapprochement as R


def cert(**kw):
    d = {"name": "CERT-1", "reference": "ref-1", "declarant": "STE KAIZEN WATER",
         "declarant_matricule": "1536635P", "customer": "Kaizen Water",
         "date_paiement": date(2026, 7, 15), "etat_depot": "Recue", "total_brut": 5304.5,
         "montant_retenue": 53.045, "sales_invoice": "ACC-SINV-1", "payment_entry": None,
         "anomalie": 0, "anomalie_raison": None, "hors_perimetre": 0}
    d.update(kw)
    return d


def facture(customer="Kaizen Water", reste=1000.0, docstatus=1):
    return lambda nom: frappe._dict({"customer": customer, "outstanding_amount": reste,
                                     "docstatus": docstatus, "company": "Aquaworld & Servicing"})


def sans_cause(facture, montant, jour=None):
    return ""


class TestGardeFous(unittest.TestCase):
    """Chaque refus repond a une facon precise de se tromper. Aucun n'est decoratif."""

    def verifier(self, c, ctx=None, fac=None):
        return P.verifier(c, ctx=ctx or R.Contexte(), charger_facture=fac or facture(),
                          cause=sans_cause)

    def test_cas_nominal_accepte(self):
        self.assertTrue(self.verifier(cert())["ok"])

    def test_hors_perimetre_refuse(self):
        self.assertIn("perimetre", self.verifier(cert(hors_perimetre=1))["raison"])

    def test_anomalie_refuse(self):
        r = self.verifier(cert(anomalie=1, anomalie_raison="assiette nulle"))
        self.assertIn("assiette nulle", r["raison"])

    def test_certificat_annule_refuse(self):
        self.assertIn("etat non exploitable", self.verifier(cert(etat_depot="Annule"))["raison"])

    def test_ecriture_deja_presente_refuse(self):
        """Le premier des anti-doublons : ne jamais recreer ce qui existe."""
        self.assertIn("porte deja", self.verifier(cert(payment_entry="ACC-PAY-9"))["raison"])

    def test_client_non_identifie_refuse(self):
        self.assertIn("client non identifie", self.verifier(cert(customer=None))["raison"])

    def test_facture_non_identifiee_refuse(self):
        """Sans facture, l'ecriture serait un acompte flottant qui fausse l'age des creances."""
        self.assertIn("facture non identifiee", self.verifier(cert(sales_invoice=None))["raison"])

    def test_facture_d_un_autre_client_refuse(self):
        r = self.verifier(cert(), fac=facture(customer="SOCOBAT"))
        self.assertIn("appartient a SOCOBAT", r["raison"])

    def test_facture_non_validee_refuse(self):
        self.assertIn("non validee", self.verifier(cert(), fac=facture(docstatus=0))["raison"])

    def test_facture_deja_soldee_refuse(self):
        """Cas le plus frequent en reel : le reglement a ete encaisse pour le TTC entier alors que
        le client avait retenu 1 %. Imputer en plus creerait un avoir fantome."""
        self.assertIn("deja soldee", self.verifier(cert(), fac=facture(reste=0.0))["raison"])

    def test_reste_du_inferieur_a_la_retenue_refuse(self):
        self.assertIn("deja soldee", self.verifier(cert(), fac=facture(reste=10.0))["raison"])

    def test_ecriture_semblable_dans_la_fenetre_refuse(self):
        """Anti-doublon sur le montant : la meme retenue a pu etre saisie sur une commande."""
        ctx = R.Contexte(pes_ras={"Kaizen Water": [{"name": "ACC-PAY-7",
                                                    "posting_date": date(2026, 7, 1),
                                                    "paid_amount": 53.045}]})
        r = self.verifier(cert(), ctx=ctx)
        self.assertIn("ACC-PAY-7", r["raison"])

    def test_une_ecriture_eloignee_ne_bloque_pas(self):
        ctx = R.Contexte(pes_ras={"Kaizen Water": [{"name": "ACC-PAY-7",
                                                    "posting_date": date(2026, 1, 1),
                                                    "paid_amount": 53.045}]})
        self.assertTrue(self.verifier(cert(), ctx=ctx)["ok"])

    def test_montant_nul_refuse(self):
        self.assertIn("montant nul", self.verifier(cert(montant_retenue=0))["raison"])

    def test_une_retenue_deja_imputee_a_la_facture_refuse(self):
        """🐛 LE SEUL CHEMIN DE L'APP QUI POUVAIT COMPTABILISER DEUX FOIS LA MEME RETENUE.

        Cas reel CLIMA FILTRI PRO : ecriture de retenue du 25/02 imputee a la facture, certificat
        declare le 03/06 — 98 jours plus tard, trois fois la fenetre d'appariement. Le certificat
        ressortait « sans ecriture » et la regularisation proposait d'en creer une seconde.
        Ce n'est pas une regularisation a faire, c'est un rapprochement a faire.
        """
        ctx = R.Contexte(pes_ras={"Kaizen Water": [{"name": "ACC-PAY-8",
                                                    "posting_date": date(2026, 2, 25),
                                                    "paid_amount": 53.045}]},
                         refs={"ACC-PAY-8": [{"doctype": "Sales Invoice", "name": "ACC-SINV-1",
                                              "montant": 53.045}]})
        r = self.verifier(cert(), ctx=ctx)
        self.assertFalse(r["ok"])
        self.assertIn("porte deja une retenue", r["raison"])
        self.assertIn("ACC-PAY-8", r["raison"])

    def test_une_retenue_imputee_a_une_AUTRE_facture_ne_bloque_pas(self):
        """Le garde-fou porte sur la creance, pas sur le client : un meme client peut avoir une
        retenue sur une facture et pas sur l'autre."""
        ctx = R.Contexte(pes_ras={"Kaizen Water": [{"name": "ACC-PAY-8",
                                                    "posting_date": date(2026, 2, 25),
                                                    "paid_amount": 53.045}]},
                         refs={"ACC-PAY-8": [{"doctype": "Sales Invoice", "name": "ACC-SINV-9",
                                              "montant": 53.045}]})
        self.assertTrue(self.verifier(cert(), ctx=ctx)["ok"])


class TestChoixDuReglementAReprendre(unittest.TestCase):
    """Quand la facture est soldee, la retenue se loge en reprenant le reglement a la baisse.

    Cas reel : facture CHAUF NORD de 1 155 DT encaissee par un cheque de 1 155 DT, alors que le
    client avait retenu 11,540 DT et l'avait declare au portail. Le cheque reel valait 1 143,46.
    Le total impute a la facture ne bouge pas ; c'est sa composition qui devient juste.
    """

    def reglements(self, *montants, jour=date(2026, 3, 11)):
        return [{"name": "ACC-PAY-%s" % i, "posting_date": jour,
                 "mode_of_payment": "Chèque", "paid_amount": m, "docstatus": 1,
                 "allocated_amount": m, "idx": i + 1} for i, m in enumerate(montants)]

    def test_le_reglement_qui_couvre_la_retenue_est_choisi(self):
        r = P.reglement_a_reprendre(cert(), self.reglements(1155.0))
        self.assertTrue(r["ok"])
        self.assertEqual(r["reglement"]["name"], "ACC-PAY-0")

    def test_le_plus_gros_reglement_n_est_PLUS_prefere(self):
        """⚠️ REGLE RETIREE APRES MESURE SUR LES DONNEES REELLES.

        « Le plus gros reglement qui couvre la retenue » est evident sur une facture reglee en une
        fois — et faux partout ailleurs. SOCIETE FM WATER PLUS solde ses factures par 19
        encaissements en especes de 5 a 1 540 DT : « le plus gros » designait un encaissement sans
        aucun rapport avec la retenue, et le reduire aurait fait disparaitre de l'argent
        reellement recu. En l'absence de regle qui DESIGNE le reglement, on demande.
        """
        r = P.reglement_a_reprendre(cert(), self.reglements(60.0, 1155.0))
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["candidats"]), 2)

    def test_un_reglement_trop_petit_ne_convient_pas(self):
        r = P.reglement_a_reprendre(cert(montant_retenue=53.045), self.reglements(20.0))
        self.assertFalse(r["ok"])
        self.assertIn("couvre", r["raison"])

    def test_un_reglement_egal_a_la_retenue_ne_convient_pas_non_plus(self):
        """Il tomberait a zero : un reglement de 0 DT n'est pas un reglement."""
        self.assertFalse(P.reglement_a_reprendre(cert(), self.reglements(53.045))["ok"])

    def test_le_reglement_du_ttc_entier_est_designe(self):
        """LE CAS NOMINAL : le client a paye net, nous avons enregistre le brut. Le reglement qui
        porte le TTC entier est celui qui contient la part jamais encaissee."""
        r = P.reglement_a_reprendre(cert(), self.reglements(300.0, 1155.0), ttc=1155.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["reglement"]["paid_amount"], 1155.0)
        self.assertIn("TTC entier", r["regle"])

    def test_le_reglement_du_jour_declare_au_portail_est_designe(self):
        """Le portail date le jour ou le client a paye net : cette date-la ne ment pas.
        Cas reel STE LOTUS CAKE DECOR — 250 DT le 06/05 puis 760 DT le 22/05, certificat du 22/05.
        """
        lignes = (self.reglements(250.0, jour=date(2026, 5, 6))
                  + [{"name": "ACC-PAY-9", "posting_date": date(2026, 5, 22),
                      "mode_of_payment": "Espèces", "paid_amount": 760.0, "docstatus": 1,
                      "allocated_amount": 760.0, "idx": 2}])
        r = P.reglement_a_reprendre(cert(montant_retenue=10.099, date_paiement=date(2026, 5, 22)),
                                    lignes)
        self.assertTrue(r["ok"])
        self.assertEqual(r["reglement"]["name"], "ACC-PAY-9")

    def test_deux_reglements_identiques_ne_se_departagent_pas(self):
        """Cas reel ABC MED : trois reglements du meme montant. Choisir a la place de l'humain,
        c'est corriger la mauvaise piece — et une piece corrigee a tort se rattrape a la main."""
        r = P.reglement_a_reprendre(cert(), self.reglements(1155.0, 1155.0, 1155.0))
        self.assertFalse(r["ok"])
        self.assertIn("lequel corriger", r["raison"])
        self.assertEqual(len(r["candidats"]), 3)

    def test_le_choix_explicite_de_l_utilisateur_prime(self):
        """C'est la sortie du refus precedent : la machine ne tranche pas, l'utilisateur si."""
        r = P.reglement_a_reprendre(cert(), self.reglements(1155.0, 1155.0), choisi="ACC-PAY-1")
        self.assertTrue(r["ok"])
        self.assertEqual(r["reglement"]["name"], "ACC-PAY-1")

    def test_un_choix_explicite_intenable_est_refuse(self):
        """Le choix humain ne dispense pas du controle : un reglement de 20 DT ne peut pas porter
        une retenue de 53."""
        r = P.reglement_a_reprendre(cert(), self.reglements(20.0, 1155.0), choisi="ACC-PAY-0")
        self.assertFalse(r["ok"])
        self.assertIn("ne peut pas porter", r["raison"])

    def test_aucun_reglement_du_tout(self):
        self.assertFalse(P.reglement_a_reprendre(cert(), [])["ok"])


class TestRangementDuPdf(unittest.TestCase):
    """« La facture si elle existe, la commande sinon » — regle demandee, car la comptabilite
    travaille sur les factures."""

    def test_la_facture_prime(self):
        self.assertEqual(PDF.cible({"sales_invoice": "SI-1", "sales_order": "SO-1",
                                    "payment_entry": "PE-1"}), ("Sales Invoice", "SI-1"))

    def test_la_commande_a_defaut(self):
        self.assertEqual(PDF.cible({"sales_invoice": None, "sales_order": "SO-1",
                                    "payment_entry": "PE-1"}), ("Sales Order", "SO-1"))

    def test_l_ecriture_en_dernier_recours(self):
        """La page compte aussi les justificatifs portes par l'ecriture de paiement."""
        self.assertEqual(PDF.cible({"sales_invoice": None, "sales_order": None,
                                    "payment_entry": "PE-1"}), ("Payment Entry", "PE-1"))

    def test_rien_a_ranger(self):
        self.assertEqual(PDF.cible({}), (None, None))

    def test_un_certificat_pose_a_la_main_vaut_justificatif(self):
        """Quand le portail refuse son PDF (il en detient deux et sa route ne sait pas choisir), le
        certificat depose a la main sur la facture est la seule preuve — et elle compte. La regle du
        nom est celle des ecritures orphelines : un seul endroit decide."""
        from bank_retenue_sync.tej import orphelines as O

        self.assertTrue(O.nomme_un_certificat("Retenue a la source Kaizen 07-2026.pdf"))
        self.assertFalse(O.nomme_un_certificat("bon de livraison.pdf"))

    def test_deux_exemplaires_du_meme_certificat_se_reconnaissent_par_le_texte(self):
        """⚠️ COMPARER LES OCTETS NE SERT A RIEN : le portail regenere le certificat a chaque
        demande et y inscrit la date, ce qui change une centaine d'octets de metadonnees — 43 606
        contre 43 722 sur le couple R.N.T.A. Le texte, lui, est identique au caractere pres."""
        a = "REPUBLIQUE TUNISIENNE\nCERTIFICAT DE RETENUE\nMontant 53,045"
        b = "REPUBLIQUE TUNISIENNE   CERTIFICAT DE RETENUE\n\nMontant 53,045\n"
        self.assertTrue(PDF.textes_concordent(a, b))

    def test_un_document_illisible_ne_prouve_rien(self):
        """Fichier absent du disque ou scan sans couche texte : on ne supprime pas ce qu'on n'a pas
        pu lire. Un justificatif detruit ne se recupere pas."""
        self.assertFalse(PDF.textes_concordent(None, "texte"))
        self.assertFalse(PDF.textes_concordent("", "texte"))

    def test_deux_certificats_differents_ne_concordent_pas(self):
        self.assertFalse(PDF.textes_concordent("Certificat 53,045", "Certificat 21,610"))

    def test_un_pdf_deja_range_n_est_pas_redemande(self):
        """Chaque PDF coute une session de scraping au portail : le redemander pour un certificat
        deja justifie serait payer deux fois pour le meme document."""
        cert = {"name": "C-1", "reference": "r-1", "pdf_attached_to_pe": 1, "anomalie": 0,
                "hors_perimetre": 0, "sales_invoice": "SI-1"}
        original, PDF.frappe.db.get_value = PDF.frappe.db.get_value, lambda *a, **k: frappe._dict(cert)
        try:
            self.assertEqual(PDF.traiter_un("C-1")["statut"], "deja attache")
        finally:
            PDF.frappe.db.get_value = original


    def test_le_nom_de_fichier_est_stable(self):
        """C'est lui qui rend l'attachement idempotent : un nom variable creerait un doublon a
        chaque passage."""
        self.assertEqual(PDF.nom_fichier("abc-123"), "certificat_ras_abc-123.pdf")
        self.assertEqual(PDF.nom_fichier("abc-123"), PDF.nom_fichier("abc-123"))

    def test_le_fichier_est_reconnu_meme_renomme_par_frappe(self):
        """⚠️ Constate en reel : `save_file` a rendu « certificat_ras_<ref>101352.pdf » parce qu'un
        fichier de ce nom existait deja sur le disque. Cherche a l'egalite stricte, le justificatif
        de la veille reste invisible et le certificat repart en telechargement — deux exemplaires du
        meme document sur la meme facture."""
        motif = PDF.motif_fichier("abc-123")
        self.assertEqual(motif, "certificat_ras_abc-123%")
        self.assertTrue(PDF.nom_fichier("abc-123").startswith(motif[:-1]))
        self.assertTrue("certificat_ras_abc-123101352.pdf".startswith(motif[:-1]))


class TestArgentCompteOuDette(unittest.TestCase):
    """⚠️ LA REGLE QUI EMPECHE DE FAIRE DISPARAITRE DE L'ARGENT RECU.

    Une ligne « dette » n'est l'image d'aucune caisse : son montant a ete deduit de la facture, il
    peut etre faux, donc corrigible. Des especes ou un cheque ont ete COMPTES par quelqu'un : les
    reduire de la retenue effacerait des comptes de l'argent physiquement recu.
    """

    def test_la_dette_seule_peut_etre_reduite(self):
        self.assertFalse(P.argent_compte("Dette non payée", "Dette non payée"))

    def test_especes_cheque_virement_sont_de_l_argent_compte(self):
        for mode in ("Espèces", "Chèque", "Virement", "Traite bancaire LC", "Carte de crédit"):
            self.assertTrue(P.argent_compte(mode, "Dette non payée"), mode)

    def test_un_mode_absent_reste_de_l_argent_compte(self):
        """Le doute profite a l'encaissement : sans mode connu, on ne touche pas au montant."""
        self.assertTrue(P.argent_compte(None, "Dette non payée"))


class TestRepartitionDeLaPartLiberee(unittest.TestCase):
    """La part que la retenue libere sur un reglement en argent compte va eteindre les ecritures
    « Dette non payee » du client — la plus ancienne d'abord.

    ⚠️ « DETTE » = ECRITURE DE MODE « Dette non payee », PAS « facture au reste du non nul ».
    Premiere version : la part partait sur les factures et commandes ouvertes. En reel, sur
    FM WATER PLUS, elle a atterri en avance sur une commande a facturer pendant que la seule vraie
    dette du client restait intacte.
    """

    def dettes(self):
        return [{"dette_pe": "PAY-D1", "doctype": "Sales Order", "name": "SO-VIEUX", "reste": 4.0},
                {"dette_pe": "PAY-D2", "doctype": "Sales Invoice", "name": "SINV-2", "reste": 500.0}]

    def test_la_plus_ancienne_d_abord_puis_le_reste(self):
        aff, reste = P.repartir(10.099, self.dettes())
        self.assertEqual([(a["name"], a["montant"]) for a in aff],
                         [("SO-VIEUX", 4.0), ("SINV-2", 6.099)])
        self.assertEqual(reste, 0.0)

    def test_chaque_part_dit_quelle_dette_elle_ramene_et_a_combien(self):
        """C'est ce que l'utilisateur verifie : 851,20 de dette doivent devenir 837,389."""
        aff, _ = P.repartir(13.811, [{"dette_pe": "PAY-D", "doctype": "Sales Order",
                                      "name": "SAL-ORD-2026-02742", "reste": 851.2}])
        self.assertEqual(aff[0]["dette_pe"], "PAY-D")
        self.assertEqual(aff[0]["dette_reste"], 837.389)

    def test_une_dette_n_absorbe_jamais_plus_qu_elle_ne_porte(self):
        aff, reste = P.repartir(10000.0, self.dettes())
        self.assertEqual(sum(a["montant"] for a in aff), 504.0)
        self.assertEqual(reste, 9496.0)

    def test_sans_ecriture_de_dette_la_part_reste_en_avance(self):
        """Le client n'a aucune dette reconnue : l'argent demeure a son credit, il n'est pas perdu
        — et surtout il ne part pas en avance sur une commande qui ne doit rien."""
        aff, reste = P.repartir(10.099, [])
        self.assertEqual(aff, [])
        self.assertEqual(reste, 10.099)


class TestSuppressionDuReglementAnnule(unittest.TestCase):
    """Reprendre un reglement, c'est l'annuler puis le refaire. Ce qu'on fait de l'original ensuite
    n'est pas une question de menage : c'est la piece qui atteste l'argent recu."""

    def test_on_supprime_quand_le_remplacant_est_valide(self):
        self.assertTrue(P.suppression_permise(valide=True, demandee=True))

    def test_jamais_de_suppression_tant_que_la_copie_est_en_brouillon(self):
        """⚠️ LA REGLE QUI PROTEGE L'ENCAISSEMENT. Sans elle, la facture redeviendrait due et plus
        aucune piece n'expliquerait pourquoi."""
        self.assertFalse(P.suppression_permise(valide=False, demandee=True))

    def test_le_reglage_decoche_conserve_l_original(self):
        self.assertFalse(P.suppression_permise(valide=True, demandee=False))


if __name__ == "__main__":
    unittest.main()
