"""Collecte des bordereaux de remise : lecture du nom du fichier, numero, nom du document ; lecture par
l'IA (mise en forme de la reponse, recadrage du scan) et recherche des pieces dans ERPNext. Pur."""
import unittest

from bank_retenue_sync.ai import bordereau_extract as X
from bank_retenue_sync.api import bordereaux as B


class TestNomDuFichier(unittest.TestCase):
    def test_facon_de_ranger_de_l_entreprise(self):
        self.assertEqual(B.lire_nom_fichier("Ordre de versement Chèques  Bq BIAT-7473685-du 09-11-2026.pdf"),
                         {"banque": "BIAT", "nature": "Chèques", "numero": "7473685", "date_remise": "2026-11-09"})
        self.assertEqual(B.lire_nom_fichier("Ordre de versement chèque Bq Zitouna-900285827-du 20-10-20267c37c5.pdf"),
                         {"banque": "Zitouna", "nature": "Chèques", "numero": "900285827", "date_remise": "2026-10-20"})   # suffixe colle par Frappe
        self.assertEqual(B.lire_nom_fichier("Ordre de versement Traites EGES - Bq Zitouna-2702065-du 17-08-2026.pdf"),
                         {"banque": "Zitouna", "nature": "Traites", "numero": "2702065", "date_remise": "2026-08-17"})
        self.assertEqual(B.lire_nom_fichier("Ordre de versement Espèce Bq Zitouna-3334383-du 12-10-2026.pdf")["nature"], "Espèces")

    def test_nom_quelconque(self):
        self.assertEqual(B.lire_nom_fichier("scan0001.pdf"), {})
        self.assertEqual(B.lire_nom_fichier(""), {})
        self.assertEqual(B.lire_nom_fichier(None), {})
        self.assertEqual(B.lire_nom_fichier("remise BIAT 7473932.jpg"), {"banque": "BIAT", "numero": "7473932"})
        self.assertEqual(B.lire_nom_fichier("bordereau du 45-13-2026.pdf"), {})                # date impossible : pas lue

    def test_numero_et_nom_du_document(self):
        self.assertEqual(B.numero_propre(" 7 473 685 "), "7473685")
        self.assertEqual(B.nom_du_bordereau("BIAT", "7473685"), "BR-BIAT-7473685")
        self.assertEqual(B.nom_du_bordereau("Zitouna", "900 285 827"), "BR-Zitouna-900285827")
        for mauvais in ("", "12", "abc", None):
            with self.assertRaises(ValueError):
                B.numero_propre(mauvais)
        with self.assertRaises(ValueError):
            B.nom_du_bordereau("Attijari", "7473685")


def _pe(name, numero, montant, client="Client", **plus):
    return {"name": name, "numero": numero, "montant": montant, "client": client, **plus}


class TestRapprochement(unittest.TestCase):
    """Le cas reel du 16/11/2026 : bordereau BIAT 7473685, trois cheques, 3 010,127."""

    PORTEFEUILLE = [
        _pe("PE-763", "4001763", 798.0, "Ideale eau"),          # le cheque precedent du meme carnet
        _pe("PE-767", "4001767", 270.0, "Ideale eau"),
        _pe("PE-066", "4001066", 1045.0, "FM WATER PLUS"),
        _pe("PE-167", "8965167", 1694.88, "Agro Techno Service"),
        _pe("PE-088", "0000088", 581.0, "Adel Krichen"),
    ]

    def test_bordereau_reel(self):
        lues = [{"numero": "4001764", "montant": 270.0}, {"numero": "4001066", "montant": 1045.0},
                {"numero": "8965167", "montant": 1695.127}]
        r = B.rapprocher(lues, self.PORTEFEUILLE)
        # Un chiffre mal forme sur le papier : meme montant -> c'est bien ce cheque, et pas son voisin de carnet.
        self.assertEqual((r[0]["etat"], r[0]["paiement"]), ("Trouvé", "PE-767"))
        self.assertIn("lu 4001764, ERPNext 4001767", r[0]["remarque"])
        self.assertEqual((r[1]["etat"], r[1]["paiement"], r[1]["remarque"]), ("Trouvé", "PE-066", ""))
        # Le bon numero, 0,247 d'ecart : un arrondi (cheque sur le total exact, paiement sur le total arrondi).
        # Trouve, l'ecart est note pour qui controle — et rien n'est signale a la personne qui remet.
        self.assertEqual((r[2]["etat"], r[2]["paiement"], r[2]["montant_erp"]), ("Trouvé", "PE-167", 1694.88))
        self.assertIn("toléré", r[2]["remarque"])
        self.assertIn("écart 0,247", r[2]["remarque"])
        etat = B.bilan(r, {"numero": "7473685", "total": 3010.127, "nombre": 3}, {"numero": "7473685"})
        self.assertEqual(etat["resume"], "3 chèques : tous trouvés")
        self.assertEqual(etat["signalements"], [])                        # tout est bon
        self.assertEqual(len(etat["remarques"]), 1)                       # l'ecart de total, pour le controle
        self.assertIn("3 009,880 dans ERPNext, le bordereau 3 010,127 : écart de 0,247", etat["remarques"][0])

    def test_ce_qui_est_signale_a_la_personne_qui_remet(self):
        lues = [{"numero": "4001767", "montant": 275.0},                  # 5 DT de trop : ce n'est plus un arrondi
                {"numero": "5555555", "montant": 12.0},                   # inconnu
                {"numero": "4001066", "montant": 1045.0}]
        r = B.rapprocher(lues, self.PORTEFEUILLE)
        self.assertEqual([p["etat"] for p in r], ["À vérifier", "Introuvable", "Trouvé"])
        etat = B.bilan(r, {"total": 1400.0, "nombre": 4})
        self.assertEqual(etat["signalements"], [
            "Chèque n° 4001767 : 275,000 sur le bordereau, 270,000 dans ERPNext.",
            "Chèque n° 5555555 (12,000) : pas trouvé dans ERPNext.",
            "Le total écrit sur le bordereau (1 400,000) ne correspond pas à la somme des montants (1 332,000).",
            "Le bordereau annonce 4 pièce(s), 3 sont écrites."])
        self.assertEqual(B.bilan([])["signalements"], ["Aucune pièce lue sur ce bordereau."])
        par_montant = B.bilan(B.rapprocher([{"numero": "1234567", "montant": 581.0}], self.PORTEFEUILLE))
        self.assertTrue(par_montant["signalements"][0].startswith("Chèque n° 1234567 : à vérifier (aucun paiement ne porte"))
        self.assertTrue(B.bilan(B.rapprocher([{"numero": "9", "montant": 1.0}], []), nature="Traites")["signalements"][0]
                        .startswith("Traite n° 9 (1,000)"))

    def test_tous_trouves_et_zeros_de_tete(self):
        r = B.rapprocher([{"numero": "88", "montant": 581.0}, {"numero": "04001066", "montant": 1045.0}], self.PORTEFEUILLE)
        self.assertEqual([p["paiement"] for p in r], ["PE-088", "PE-066"])
        self.assertEqual(B.bilan(r, {"total": 1626.0, "nombre": 2})["resume"], "2 chèques : tous trouvés")
        self.assertEqual(B.bilan(r[:1])["resume"], "1 chèque : trouvé")
        self.assertEqual(B.bilan(r, nature="Traites")["resume"], "2 traites : toutes trouvées")
        self.assertEqual(B.bilan([])["resume"], "aucune pièce lue sur le scan")

    def test_un_paiement_ne_sert_qu_une_fois(self):
        # Deux lignes lues pour un seul paiement : la seconde ne le reprend pas.
        r = B.rapprocher([{"numero": "4001066", "montant": 1045.0}, {"numero": "4001066", "montant": 1045.0}], self.PORTEFEUILLE)
        self.assertEqual([p["etat"] for p in r], ["Trouvé", "Introuvable"])
        # Le numero exact est servi AVANT le numero voisin, meme si la ligne voisine vient en premier.
        deux = [_pe("A", "4001764", 270.0), _pe("B", "4001767", 270.0)]
        r = B.rapprocher([{"numero": "4001767", "montant": 270.0}, {"numero": "4001764", "montant": 270.0}], deux)
        self.assertEqual([p["paiement"] for p in r], ["B", "A"])

    def test_par_le_montant_seul(self):
        r = B.rapprocher([{"numero": "1234567", "montant": 581.0}], self.PORTEFEUILLE)
        self.assertEqual((r[0]["etat"], r[0]["paiement"]), ("À vérifier", "PE-088"))
        self.assertIn("un seul du portefeuille a ce montant", r[0]["remarque"])
        # Deux paiements au meme montant : on ne choisit pas.
        deux = [_pe("A", "1111111", 100.0), _pe("B", "2222222", 100.0)]
        r = B.rapprocher([{"numero": "9999999", "montant": 100.0}], deux)
        self.assertEqual((r[0]["etat"], r[0]["paiement"]), ("À vérifier", None))
        self.assertIn("plusieurs ont ce montant", r[0]["remarque"])

    def test_introuvable_et_deja_sorti_du_portefeuille(self):
        r = B.rapprocher([{"numero": "5555555", "montant": 12.0}], self.PORTEFEUILLE)
        self.assertEqual((r[0]["etat"], r[0]["paiement"]), ("Introuvable", None))
        ailleurs = [_pe("PE-BQ", "5555555", 12.0, compte="BIAT - A&S")]
        r = B.rapprocher([{"numero": "5555555", "montant": 12.0}], self.PORTEFEUILLE, ailleurs)
        self.assertEqual((r[0]["etat"], r[0]["paiement"]), ("Trouvé", "PE-BQ"))
        self.assertIn("plus dans le portefeuille : paiement sur « BIAT - A&S »", r[0]["remarque"])
        # Deja sur un encaissement : trouve, et dit.
        r = B.rapprocher([{"numero": "88", "montant": 581.0}], [_pe("PE-088", "0000088", 581.0, en_encaissement=True)])
        self.assertEqual(r[0]["remarque"], "déjà porté sur un encaissement")

    def test_voisin_de_carnet_non_confondu(self):
        # 4001764 lu pour 500 : ni le 4001763 (798) ni le 4001767 (270) ne sont « proches ».
        r = B.rapprocher([{"numero": "4001764", "montant": 500.0}], self.PORTEFEUILLE)
        self.assertEqual(r[0]["etat"], "Introuvable")

    def test_numeros_voisins(self):
        self.assertTrue(B.numeros_voisins("4001764", "4001767"))
        self.assertTrue(B.numeros_voisins("40001767", "4001767"))            # un chiffre en trop
        self.assertTrue(B.numeros_voisins("0001259", "1250"))              # les zeros de tete ne comptent pas
        self.assertTrue(B.numeros_voisins("0000088", "89"))
        self.assertFalse(B.numeros_voisins("4001764", "4001764"))
        self.assertFalse(B.numeros_voisins("4001764", "4001777"))
        self.assertFalse(B.numeros_voisins("123", "124"))
        self.assertFalse(B.numeros_voisins("", "4001764"))

    def test_remarques_du_bilan(self):
        r = B.rapprocher([{"numero": "4001066", "montant": 1045.0, "doute": True}, {"numero": "7", "montant": 3.0, "doute": True}],
                         self.PORTEFEUILLE)
        etat = B.bilan(r, {"numero": "7473685", "total": 1050.0, "nombre": 3}, {"numero": "7473933", "montant_total": 1048.0})
        texte = " | ".join(etat["remarques"])
        self.assertEqual(etat["resume"], "2 chèques : 1 trouvé, 1 introuvable")
        self.assertIn("La somme des montants lus (1 048,000) ne tombe pas sur le total écrit sur le bordereau (1 050,000)", texte)
        self.assertIn("annonce 3 pièce(s), 2 lue(s)", texte)
        self.assertIn("(7473685) n’est pas celui saisi (7473933)", texte)
        self.assertIn("Montant total saisi : 1 048,000 ; lu sur le scan : 1 050,000", texte)
        self.assertIn("L’IA hésite", texte)


class TestIdentite(unittest.TestCase):
    """Le depot : on donne le scan, la banque, le numero, la nature et la date en sortent seuls."""

    LU = {"banque_remise": "BIAT", "nature": "Chèques", "numero": "7473685", "date_remise": "2026-11-09"}

    def test_photo_de_telephone(self):
        # Le nom d'une photo ne dit rien — et surtout pas le numero du bordereau.
        indices = B.lire_nom_fichier("IMG_20261116_101500.jpg")
        q = B.identite(indices, self.LU, "2026-11-16")
        self.assertEqual((q["banque"], q["numero"], q["nature"], q["date_remise"]), ("BIAT", "7473685", "Chèques", "2026-11-09"))
        self.assertEqual((q["manque"], q["date_source"]), ([], "scan"))

    def test_nom_range_a_la_facon_de_l_entreprise(self):
        indices = B.lire_nom_fichier("Ordre de versement Traites EGES - Bq Zitouna-2702065-du 17-08-2026.pdf")
        q = B.identite(indices, {"banque_remise": None, "nature": None, "numero": "", "date_remise": None}, "2026-11-16")
        self.assertEqual((q["banque"], q["numero"], q["nature"], q["date_remise"], q["date_source"], q["manque"]),
                         ("Zitouna", "2702065", "Traites", "2026-08-17", "nom", []))

    def test_ce_qui_manque_est_demande(self):
        q = B.identite({}, {"banque_remise": None, "numero": "12", "date_remise": None}, "2026-11-16")
        self.assertEqual(sorted(q["manque"]), ["banque", "numero"])
        self.assertEqual((q["nature"], q["date_remise"]), ("Chèques", "2026-11-16"))
        # Le nom du fichier et le scan ne disent pas le meme numero : on ne tranche pas.
        indices = B.lire_nom_fichier("Ordre de versement Chèques Bq BIAT-7473933-du 09-11-2026.pdf")
        q = B.identite(indices, self.LU, "2026-11-16")
        self.assertEqual(q["manque"], ["numero"])
        self.assertIn("7473933", q["conflit"])
        self.assertIn("7473685", q["conflit"])

    def test_date_lue_invraisemblable(self):
        for lue in ("2016-11-09", "2026-12-25", "2026-08-01", None):          # annee mal lue, futur, trop vieux
            q = B.identite({}, dict(self.LU, date_remise=lue), "2026-11-16")
            self.assertEqual((q["date_remise"], q["date_source"]), ("2026-11-16", "jour"), lue)


class TestEchec(unittest.TestCase):
    def test_message_pour_la_caisse(self):
        self.assertIn("introuvable sur le serveur", B.message_d_echec(FileNotFoundError(2, "No such file")))
        try:
            import json
            json.loads("pas du json")
        except ValueError as e:
            self.assertIn("réponse exploitable", B.message_d_echec(e))
        self.assertIn("n’a pas répondu", B.message_d_echec(type("APITimeoutError", (Exception,), {})("x")))
        self.assertIn("clé OpenAI", B.message_d_echec(type("AuthenticationError", (Exception,), {})("x")))
        self.assertEqual(B.message_d_echec(ValueError("Format non lisible par l’IA : <b>PDF</b>")),
                         "Format non lisible par l’IA : PDF")


class TestLectureIA(unittest.TestCase):
    def test_normaliser(self):
        lu = X.normaliser({"numero": "N° 7473685", "date_remise": "2026-11-09", "total": "3.010,127", "nombre": "3", "pieces": [
            {"numero": "4001 767", "banque": "Zitouna", "tireur": "Ideal eau", "montant": "270,000", "doute": True},
            {"numero": "4001066", "montant": 1045},
            {"numero": "", "montant": None},                       # ligne vide du tableau
            "n'importe quoi",
            {"numero": "8965167", "banque": None, "montant": 1695.127, "echeance": "09/11/2026"}]})
        self.assertEqual((lu["numero"], lu["date_remise"], lu["total"], lu["nombre"]), ("7473685", "2026-11-09", 3010.127, 3))
        self.assertEqual([(p["numero"], p["montant"], p["doute"]) for p in lu["pieces"]],
                         [("4001767", 270.0, True), ("4001066", 1045.0, False), ("8965167", 1695.127, False)])
        self.assertIsNone(lu["pieces"][2]["echeance"])               # date mal formee : ecartee
        self.assertTrue(lu["total_concorde"])
        self.assertEqual(X.normaliser(None)["pieces"], [])
        self.assertEqual((X.normaliser(None)["banque_remise"], X.normaliser(None)["nature"]), (None, None))
        for ecrit, banque in (("BIAT", "BIAT"), ("Banque Zitouna", "Zitouna"), ("بنك الزيتونة / Banque ZITOUNA", "Zitouna"), ("BNA", None)):
            self.assertEqual(X.normaliser({"banque_remise": ecrit})["banque_remise"], banque)
        for ecrit, nature in (("cheques", "Chèques"), ("Remise de chèques", "Chèques"), ("traites", "Traites"),
                              ("remise d'effets", "Traites"), ("especes", "Espèces"), ("?", None)):
            self.assertEqual(X.normaliser({"nature": ecrit})["nature"], nature)
        self.assertFalse(X.normaliser({"total": 10, "pieces": [{"numero": "1234567", "montant": 9}]})["total_concorde"])

    def test_json_du_modele(self):
        self.assertEqual(X.json_du_modele('```json\n{"numero": "1"}\n```'), {"numero": "1"})
        self.assertEqual(X.json_du_modele(' {"numero": "1"} '), {"numero": "1"})

    def test_zone_utile(self):
        from PIL import Image, ImageDraw
        page = Image.new("RGB", (2480, 3500), "white")
        dessin = ImageDraw.Draw(page)
        dessin.rectangle((120, 200, 2300, 1300), fill=(255, 170, 210))        # le bordereau, rose
        dessin.rectangle((0, 0, 30, 3500), fill=(200, 200, 200))             # l'ombre de la reliure
        dessin.ellipse((900, 2900, 930, 2930), fill="red")                    # une tache
        zone = X.zone_utile(page)
        self.assertLess(zone.height, 1300)
        self.assertGreater(zone.height, 1000)
        self.assertGreater(zone.width, 2100)
        # Rien de net (page blanche, photo pleine) : l'image est rendue entiere.
        self.assertEqual(X.zone_utile(Image.new("RGB", (800, 600), "white")).size, (800, 600))
        self.assertEqual(X.zone_utile(Image.new("RGB", (800, 600), (90, 80, 70))).size, (800, 600))


if __name__ == "__main__":
    unittest.main()
