"""Tests de l'autre sens du flux TEJ : les retenues comptabilisees sans certificat.

Convention de l'app : `unittest.TestCase` pur, donnees injectees, aucun acces reseau ni base.
Les montants viennent des ecritures reelles de 2026.
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.tej import orphelines as O


def piece(name="ACC-PAY-1", montant=53.77, jour=date(2026, 2, 28), customer="STE TAURUS",
          facture="ACC-SINV-2026-00170", commande=None, justificatifs=None, lies=None):
    return {"name": name, "customer": customer, "posting_date": jour, "paid_amount": montant,
            "sales_invoice": facture, "sales_order": commande, "reference_no": None,
            "justificatifs": justificatifs or [], "certificats_lies": lies or []}


def fichier(nom="certificat retenue source.pdf", source="facture"):
    return {"file_name": nom, "file_url": "/files/%s" % nom, "source": source,
            "source_doctype": "Sales Invoice", "source_name": "ACC-SINV-2026-00170"}


def certificat(name="CERT-1", montant=53.76, jour=date(2026, 2, 11), statut="Ambiguous",
               pe=None, declarant="STE TAURUS"):
    return {"name": name, "reference": "ref-%s" % name, "declarant": declarant, "customer": None,
            "montant_retenue": montant, "date_paiement": jour, "match_status": statut,
            "payment_entry": pe}


class TestVerdicts(unittest.TestCase):
    def test_un_certificat_non_rapproche_qui_colle_est_un_rapprochement_a_faire(self):
        """Cas reel STE TAURUS : 4 certificats bloques sur un doublon de fiche client, et 4
        ecritures orphelines en face. Ce n'est pas un trou, c'est un rapprochement."""
        [l] = O.apparier([piece()], [certificat()])
        self.assertEqual(l["verdict"], O.PROBABLE)
        self.assertEqual(l["candidats"][0]["name"], "CERT-1")

    def test_le_timbre_ne_fait_pas_rater_le_rapprochement(self):
        """53,760 declares contre 53,770 comptabilises : 1 % du timbre de 1 DT."""
        [l] = O.apparier([piece(montant=53.77)], [certificat(montant=53.76)])
        self.assertEqual(l["verdict"], O.PROBABLE)

    def test_un_montant_proche_mais_pas_au_millime_ne_rapproche_pas(self):
        """⚠️ La marge est celle de l'identification (0,020), pas celle de l'appariement — qui
        plancherait a 1 DT et rapprocherait n'importe quelle petite retenue de n'importe quelle
        autre."""
        [l] = O.apparier([piece(montant=4.400)], [certificat(montant=3.710)])
        self.assertEqual(l["verdict"], O.SANS_CERTIFICAT)

    def test_un_certificat_deja_rapproche_ailleurs_n_explique_rien(self):
        """Sa piece est ailleurs : le proposer ferait croire a une solution qui n'en est pas une."""
        [l] = O.apparier([piece()], [certificat(pe="ACC-PAY-9", statut="Auto Matched")])
        self.assertEqual(l["verdict"], O.SANS_CERTIFICAT)

    def test_hors_fenetre_le_certificat_n_explique_rien(self):
        [l] = O.apparier([piece(jour=date(2026, 2, 28))],
                         [certificat(jour=date(2026, 8, 30))])
        self.assertEqual(l["verdict"], O.SANS_CERTIFICAT)

    def test_avant_le_perimetre_l_absence_de_certificat_n_est_pas_une_alerte(self):
        """Absence de preuve, pas preuve d'absence : le portail n'est pas suivi sur 2025."""
        [l] = O.apparier([piece(jour=date(2025, 11, 3))], [], annee_min=2026)
        self.assertEqual(l["verdict"], O.HORS_PERIODE)

    def test_sans_certificat_est_la_seule_vraie_alerte(self):
        """Le credit d'impot existe dans nos comptes et nulle part ailleurs : il n'est pas
        opposable au fisc, il est a reclamer au client."""
        [l] = O.apparier([piece()], [])
        self.assertEqual(l["verdict"], O.SANS_CERTIFICAT)
        self.assertIn("reclamer", l["explication"])

    def test_plusieurs_certificats_possibles_sont_tous_rendus(self):
        """STE TAURUS declare quatre fois 53,760 dans l'annee : la liste doit remonter entiere,
        c'est a l'humain de rattacher chacune a son ecriture."""
        [l] = O.apparier([piece()], [certificat(name="A"), certificat(name="B")])
        self.assertEqual(l["verdict"], O.PROBABLE)
        self.assertEqual(len(l["candidats"]), 2)
        self.assertIn("2 certificats", l["explication"])

    def test_les_alertes_remontent_en_tete(self):
        """Un tableau qui noie « sans certificat » sous les rapprochements a faire ne sert a rien."""
        lignes = O.apparier([piece(name="RAPPROCHABLE"), piece(name="ORPHELINE", montant=21.61)],
                            [certificat()])
        self.assertEqual([l["name"] for l in lignes], ["ORPHELINE", "RAPPROCHABLE"])


class TestJustificatifManuel(unittest.TestCase):
    """Le portail n'est obligatoire que depuis avril 2026 : le certificat papier scanne sur la
    facture prouve la retenue tout autant, et le paiement est alors tenu pour rapproche."""

    def test_un_certificat_papier_sur_la_facture_justifie_la_retenue(self):
        [l] = O.apparier([piece(justificatifs=[fichier()])], [])
        self.assertEqual(l["verdict"], O.CERTIFICAT_MANUEL)
        self.assertTrue(l["certificat_manuel"])
        self.assertIn("tenu pour rapproche", l["explication"])

    def test_le_pdf_telecharge_au_portail_n_est_pas_un_justificatif_manuel(self):
        """Sinon un certificat du portail se compterait comme preuve « hors portail » — et une
        retenue non declaree passerait pour justifiee."""
        cert = certificat()
        [l] = O.apparier(
            [piece(montant=99.99, justificatifs=[fichier("certificat_ras_ref-CERT-1.pdf")])],
            [cert])
        self.assertEqual(l["verdict"], O.SANS_CERTIFICAT)
        self.assertFalse(l["certificat_manuel"])

    def test_une_piece_jointe_qui_ne_se_nomme_pas_ne_prouve_rien_mais_est_signalee(self):
        """Un bon de livraison scanne n'est pas un certificat. Taire son existence enverrait
        pourtant relancer un client qui a peut-etre deja fourni, sous un mauvais nom."""
        [l] = O.apparier([piece(justificatifs=[fichier("BL-2026-00170.pdf")])], [])
        self.assertEqual(l["verdict"], O.SANS_CERTIFICAT)
        self.assertIn("aucune ne se nomme comme un certificat", l["explication"])
        self.assertIn("BL-2026-00170.pdf", l["explication"])

    def test_le_justificatif_est_lu_sur_la_commande_quand_il_n_y_a_pas_de_facture(self):
        """`tej/pdf.cible` range le certificat sur la commande tant qu'elle n'est pas facturee."""
        [l] = O.apparier([piece(facture=None, commande="ACC-SO-2026-00042",
                                justificatifs=[fichier(source="commande")])], [])
        self.assertEqual(l["verdict"], O.CERTIFICAT_MANUEL)
        self.assertIn("commande", l["explication"])

    def test_ce_qui_nomme_un_certificat_et_ce_qui_n_en_nomme_pas(self):
        for nom in ("certificat retenue.pdf", "RAS 2026.pdf", "attestation-retenue.jpg",
                    "CERTIF_CLIENT.pdf"):
            self.assertTrue(O.nomme_un_certificat(nom), nom)
        # « ras » en sous-chaine attraperait « brasserie » : le sigle se lit en jeton entier.
        for nom in ("brasserie facture.pdf", "BL-123.pdf", "devis.pdf", ""):
            self.assertFalse(O.nomme_un_certificat(nom), nom)


class TestComparaisonAvecLeTej(unittest.TestCase):
    """Quand les deux preuves existent, on les confronte : meme document regularise, ou deux
    retenues distinctes ?"""

    def test_le_papier_et_le_portail_au_meme_montant_concordent(self):
        lie = certificat(name="TEJ-1", montant=53.76, pe="ACC-PAY-9", statut="Auto Matched")
        [l] = O.apparier([piece(justificatifs=[fichier()], lies=[lie])], [])
        c = l["comparaison"]
        self.assertTrue(c["concordant"])
        self.assertEqual(c["ecart"], 0.01)
        self.assertIn("timbre fiscal", c["texte"])

    def test_un_ecart_de_montant_pose_la_question_au_lieu_de_conclure(self):
        lie = certificat(name="TEJ-1", montant=21.61, pe="ACC-PAY-9", statut="Auto Matched")
        [l] = O.apparier([piece(justificatifs=[fichier()], lies=[lie])], [])
        self.assertEqual(l["verdict"], O.CERTIFICAT_MANUEL)
        self.assertFalse(l["comparaison"]["concordant"])
        self.assertIn("ECART", l["comparaison"]["texte"])

    def test_sans_certificat_au_portail_le_papier_est_la_seule_preuve(self):
        [l] = O.apparier([piece(justificatifs=[fichier()])], [])
        self.assertIsNone(l["comparaison"]["montant_tej"])
        self.assertIn("seule preuve", l["comparaison"]["texte"])

    def test_depuis_avril_2026_le_papier_seul_est_une_question_au_declarant(self):
        avant = O.apparier([piece(jour=date(2026, 2, 28), justificatifs=[fichier()])], [])[0]
        apres = O.apparier([piece(jour=date(2026, 5, 12), justificatifs=[fichier()])], [])[0]
        self.assertNotIn("obligatoire", avant["comparaison"]["texte"])
        self.assertIn("obligatoire", apres["comparaison"]["texte"])

    def test_le_papier_est_confronte_au_certificat_qui_colle_au_montant_et_a_la_date(self):
        """Cas reel SPH Khamsa : « certificate Khamsa.pdf » sur la facture et un certificat TEJ de
        24,40 non rapproche. C'est le meme document — le rapprochement reste a faire, mais le dire
        evite de comptabiliser une seconde retenue."""
        [l] = O.apparier([piece(montant=24.41, justificatifs=[fichier("certificate Khamsa.pdf")])],
                         [certificat(montant=24.40, statut="Sans piece")])
        self.assertEqual(l["verdict"], O.PROBABLE)
        self.assertTrue(l["comparaison"]["concordant"])
        self.assertIn("meme montant", l["comparaison"]["texte"])

    def test_sans_papier_un_simple_candidat_ne_produit_pas_de_comparaison(self):
        """L'explication le dit deja : la repeter en « comparaison » ferait croire a une seconde
        source de preuve."""
        [l] = O.apparier([piece()], [certificat()])
        self.assertIsNone(l["comparaison"])

    def test_un_certificat_declare_sur_la_meme_facture_est_un_rapprochement_a_faire(self):
        """L'imputation designe la creance la ou la date et le montant ne disent rien — meme
        lecon que `rapprochement.apparier_par_facture`."""
        lie = certificat(name="TEJ-1", montant=12.34, jour=date(2026, 8, 30), statut="Sans piece")
        [l] = O.apparier([piece(lies=[lie])], [])
        self.assertEqual(l["verdict"], O.PROBABLE)
        self.assertEqual(l["candidats"][0]["via"], "piece")
        self.assertIn("declare sur la meme facture", l["explication"])


class TestDoublePassage(unittest.TestCase):
    def test_une_retenue_deja_portee_par_une_autre_ecriture_est_signalee(self):
        """Le seul cas ou un credit d'impot peut avoir ete compte deux fois."""
        lie = certificat(name="TEJ-1", montant=53.77, pe="ACC-PAY-9", statut="Auto Matched")
        [l] = O.apparier([piece(lies=[lie])], [])
        self.assertIn("double comptabilisation", l["alerte"])

    def test_l_alerte_passe_devant_les_relances(self):
        lie = certificat(name="TEJ-1", montant=53.77, pe="ACC-PAY-9", statut="Auto Matched")
        lignes = O.apparier([piece(name="SANS-CERT", montant=21.61, jour=date(2026, 1, 5)),
                             piece(name="DOUBLON", lies=[lie])], [])
        self.assertEqual([l["name"] for l in lignes], ["DOUBLON", "SANS-CERT"])

    def test_deux_retenues_de_montants_differents_sur_une_facture_ne_sont_pas_un_doublon(self):
        lie = certificat(name="TEJ-1", montant=21.61, pe="ACC-PAY-9", statut="Auto Matched")
        [l] = O.apparier([piece(lies=[lie])], [])
        self.assertIsNone(l["alerte"])


class TestSynthese(unittest.TestCase):
    def test_un_compte_et_un_montant_par_verdict(self):
        lignes = O.apparier([piece(name="A"), piece(name="B", montant=21.61),
                             piece(name="C", montant=28.9, jour=date(2025, 6, 1))],
                            [certificat()], annee_min=2026)
        s = O.synthese(lignes)
        self.assertEqual((s["probables"], s["sans_certificat"], s["hors_periode"]), (1, 1, 1))
        self.assertEqual(s["montant_sans_certificat"], 21.61)
        self.assertEqual(s["montant_total"], round(53.77 + 21.61 + 28.9, 3))

    def test_le_papier_sort_la_ligne_des_relances(self):
        """Le chiffre qui declenche une relance client ne doit compter que ce qui n'est prouve
        nulle part — ni au portail, ni sur la facture."""
        lignes = O.apparier([piece(name="A", montant=21.61),
                             piece(name="B", montant=28.9, justificatifs=[fichier()])], [])
        s = O.synthese(lignes)
        self.assertEqual((s["sans_certificat"], s["certificat_manuel"]), (1, 1))
        self.assertEqual((s["montant_sans_certificat"], s["montant_certificat_manuel"]),
                         (21.61, 28.9))


if __name__ == "__main__":
    unittest.main()
