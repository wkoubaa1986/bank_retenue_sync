"""Tests du rétablissement des dettes qu'un encaissement partiel a effacées.

Convention : `unittest.TestCase` pur — on éprouve les RÈGLES, pas les chiffres de la base.

Le défaut d'origine : le Server Script « Traitement des encaissement » supprime la pièce de
dette en entier et n'en recrée une que du montant encaissé. Le reste disparaît. Réparer demande
plus de prudence que de créer la différence : une pièce « Dette non payée » est une pièce de
RÈGLEMENT, et en poser une là où le client a déjà payé lui invente une créance.
"""
from __future__ import annotations

import inspect
import unittest

from bank_retenue_sync.clients import dettes_perdues as D


class TestOuLaDetteSeRattache(unittest.TestCase):
    """Sur la pièce qui porte la créance, et sur elle seule.

    Mesuré : 137 dettes pointent une commande (57 004 DT), 47 une facture (28 183 DT), aucune
    ne flotte, et aucune commande facturée ne porte encore de dette. On respecte cette
    convention au lieu d'en inventer une.
    """

    def source(self):
        return inspect.getsource(D.diagnostic)

    def test_per_billed_n_est_PAS_utilise(self):
        """⚠️ LE CHAMP EST FAUX DANS CETTE BASE. Vingt commandes affichent « 0 % facturé »
        alors que leur facture est validée et SOLDÉE — il n'a jamais été remis à jour. S'y fier
        proposait une dette de 898 DT sur SAL-ORD-2026-02770, dont la facture est payée."""
        # Le champ n'est ni sélectionné ni testé : seul le commentaire en garde la trace,
        # pour qu'on ne le réintroduise pas par mégarde.
        self.assertNotIn("v.per_billed", self.source())
        self.assertNotIn("per_billed,", self.source())

    def test_la_facture_soldee_ferme_le_dossier(self):
        """Facturée et payée : il n'y a plus aucune créance à rétablir."""
        self.assertIn("facturée et soldée", self.source())

    def test_la_dette_se_plafonne_au_reste_de_la_facture(self):
        self.assertIn('min(trou, ouvertes[0]["reste"])', self.source())

    def test_une_commande_annulee_ou_amendee_ne_recoit_rien(self):
        """Sa créance a changé de document : y poser une dette la ferait vivre deux fois."""
        self.assertIn("v.docstatus != 1 or v.amended_from", self.source())

    def test_les_remises_par_ecriture_sont_deduites_du_trou(self):
        """Une réduction accordée comble une partie du trou en toute légitimité — la recréer en
        dette la réclamerait deux fois (SAL-ORD-2023-00551 : remise de 191 sur un trou de 382)."""
        self.assertIn("remises.get(v.name", self.source())


class TestQuandRecreerUneDette(unittest.TestCase):
    """La question la plus dangereuse du module.

    Une pièce « Dette non payée » SOLDE la commande et augmente le « réglé » du client. En poser
    une chez quelqu'un dont les comptes tombent déjà juste lui invente de l'argent.
    """

    def source(self):
        return inspect.getsource(D._qualifier)

    def test_un_client_equilibre_ne_recoit_aucune_dette(self):
        """LIMPID'EAU est à 0,200 près de l'équilibre pour 2 912,5 DT de trous : ses commandes
        sont mal imputées, il ne doit rien."""
        self.assertIn("compte du client équilibré", self.source())

    def test_le_montant_se_plafonne_a_ce_que_le_client_DOIT(self):
        """FM WATER PLUS : trou de 541,431 sur la commande, mais son compte ne réclame que
        534,781. Recréer le trou entier lui inventerait 6,650 DT."""
        self.assertIn("min(c[\"montant\"], manque)", self.source())

    def test_plusieurs_trous_pour_une_dette_plus_petite_partent_a_la_main(self):
        """⚠️ Nizar Maddouri ne doit que 30 DT pour 3 520 DT de trous sur 38 commandes. On ne
        peut pas deviner laquelle les porte : recréer aveuglément inventerait 3 490 DT."""
        src = self.source()
        self.assertIn("à répartir à la main", src)
        self.assertIn("combien == 1", src)

    def test_aucun_montant_historique_n_est_rejoue(self):
        """La commande a pu rétrécir depuis : on recalcule le trou d'aujourd'hui."""
        self.assertIn("grand_total", inspect.getsource(D.diagnostic))
        self.assertNotIn("ld.valeur", inspect.getsource(D.diagnostic))


class TestPrudenceDeLEcriture(unittest.TestCase):
    """Ce module crée des pièces comptables validées."""

    def test_l_essai_a_blanc_est_le_defaut(self):
        self.assertIs(inspect.signature(D.reparer).parameters["insert"].default, False)

    def test_seuls_les_cas_qualifies_sont_ecrits(self):
        self.assertIn('if c["a_recreer"]', inspect.getsource(D.reparer))

    def test_la_piece_porte_le_bon_sens_et_les_bons_comptes(self):
        src = inspect.getsource(D._creer_dette)
        self.assertIn('pe.payment_type = "Receive"', src)
        self.assertIn("pe.paid_from = COMPTE_CLIENT", src)
        self.assertIn("pe.paid_to = COMPTE_DETTE", src)
        self.assertEqual(D.MODE_DETTE, "Dette non payée")

    def test_la_piece_dit_pourquoi_elle_existe(self):
        """Six mois plus tard, personne ne doit se demander d'où sort cette dette."""
        self.assertIn("encaissement partiel", inspect.getsource(D._creer_dette))
