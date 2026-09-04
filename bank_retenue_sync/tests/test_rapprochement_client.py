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


class TestVentilationParType(unittest.TestCase):
    """Le détail des règlements par TYPE, déplié sous chaque client.

    ⚠️ « RÉGLÉ » NE VEUT PAS DIRE « ENCAISSÉ ». Une pièce de mode « Dette non payée » solde une
    commande sans qu'un dinar ait bougé, un chèque en portefeuille n'est pas encore de l'argent,
    et une retenue à la source n'arrivera jamais sur le compte. Sur la base réelle, 66 059 DT
    sur 997 770 sont dans ce cas.
    """

    def test_le_type_de_compte_ne_peut_PAS_servir_a_trancher(self):
        """⚠️ MESURÉ LE 04/09/2026 : dans ce plan comptable, TOUS les comptes de destination
        sont typés « Bank » ou « Cash » — y compris Dettes, Chèques, Livraison Aramex et Perte
        de non paiement. S'y fier ferait passer les dettes non payées pour de l'argent reçu.
        C'est le GROUPE PARENT qui tranche."""
        import inspect

        src = inspect.getsource(R._groupes_de_comptes)
        self.assertIn("parent_account", src)
        self.assertNotIn("account_type", src)

    def test_ce_qui_SOLDE_la_creance_n_est_pas_que_du_cash(self):
        """⚠️ LE DRAPEAU DIT « LE CLIENT NE DOIT PLUS CELA », pas « l'argent est en caisse ».

        La retenue à la source en fait partie (décision utilisateur 04/09/2026) : le client la
        verse au Trésor pour notre compte, il a donc payé — nous recevons un crédit d'impôt au
        lieu d'espèces. La ranger en « attente » faisait paraître 4 916 DT impayés sur 155
        pièces alors que rien n'est dû. Même raisonnement que pour l'écriture de journal.
        """
        self.assertEqual(R.GROUPES_ENCAISSES,
                         (R.GROUPE_BANQUE, R.GROUPE_CAISSE, R.GROUPE_IMPOTS))

    def test_ce_qui_reste_dehors_est_une_promesse(self):
        """Un chèque en portefeuille, une dette au compte de créance, une perte assumée : rien
        de tout cela n'éteint la créance."""
        self.assertNotIn(R.GROUPE_CREANCE, R.GROUPES_ENCAISSES)
        self.assertNotIn("Charges Indirectes - A&S", R.GROUPES_ENCAISSES)

    def test_la_retenue_a_la_source_solde(self):
        c = R.categorie("Retenue a la source vente", "Avance  impôt société - A&S",
                        R.GROUPE_IMPOTS)
        self.assertTrue(c["encaisse"])
        self.assertIn("Trésor", c["libelle"])

    def test_un_cheque_encaisse_et_un_cheque_en_portefeuille_sont_deux_types(self):
        """C'est la distinction demandée : le mode seul ne dit pas si l'argent est arrivé."""
        encaisse = R.categorie("Chèque", "STE430127B - Zitouna - A&S", R.GROUPE_BANQUE)
        portefeuille = R.categorie("Chèque", "Chèques - A&S", R.GROUPE_CREANCE)
        self.assertTrue(encaisse["encaisse"])
        self.assertFalse(portefeuille["encaisse"])
        self.assertNotEqual(encaisse["cle"], portefeuille["cle"])
        self.assertIn("encaissé en banque", encaisse["libelle"])
        self.assertIn("pas encore encaissé", portefeuille["libelle"])

    def test_les_especes_de_caisse_et_versees_en_banque_se_distinguent(self):
        caisse = R.categorie("Espèces", "Espèces - A&S", R.GROUPE_CAISSE)
        versees = R.categorie("Espèces", "Compte TAWFIR - Banque Zitouna - A&S", R.GROUPE_BANQUE)
        self.assertTrue(caisse["encaisse"] and versees["encaisse"])
        self.assertNotEqual(caisse["libelle"], versees["libelle"])

    def test_une_dette_non_payee_n_est_pas_de_l_argent(self):
        c = R.categorie("Dette non payée", "Dettes - A&S", R.GROUPE_CREANCE)
        self.assertFalse(c["encaisse"])

    def test_un_groupe_inconnu_garde_son_nom_au_lieu_de_disparaitre(self):
        """Une nouvelle famille de comptes doit apparaître d'elle-même dans la ventilation,
        sans qu'on touche au code — sinon elle serait silencieusement rangée ailleurs."""
        c = R.categorie("Chèque", "Nouveau compte - A&S", "Famille inedite - A&S")
        self.assertIn("Famille inedite", c["libelle"])
        self.assertFalse(c["encaisse"])

    def test_un_mode_absent_ne_fait_pas_disparaitre_la_ligne(self):
        c = R.categorie(None, "Espèces - A&S", R.GROUPE_CAISSE)
        self.assertIn("Sans mode", c["libelle"])

    def test_la_cle_croise_le_mode_ET_le_compte(self):
        """Deux comptes bancaires différents restent deux lignes : le total par compte est ce
        qui permet de retrouver l'argent."""
        a = R.categorie("Virement", "STE430127B - Zitouna - A&S", R.GROUPE_BANQUE)
        b = R.categorie("Virement", "Economiq Aqua Solution - A&S", R.GROUPE_BANQUE)
        self.assertNotEqual(a["cle"], b["cle"])

    def test_l_ecriture_de_journal_est_un_type_a_part(self):
        """Réduction accordée, avoir, régularisation : cela n'a ni mode de paiement ni compte
        de destination, d'où sa catégorie propre.

        ⚠️ ELLE COMPTE COMME ENCAISSÉE depuis le 04/09/2026 (décision utilisateur) : sur un
        compte client, une écriture SOLDE réellement la créance. Le test qui exigeait
        `encaisse: False` disait le contraire et a été repris."""
        import inspect

        self.assertIn("CLE_JOURNAL", inspect.getsource(R.lignes))

    def test_la_ventilation_est_ramenee_en_une_requete(self):
        import inspect

        self.assertIn("GROUP BY party, mode_of_payment, paid_to",
                      inspect.getsource(R.ventilation))


class TestRepriseDHistorique(unittest.TestCase):
    """Les soldes d'AVANT la migration ne sont pas des ventes de cet ERP.

    Onze factures d'ouverture (31 322 DT) portent ce que les clients devaient avant la bascule ;
    vingt et un paiements les soldent, et treize écritures passent par le compte temporaire
    d'ouverture. Ces règlements n'ont, par construction, AUCUNE commande en face.

    ⚠️ MESURE DU 04/09/2026 : les compter dans la comparaison faisait paraître ECONOMIQ AQUA
    SOLUTIONS surpayé de 20 641 DT. Sa reprise vaut 20 824 : une fois sortie, son écart réel est
    de −183 DT. Dix-sept clients sont dans ce cas, pour 36 602 DT au total.
    """

    def source(self, fn):
        import inspect

        return inspect.getsource(fn)

    def test_les_paiements_de_reprise_soldent_une_facture_d_ouverture(self):
        self.assertIn("si.is_opening = 'Yes'", self.source(R.paiements_ouverture))

    def test_seul_le_TYPE_de_l_ecriture_designe_une_reprise(self):
        """⚠️ LE COMPTE D'OUVERTURE NE PEUT PAS SERVIR DE MARQUEUR.

        Une première version reconnaissait aussi les écritures dont la CONTREPARTIE est
        « Compte temporaire - compte d'overture ». Or ce compte sert de compte de passage pour
        des avoirs et des ajustements ordinaires : « Reliquat avoir paiement », « Ajustement
        Erreur Néjib pour les osmoseurs »… Sur LIMPID'EAU, la règle rangeait 2 621,303 DT
        d'avoirs en reprise, et l'écran annonçait un impayé de 2 621 chez un client dont les
        comptes tombent juste à 0,200 près — ce que le bandeau « Totaux cohérents » de sa fiche
        affichait pourtant déjà (04/09/2026).

        Le nom d'un compte ne prouve rien de l'intention de l'écriture.
        """
        src = self.source(R.reprise_journal)
        self.assertIn("je.voucher_type = %s", src)
        self.assertEqual(R.VOUCHER_OUVERTURE, "Opening Entry")
        self.assertNotIn("autre.account LIKE", src)

    def test_la_reprise_sort_du_delta(self):
        """C'est tout l'objet du cas spécial."""
        self.assertIn("regle - reprise - cde_total", self.source(R.lignes))

    def test_la_reprise_n_est_ventilee_qu_une_fois(self):
        """Elle a sa propre catégorie : la laisser aussi dans la ventilation par mode la
        compterait deux fois et le total ne tomberait plus juste."""
        src = self.source(R.lignes)
        self.assertIn("ventilation(exclure=pe_reprise)", src)
        self.assertIn("jrn_net - rep_j_total", src)

    def test_la_ventilation_sait_exclure_des_pieces(self):
        self.assertIn("NOT IN", self.source(R.ventilation))


class TestTotalQuiSAdditionne(unittest.TestCase):
    """Le total de la ventilation DOIT égaler la colonne « Réglé ».

    C'est le contrôle que l'utilisateur fait à l'œil : un écran dont le détail ne redonne pas
    le total affiché ne se croit pas. Vérifié sur les 103 clients en écart — zéro divergence.
    """

    def test_le_total_ventile_est_calcule_et_rendu(self):
        src = __import__("inspect").getsource(R.lignes)
        self.assertIn('ligne["total_ventile"]', src)
        self.assertIn('sum(x["total"] for x in cats)', src)

    def test_l_ecriture_de_journal_compte_comme_encaissee(self):
        """Décision utilisateur 04/09/2026 : sur un compte client, une écriture SOLDE
        réellement la créance — remise accordée, avoir, régularisation."""
        src = __import__("inspect").getsource(R.lignes)
        bloc = src[src.index("CLE_JOURNAL"):]
        self.assertIn('"encaisse": True', bloc[:600])


import inspect  # noqa: E402  (les classes ci-dessus importent inspect localement)


class TestDetailDesLivraisons(unittest.TestCase):
    """Ce que la colonne « BL validés » ne peut pas dire à elle seule.

    Mesuré sur la base : 10 601 bons validés, 4 RETOURS (−333,600 DT), 45 bons restés en
    BROUILLON (25 705 DT), aucun annulé. Et le brouillon explique souvent l'écart en entier :
    Abdelaziz Amraoui a 970 DT de commandes, 970 DT d'écart de livraison, et un bon de 970 DT
    jamais validé.
    """

    def source(self):
        return inspect.getsource(R.livraisons_detail)

    def test_les_quatre_etats_sont_distingues(self):
        src = self.source()
        for etat in ("annules", "brouillons", "retours", "livres"):
            self.assertIn('"%s"' % etat, src)

    def test_un_retour_est_un_bon_VALIDE_de_montant_negatif(self):
        """Il n'est pas dans un état à part : c'est `is_return` qui le désigne, et son total
        négatif vient naturellement en déduction du net livré."""
        self.assertIn("is_return", self.source())
        self.assertIn("docstatus == 2", self.source())

    def test_le_total_des_BL_garde_les_retours(self):
        """⚠️ C'est le NET livré qu'on compare aux commandes. Exclure les retours ferait
        paraître sur-livrés les clients qui ont rendu de la marchandise."""
        self.assertIn("docstatus = 1", inspect.getsource(R.bons_de_livraison))
        self.assertNotIn("is_return", inspect.getsource(R.bons_de_livraison))

    def test_tout_passe_par_un_seul_GROUP_BY(self):
        self.assertIn("GROUP BY customer, docstatus, is_return", self.source())

    def test_chaque_client_porte_son_detail(self):
        self.assertIn('"livraisons": livr.get(c.name, {})', inspect.getsource(R.lignes))
