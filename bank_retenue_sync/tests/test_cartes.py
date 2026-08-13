"""Tests des paiements de la carte technologique (bank/cartes.py).

Meme convention que les tests existants : `unittest.TestCase` pur, entrees injectees, aucun acces
reseau ni base.
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.bank import cartes as K


def ligne(reference="262299", statut="Transaction approuvée", montant=393.651, jour=10,
          mois=7, detail="FACEBK AQ8P2WR7X2 IE"):
    return {"date": date(2026, mois, jour), "operation": "Achat internet", "detail": detail,
            "statut": statut, "reference": reference, "montant": montant}


class TestStatutDeLaTransaction(unittest.TestCase):
    """LA regle du module : un refus de carte n'est pas un paiement.

    Sur l'export reel du 21/07, 8 lignes sur 17 sont des « Solde insuffisant » pour 3 438 DT —
    les comptabiliser aurait invente autant de charges. Le montant ne les distingue pas d'un
    paiement : seul le statut tranche.
    """

    def test_transaction_approuvee(self):
        self.assertTrue(K.est_approuve(ligne()))

    def test_solde_insuffisant_refuse(self):
        self.assertFalse(K.est_approuve(ligne(reference="", statut="Solde insuffisant")))

    def test_statut_approuve_sans_reference_refuse(self):
        """Sans reference il n'y a aucune cle d'idempotence : on ne cree rien a l'aveugle."""
        self.assertFalse(K.est_approuve(ligne(reference="")))

    def test_insensible_a_la_casse_et_a_l_accent_final(self):
        self.assertTrue(K.est_approuve(ligne(statut="TRANSACTION APPROUVEE")))
        self.assertTrue(K.est_approuve(ligne(statut="transaction approuvée")))

    def test_statut_vide_refuse(self):
        self.assertFalse(K.est_approuve(ligne(statut="")))


class TestReference(unittest.TestCase):
    def test_le_format_est_celui_des_saisies_manuelles(self):
        """L'idempotence repose dessus : les ecritures historiques portent deja ce libelle."""
        self.assertEqual(K.reference_ecriture(ligne()), "Facture Facebook 262299")

    def test_espaces_ignores(self):
        self.assertEqual(K.reference_ecriture(ligne(reference="  927555 ")),
                         "Facture Facebook 927555")


class TestSelection(unittest.TestCase):
    """`a_comptabiliser` sans base : on injecte les lignes ET la date de depart."""

    def setUp(self):
        self._exists = K.frappe.db.exists
        K.frappe.db.exists = lambda *a, **k: False

    def tearDown(self):
        K.frappe.db.exists = self._exists

    def test_reprend_apres_la_derniere_ecriture(self):
        lignes = [ligne(reference="203935", jour=27, mois=4),
                  ligne(reference="927555", jour=18, mois=7, montant=629.202)]
        out = K.a_comptabiliser(lignes=lignes, depuis=date(2026, 7, 10))
        self.assertEqual([l["reference"] for l in out], ["927555"])

    def test_sans_date_de_depart_tout_est_candidat(self):
        lignes = [ligne(reference="203935", jour=27, mois=4), ligne(reference="927555", jour=18)]
        out = K.a_comptabiliser(lignes=lignes, depuis=None)
        self.assertEqual(len(out), 2)

    def test_les_refus_ne_sont_jamais_candidats(self):
        lignes = [ligne(reference="", statut="Solde insuffisant", jour=18, montant=314.755)]
        self.assertEqual(K.a_comptabiliser(lignes=lignes, depuis=None), [])

    def test_le_resultat_est_trie_par_date(self):
        lignes = [ligne(reference="927555", jour=18), ligne(reference="203935", jour=27, mois=4)]
        out = K.a_comptabiliser(lignes=lignes, depuis=None)
        self.assertEqual([l["reference"] for l in out], ["203935", "927555"])


class TestSelectionAvecEcritureExistante(unittest.TestCase):
    def setUp(self):
        self._exists = K.frappe.db.exists
        # Simule une ecriture deja saisie pour 262299 — le cas des 8 factures historiques.
        K.frappe.db.exists = lambda dt, f=None: bool(
            f and f.get("cheque_no") == "Facture Facebook 262299")

    def tearDown(self):
        K.frappe.db.exists = self._exists

    def test_une_ecriture_existante_ecarte_la_ligne(self):
        """L'idempotence tient au numero, pas a la date : c'est ce qui permet de relacher la
        date de depart pour un rattrapage sans risquer le doublon."""
        lignes = [ligne(reference="262299"), ligne(reference="927555", jour=18)]
        out = K.a_comptabiliser(lignes=lignes, depuis=None)
        self.assertEqual([l["reference"] for l in out], ["927555"])


class TestRegularisationDesFrais(unittest.TestCase):
    """L'ecart entre solde reel et solde comptable n'est un FRAIS que si tout est comptabilise."""

    def setUp(self):
        self._a_comptabiliser, self._ecart = K.a_comptabiliser, K.ecart_carte

    def tearDown(self):
        K.a_comptabiliser, K.ecart_carte = self._a_comptabiliser, self._ecart

    def test_operation_en_attente_bloque_la_regularisation(self):
        """LE controle du module : comptabiliser l'ecart alors qu'un paiement manque creerait une
        charge fictive ET masquerait l'operation. Deux erreurs pour le prix d'une."""
        K.a_comptabiliser = lambda *a, **k: [ligne(reference="927555", montant=629.202)]
        K.ecart_carte = lambda: {"comptable": 774.427, "reel": 144.715, "ecart": 629.712}
        r = K.sync_frais_carte(insert=False)
        self.assertEqual(r["statut"], "operations non comptabilisees")
        self.assertEqual(r["reste"], 1)

    def test_aligne_quand_l_ecart_est_nul(self):
        K.a_comptabiliser = lambda *a, **k: []
        K.ecart_carte = lambda: {"comptable": 144.715, "reel": 144.715, "ecart": 0.0}
        self.assertEqual(K.sync_frais_carte(insert=False)["statut"], "aligne")

    def test_ecart_infime_ignore(self):
        K.a_comptabiliser = lambda *a, **k: []
        K.ecart_carte = lambda: {"comptable": 144.716, "reel": 144.715, "ecart": 0.001}
        self.assertEqual(K.sync_frais_carte(insert=False)["statut"], "aligne")


class TestMontantDeLaRecharge(unittest.TestCase):
    """Le seuil DECLENCHE, il ne dimensionne pas : sous 700 DT on vire une recharge type de
    1500 DT, plafonnee par le solde disponible en banque.

    Virer seulement le manque (555 DT quand la carte est a 144) ferait replonger la carte sous le
    seuil des le premier debit Facebook — le releve reel montre des debits de 600 DT en moyenne,
    donc le meme virement serait a refaire la semaine suivante.
    """

    def setUp(self):
        self._seuil, self._montant, self._dispo, self._reel = (
            K.seuil_recharge, K.montant_recharge, K.solde_disponible_banque, K.solde_carte)
        K.seuil_recharge = lambda: 700.0
        K.montant_recharge = lambda: 1500.0
        K.solde_disponible_banque = lambda: 55922.449
        K.solde_carte = lambda: {"solde": 144.715}

    def tearDown(self):
        (K.seuil_recharge, K.montant_recharge, K.solde_disponible_banque,
         K.solde_carte) = (self._seuil, self._montant, self._dispo, self._reel)

    def test_sous_le_seuil_on_vire_la_recharge_type(self):
        r = K.recharge_a_faire()
        self.assertEqual(r["statut"], "a virer")
        self.assertEqual(r["montant"], 1500.0)
        self.assertFalse(r["plafonne"])

    def test_au_dessus_du_seuil_aucun_virement(self):
        self.assertEqual(K.recharge_a_faire(solde=1394.427)["montant"], 0.0)

    def test_pile_sur_le_seuil_ne_declenche_pas(self):
        """Le seuil est un plancher a atteindre, pas a depasser."""
        self.assertEqual(K.recharge_a_faire(solde=700.0)["statut"], "au dessus du seuil")

    def test_plafonne_par_le_solde_disponible_en_banque(self):
        """Proposer 1500 DT quand la banque en a 400, ce n'est pas une consigne : c'est un rejet
        de virement a venir."""
        K.solde_disponible_banque = lambda: 412.5
        r = K.recharge_a_faire()
        self.assertEqual(r["montant"], 412.5)
        self.assertTrue(r["plafonne"])
        self.assertEqual(r["cible"], 1500.0)

    def test_compte_a_sec_aucun_virement_proposable(self):
        K.solde_disponible_banque = lambda: 0.0
        r = K.recharge_a_faire()
        self.assertEqual(r["montant"], 0.0)
        self.assertEqual(r["statut"], "solde bancaire insuffisant")

    def test_solde_bancaire_negatif_ne_donne_pas_un_montant_negatif(self):
        """Compte a decouvert : le plafond tombe a zero, jamais sous zero — un virement negatif
        n'existe pas, et l'afficher ferait un total de tableau faux."""
        K.solde_disponible_banque = lambda: -1200.0
        self.assertEqual(K.recharge_a_faire()["montant"], 0.0)

    def test_solde_bancaire_inconnu_propose_la_recharge_pleine(self):
        """Aucune capture archivee : on propose 1500 en le disant, plutot que de taire la ligne.
        Une carte a sec est un fait ; l'ignorer faute de connaitre la banque arrete les campagnes."""
        K.solde_disponible_banque = lambda: None
        r = K.recharge_a_faire()
        self.assertEqual(r["montant"], 1500.0)
        self.assertIsNone(r["disponible"])

    def test_seuil_zero_desactive_la_proposition(self):
        K.seuil_recharge = lambda: 0.0
        self.assertEqual(K.recharge_a_faire()["statut"], "desactive")


class TestAlerteRecharge(unittest.TestCase):
    """Sous le seuil, une alerte — mais UNE SEULE PAR JOUR.

    On remplace les DEUX effets de bord du module, jamais `frappe.get_doc` ni `frappe.db.exists` :
    les patcher globalement avait casse 73 tests d'autres modules, le runner de Frappe les
    appelant lui aussi entre les tests.
    """

    def setUp(self):
        self._deja, self._poser, self._dest = (K._deja_alerte_aujourdhui, K._poser_notification,
                                               K._destinataires)
        self.poses = []
        K._deja_alerte_aujourdhui = lambda: False
        K._destinataires = lambda: ["nejib@example.com"]
        K._poser_notification = lambda u, s, m: self.poses.append({"user": u, "subject": s})

    def tearDown(self):
        K._deja_alerte_aujourdhui, K._poser_notification, K._destinataires = (
            self._deja, self._poser, self._dest)

    def test_sous_le_seuil_une_alerte_est_posee(self):
        r = K.alerte_recharge(solde=144.715, seuil=700)
        self.assertEqual(r["statut"], "alerte posee")
        self.assertTrue(self.poses)
        self.assertIn("144.715", self.poses[0]["subject"])

    def test_au_dessus_du_seuil_rien(self):
        r = K.alerte_recharge(solde=1394.427, seuil=700)
        self.assertEqual(r["statut"], "au dessus du seuil")
        self.assertEqual(self.poses, [])

    def test_seuil_zero_desactive(self):
        self.assertEqual(K.alerte_recharge(solde=0.0, seuil=0)["statut"], "desactive")
        self.assertEqual(self.poses, [])

    def test_pile_sur_le_seuil_ne_declenche_pas(self):
        """Le seuil est un plancher a atteindre, pas a depasser."""
        self.assertEqual(K.alerte_recharge(solde=700.0, seuil=700)["statut"],
                         "au dessus du seuil")


class TestAlerteNonRepetee(unittest.TestCase):
    def setUp(self):
        self._deja, self._poser, self._dest = (K._deja_alerte_aujourdhui, K._poser_notification,
                                               K._destinataires)
        self.poses = []
        K._deja_alerte_aujourdhui = lambda: True        # une alerte est deja partie aujourd'hui
        K._destinataires = lambda: ["nejib@example.com"]
        K._poser_notification = lambda u, s, m: self.poses.append({"user": u, "subject": s})

    def tearDown(self):
        K._deja_alerte_aujourdhui, K._poser_notification, K._destinataires = (
            self._deja, self._poser, self._dest)

    def test_pas_deux_alertes_le_meme_jour(self):
        """La tache tourne tous les jours et le solde reste bas jusqu'a la recharge : repeter
        l'alerte a chaque passage la rendrait invisible."""
        r = K.alerte_recharge(solde=144.715, seuil=700)
        self.assertEqual(r["statut"], "deja alerte aujourd'hui")
        self.assertEqual(self.poses, [])

    def test_force_passe_outre(self):
        r = K.alerte_recharge(solde=144.715, seuil=700, force=True)
        self.assertEqual(r["statut"], "alerte posee")
        self.assertTrue(self.poses)


class TestControleDuReleve(unittest.TestCase):
    """« Mes livres sont-ils a jour avec la carte ? » — la question que pose le rapport.

    Deux preuves doivent concorder : chaque paiement approuve porte son ecriture, ET le solde
    comptable egale le solde reel. Les tests ci-dessous verifient qu'aucune des deux ne peut a
    elle seule faire dire « a jour ».
    """

    def setUp(self):
        self._get_all, self._comptable, self._reel, self._seuil = (
            K.frappe.db.get_all, K.solde_comptable, K.solde_carte, K.seuil_recharge)
        self.ecritures = {}                     # {cheque_no: docstatus}
        K.frappe.db.get_all = lambda dt, filters=None, fields=None, **k: [
            K.frappe._dict({"name": "ACC-JV-%s" % i, "cheque_no": ref, "docstatus": st,
                            "posting_date": None})
            for i, (ref, st) in enumerate(self.ecritures.items())
            if ref in (filters or {}).get("cheque_no", ["in", []])[1]]
        K.solde_comptable = lambda *a, **k: 144.715
        K.solde_carte = lambda: {"solde": 144.715, "lu_le": "2026-08-11T19:12:24",
                                 "plafond_restant": 2755.695}
        K.seuil_recharge = lambda: 700.0

    def tearDown(self):
        (K.frappe.db.get_all, K.solde_comptable, K.solde_carte,
         K.seuil_recharge) = (self._get_all, self._comptable, self._reel, self._seuil)

    def test_tout_comptabilise_et_soldes_egaux_donne_a_jour(self):
        self.ecritures = {"Facture Facebook 927555": 1}
        d = K.etat_controle(lignes=[ligne(reference="927555", jour=18)])
        self.assertTrue(d["a_jour"])
        self.assertEqual(d["resume"]["comptabilisees"], 1)
        self.assertEqual(d["resume"]["a_comptabiliser"], 0)

    def test_un_paiement_sans_ecriture_met_les_livres_en_retard(self):
        """Le cas qui a motive le controle : deux paiements de juillet jamais saisis."""
        d = K.etat_controle(lignes=[ligne(reference="927555", jour=18, montant=629.202)])
        self.assertFalse(d["a_jour"])
        self.assertEqual(d["resume"]["a_comptabiliser"], 1)
        self.assertEqual(d["resume"]["montant_a_comptabiliser"], 629.202)
        self.assertEqual(d["lignes"][0]["etat"], K.ETAT_A_COMPTABILISER)

    def test_un_refus_n_est_ni_comptabilise_ni_manquant(self):
        """Il figure au controle — c'est en le voyant ECARTE qu'on verifie qu'il l'a bien ete."""
        d = K.etat_controle(lignes=[ligne(reference="", statut="Solde insuffisant",
                                          montant=314.755)])
        self.assertEqual(d["resume"]["refusees"], 1)
        self.assertEqual(d["resume"]["a_comptabiliser"], 0)
        self.assertEqual(d["lignes"][0]["etat"], K.ETAT_REFUSEE)
        self.assertTrue(d["a_jour"])

    def test_ecart_de_solde_sans_operation_manquante_n_est_pas_a_jour(self):
        """Rien ne manque au releve, mais les soldes divergent : c'est un frais a regulariser,
        pas un feu vert. Le rapport doit le dire en orange, pas en vert."""
        self.ecritures = {"Facture Facebook 927555": 1}
        K.solde_comptable = lambda *a, **k: 145.214
        d = K.etat_controle(lignes=[ligne(reference="927555", jour=18)])
        self.assertFalse(d["a_jour"])
        self.assertEqual(d["ecart"], 0.499)
        self.assertEqual(d["resume"]["a_comptabiliser"], 0)

    def test_service_muet_ne_fait_pas_conclure_a_jour(self):
        """Sans solde reel il n'y a qu'une preuve sur deux : on ne declare pas des livres a jour
        sur un controle qu'on n'a pas pu faire."""
        self.ecritures = {"Facture Facebook 927555": 1}

        def muet():
            raise RuntimeError("service injoignable")

        K.solde_carte = muet
        d = K.etat_controle(lignes=[ligne(reference="927555", jour=18)])
        self.assertFalse(d["a_jour"])
        self.assertIsNone(d["ecart"])
        self.assertIsNone(d["solde_reel"])
        self.assertEqual(d["resume"]["a_comptabiliser"], 0)   # le controle ligne a ligne subsiste

    def test_une_ecriture_en_brouillon_compte_mais_est_signalee(self):
        """Un brouillon suffit a l'idempotence — il ne faut pas creer un doublon — mais il n'a pas
        d'effet comptable : le taire laisserait croire que la depense est enregistree."""
        self.ecritures = {"Facture Facebook 927555": 0}
        d = K.etat_controle(lignes=[ligne(reference="927555", jour=18)])
        self.assertEqual(d["resume"]["comptabilisees"], 1)
        self.assertEqual(d["resume"]["brouillons"], 1)
        self.assertTrue(d["lignes"][0]["brouillon"])

    def test_les_lignes_sont_rendues_du_plus_recent_au_plus_ancien(self):
        d = K.etat_controle(lignes=[ligne(reference="353897", jour=21, mois=5),
                                    ligne(reference="927555", jour=18, mois=7)])
        self.assertEqual([l["reference"] for l in d["lignes"]],
                         ["Facture Facebook 927555", "Facture Facebook 353897"])


if __name__ == "__main__":
    unittest.main()
