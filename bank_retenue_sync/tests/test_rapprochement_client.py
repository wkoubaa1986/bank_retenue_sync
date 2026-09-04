"""Tests du rapprochement par client.

Convention : `unittest.TestCase` pur — on éprouve les RÈGLES, pas les chiffres de la base.

La question de l'écran : pour un client donné, ses commandes TTC, ses bons de livraison validés
et ses règlements se répondent-ils ? Sur les données réelles du 04/09/2026, 108 clients sur
3 360 divergent, et 33 ont des commandes sans le moindre BL.
"""
from __future__ import annotations

import unittest

from bank_retenue_sync.clients import rapprochement as R


class TestTolerance(unittest.TestCase):
    """Ce qui mérite du rouge, et ce qui n'est que la mécanique normale."""

    def test_le_timbre_ne_fait_pas_un_ecart(self):
        """⚠️ 1 DT sépare une commande de sa facture sur presque tous les dossiers. Peindre
        cela en rouge noierait les vrais écarts sous des milliers de faux."""
        self.assertEqual(R.TOLERANCE, 1.0)
        self.assertFalse(R.ecart_significatif(1.0))
        self.assertFalse(R.ecart_significatif(-1.0))
        self.assertFalse(R.ecart_significatif(0.0))

    def test_au_dela_du_timbre_il_y_a_quelque_chose_a_voir(self):
        self.assertTrue(R.ecart_significatif(1.001))
        self.assertTrue(R.ecart_significatif(-450.0))

    def test_le_signe_ne_change_rien_au_verdict(self):
        """Un client qui a TROP payé est aussi anormal que celui qui n'a pas assez payé —
        c'est souvent une avance jamais soldée, ou un règlement affecté au mauvais client."""
        for v in (2.0, -2.0, 4000.0, -4000.0):
            self.assertTrue(R.ecart_significatif(v), v)


class TestSensDuJournal(unittest.TestCase):
    """Sur un compte de tiers, un CRÉDIT diminue la créance.

    L'écriture de journal agit alors comme un règlement (avoir, régularisation, perte). La
    rendre en `debit - credit` aurait inversé le signe et fait paraître soldés les clients qui
    devaient encore, et débiteurs ceux qu'on avait déjà remboursés.
    """

    def test_la_requete_rend_credit_moins_debit(self):
        import inspect

        src = inspect.getsource(R.journal)
        self.assertIn("SUM(jea.credit - jea.debit)", src)

    def test_seules_les_ecritures_validees_comptent(self):
        import inspect

        self.assertIn("je.docstatus = 1", inspect.getsource(R.journal))


class TestPerimetreDesSources(unittest.TestCase):
    """Ce qu'on additionne, et ce qu'on écarte."""

    def source(self, fn):
        import inspect

        return inspect.getsource(fn)

    def test_les_commandes_annulees_ne_doivent_rien_a_personne(self):
        self.assertIn("docstatus = 1", self.source(R.commandes))

    def test_seuls_les_BL_VALIDES_comptent(self):
        """C'est la question posée : « a-t-il des BL validés ? ». Un brouillon n'a rien livré."""
        self.assertIn("docstatus = 1", self.source(R.bons_de_livraison))

    def test_seuls_les_encaissements_RECUS_comptent(self):
        """Un paiement émis vers un fournisseur n'a rien à faire dans le compte d'un client."""
        src = self.source(R.reglements)
        self.assertIn("payment_type = 'Receive'", src)
        self.assertIn("party_type = 'Customer'", src)

    def test_les_deux_natures_d_avance_sont_distinguees(self):
        """⚠️ Elles ne veulent pas dire la même chose : « non affectée » est de l'argent reçu
        qui ne pointe sur RIEN — le cas suspect ; « sur commande » est normal tant que la
        facture n'existe pas."""
        src = self.source(R.avances)
        self.assertIn("unallocated_amount", src)
        self.assertIn("reference_doctype = 'Sales Order'", src)


class TestRechercheTelephone(unittest.TestCase):
    """Chercher un client par son numéro, tel qu'il est écrit sur le devis."""

    def test_les_deux_champs_reellement_remplis_sont_cherches(self):
        """Mesuré sur la base : `custom_liste_telephone` couvre 5 193 clients, `mobile_no` 894,
        et les deux autres champs « téléphone » de la fiche sont VIDES."""
        self.assertEqual(R.CHAMPS_TELEPHONE, ("custom_liste_telephone", "mobile_no"))

    def test_les_espaces_du_numero_sont_neutralises(self):
        """Les numéros sont stockés « 26 130 274 » : chercher « 26130274 » ne trouverait rien
        sans retirer les espaces des DEUX côtés."""
        import inspect

        src = inspect.getsource(R._clients)
        self.assertIn("REPLACE", src)
        self.assertIn("qtel", src)


class TestAgregation(unittest.TestCase):
    """Comment les chiffres sont ramenés — la seule chose qui rende l'écran utilisable."""

    def test_une_seule_requete_par_source(self):
        """⚠️ 5 220 clients pour ~10 000 pièces de chaque type. Une requête par client mettrait
        la page à genoux ; tout passe par des GROUP BY (mesuré : 0,8 s pour 3 360 clients)."""
        import inspect

        # Les trois premières passent par `_somme`, qui porte le GROUP BY ; `journal` a sa
        # propre requête parce que son signe s'inverse.
        self.assertIn("GROUP BY", inspect.getsource(R._somme))
        self.assertIn("GROUP BY", inspect.getsource(R.journal))
        for fn in (R.commandes, R.bons_de_livraison, R.reglements):
            self.assertIn("_somme(", inspect.getsource(fn), fn.__name__)

    def test_les_clients_sans_aucune_piece_sont_ecartes(self):
        """Ils n'ont rien à révéler et noieraient la liste — 613 clients sans groupe, pour la
        plupart jamais servis."""
        import inspect

        self.assertIn("if not (cde_nb or bl_nb or pay_nb or jrn_nb)",
                      inspect.getsource(R.lignes))


class TestSeuilsReglables(unittest.TestCase):
    """Les deux seuils se règlent — ils ne sont pas la même question.

    Un écart de règlement et un écart de livraison n'ont pas le même montant acceptable : le
    premier suit le timbre fiscal, le second dépend de ce qu'on tolère entre une commande et ce
    qui en est sorti (demande utilisateur 04/09/2026).
    """

    def test_le_defaut_reste_le_timbre(self):
        self.assertEqual(R.TOLERANCE, 1.0)

    def test_un_seuil_explicite_prime_sur_le_defaut(self):
        self.assertFalse(R.ecart_significatif(50.0, seuil=100.0))
        self.assertTrue(R.ecart_significatif(150.0, seuil=100.0))

    def test_le_seuil_zero_signale_le_moindre_centime(self):
        """⚠️ ZÉRO EST UN CHOIX VALIDE — « signale-moi tout ». Un `or TOLERANCE` quelque part
        dans la chaîne l'écraserait et rendrait le réglage muet ; mesuré sur la base, il fait
        passer de 108 à 145 clients signalés."""
        self.assertTrue(R.ecart_significatif(0.001, seuil=0))
        self.assertFalse(R.ecart_significatif(0.0, seuil=0))

    def test_les_deux_seuils_sont_lus_separement(self):
        import inspect

        src = inspect.getsource(R.lignes)
        self.assertIn('seuils["montant"]', src)
        self.assertIn('seuils["bl"]', src)

    def test_les_seuils_sont_lus_une_seule_fois_par_page(self):
        """`lignes` boucle sur des milliers de clients : un get_single_value par ligne
        rechargerait le réglage autant de fois."""
        import inspect

        src = inspect.getsource(R.lignes)
        self.assertEqual(src.count("tolerances()"), 1)

    def test_un_reglage_absent_ne_fait_pas_echouer_l_ecran(self):
        """Un bench où la migration n'est pas passée doit quand même pouvoir ouvrir la page."""
        import inspect

        self.assertIn("except Exception", inspect.getsource(R._reglage))


class TestPaiementsGroupes(unittest.TestCase):
    """Un règlement est souvent GROUPÉ, et la ligne seule ne dit alors rien.

    Mesuré sur ECONOMIQ AQUA SOLUTIONS : 131 règlements, dont 16 couvrent plusieurs pièces —
    un versement de 3 960 DT réparti sur deux commandes, un de 2 700 sur quatre. Lire le
    montant sans savoir ce qu'il éteint rend l'écart du client inexplicable.
    """

    def source(self):
        import inspect

        from bank_retenue_sync.api import rapprochement_client as API

        return inspect.getsource(API._paiements_detailles)

    def test_les_affectations_sont_ramenees_en_une_requete(self):
        """Un `get_all` par paiement ferait 131 allers-retours sur un gros client."""
        src = self.source()
        self.assertIn("per.parent IN", src)
        self.assertNotIn("for p in paiements:\n        refs", src)

    def test_l_ordre_de_la_piece_est_conserve(self):
        self.assertIn("ORDER BY per.parent, per.idx", self.source())

    def test_groupe_se_lit_sur_le_NOMBRE_de_pieces(self):
        """Deux factures de 30 DT sont un paiement groupé ; un règlement de 4 000 DT sur une
        seule facture ne l'est pas."""
        self.assertIn("len(lignes) > 1", self.source())

    def test_les_affectations_annulees_sont_ecartees(self):
        self.assertIn("per.docstatus < 2", self.source())
