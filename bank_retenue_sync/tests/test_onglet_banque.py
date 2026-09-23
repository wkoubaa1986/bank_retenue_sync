"""L'onglet « Banque » n'est visible que des porteurs du role « Banque » (23/09/2026).

Convention : `unittest.TestCase` pur — on éprouve les RÈGLES, pas la base.
"""
from __future__ import annotations

import inspect
import json
import os
import unittest

from bank_retenue_sync import install


class TestOngletBanque(unittest.TestCase):
    def test_le_role_est_pose_a_chaque_migrate(self):
        """⚠️ L'import de `banque.json` est SAUTÉ dès que la fiche en base est plus récente que
        le fichier : seul le hook garantit la restriction en prod."""
        self.assertEqual(install.ROLE_BANQUE, "Banque")
        self.assertIn("_restreindre_onglet_banque()", inspect.getsource(install.after_migrate))

    def test_le_hook_est_idempotent(self):
        src = inspect.getsource(install._restreindre_onglet_banque)
        self.assertIn('frappe.db.exists("Role", ROLE_BANQUE)', src)
        self.assertIn("if any(r.role == ROLE_BANQUE for r in ws.roles)", src)
        # La liste des espaces est mise en cache PAR UTILISATEUR.
        self.assertIn("frappe.clear_cache()", src)

    def test_le_fichier_de_module_porte_aussi_le_role(self):
        chemin = os.path.join(os.path.dirname(install.__file__), "bank_retenue_sync",
                              "workspace", "banque", "banque.json")
        with open(chemin, encoding="utf-8") as f:
            ws = json.load(f)
        self.assertEqual([r["role"] for r in ws["roles"]], ["Banque"])

    def test_l_attribution_aux_comptes_est_un_patch_qui_ne_joue_qu_une_fois(self):
        """Une décision, pas une structure : retiré du rôle sur sa fiche, un utilisateur ne le
        retrouve pas au déploiement suivant."""
        from bank_retenue_sync.patches import restreindre_onglet_banque as p

        self.assertNotIn("UTILISATEURS", inspect.getsource(install))
        self.assertEqual(sorted(p.UTILISATEURS),
                         ["aquaworld.servicing@gmail.com", "koubaawassim@gmail.com"])
        # Décision utilisateur 23/09/2026 : pas Nizar Maddouri, malgré son rôle Accounts User.
        self.assertNotIn("aquaworld.commercial@gmail.com", p.UTILISATEURS)
        chemin = os.path.join(os.path.dirname(install.__file__), "patches.txt")
        with open(chemin, encoding="utf-8") as f:
            contenu = f.read()
        self.assertIn("bank_retenue_sync.patches.restreindre_onglet_banque\n", contenu)
        # ⚠️ Une ligne collée à la précédente = migrate de déploiement cassé.
        self.assertTrue(contenu.endswith("\n"))
