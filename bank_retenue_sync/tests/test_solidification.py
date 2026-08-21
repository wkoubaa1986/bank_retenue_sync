"""Tests de la solidification des taches planifiees (2026-08-21).

Trois defaillances constatees en prod le 2026-08-21, trois protections :
  - ordre adaptatif des fenetres d'export (`movements._ordered_windows`) — le portail en rade
    faisait bruler ~4 minutes sur 118 j puis 55 j avant la fenetre courte qui passait ;
  - le kill du scheduler rq n'est plus ravale comme un incident du service
    (`orchestrator._est_kill_scheduler`) ;
  - marqueur de derniere synchro reussie + alerte apres 24 h de panne
    (`orchestrator._marquer_synchro`).

Meme convention que les tests existants : `unittest.TestCase` pur, entrees/sorties injectees,
aucun acces reseau ; la base n'est touchee qu'a travers des mocks.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from bank_retenue_sync import orchestrator
from bank_retenue_sync.bank import movements as mv


class TestOrdreDesFenetres(unittest.TestCase):
    """L'ordre d'essai des fenetres depend de la fraicheur du registre, pas d'une constante."""

    def test_registre_frais_essaie_la_fenetre_courte_d_abord(self):
        hier = date.today() - timedelta(days=1)
        self.assertEqual(mv._ordered_windows(hier), (13, 27, 55, 118))

    def test_la_limite_de_fraicheur_est_inclusive(self):
        limite = date.today() - timedelta(days=mv._REGISTRE_FRESH_MAX_AGE)
        self.assertEqual(mv._ordered_windows(limite), (13, 27, 55, 118))

    def test_registre_vieux_garde_l_ordre_large_d_abord(self):
        vieux = date.today() - timedelta(days=mv._REGISTRE_FRESH_MAX_AGE + 1)
        self.assertEqual(mv._ordered_windows(vieux), mv._EXPORT_WINDOWS)

    def test_registre_vide_garde_l_ordre_large_d_abord(self):
        self.assertEqual(mv._ordered_windows(None), mv._EXPORT_WINDOWS)

    def test_date_illisible_ne_plante_pas_et_reste_prudente(self):
        self.assertEqual(mv._ordered_windows("n'importe quoi"), mv._EXPORT_WINDOWS)

    def test_les_deux_ordres_couvrent_les_memes_fenetres(self):
        # L'adaptation change l'ORDRE, jamais l'eventail : une regression qui perdrait une
        # fenetre passerait inapercue sans ce test.
        self.assertEqual(set(mv._ordered_windows(date.today())), set(mv._EXPORT_WINDOWS))


class TestKillScheduler(unittest.TestCase):
    """Le timeout rq doit remonter ; les erreurs applicatives restent des diagnostics."""

    def test_le_timeout_rq_est_reconnu(self):
        from rq.timeouts import JobTimeoutException
        self.assertTrue(orchestrator._est_kill_scheduler(
            JobTimeoutException("Task exceeded maximum timeout value (300 seconds)")))

    def test_une_erreur_applicative_n_est_pas_un_kill(self):
        for e in (RuntimeError("job jb_x failed"), TimeoutError("job toujours running"),
                  ValueError("reponse sans job_id")):
            self.assertFalse(orchestrator._est_kill_scheduler(e))


class TestMarquageSynchro(unittest.TestCase):
    """Succes -> horodatage pose et alerte re-armee ; panne installee -> UNE notification."""

    def test_succes_pose_l_horodatage_et_rearme_l_alerte(self):
        with patch.object(orchestrator.frappe.db, "set_single_value") as set_single:
            orchestrator._marquer_synchro(refresh_ok=True)
        (doctype, valeurs), _ = set_single.call_args
        self.assertEqual(doctype, "Bank Retenue Sync Settings")
        self.assertIn("derniere_synchro_bancaire", valeurs)
        self.assertEqual(valeurs["alerte_synchro_envoyee"], 0)

    def test_echec_recent_ne_notifie_pas(self):
        recent = orchestrator.frappe.utils.now_datetime() - timedelta(hours=2)
        valeurs = {"alerte_synchro_envoyee": 0, "derniere_synchro_bancaire": recent}
        with patch.object(orchestrator.frappe.db, "get_single_value",
                          side_effect=lambda dt, f: valeurs[f]), \
             patch.object(orchestrator.frappe.db, "set_single_value") as set_single, \
             patch.object(orchestrator.frappe, "get_doc") as get_doc:
            orchestrator._marquer_synchro(refresh_ok=False)
        get_doc.assert_not_called()
        set_single.assert_not_called()

    def test_panne_installee_notifie_une_fois_et_pose_le_drapeau(self):
        vieux = orchestrator.frappe.utils.now_datetime() - timedelta(hours=30)
        valeurs = {"alerte_synchro_envoyee": 0, "derniere_synchro_bancaire": vieux}
        doc = MagicMock()
        with patch.object(orchestrator.frappe.db, "get_single_value",
                          side_effect=lambda dt, f: valeurs[f]), \
             patch.object(orchestrator.frappe.db, "set_single_value") as set_single, \
             patch.object(orchestrator.frappe, "get_doc", return_value=doc) as get_doc, \
             patch.object(orchestrator, "_system_managers",
                          return_value=["comptable@exemple.tn"]):
            orchestrator._marquer_synchro(refresh_ok=False)
        get_doc.assert_called_once()
        self.assertEqual(get_doc.call_args[0][0]["doctype"], "Notification Log")
        self.assertEqual(get_doc.call_args[0][0]["for_user"], "comptable@exemple.tn")
        doc.insert.assert_called_once_with(ignore_permissions=True)
        set_single.assert_called_once_with(
            "Bank Retenue Sync Settings", "alerte_synchro_envoyee", 1)

    def test_le_drapeau_deja_pose_coupe_toute_nouvelle_notification(self):
        with patch.object(orchestrator.frappe.db, "get_single_value", return_value=1), \
             patch.object(orchestrator.frappe, "get_doc") as get_doc:
            orchestrator._marquer_synchro(refresh_ok=False)
        get_doc.assert_not_called()

    def test_aucune_synchro_connue_notifie_aussi(self):
        # Premiere installation ou champ jamais rempli : une panne sans historique doit alerter,
        # pas attendre un succes qui ne viendra peut-etre jamais.
        valeurs = {"alerte_synchro_envoyee": 0, "derniere_synchro_bancaire": None}
        doc = MagicMock()
        with patch.object(orchestrator.frappe.db, "get_single_value",
                          side_effect=lambda dt, f: valeurs[f]), \
             patch.object(orchestrator.frappe.db, "set_single_value"), \
             patch.object(orchestrator.frappe, "get_doc", return_value=doc), \
             patch.object(orchestrator, "_system_managers", return_value=["u@x.tn"]):
            orchestrator._marquer_synchro(refresh_ok=False)
        doc.insert.assert_called_once()

    def test_jamais_bloquant(self):
        # Une erreur du marquage ne doit jamais faire echouer la verification elle-meme.
        with patch.object(orchestrator.frappe.db, "set_single_value",
                          side_effect=RuntimeError("base indisponible")), \
             patch.object(orchestrator.frappe, "log_error") as log_error:
            orchestrator._marquer_synchro(refresh_ok=True)   # ne doit pas lever
        log_error.assert_called_once()


class TestMouvementsGeres(unittest.TestCase):
    """Les flux de CREATION ne remontent jamais avant `periode_debut_gestion` : les mois
    anterieurs sont equilibres a la main (cas reel ACC-JV-2026-00621 : versement d'especes du
    31/03 entre par backfill, ecriture de rattrapage creee en mars — periode deja arbitree)."""

    def _mvts(self):
        return [{"date": date(2026, 3, 31), "credit": 1150.0},     # avant le plancher
                {"date": date(2026, 6, 30), "credit": 10.0},       # veille du plancher
                {"date": date(2026, 7, 1), "credit": 20.0},        # premier jour gere
                {"date": date(2026, 8, 20), "credit": 1812.7}]

    def test_le_plancher_ecarte_les_mois_anterieurs(self):
        with patch.object(orchestrator, "_periode_debut_gestion", return_value="2026-07"):
            garde = orchestrator._movements_geres(self._mvts())
        self.assertEqual([str(m["date"]) for m in garde], ["2026-07-01", "2026-08-20"])

    def test_sans_plancher_rien_n_est_filtre(self):
        with patch.object(orchestrator, "_periode_debut_gestion", return_value=""):
            self.assertEqual(len(orchestrator._movements_geres(self._mvts())), 4)

    def test_plancher_illisible_ne_filtre_rien(self):
        # Mieux vaut le comportement historique qu'un registre vide en silence.
        with patch.object(orchestrator, "_periode_debut_gestion", return_value="n'importe"):
            self.assertEqual(len(orchestrator._movements_geres(self._mvts())), 4)

    def test_mouvement_sans_date_est_ecarte_par_prudence(self):
        with patch.object(orchestrator, "_periode_debut_gestion", return_value="2026-07"):
            self.assertEqual(orchestrator._movements_geres([{"credit": 5.0}]), [])


class TestSoldeDerive(unittest.TestCase):
    """Capture en panne mais mouvements extraits -> le solde du jour se DEDUIT de la derniere
    capture + le net du registre depuis sa derniere operation (demande utilisateur 2026-08-21)."""

    @staticmethod
    def _ancre(**kw):
        import frappe
        base = {"name": "SOLDE-2026-00255", "solde_banque": 54100.454,
                "date_solde": date(2026, 8, 20), "derniere_operation": date(2026, 8, 20)}
        base.update(kw)
        return frappe._dict(base)

    def test_calcul_depuis_la_derniere_operation(self):
        from bank_retenue_sync.bank import solde as S
        flux = {"credits": 2000.0, "debits": 187.3, "net": 1812.7, "mouvements": 3}
        with patch.object(S, "dernier_solde", return_value=self._ancre()), \
             patch.object(S, "flux_registre", return_value=flux) as fr, \
             patch.object(S.frappe.db, "get_all",
                          return_value=[{"date": date(2026, 8, 21)}]):
            out = S.solde_derive()
        self.assertAlmostEqual(out["solde_calcule"], 54100.454 + 1812.7, places=3)
        self.assertEqual(out["ancre"], "derniere_operation")
        self.assertEqual(out["mouvements_ajoutes"], 3)
        # STRICTEMENT posterieur a la derniere operation : elle est deja DANS le solde capture.
        self.assertEqual(str(fr.call_args.kwargs["date_min"]), "2026-08-21")

    def test_repli_sur_date_solde_annonce_son_hypothese(self):
        from bank_retenue_sync.bank import solde as S
        ancre = self._ancre(derniere_operation=None)
        with patch.object(S, "dernier_solde", return_value=ancre), \
             patch.object(S, "flux_registre",
                          return_value={"credits": 0, "debits": 0, "net": 0, "mouvements": 0}), \
             patch.object(S.frappe.db, "get_all", return_value=[]):
            out = S.solde_derive()
        self.assertEqual(out["ancre"], "date_solde")

    def test_sans_aucune_capture_on_ne_fabrique_rien(self):
        from bank_retenue_sync.bank import solde as S
        with patch.object(S, "dernier_solde", return_value=None):
            self.assertIsNone(S.solde_derive())

    def test_capture_sans_aucune_date_on_ne_fabrique_rien(self):
        from bank_retenue_sync.bank import solde as S
        ancre = self._ancre(derniere_operation=None, date_solde=None)
        with patch.object(S, "dernier_solde", return_value=ancre):
            self.assertIsNone(S.solde_derive())

    def test_registre_sans_nouveaute_rend_le_solde_capture(self):
        from bank_retenue_sync.bank import solde as S
        with patch.object(S, "dernier_solde", return_value=self._ancre()), \
             patch.object(S, "flux_registre",
                          return_value={"credits": 0, "debits": 0, "net": 0, "mouvements": 0}), \
             patch.object(S.frappe.db, "get_all", return_value=[]):
            out = S.solde_derive()
        self.assertAlmostEqual(out["solde_calcule"], 54100.454, places=3)
        self.assertEqual(out["mouvements_ajoutes"], 0)


class TestRechargeCarteChaineLaCarte(unittest.TestCase):
    """Un chargement de carte importe par le run declenche la mise a jour de la carte —
    detection par `premiere_vue`, pas par les cles de l'upsert (qui contiennent aussi les
    mouvements simplement revus : une vieille recharge re-vue redeclencherait pour rien)."""

    def test_detection_sur_les_seuls_mouvements_nouveaux(self):
        depuis = orchestrator.frappe.utils.now_datetime()
        with patch.object(orchestrator.frappe.db, "exists", return_value="une-cle") as exists:
            self.assertTrue(orchestrator._recharge_carte_importee(depuis))
        filtres = exists.call_args[0][1]
        self.assertEqual(filtres["regle"], "chargement_carte")
        self.assertEqual(filtres["premiere_vue"], [">=", depuis])

    def test_aucune_recharge_aucun_declenchement(self):
        with patch.object(orchestrator.frappe.db, "exists", return_value=None):
            self.assertFalse(orchestrator._recharge_carte_importee(
                orchestrator.frappe.utils.now_datetime()))


class TestExportMouvementsOk(unittest.TestCase):
    """L'alerte surveille l'export des MOUVEMENTS ; les remises seules ne declenchent rien."""

    def test_ok_nominal(self):
        self.assertTrue(orchestrator._export_mouvements_ok("ok"))

    def test_echec_des_seules_remises_ne_compte_pas(self):
        diag = [{"type": "refresh", "source": "remises", "reason": "Champ identifiant introuvable"}]
        self.assertTrue(orchestrator._export_mouvements_ok(diag))

    def test_echec_des_mouvements_compte(self):
        diag = [{"type": "refresh", "source": "mouvements", "reason": "page de connexion"}]
        self.assertFalse(orchestrator._export_mouvements_ok(diag))

    def test_export_illisible_compte(self):
        self.assertFalse(orchestrator._export_mouvements_ok("export illisible : ..."))

    def test_refresh_jamais_tente(self):
        self.assertFalse(orchestrator._export_mouvements_ok(None))


class TestDispatch(unittest.TestCase):
    """Le tick planifie ne travaille pas : il met le job en file sur la queue longue."""

    def test_le_tick_enqueue_sur_la_queue_longue_avec_dedup(self):
        from bank_retenue_sync.tasks import daily
        with patch.object(daily, "_enabled", return_value=True), \
             patch.object(daily.frappe, "enqueue") as enqueue:
            daily.verification_bancaire()
        enqueue.assert_called_once()
        args, kwargs = enqueue.call_args
        self.assertEqual(args[0],
                         "bank_retenue_sync.tasks.daily.verification_bancaire_job")
        self.assertEqual(kwargs["queue"], "long")
        self.assertGreaterEqual(kwargs["timeout"], 3600)
        self.assertTrue(kwargs["deduplicate"])
        self.assertEqual(kwargs["job_id"], "brs-verification_bancaire")

    def test_coupe_circuit_desactive_aucune_mise_en_file(self):
        from bank_retenue_sync.tasks import daily
        with patch.object(daily, "_enabled", return_value=False), \
             patch.object(daily.frappe, "enqueue") as enqueue:
            self.assertIsNone(daily.verification_bancaire())
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
