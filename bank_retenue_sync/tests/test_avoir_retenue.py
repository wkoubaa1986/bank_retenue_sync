"""L'avoir ne d'une retenue a la source (17/11/2026) : ce que la regularisation note sur le certificat.
Fonction pure : aucun site."""
import unittest

from bank_retenue_sync.tej import paiements as P


class TestAvoirANoter(unittest.TestCase):
    def test_argent_compte_dont_une_part_reste_sans_dette_a_eteindre(self):
        # MH2C : traite de 4 005,000 pour le TTC entier, puis certificat de 40,040 ; aucune autre dette.
        self.assertEqual(P.avoir_a_noter(True, 40.04, True), 40.04)

    def test_tout_a_ete_reaffecte_a_une_autre_dette(self):
        self.assertEqual(P.avoir_a_noter(True, 0.0, True), 0.0)
        self.assertEqual(P.avoir_a_noter(True, 0.0004, True), 0.0)        # un demi-millime n'est pas un avoir

    def test_une_dette_reduite_ne_laisse_aucun_avoir(self):
        # Le reglement repris etait une « Dette non payée » : ce n'est pas de l'argent, rien n'a ete verse en trop.
        self.assertEqual(P.avoir_a_noter(False, 40.04, True), 0.0)

    def test_sans_reglement_repris(self):
        self.assertEqual(P.avoir_a_noter(True, 40.04, False), 0.0)
        self.assertEqual(P.avoir_a_noter(True, None, True), 0.0)


GROUPE = {"reglement": "PE-NET", "mode": "Virement", "montant": 3728.425, "date": "2026-11-10", "total_ttc": 3766.046,
          "factures": [{"name": "SINV-1", "numero": "1955", "date": "2026-08-08", "ttc": 2257.136, "impute": 2257.136},
                       {"name": "SINV-2", "numero": None, "date": "2026-08-14", "ttc": 331.0, "impute": 327.29},
                       {"name": "SINV-3", "numero": "2093", "date": "2026-08-24", "ttc": 135.0, "impute": 97.379}]}


class TestFacturesCouvertes(unittest.TestCase):
    """17/11/2026 : une meme retenue couvre plusieurs factures reglees d'un seul paiement net ; la trace le dit
    sur le certificat et dans l'historique de chaque facture."""

    def test_resume_lisible(self):
        from bank_retenue_sync.tej import couverture as C

        texte = C.resume(GROUPE, "SINV-3", 37.621)
        lignes = texte.split("\n")
        self.assertIn("Retenue de 37,621 sur 3 factures réglées ensemble par PE-NET (virement de 3 728,425, le 10/11/2026)", lignes[0])
        self.assertIn("la retenue est enregistrée sur SINV-3", lignes[0])
        self.assertEqual(lignes[1], "- SINV-1 (n° 1955) du 08/08/2026 : 2 257,136")
        self.assertEqual(lignes[2], "- SINV-2 du 14/08/2026 : 331,000 dont 327,290 réglés par ce paiement")   # reglee en partie : dit
        self.assertEqual(lignes[3], "- SINV-3 (n° 2093) du 24/08/2026 : 135,000 — porte la retenue")          # jamais « en partie »
        self.assertEqual(lignes[4], "Total des factures : 3 766,046")

    def test_pas_de_groupe_pas_de_resume(self):
        from bank_retenue_sync.tej import couverture as C

        self.assertEqual(C.resume(None), "")
        self.assertEqual(C.resume({"factures": [GROUPE["factures"][0]]}), "")        # une seule facture n'est pas un groupe

    def test_note_de_l_historique_et_son_marqueur(self):
        from bank_retenue_sync.tej import couverture as C

        cert = {"name": "REF-1", "customer": "Client", "date_paiement": "2026-11-09", "montant_retenue": 37.621,
                "numero_chez_declarant": "F AOUT 2026"}
        note = C.note_facture(cert, GROUPE, "SINV-3")
        for morceau in ("certificat TEJ « F AOUT 2026 » du 09/11/2026 (Client)", "retenue de 37,621 sur 3 factures",
                        "SINV-1, SINV-2, SINV-3", "par PE-NET", "enregistrée sur SINV-3"):
            self.assertIn(morceau, note)
        self.assertTrue(note.endswith("[RAS REF-1]"))                                # c'est lui qui evite le doublon
        self.assertIn("l’une d’elles", C.note_facture(cert, GROUPE, None))


if __name__ == "__main__":
    unittest.main()
