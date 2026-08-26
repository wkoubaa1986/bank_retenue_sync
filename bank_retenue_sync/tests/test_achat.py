"""Tests des regles de la facture d'achat locale.

Convention de l'app : `unittest.TestCase` pur, donnees injectees, aucun acces reseau ni base.
Les montants viennent de factures reelles (ELECTROQUIP, ACC-PINV-2026-00088).
"""
from __future__ import annotations

import unittest
from datetime import date

from bank_retenue_sync.achat import regles as R
from bank_retenue_sync.achat import retenue as RET


# La table des taxes d'ELECTROQUIP (ACC-PINV-2026-00088), telle qu'elle est saisie.
TAXES = [{"account_head": "TVA 19% - A&S", "tax_amount": 175.311, "add_deduct_tax": "Add"},
         {"account_head": "Retenue a la source achat - A&S", "tax_amount": 11.99,
          "add_deduct_tax": "Deduct"}]


def facture(pays="Tunisia", stock=1, magasin="Magasins - A&S", bill_no="26FA01134",
            bill_date=date(2026, 8, 3), ttc=1099.011, tva=175.311, ht=923.7, controle=None):
    return {"pays_fournisseur": pays, "update_stock": stock, "set_warehouse": magasin,
            "bill_no": bill_no, "bill_date": bill_date, "total_ttc": ttc, "total_tva": tva,
            "total_ht": ht,
            "controle_retenue": controle or {"verdict": "conforme", "due": 10.99}}


PIECE = [{"name": "F-1", "file_name": "Erectroquip.pdf"}]
PIECE_IMAGE = [{"name": "F-2", "file_name": "photo facture.jpg"}]


class TestPerimetre(unittest.TestCase):
    def test_le_fournisseur_etranger_est_hors_sujet(self):
        """Une facture chinoise n'a ni retenue a la source ni scan obligatoire : lui appliquer ces
        regles bloquerait des saisies parfaitement legitimes."""
        self.assertEqual(R.bloquants(facture(pays="China", stock=0, magasin=None, bill_no=None),
                                     []), [])

    def test_le_pays_se_lit_sans_se_soucier_de_la_casse(self):
        self.assertTrue(R.est_local("tunisia"))
        self.assertTrue(R.est_local(" Tunisia "))
        self.assertFalse(R.est_local(None))


class TestCeQuiBloque(unittest.TestCase):
    """⚠️ DEUX MOTIFS SEULEMENT. Tout ce qui peut etre corrige l'est plutot que refuse : stock,
    magasin, numero, dates et retenue se posent seuls a l'enregistrement. Refuser une facture pour
    une case a cocher qu'on sait cocher soi-meme fait perdre du temps sans rien proteger."""

    def test_une_facture_avec_son_pdf_et_des_totaux_concordants_passe(self):
        self.assertEqual(R.bloquants(facture(), PIECE), [])

    def test_sans_pdf_on_refuse(self):
        [m] = R.bloquants(facture(), [])
        self.assertIn("PDF", m)

    def test_une_piece_jointe_qui_n_est_pas_un_pdf_ne_suffit_pas(self):
        """Un JPG ou un DOCX se joint aussi bien, mais ne s'imprime pas pareil au controle et
        l'extraction ne sait pas l'ouvrir."""
        [m] = R.bloquants(facture(), PIECE_IMAGE)
        self.assertIn("PDF", m)
        self.assertIn("1 piece(s) jointe(s)", m)

    def test_le_pdf_est_reconnu_quelle_que_soit_la_casse(self):
        self.assertTrue(R.pdf_present([{"file_name": "FACTURE.PDF"}]))
        self.assertFalse(R.pdf_present([{"file_name": "facture.pdf.jpg"}]))
        self.assertFalse(R.pdf_present([]))

    def test_le_stock_le_numero_et_les_dates_ne_bloquent_plus(self):
        """Ils sont corriges a l'enregistrement : les refuser ici serait refuser deux fois."""
        nue = facture(stock=0, magasin=None, bill_no=None, bill_date=None)
        self.assertEqual(R.bloquants(nue, PIECE), [])

    def test_une_retenue_fausse_ne_bloque_plus(self):
        """Elle est ramenee au montant du a l'enregistrement."""
        f = facture(controle={"verdict": "montant faux", "due": 10.99, "saisie": 11.99})
        self.assertEqual(R.bloquants(f, PIECE), [])


class TestEcartsImportants(unittest.TestCase):
    """Cas reel ELECTROQUIP : la saisie porte 923,700 / 175,311 / 1 099,011 et le scan se lit
    922,688 / 175,311 / 1 097,999 — un chiffre mal reconnu, pas une erreur de saisie."""

    SCAN = {"total_ht": 922.688, "total_tva": 175.311, "total_ttc": 1097.999, "coherent": 1}

    def test_le_bruit_de_lecture_ne_bloque_pas(self):
        """1,012 DT d'ecart sur le TTC comme sur le HT, et une TVA lue au millime exact. C'est la
        facture JUSTE : elle doit passer les trois seuils, y compris le TTC resserre a 1,5 DT — qui
        a ete choisi pour couvrir ce bruit-la, la ou 1,0 l'aurait refuse."""
        self.assertEqual(R.ecarts_importants(facture(), self.SCAN), [])

    def test_un_vrai_ecart_bloque(self):
        scan = dict(self.SCAN, total_ttc=1050.0, total_ht=874.689)
        m = R.ecarts_importants(facture(), scan)
        self.assertTrue(any("TTC" in x for x in m))

    def test_le_seuil_du_HT_est_le_plus_grand_de_un_dinar_et_du_pourcentage(self):
        self.assertEqual(R.seuil_ecart(1099.011), 10.99)
        self.assertEqual(R.seuil_ecart(50.0), 1.0)        # 1 % vaudrait 0,5 : le plancher gagne

    def test_le_seuil_de_la_TVA_se_calcule_sur_la_TVA_LUE(self):
        """Sur la valeur du scan, pas sur la saisie : une TVA gonflee ne doit pas s'acheter au
        passage la tolerance qui va avec."""
        self.assertEqual(R.seuil_tva(175.311), 0.175)

    def test_une_lecture_incoherente_n_accuse_personne(self):
        """⚠️ Si HT + TVA ne tombe pas sur le TTC du scan, c'est la LECTURE qui est fausse. Un
        modele a rendu 971,25 de HT sur cette facture : cette lecture-la ne bouclait pas et
        n'aurait jamais du peser sur une decision."""
        scan = {"total_ht": 971.25, "total_tva": 176.311, "total_ttc": 1098.999, "coherent": 0}
        self.assertEqual(R.ecarts_importants(facture(), scan), [])

    def test_la_TVA_FAUSSE_DE_00092_EST_MAINTENANT_ARRETEE(self):
        """⚠️ CAS REEL, ET LE TROU QUE CE TEST BOUCHE. Sur ACC-PINV-2026-00092 la TVA a ete ramenee
        a la main de 175,311 a 170,000 : 5,311 DT deduits en trop. Le seuil unique, calcule sur le
        TTC, valait 10,937 — la facture s'est validee sans un mot, et la retenue a la source,
        assise sur un TTC devenu faux, avec elle. Le seuil de la TVA vaut desormais 0,175."""
        fausse = facture(ttc=1093.7, tva=170.0)
        m = R.ecarts_importants(fausse, self.SCAN)
        self.assertTrue(any("TVA" in x for x in m), m)
        self.assertTrue(any("TTC" in x for x in m), m)   # 4,299 d'ecart, au-dela des 1,5 admis

    def test_un_millime_de_TVA_ne_bloque_pas_une_grosse_facture(self):
        """Le pourcentage suit l'echelle : 0,1 % de 733 DT laisse passer un chiffre mal lu."""
        gros = facture(ttc=4593.899, tva=733.0, ht=3860.4)
        scan = {"total_ht": 3860.4, "total_tva": 733.499, "total_ttc": 4593.899, "coherent": 1}
        self.assertEqual([x for x in R.ecarts_importants(gros, scan) if "TVA" in x], [])

    def test_sans_extraction_rien_ne_bloque(self):
        self.assertEqual(R.ecarts_importants(facture(), None), [])

    def test_une_extraction_sans_drapeau_de_coherence_ne_bloque_pas_non_plus(self):
        """⚠️ CE TEST GARDE UN BUG REEL, PAS UNE HYPOTHESE. `extraction_de` a longtemps lu tous les
        montants SAUF `coherent` : la regle recevait un dict complet, n'y trouvait pas le drapeau,
        et rendait la main sans rien comparer. Le seul controle qui confronte la saisie au scan
        etait mort en silence — et toutes les factures passaient. Que la fonction soit prudente est
        voulu ; que l'appelant oublie le champ ne doit plus arriver."""
        scan = {"total_ht": 500.0, "total_tva": 95.0, "total_ttc": 595.0}
        self.assertEqual(R.ecarts_importants(facture(), scan), [])


class TestLectureDeLaTableDesTaxes(unittest.TestCase):
    """⚠️ NI `grand_total` NI `total_taxes_and_charges` NE DISENT CE QU'ON CROIT quand une retenue
    est deja saisie — c'est l'erreur qui a fausse la premiere version du controle."""

    def test_la_tva_est_la_somme_des_lignes_de_tva(self):
        """`total_taxes_and_charges` vaut 163,321 sur ELECTROQUIP : 175,311 de TVA MOINS 11,990 de
        retenue. Le comparer au scan accusait la facture d'un ecart qui n'existait pas."""
        self.assertEqual(R.tva_facturee(TAXES), 175.311)

    def test_le_ttc_du_scan_est_celui_d_avant_la_retenue(self):
        """`grand_total` (1 087,021) est deja net de la retenue ; le fournisseur, lui, facture
        1 099,011 — et c'est ce nombre qui est imprime sur le scan."""
        self.assertEqual(R.ttc_avant_retenue(1087.021, TAXES), 1099.011)

    def test_la_retenue_saisie_se_lit_sur_les_lignes_en_deduction(self):
        self.assertEqual(R.retenue_saisie(TAXES), 11.99)

    def test_sans_retenue_le_ttc_ne_bouge_pas(self):
        self.assertEqual(R.ttc_avant_retenue(500.0, [TAXES[0]]), 500.0)


class TestAssietteEtRetenue(unittest.TestCase):
    """⚠️ L'ASSIETTE EXCLUT LE TIMBRE. Sur les 17 factures locales de 2026 depassant le seuil,
    « 1 % du TTC hors timbre » tombe au millime sur dix d'entre elles."""

    def test_le_timbre_sort_de_l_assiette(self):
        taxes = [{"account_head": "TVA 7% - A&S", "tax_amount": 253.832, "add_deduct_tax": "Add"},
                 {"account_head": "Timbre Fiscal - A&S", "tax_amount": 1.0, "add_deduct_tax": "Add"},
                 {"account_head": "Retenue a la source achat - A&S", "tax_amount": 41.8,
                  "add_deduct_tax": "Deduct"}]
        # cas reel ACC-PINV-2026-00001 : TTC avant retenue 4 181,0, timbre 1,0
        self.assertEqual(R.assiette_retenue(4139.2, taxes), 4180.0)
        self.assertEqual(R.retenue_due(4180.0, ttc=4181.0), 41.8)

    def test_au_dessus_du_seuil_un_pour_cent(self):
        self.assertEqual(R.retenue_due(1099.011, ttc=1099.011), 10.99)

    def test_sous_le_seuil_aucune_retenue(self):
        self.assertEqual(R.retenue_due(999.999, ttc=999.999), 0.0)

    def test_le_seuil_se_lit_sur_le_ttc_et_l_assiette_sert_de_base(self):
        """Une facture a 1 000,500 TTC dont 1 DT de timbre : le seuil est franchi, la base est
        999,500. Confondre les deux changerait le resultat sur chaque facture timbree."""
        self.assertEqual(R.retenue_due(999.5, ttc=1000.5), 9.995)

    def test_le_seuil_et_le_taux_sont_parametrables(self):
        """La loi de finances les revise : ils ne sont pas ecrits dans le code."""
        self.assertEqual(R.retenue_due(2000.0, seuil=1500.0, taux=1.5, ttc=2000.0), 30.0)


class TestControleDeLaRetenue(unittest.TestCase):
    def test_une_retenue_juste_est_conforme(self):
        taxes = [TAXES[0], {"account_head": "Retenue a la source achat - A&S",
                            "tax_amount": 10.99, "add_deduct_tax": "Deduct"}]
        c = R.controle_retenue(1088.021, taxes)
        self.assertEqual(c["verdict"], "conforme")

    def test_electroquip_retient_un_dinar_de_trop(self):
        """Cas reel : 11,990 saisis pour 10,990 dus."""
        c = R.controle_retenue(1087.021, TAXES)
        self.assertEqual((c["verdict"], c["due"], c["saisie"], c["ecart"]),
                         ("montant faux", 10.99, 11.99, 1.0))

    def test_une_retenue_absente_est_signalee_comme_manquante(self):
        """Quatre factures de 2026 sont dans ce cas : 52,234 DT jamais retenus."""
        c = R.controle_retenue(1100.977, [{"account_head": "TVA 19% - A&S", "tax_amount": 176.627,
                                           "add_deduct_tax": "Add"}])
        self.assertEqual(c["verdict"], "manquante")
        self.assertEqual(c["due"], 11.01)

    def test_une_base_calculee_sur_le_ht_est_signalee(self):
        """Deux factures retiennent 1 % du HT au lieu du TTC : 16,523 au lieu de 19,660."""
        taxes = [{"account_head": "TVA 19% - A&S", "tax_amount": 313.743, "add_deduct_tax": "Add"},
                 {"account_head": "Retenue a la source achat - A&S", "tax_amount": 16.523,
                  "add_deduct_tax": "Deduct"}]
        c = R.controle_retenue(1949.5, taxes)
        self.assertEqual(c["verdict"], "montant faux")
        self.assertEqual(c["due"], 19.66)


class TestDateLueSurLeScan(unittest.TestCase):
    """⚠️ La lecture de l'ANNEE est le point faible du modele : sur une meme facture, trois
    lectures ont rendu 2020, 2023 et 2026. Le numero et les montants, eux, etaient stables."""

    def test_une_date_proche_est_posee(self):
        self.assertTrue(R.date_plausible("2026-08-03", date(2026, 8, 3)))

    def test_une_facture_saisie_avec_retard_reste_plausible(self):
        self.assertTrue(R.date_plausible("2026-06-15", date(2026, 8, 3)))

    def test_une_annee_mal_lue_est_ecartee(self):
        """2020 pour une facture de 2026 : poser cette date deciderait de l'exercice de
        rattachement sans que personne ne la relise."""
        self.assertFalse(R.date_plausible("2020-08-03", date(2026, 8, 3)))
        self.assertFalse(R.date_plausible("2023-08-03", date(2026, 8, 3)))

    def test_une_date_illisible_est_ecartee(self):
        for valeur in (None, "", "pas une date"):
            self.assertFalse(R.date_plausible(valeur, date(2026, 8, 3)))


class TestDateDeComptabilisation(unittest.TestCase):
    """La date de comptabilisation suit celle de la facture fournisseur — ce qui donne a
    `date_plausible` une portee bien plus grande : elle decide desormais de l'EXERCICE."""

    def test_une_annee_mal_lue_ne_peut_pas_deplacer_l_exercice(self):
        """Le modele a rendu 2020 puis 2023 pour une facture d'aout 2026. Poser cette date, c'est
        comptabiliser dans un exercice clos."""
        for lue in ("2020-08-03", "2023-08-03", "2027-12-31"):
            self.assertFalse(R.date_plausible(lue, date(2026, 8, 3)), lue)

    def test_une_date_du_mois_precedent_reste_posable(self):
        """Une facture de juillet saisie en aout : la comptabilisation doit bien reculer en
        juillet."""
        self.assertTrue(R.date_plausible("2026-07-12", date(2026, 8, 3)))

    def test_une_mauvaise_date_DEJA_EN_BASE_ne_deplace_rien_non_plus(self):
        """⚠️ CAS REEL, ET LE PIRE DES DEUX. ACC-PINV-2026-00088 porte 2020-08-03 en date
        fournisseur : une annee fausse posee avant que le filtre n'existe. Filtrer ce que le scan
        PROPOSE ne suffit donc pas — il faut aussi se mefier de ce que le champ CONTIENT, sans quoi
        le simple fait d'enregistrer ramenait la comptabilisation de 2026 a 2020.

        La date arrive ici en `datetime.date` et non en chaine : c'est le chemin de la base, pas
        celui de l'extraction."""
        self.assertFalse(R.date_plausible(date(2020, 8, 3), date(2026, 8, 3)))


class _Ligne:
    """Une ligne de la table des taxes, reduite a ce que le geste regarde."""

    def __init__(self, account_head, add_deduct_tax="Deduct", cost_center=None):
        self.account_head, self.add_deduct_tax, self.cost_center = (account_head, add_deduct_tax,
                                                                    cost_center)


class _Facture:
    """Une facture reduite a sa table des taxes et a son centre de couts.

    `cost_center` est renseigne pour que `centre_de_cout` s'arrete avant la base : la convention de
    l'app est de ne rien interroger dans les tests.
    """

    def __init__(self, taxes, cost_center="Principal - A&S"):
        self._d = {"taxes": taxes, "cost_center": cost_center}

    def get(self, k, defaut=None):
        return self._d.get(k, defaut)


class TestCentreDeCoutDeLaRetenue(unittest.TestCase):
    """⚠️ LE MONTANT JUSTE NE SUFFIT PAS. `Retenue a la source achat - A&S` est un compte de
    RESULTAT : ERPNext refuse toute ecriture de resultat sans centre de couts, et l'ecriture de taxe
    reprend la colonne de la ligne sans se rabattre sur le defaut de la societe. Le defaut
    `:Company` du champ n'est pose qu'a l'ecran, jamais par un `append()` cote serveur — la ligne
    posee par la machine partait donc vide. Cas reel : ACC-PINV-2026-00092, refusee a la validation
    apres avoir ete corrigee sans bruit."""

    def test_une_ligne_sans_centre_le_recoit(self):
        f = _Facture([_Ligne("TVA 19% - A&S", "Add", "Principal - A&S"),
                      _Ligne("Retenue a la source achat - A&S")])
        self.assertEqual(RET.completer_centre(f)["statut"], "pose")
        self.assertEqual(f.get("taxes")[1].cost_center, "Principal - A&S")

    def test_un_centre_deja_choisi_n_est_pas_ecrase(self):
        """Sur une societe a plusieurs centres, celui que l'utilisateur a designe vaut mieux que le
        defaut."""
        f = _Facture([_Ligne("Retenue a la source achat - A&S", cost_center="Atelier - A&S")])
        self.assertEqual(RET.completer_centre(f)["statut"], "deja pose")
        self.assertEqual(f.get("taxes")[0].cost_center, "Atelier - A&S")

    def test_sans_ligne_de_retenue_il_n_y_a_rien_a_porter(self):
        """Une facture sous le seuil : pas de ligne, donc pas de manque a signaler — et surtout pas
        celui d'un centre de couts que rien ne reclame."""
        f = _Facture([_Ligne("TVA 19% - A&S", "Add", "Principal - A&S")], cost_center=None)
        self.assertEqual(RET.completer_centre(f)["statut"], "ligne introuvable")

    def test_sans_centre_par_defaut_le_manque_est_nomme(self):
        """Ni sur la facture ni sur la societe : le geste ne devine pas, il dit ce qui bloquera."""
        f = _Facture([_Ligne("Retenue a la source achat - A&S")], cost_center=None)
        vrai = RET.centre_de_cout
        RET.centre_de_cout = lambda doc: None
        try:
            self.assertEqual(RET.completer_centre(f)["statut"], "aucun centre de couts")
        finally:
            RET.centre_de_cout = vrai


class TestPlancherDuPerimetre(unittest.TestCase):
    """⚠️ SEUL L'EXERCICE 2026 EST CONTROLE (decision du 26/08/2026). Une facture comptabilisee
    avant le 01/01/2026 appartient a un exercice clos : y poser une retenue ou y deplacer une date
    au moment d'un enregistrement tardif ferait bouger des ecritures que plus personne ne doit
    toucher."""

    def test_une_facture_2026_est_dans_le_perimetre(self):
        self.assertTrue(R.dans_le_perimetre(date(2026, 1, 1)))
        self.assertTrue(R.dans_le_perimetre("2026-08-26"))

    def test_une_facture_2025_est_hors_perimetre(self):
        self.assertFalse(R.dans_le_perimetre(date(2025, 12, 31)))
        self.assertFalse(R.dans_le_perimetre("2020-08-03"))

    def test_le_plancher_est_reglable(self):
        self.assertFalse(R.dans_le_perimetre("2026-06-30", plancher="2026-07-01"))
        self.assertTrue(R.dans_le_perimetre("2026-07-01", plancher="2026-07-01"))

    def test_dans_le_doute_on_controle(self):
        """Une date absente (ERPNext posera la date du jour) ou illisible ne doit pas offrir une
        sortie de perimetre silencieuse : le plancher exclut le passe connu, pas l'inconnu."""
        self.assertTrue(R.dans_le_perimetre(None))
        self.assertTrue(R.dans_le_perimetre("n'importe quoi"))


class _LigneMontant:
    """Une ligne de taxe avec son montant — ce que `corriger_ligne` lit et redresse."""

    def __init__(self, account_head, tax_amount, add_deduct_tax="Add",
                 charge_type="On Net Total", rate=1.0):
        self.account_head, self.tax_amount, self.add_deduct_tax = (account_head, tax_amount,
                                                                   add_deduct_tax)
        self.charge_type, self.rate = charge_type, rate


class _FactureMontants:
    """Une facture reduite a ses taxes et a son total, sans base ni recalcul ERPNext."""

    def __init__(self, taxes, grand_total):
        self._d = {"taxes": taxes}
        self.grand_total = grand_total

    def get(self, k, defaut=None):
        return self._d.get(k, defaut)

    def calculate_taxes_and_totals(self):
        pass


class TestCorrectionMultiLignes(unittest.TestCase):
    """⚠️ `saisie` EST LA SOMME DES LIGNES DE DEDUCTION. Deux lignes de retenue saisies (doublon de
    saisie), et l'ancienne correction ne redressait que la premiere : le total restait faux, le
    verdict aussi, et rien ne le disait.

    ⚠️ PIEGE DU MODE LIBRAIRIE (CI sans site) : `frappe.utils.flt(x, precision)` retourne 0 —
    l'arrondi interne a besoin du contexte de site et flt avale l'exception. `_seuil()` valait
    donc 0 en CI et le du tombait a zero, la ou bench (site charge) rendait 1000. Les reglages
    sont neutralises ici, comme le fait deja TestCentreDeCoutDeLaRetenue pour la base."""

    def setUp(self):
        self._seuil, self._taux = RET._seuil, RET._taux
        RET._seuil = lambda: R.SEUIL_RETENUE
        RET._taux = lambda: R.TAUX_RETENUE

    def tearDown(self):
        RET._seuil, RET._taux = self._seuil, self._taux

    def test_les_doublons_sont_ramenes_a_zero(self):
        taxes = [_LigneMontant("TVA 19% - A&S", 175.311),
                 _LigneMontant("Retenue a la source achat - A&S", 11.99, "Deduct"),
                 _LigneMontant("Retenue a la source achat - A&S", 11.99, "Deduct")]
        # grand_total net des DEUX retenues : 1 099,011 - 23,98.
        f = _FactureMontants(taxes, 1075.031)
        res = RET.corriger_ligne(f)
        self.assertEqual(res["statut"], "corrigee")
        self.assertEqual(res["doublons_annules"], 1)
        self.assertEqual(round(taxes[1].tax_amount, 3), 10.99)
        self.assertEqual(taxes[2].tax_amount, 0)

    def test_une_seule_ligne_fausse_se_corrige_comme_avant(self):
        taxes = [_LigneMontant("TVA 19% - A&S", 175.311),
                 _LigneMontant("Retenue a la source achat - A&S", 15.0, "Deduct")]
        f = _FactureMontants(taxes, 1084.011)
        res = RET.corriger_ligne(f)
        self.assertEqual(res["statut"], "corrigee")
        self.assertNotIn("doublons_annules", res)
        self.assertEqual(round(taxes[1].tax_amount, 3), 10.99)

    def test_la_ligne_corrigee_devient_Actual(self):
        """⚠️ LE BUG QUI RENDAIT LA CORRECTION INOPERANTE (26/08/2026, ACC-PINV-2026-00091) :
        saisie en « On Net Total » a 1 %, la ligne etait recalculee depuis le taux a chaque
        calculate_taxes_and_totals — le tax_amount pose par la correction etait aussitot ecrase
        (16,523 = 1 % du HT au lieu du TTC hors timbre). La ligne corrigee doit devenir Actual,
        taux a zero, pour que le montant calcule soit celui qui fasse foi."""
        taxes = [_LigneMontant("TVA 19% - A&S", 313.743),
                 _LigneMontant("Retenue a la source achat - A&S", 16.523, "Deduct")]
        f = _FactureMontants(taxes, 1949.5)
        res = RET.corriger_ligne(f)
        self.assertEqual(res["statut"], "corrigee")
        self.assertEqual(taxes[1].charge_type, "Actual")
        self.assertEqual(taxes[1].rate, 0)
        self.assertEqual(round(taxes[1].tax_amount, 3), 19.66)


class TestOrphelinsTej(unittest.TestCase):
    """L'AUTRE SENS du recapitulatif : un certificat vivant du portail sans facture locale est une
    declaration au fisc sans comptabilite — aucun des deux tableaux simples ne le montrait."""

    VIVANTS = ("REÇUE", "RECUE", "EN COURS", "VALIDÉE", "VALIDEE")
    CLES = {("FA-2026-0012", "1234567A")}

    def _orphelins(self, export):
        return RET.orphelins_tej(export, self.CLES, "2026-01-01", self.VIVANTS)

    def test_un_certificat_apparie_n_est_pas_orphelin(self):
        self.assertEqual(self._orphelins(
            [{"numero": "FA-2026-0012", "beneficiaire": "1234567A", "etat": "VALIDEE",
              "date_paiement": "2026-02-05"}]), [])

    def test_un_certificat_sans_facture_ressort(self):
        out = self._orphelins(
            [{"numero": "FA-2026-9999", "beneficiaire": "1234567A", "etat": "VALIDEE",
              "date_paiement": "2026-02-05"}])
        self.assertEqual(len(out), 1)

    def test_le_plancher_s_applique_aussi_au_portail(self):
        """Demande du 26/08/2026 : tout part du 01/01/2026, meme cote TEJ."""
        self.assertEqual(self._orphelins(
            [{"numero": "FA-2025-0100", "beneficiaire": "1234567A", "etat": "VALIDEE",
              "date_paiement": "2025-11-20"}]), [])

    def test_un_annule_ne_compte_pas(self):
        self.assertEqual(self._orphelins(
            [{"numero": "FA-2026-9999", "beneficiaire": "1234567A", "etat": "ANNULÉ",
              "date_paiement": "2026-02-05"}]), [])

    def test_une_date_illisible_garde_le_certificat(self):
        """Le doute se montre, il ne se cache pas."""
        out = self._orphelins(
            [{"numero": "FA-2026-9999", "beneficiaire": "1234567A", "etat": "VALIDEE",
              "date_paiement": "pas une date", "cree": None}])
        self.assertEqual(len(out), 1)


class TestRapprochementsSuggeres(unittest.TestCase):
    """SUGGESTIF, jamais lie : meme matricule + paiement proche de la comptabilisation. Le cran
    au-dessus de « rien » — l'ecran montre la paire, l'humain tranche."""

    LIGNES = [{"facture": "ACC-PINV-2026-00003", "matricule": "1646863M", "date": "2026-02-06"},
              {"facture": "ACC-PINV-2026-00026", "matricule": "9999999Z", "date": "2026-04-20"}]

    def test_la_date_du_portail_se_lit_en_jour_mois_annee(self):
        """« 05-02-2026 » est ambigu pour un parseur souple : ici c'est le 5 fevrier, point."""
        self.assertEqual(str(RET._date_portail("05-02-2026")), "2026-02-05")
        self.assertEqual(str(RET._date_portail("2026-02-05")), "2026-02-05")
        self.assertIsNone(RET._date_portail("pas une date"))

    def test_meme_matricule_et_meme_jour_suggerent(self):
        out = RET.rapprochements_suggeres(
            [{"numero": "2026-0012", "reference": "ref-1", "beneficiaire": "1646863M",
              "date_paiement": "05-02-2026"}], self.LIGNES)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["facture"], "ACC-PINV-2026-00003")
        self.assertEqual(out[0]["ecart_jours"], 1)

    def test_un_autre_matricule_ne_suggere_rien(self):
        """Le montant et la date peuvent coincider par hasard ; le matricule, non."""
        out = RET.rapprochements_suggeres(
            [{"numero": "X", "reference": "r", "beneficiaire": "1111111A",
              "date_paiement": "06-02-2026"}], self.LIGNES)
        self.assertEqual(out, [])

    def test_trop_loin_dans_le_temps_ne_suggere_rien(self):
        out = RET.rapprochements_suggeres(
            [{"numero": "X", "reference": "r", "beneficiaire": "1646863M",
              "date_paiement": "20-03-2026"}], self.LIGNES)
        self.assertEqual(out, [])

    def test_les_numeros_emboites_suggerent_malgre_la_date(self):
        """Cas reel « 26FA01134_V2 » : suffixe ajoute pour passer le refus de contenu identique
        apres annulation — la date de paiement portee au portail est celle de la SOUMISSION
        (10 jours apres la comptabilisation), le matricule manque sur la fiche : seuls les
        numeros disent encore la verite."""
        lignes = [{"facture": "ACC-PINV-2026-00088", "matricule": None,
                   "date": "2026-08-03", "bill_no": "26FA01134"}]
        out = RET.rapprochements_suggeres(
            [{"numero": "26FA01134_V2", "reference": "r", "beneficiaire": "1802542W",
              "date_paiement": "13-08-2026"}], lignes)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["facture"], "ACC-PINV-2026-00088")
        self.assertEqual(out[0]["motif"], "numero")

    def test_des_numeros_semblables_avec_des_matricules_differents_se_taisent(self):
        """⚠️ CAS REEL DU 26/08/2026 : « 4/2026 » (NIZAR BELGUITH, loyer 7,14 DT) s'emboitait dans
        le bill_no « 04/2026 » de la facture M.F.K — et le certificat a ete attache A TORT en
        prod sur la foi des numeros. Des numeros semblables n'excusent pas des beneficiaires
        differents : quand les deux matricules sont connus et different, pas de suggestion."""
        lignes = [{"facture": "ACC-PINV-2026-00026", "matricule": "9999999Z",
                   "date": "2026-04-20", "bill_no": "04/2026"}]
        out = RET.rapprochements_suggeres(
            [{"numero": "4/2026", "reference": "r", "beneficiaire": "1144181A",
              "date_paiement": "20-02-2026"}], lignes)
        self.assertEqual(out, [])

    def test_un_bill_no_trop_court_ne_s_emboite_pas(self):
        """« 30/2026 » contiendrait « 2026 » : cinq caracteres minimum, sinon tout s'emboite."""
        lignes = [{"facture": "F", "matricule": None, "date": "2026-06-20", "bill_no": "2026"}]
        out = RET.rapprochements_suggeres(
            [{"numero": "30/2026", "reference": "r", "beneficiaire": "1144181A",
              "date_paiement": "20-06-2026"}], lignes)
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
