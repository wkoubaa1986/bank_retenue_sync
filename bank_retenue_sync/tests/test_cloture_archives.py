"""Les garde-fous de l API des archives : quel verbe HTTP, quel role, quel fichier.

`supprimer_dossier` efface un ZIP du disque sans retour possible, et ce ZIP a pu etre remis au
comptable. Trois barrieres le protegent, et chacune a deja manque quelque part :

  · le VERBE — `@frappe.whitelist()` sans argument ouvre GET, POST, PUT et DELETE. Une
    destruction atteignable en NAVIGUANT vers une URL part avec la session du gestionnaire, sans
    confirmation, et hors de la protection CSRF que Frappe n applique qu aux requetes non
    idempotentes ;
  · le ROLE — lecture pour tout le monde, ecriture pour les gestionnaires seuls ;
  · le NOM DU FICHIER — sans lui, la methode devient une porte ouverte sur n importe quel fichier
    prive du site.

⚠️ SANS SITE, DONC AVEC UN FAUX `frappe`. Le module `cloture` fait `import frappe` puis
`frappe.db`, `frappe.only_for`, `frappe.delete_doc` : remplacer cet attribut de module suffit a
executer la methode pour de vrai, sans base, et a REGARDER si la suppression a eu lieu. C est ce
qui distingue ces tests d une relecture de source : ils appellent la fonction.
"""
import unittest
from unittest import mock

import frappe

from bank_retenue_sync.api import cloture

ARCHIVE = "Dossier facturation 2026-07 (20260801-1030).zip"

FICHIERS = {
    "F-DOSSIER": {"name": "F-DOSSIER", "file_name": ARCHIVE, "is_private": 1,
                  "creation": "2026-08-01 10:30:00"},
    "F-AOUT": {"name": "F-AOUT", "file_name": "Dossier facturation 2026-08 (20260901-0900).zip",
               "is_private": 1, "creation": "2026-09-01 09:00:00"},
    "F-PAIE": {"name": "F-PAIE", "file_name": "Bulletin de paie juillet.pdf", "is_private": 1,
               "creation": "2026-08-02 08:00:00"},
}


def _nu(fn):
    """La fonction sous le decorateur.

    `@frappe.whitelist` enveloppe la methode dans un validateur de types qui lit `frappe.local` —
    non lie hors site. On appelle donc la fonction enveloppee : ce qu on teste ici est son corps,
    pas la conversion des arguments d une requete HTTP.
    """
    return getattr(fn, "__wrapped__", fn)


class FauxDb:
    def __init__(self):
        self.commits = 0

    def get_value(self, doctype, name, champs, as_dict=False):
        row = FICHIERS.get(name)
        return frappe._dict(row) if row else None

    def commit(self):
        self.commits += 1


class FauxFrappe:
    """Juste ce que `supprimer_dossier` touche : les roles, la base, la suppression."""

    def __init__(self, roles):
        self.roles = set(roles)
        self.db = FauxDb()
        self.supprimes = []

    def only_for(self, roles):
        if not self.roles & set(roles):
            raise PermissionError("rôles insuffisants : %s" % sorted(self.roles))

    def throw(self, message, *args, **kwargs):
        raise ValueError(message)

    def delete_doc(self, doctype, name, **kwargs):
        self.supprimes.append((doctype, name, kwargs))


class TestVerbesHttp(unittest.TestCase):
    """Une destruction ne se declenche pas en suivant un lien."""

    def _verbes(self, fn):
        return frappe.allowed_http_methods_for_whitelisted_func[fn]

    def test_supprimer_un_dossier_n_est_atteignable_qu_en_post(self):
        self.assertEqual(self._verbes(cloture.supprimer_dossier), ["POST"])

    def test_get_put_et_delete_sont_refuses_sur_la_suppression(self):
        for verbe in ("GET", "PUT", "DELETE"):
            self.assertNotIn(verbe, self._verbes(cloture.supprimer_dossier))

    def test_le_telechargement_reste_ouvert_au_get(self):
        # Il est servi depuis un <a href> : le fermer au GET casserait le lien de l ecran.
        self.assertIn("GET", self._verbes(cloture.telecharger_dossier))

    def test_la_comparaison_ne_detruit_rien_et_reste_ouverte(self):
        self.assertIn("GET", self._verbes(cloture.comparer_archive))
        self.assertIn("POST", self._verbes(cloture.comparer_archive))


class TestSuppressionDUneArchive(unittest.TestCase):
    """Qui peut supprimer, et QUOI. Un refus doit laisser le disque intact."""

    def _supprimer(self, roles, fichier, mois="2026-07"):
        faux = FauxFrappe(roles)
        with mock.patch.object(cloture, "frappe", faux), \
                mock.patch.object(cloture, "_", lambda texte: texte):
            try:
                resultat = _nu(cloture.supprimer_dossier)(mois=mois, fichier=fichier)
            except Exception as e:
                return faux, e
        return faux, resultat

    def test_un_gestionnaire_comptable_supprime_l_archive_du_mois(self):
        faux, resultat = self._supprimer(["Accounts Manager"], "F-DOSSIER")
        self.assertEqual([(d, n) for d, n, _ in faux.supprimes], [("File", "F-DOSSIER")])
        self.assertEqual(resultat["nom_fichier"], ARCHIVE)
        self.assertEqual(faux.db.commits, 1)

    def test_la_suppression_passe_outre_les_droits_du_fichier(self):
        # L archive appartient au worker (Administrator) : sans ce drapeau, le gestionnaire qui
        # vient de la demander se voit refuser sa propre archive.
        faux, _resultat = self._supprimer(["System Manager"], "F-DOSSIER")
        self.assertTrue(faux.supprimes[0][2].get("ignore_permissions"))

    def test_un_accounts_user_ne_supprime_rien(self):
        faux, erreur = self._supprimer(["Accounts User"], "F-DOSSIER")
        self.assertIsInstance(erreur, PermissionError)
        self.assertEqual(faux.supprimes, [])
        self.assertEqual(faux.db.commits, 0)

    def test_un_role_quelconque_ne_supprime_rien(self):
        faux, erreur = self._supprimer(["Employee"], "F-DOSSIER")
        self.assertIsInstance(erreur, PermissionError)
        self.assertEqual(faux.supprimes, [])

    def test_le_role_est_verifie_avant_meme_de_lire_le_fichier(self):
        faux, erreur = self._supprimer(["Accounts User"], "F-PAIE")
        self.assertIsInstance(erreur, PermissionError)
        self.assertEqual(faux.supprimes, [])

    def test_un_fichier_hors_motif_est_refuse(self):
        faux, erreur = self._supprimer(["Accounts Manager"], "F-PAIE")
        self.assertIsInstance(erreur, ValueError)
        self.assertIn("Archive introuvable", str(erreur))
        self.assertEqual(faux.supprimes, [])

    def test_l_archive_d_un_autre_mois_est_refusee(self):
        faux, erreur = self._supprimer(["Accounts Manager"], "F-AOUT")
        self.assertIsInstance(erreur, ValueError)
        self.assertEqual(faux.supprimes, [])

    def test_un_fichier_inconnu_ou_absent_est_refuse(self):
        for fichier in ("F-INEXISTANT", None, ""):
            faux, erreur = self._supprimer(["Accounts Manager"], fichier)
            self.assertIsInstance(erreur, ValueError, "fichier %r" % (fichier,))
            self.assertEqual(faux.supprimes, [])

    def test_les_roles_d_ecriture_excluent_le_simple_utilisateur_comptable(self):
        self.assertNotIn("Accounts User", cloture.ROLES_ECRITURE)
        self.assertIn("Accounts User", cloture.ROLES_LECTURE)


if __name__ == "__main__":
    unittest.main()
