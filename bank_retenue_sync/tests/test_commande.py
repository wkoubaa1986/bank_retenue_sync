"""Tests du regroupement des lignes d'une commande d'achat.

Convention de l'app : `unittest.TestCase` pur, donnees injectees, aucun acces reseau ni base.
Les lignes ressemblent a celles d'une commande d'import : meme article commande plusieurs fois.
"""
from __future__ import annotations

import unittest

from bank_retenue_sync.achat import commande as C


def ligne(name, item_code, qty, rate=10.0, uom="Nos"):
    return {"name": name, "item_code": item_code, "qty": qty, "rate": rate, "uom": uom}


class TestRegroupement(unittest.TestCase):
    def test_deux_lignes_du_meme_article_n_en_font_plus_qu_une(self):
        res = C.regrouper([ligne("a", "ART-1", 3), ligne("b", "ART-1", 5)])
        self.assertEqual(res["supprimer"], ["b"])
        self.assertEqual(res["doublons"], 1)
        self.assertEqual([(l["name"], l["qty"]) for l in res["conserver"]], [("a", 8.0)])
        self.assertEqual(res["conserver"][0]["fusionnees"], 2)

    def test_la_ligne_conservee_est_la_premiere_et_garde_sa_place(self):
        """Elle garde sa position, donc son entrepot, sa date de reception et sa description."""
        res = C.regrouper([ligne("a", "ART-1", 1), ligne("b", "ART-2", 2), ligne("c", "ART-1", 4)])
        self.assertEqual([l["name"] for l in res["conserver"]], ["a", "b"])
        self.assertEqual([l["qty"] for l in res["conserver"]], [5.0, 2.0])
        self.assertEqual(res["supprimer"], ["c"])

    def test_des_articles_distincts_restent_intacts_et_dans_l_ordre(self):
        res = C.regrouper([ligne("a", "ART-1", 1), ligne("b", "ART-2", 2), ligne("c", "ART-3", 3)])
        self.assertEqual(res["doublons"], 0)
        self.assertEqual(res["supprimer"], [])
        self.assertEqual([l["name"] for l in res["conserver"]], ["a", "b", "c"])
        self.assertTrue(all(l["fusionnees"] == 1 for l in res["conserver"]))
        self.assertEqual(res["non_fusionnees"], [])

    def test_plusieurs_groupes_se_fusionnent_du_meme_coup(self):
        res = C.regrouper([ligne("a", "ART-1", 1), ligne("b", "ART-2", 2), ligne("c", "ART-1", 1),
                           ligne("d", "ART-2", 3), ligne("e", "ART-1", 1)])
        self.assertEqual(res["doublons"], 3)
        self.assertEqual(sorted(res["supprimer"]), ["c", "d", "e"])
        self.assertEqual([(l["name"], l["qty"]) for l in res["conserver"]],
                         [("a", 3.0), ("b", 5.0)])

    def test_une_commande_sans_ligne_ne_fait_rien(self):
        for vide in ([], None):
            res = C.regrouper(vide)
            self.assertEqual(res["doublons"], 0)
            self.assertEqual(res["conserver"], [])
            self.assertEqual(res["non_fusionnees"], [])

    def test_les_quantites_decimales_se_somment_au_millieme(self):
        """⚠️ Additionner des flottants sans arrondir ecrit 2.9999999999999996 dans la case."""
        res = C.regrouper([ligne("a", "ART-1", 0.1), ligne("b", "ART-1", 0.2)])
        self.assertEqual(res["conserver"][0]["qty"], 0.3)

    def test_une_ligne_sans_article_n_est_jamais_regroupee(self):
        res = C.regrouper([ligne("a", None, 1), ligne("b", "", 2), ligne("c", None, 3)])
        self.assertEqual(res["doublons"], 0)
        self.assertEqual([l["name"] for l in res["conserver"]], ["a", "b", "c"])


class TestCeQuiNeSeFusionnePas(unittest.TestCase):
    """⚠️ Sommer deux lignes du meme article a deux prix differents inventerait un prix qui n'a ete
    convenu avec personne. Elles restent, et le resultat le SIGNALE."""

    def test_deux_prix_differents_ne_se_fusionnent_pas(self):
        res = C.regrouper([ligne("a", "ART-1", 1, rate=10), ligne("b", "ART-1", 2, rate=12)])
        self.assertEqual(res["doublons"], 0)
        self.assertEqual([l["name"] for l in res["conserver"]], ["a", "b"])
        self.assertEqual(res["non_fusionnees"],
                         [{"item_code": "ART-1", "lignes": 2, "motif": "prix"}])

    def test_deux_unites_differentes_ne_se_fusionnent_pas(self):
        res = C.regrouper([ligne("a", "ART-1", 1, uom="Nos"), ligne("b", "ART-1", 2, uom="Box")])
        self.assertEqual(res["doublons"], 0)
        self.assertEqual(res["non_fusionnees"],
                         [{"item_code": "ART-1", "lignes": 2, "motif": "unite"}])

    def test_prix_et_unite_differents_se_disent_tous_les_deux(self):
        res = C.regrouper([ligne("a", "ART-1", 1, rate=10, uom="Nos"),
                           ligne("b", "ART-1", 2, rate=12, uom="Box")])
        self.assertEqual(res["non_fusionnees"],
                         [{"item_code": "ART-1", "lignes": 2, "motif": "prix et unite"}])

    def test_on_fusionne_ce_qui_se_fusionne_et_on_signale_le_reste(self):
        """Trois lignes, deux au meme prix : celles-la se regroupent, la troisieme est signalee."""
        res = C.regrouper([ligne("a", "ART-1", 1, rate=10), ligne("b", "ART-1", 2, rate=10),
                           ligne("c", "ART-1", 4, rate=12)])
        self.assertEqual(res["supprimer"], ["b"])
        self.assertEqual([(l["name"], l["qty"]) for l in res["conserver"]], [("a", 3.0), ("c", 4.0)])
        self.assertEqual(res["non_fusionnees"],
                         [{"item_code": "ART-1", "lignes": 2, "motif": "prix"}])

    def test_un_prix_identique_au_millieme_pres_reste_un_doublon(self):
        """Deux saisies identiques peuvent differer au quinzieme chiffre apres la virgule."""
        res = C.regrouper([ligne("a", "ART-1", 1, rate=0.1 + 0.2), ligne("b", "ART-1", 2, rate=0.3)])
        self.assertEqual(res["doublons"], 1)


if __name__ == "__main__":
    unittest.main()
