"""Tests de la retenue à la source ACHAT sur les dépenses de caisse facturées.

Convention : `unittest.TestCase` pur. Le modèle est ACC-JV-2026-00698 (04/09/2026) :
1 310,000 TTC — 1 101,000 HT + 209,000 TVA — retenue 13,100, banque 1 296,900.
"""
from __future__ import annotations

import unittest

from bank_retenue_sync.achat import retenue_depense as D
from bank_retenue_sync.tej import emis_journal as F


class TestLaRegle(unittest.TestCase):

    def test_l_exemple_de_reference_tombe_juste(self):
        self.assertEqual(D.retenue_due(1310.0), 13.1)
        self.assertEqual(D.net_a_payer(1310.0, 13.1), 1296.9)

    def test_le_seuil_est_inclusif(self):
        self.assertEqual(D.retenue_due(999.0), 0.0)
        self.assertEqual(D.retenue_due(1000.0), 10.0)

    def test_le_MONTANT_SEUL_ne_declenche_pas(self):
        """⚠️ MESURÉ LE 04/09/2026 : sur les quatre écritures de plus de 1 000 DT passées en
        caisse depuis le 01/09, TROIS sont des primes de salariés (1 500, 2 200, 3 000 DT),
        toutes de type « Dépense non facturée ». Y retenir 1 % serait faux — une prime relève de
        l'IRPP et de la CNSS. Le type tranche autant que le montant."""
        self.assertEqual(D.retenue_due(3000.0, 0, "Dépense non facturée"), 0.0)
        self.assertEqual(D.retenue_due(2500.0, 0, "Facture d'achat"), 25.0)
        self.assertEqual(D.retenue_due(1310.0, 0, "Dépense avec facture"), 13.1)

    def test_l_assiette_exclut_le_timbre(self):
        """Même règle que sur les factures d'achat, vérifiée au millime sur 17 factures
        locales de 2026."""
        self.assertEqual(D.retenue_due(1310.0, 1.0), 13.09)

    def test_le_timbre_peut_faire_passer_sous_le_seuil(self):
        self.assertEqual(D.retenue_due(1000.5, 1.0), 0.0)

    def test_les_types_assujettis_sont_ceux_qui_ont_un_fournisseur(self):
        self.assertEqual(D.TYPES_ASSUJETTIS, ("Dépense avec facture", "Facture d'achat"))

    def test_le_seuil_et_le_taux_viennent_des_reglages_des_factures(self):
        """On n'invente pas une seconde règle : c'est la même que pour les factures d'achat."""
        import inspect

        src = inspect.getsource(D)
        self.assertIn("R._seuil()", src)
        self.assertIn("R._taux()", src)


class TestLectureDeLaPiece(unittest.TestCase):
    """La remarque est la SEULE mémoire de la retenue sur l'écriture — pas de champ dédié, donc
    pas de champ à migrer et pas de risque que le justificatif et sa trace divergent."""

    def test_elle_relit_ce_qu_elle_a_ecrit(self):
        lu = F.lire_piece("Achat X\nRetenue à la source achat : 13.1 sur 1310.0\n"
                          "Fournisseur : MS TECHAUTOMATION sarl\nFacture n° FA9260543")
        self.assertEqual(lu["retenue"], 13.1)
        self.assertEqual(lu["ttc"], 1310.0)
        self.assertEqual(lu["fournisseur"], "MS TECHAUTOMATION sarl")
        self.assertEqual(lu["numero_facture"], "FA9260543")

    def test_une_remarque_sans_retenue_ne_leve_pas(self):
        lu = F.lire_piece("Prime 3ème Trimestre · Type : Dépense non facturée")
        self.assertEqual((lu["retenue"], lu["ttc"]), (0.0, 0.0))

    def test_un_montant_a_espaces_est_lu(self):
        """Les montants s'écrivent « 1 310,000 » dans les remarques françaises."""
        self.assertEqual(F.lire_piece("Retenue à la source achat : 13,100 sur 1 310,000")["ttc"],
                         1310.0)


class TestPerimetre(unittest.TestCase):
    """Ce qui n'a jamais de certificat à émettre."""

    def test_les_flux_automatiques_sont_exclus(self):
        """Aramex et Total n'entrent pas par la caisse et leurs fournisseurs ne sont pas
        locaux (décision utilisateur 04/09/2026)."""
        for libelle in ("Facture Total 08-2026", "Facture Aramex 09-2026",
                        "Frais bancaire 09-2026"):
            self.assertTrue(F.exclue(libelle), libelle)

    def test_une_depense_de_caisse_ne_l_est_pas(self):
        self.assertFalse(F.exclue("Dépense caisse — Fac 9260543 - Achat Compteuse"))

    def test_une_depense_A_PAYER_est_exclue(self):
        """Elle n'a encore rien réglé : sa retenue naîtra avec son règlement."""
        self.assertTrue(F.exclue("Dépense à payer — Achat X"))

    def test_rien_avant_le_premier_septembre(self):
        """Les écritures antérieures n'ont pas été saisies sous cette règle : les rattraper
        produirait des déclarations que personne n'a préparées."""
        self.assertEqual(F.DEPUIS, "2026-09-01")


class TestAdaptateurDEmission(unittest.TestCase):
    """L'émission depuis une ÉCRITURE, sans dupliquer `tej/emis`.

    ⚠️ C'EST UN ADAPTATEUR, PAS UNE SECONDE IMPLÉMENTATION. `tej/emis` porte les quatre barrières
    anti-doublon, la clé d'idempotence, le contrôle du montant calculé par le portail et le PDF
    attaché. Une seconde émission aurait divergé de celle-ci au premier changement du portail.
    """

    def source(self, fn):
        import inspect

        return inspect.getsource(fn)

    def test_le_contexte_rend_les_memes_cles_que_pour_une_facture(self):
        from bank_retenue_sync.tej import emis as E

        attendues = set(E.contexte.__doc__ and [] or [])  # documentation seulement
        src = self.source(F.contexte)
        for cle in ("facture", "matricule", "bill_no", "date_paiement", "montant_ht",
                    "taux_tva", "exercice", "manques"):
            self.assertIn('"%s"' % cle, src, cle)

    def test_l_emission_partagee_accepte_un_contexte(self):
        """Sans ce paramètre, il aurait fallu réécrire `emettre` — et les barrières avec."""
        import inspect

        from bank_retenue_sync.tej import emis as E

        self.assertIn("ctx", inspect.signature(E.emettre).parameters)
        self.assertIn("ctx = ctx or contexte(facture)", inspect.getsource(E.emettre))

    def test_le_HT_se_deduit_du_TTC_et_de_la_TVA(self):
        """Une écriture de caisse ne porte pas de « net_total » : elle porte le TTC et la ligne
        de TVA. Sur ACC-JV-2026-00698 : 1 310,000 − 209,000 = 1 101,000."""
        self.assertIn("ttc - tva", self.source(F._ht_et_taux))

    def test_deux_taux_de_TVA_rendent_le_taux_indeterminable(self):
        """TEJ n'accepte qu'un taux par opération : mieux vaut refuser que déclarer au hasard."""
        src = self.source(F._ht_et_taux)
        self.assertIn("taux = t if taux in (None, t) else -1", src)
        self.assertIn("return flt(ttc - tva, 3), None", src)

    def test_rien_ne_part_sans_dry_run_explicite(self):
        import inspect

        self.assertIs(inspect.signature(F.emettre).parameters["dry_run"].default, True)

    def test_l_etat_ne_bascule_qu_avec_une_reference(self):
        """Sans elle, rien ne prouve qu'une déclaration est partie — et marquer « Émis » ferait
        perdre la ligne de vue pour toujours."""
        self.assertIn('if not frappe.utils.cint(dry_run) and reference:',
                      self.source(F.emettre))

    def test_la_synchronisation_ne_touche_jamais_une_ligne_emise(self):
        """La référence du certificat est la preuve d'une déclaration partie."""
        self.assertIn('if doc.statut == "Émis":', self.source(F.synchroniser))


class TestBoutonSurLEcriture(unittest.TestCase):
    """Le geste depuis la fiche de l'écriture — « appuyer sur le bouton et lancer l'émission »."""

    def source(self, fn):
        import inspect

        return inspect.getsource(fn)

    def test_l_etat_se_recalcule_a_la_lecture(self):
        """⚠️ Le matricule vit sur la fiche du fournisseur et le rattachement peut avoir été
        défait ailleurs. Afficher un statut mémorisé faisait annoncer « À émettre » sur une
        ligne à laquelle il manquait son fournisseur — vu en test le 04/09/2026."""
        self.assertIn("_reetat(doc)", self.source(F.etat))

    def test_une_ecriture_non_validee_n_est_pas_concernee(self):
        """Avant validation, ni le montant ni la date de la retenue ne sont définitifs."""
        self.assertIn("je.docstatus != 1", self.source(F.etat))

    def test_les_flux_automatiques_et_l_anterieur_sont_ecartes(self):
        src = self.source(F.etat)
        self.assertIn("exclue(je.cheque_no)", src)
        self.assertIn("< DEPUIS", src)

    def test_l_ecran_ne_recopie_PAS_le_matricule(self):
        """Il se corrige sur la fiche du fournisseur : deux endroits pour la même donnée
        finiraient par se contredire."""
        src = self.source(F.completer)
        self.assertNotIn("matricule=", src.split("def completer")[1].split("doc.save")[0]
                         .replace("_matricule(supplier)", ""))

    def test_completer_refuse_une_ligne_deja_emise(self):
        self.assertIn('if doc.statut == "Émis":', self.source(F.completer))

    def test_un_statut_humain_n_est_pas_ecrase(self):
        """« Ignoré » est une décision : le recalcul ne doit pas la défaire."""
        self.assertIn('doc.statut if doc.statut in ("Émis", "Ignoré")', self.source(F._reetat))


class TestLectureDeLaFacture(unittest.TestCase):
    """Extraire le fournisseur ET son matricule du scan déjà attaché à l'écriture."""

    def source(self, fn):
        import inspect

        return inspect.getsource(fn)

    def test_la_lecture_ne_cree_rien(self):
        """⚠️ Un doublon de fournisseur se paie longtemps : ses factures se répartissent sur
        deux fiches et aucun solde ne veut plus rien dire. La lecture propose, la création est
        un second geste."""
        src = self.source(F.lire_facture)
        self.assertNotIn("_supplier(", src)
        self.assertIn("ON NE CREE RIEN ICI", src)

    def test_la_creation_garde_le_garde_fou_du_doute(self):
        """`caisse_depenses._supplier` refuse quand des fiches proches existent. On ne le
        contourne pas."""
        self.assertIn("from customization_app.caisse_depenses import _supplier",
                      self.source(F.creer_fournisseur))

    def test_une_ligne_deja_emise_refuse_la_creation(self):
        self.assertIn('if doc.statut == "Émis":', self.source(F.creer_fournisseur))

    def test_fichier_ABSENT_et_AUCUN_fichier_sont_deux_messages(self):
        """⚠️ Un bench restauré depuis une sauvegarde de BASE SEULE connaît les fichiers sans
        les avoir sur disque (constaté en dev le 04/09/2026). Dire « aucune pièce jointe »
        enverrait chercher une photo qui existe pourtant."""
        src = self.source(F.lire_facture)
        self.assertIn("aucune pièce jointe sur cette écriture", src)
        self.assertIn("son fichier est introuvable", src)

    def test_le_pdf_et_l_image_sont_tous_deux_acceptes(self):
        """Les fournisseurs envoient les deux ; le lecteur sait lire l'un et l'autre."""
        src = self.source(F._scan_de)
        self.assertIn("application/pdf", src)
        self.assertIn("image/png", src)

    def test_la_longueur_du_retour_de_decrire_n_est_pas_supposee(self):
        """`_decrire` a rendu selon les versions un couple ou un tuple plus long : indexer en
        dur casserait à la prochaine évolution de la caisse."""
        self.assertIn("len(lu) > 1", self.source(F.lire_facture))


class TestChoixDeLaPieceJointe(unittest.TestCase):
    """⚠️ « LA PREMIÈRE PIÈCE JOINTE » N'EST PAS « LA FACTURE ».

    Seize écritures de dépense en portent plusieurs. Mesuré le 04/09/2026 :
      - des PAGES d'une même facture (« -p1 », « -p2 », « -p3 ») ;
      - des documents de PAIEMENT : « DETAIL DE VIREMENT.docx », « Notification de
        paiement.pdf », « Bon de paiement.pdf », « Chq de paiement » ;
      - des .docx que le modèle ne sait pas lire.

    Prendre la première venue faisait lire l'avis de virement au lieu de la facture.
    """

    def test_une_facture_bat_un_document_de_paiement(self):
        self.assertGreater(F.score_piece("Fac N° 35301-SOGEQ.pdf"),
                           F.score_piece("Fac SONEDE-Notification de paiement.pdf"))
        self.assertGreater(F.score_piece("Fac N° 00402-Patisserie TULIPE.pdf"),
                           F.score_piece("Fac Patisserie TULIP - Bon de paiement.pdf"))

    def test_la_page_1_bat_les_suivantes(self):
        """L'en-tête porte le matricule fiscal : lire la page 2 ne rend ni le nom ni le
        matricule."""
        self.assertGreater(F.score_piece("Fac N°36355-SOGEQ-p1.pdf"),
                           F.score_piece("Fac N°36355-SOGEQ-p2.pdf"))
        self.assertGreater(F.score_piece("Fac N°36355-SOGEQ-p1.pdf"),
                           F.score_piece("Fac N°36355-SOGEQ-p3.pdf"))

    def test_ce_qu_on_ne_sait_pas_lire_est_ecarte(self):
        """Envoyer un .docx au modèle ne rend rien."""
        self.assertEqual(F.score_piece("DETAIL DE VIREMENT-SOGEQ.docx"), -1)
        self.assertEqual(F.score_piece("note.txt"), -1)

    def test_les_formats_lisibles_sont_acceptes(self):
        for nom in ("facture.pdf", "facture.PNG", "facture.jpg", "facture.jpeg"):
            self.assertGreaterEqual(F.score_piece(nom), 0, nom)

    def test_le_cheque_ne_gagne_jamais(self):
        """Sur ACC-JV-2026-00698 : « facture-… » bat « …-Chq de paiement »."""
        self.assertGreater(F.score_piece("facture-ACC-JV-2026-00693877c30.pdf"),
                           F.score_piece("Fac N° 9260543 -MS TechAutomation-Chq de paiement.pdf"))

    def test_l_utilisateur_peut_trancher_lui_meme(self):
        """Le classement est une proposition, pas un verdict : l'écran laisse essayer une autre
        pièce."""
        import inspect

        self.assertIn("fichier", inspect.signature(F.lire_facture).parameters)
        self.assertIn('"pieces": pieces', inspect.getsource(F.lire_facture))
