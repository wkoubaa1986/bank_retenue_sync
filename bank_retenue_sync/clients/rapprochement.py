"""Rapprochement par CLIENT : ce qu'il a commandé, ce qu'on lui a livré, ce qu'il a payé.

Trois totaux qui devraient se répondre :

    commandes TTC   ==   bons de livraison validés   ==   règlements reçus

Quand ils divergent, c'est l'un de ces trois cas, et l'écran doit permettre de les distinguer
sans ouvrir une seule fiche :
  - livré sans être payé          -> BL == commandes, règlements en dessous ;
  - payé sans être livré          -> règlements == commandes, BL en dessous (avance, ou BL oublié) ;
  - commande jamais honorée       -> BL et règlements tous deux en dessous.

⚠️ CE MODULE NE LIT QUE DES AGRÉGATS. 5 220 clients pour ~10 000 commandes, autant de BL et de
paiements : une requête par client mettrait la page à genoux. Tout passe par des GROUP BY, un par
source, recollés en mémoire.

⚠️ CE MODULE N'ÉCRIT RIEN, sauf la liste des clients à ignorer. Aucune pièce comptable n'est
créée, modifiée ou annulée ici : c'est un écran de CONSTAT.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

PRECISION = 3

# Le timbre fiscal — 1 DT — sépare une commande de sa facture sur presque tous les dossiers.
# En dessous de ce seuil, un delta n'est pas un écart : c'est la mécanique normale. C'est le
# DÉFAUT, pas une règle figée : les deux seuils se règlent dans « Bank Retenue Sync Settings »
# (demande utilisateur 04/09/2026), parce que le montant acceptable n'est pas le même selon
# qu'on regarde un règlement ou une livraison.
TOLERANCE = 1.0
CHAMP_TOLERANCE_MONTANT = "tolerance_ecart_montant"
CHAMP_TOLERANCE_BL = "tolerance_ecart_bl"


def _reglage(champ) -> float:
    """Le seuil réglé, ou le défaut. Ne lève jamais : un écran de constat doit s'ouvrir même
    quand le réglage n'existe pas encore (bench où la migration n'est pas passée)."""
    try:
        v = frappe.db.get_single_value("Bank Retenue Sync Settings", champ)
    except Exception:
        return TOLERANCE
    # ⚠️ ZÉRO EST UN CHOIX VALIDE — « signale-moi le moindre centime » — alors que None veut
    # dire « pas réglé ». Un `or TOLERANCE` aurait écrasé le zéro et rendu le réglage muet.
    return TOLERANCE if v is None else flt(v)


def tolerances() -> dict:
    return {"montant": _reglage(CHAMP_TOLERANCE_MONTANT), "bl": _reglage(CHAMP_TOLERANCE_BL)}

DOCTYPE_IGNORE = "BRS Client Rapprochement Ignore"

# Les champs où un numéro de téléphone peut vivre. `custom_liste_telephone` est le champ réel
# (5 193 clients renseignés) ; `mobile_no` n'en couvre que 894, et les deux autres champs
# « téléphone » de la fiche sont vides. On cherche dans les deux qui servent.
CHAMPS_TELEPHONE = ("custom_liste_telephone", "mobile_no")


def _somme(table, champ_client, condition, champ_montant="grand_total", params=()):
    """{client: (total, nb)} — un GROUP BY, jamais une requête par client."""
    rows = frappe.db.sql(
        f"""SELECT `{champ_client}` AS cle, SUM(`{champ_montant}`) AS total, COUNT(*) AS nb
            FROM `tab{table}` WHERE {condition} AND `{champ_client}` IS NOT NULL
            GROUP BY `{champ_client}`""", params, as_dict=True)
    return {r.cle: (flt(r.total, PRECISION), int(r.nb or 0)) for r in rows}


def commandes() -> dict:
    """Commandes VALIDÉES, TTC. Les annulées et les brouillons ne doivent rien à personne."""
    return _somme("Sales Order", "customer", "docstatus = 1")


def bons_de_livraison() -> dict:
    """BL VALIDÉS uniquement — c'est la question posée : « a-t-il des BL validés ? ».

    ⚠️ LES RETOURS SONT DEDANS, ET C'EST VOULU. Un retour est un bon de livraison validé de
    montant NÉGATIF (4 dans la base, −333,600 DT) : il vient naturellement en déduction, et
    c'est bien le net livré qu'on veut comparer aux commandes. Le détail par état les isole.
    """
    return _somme("Delivery Note", "customer", "docstatus = 1")


def livraisons_detail() -> dict:
    """{client: {livres, retours, brouillons, annules}} — chaque état avec son total et son nb.

    Trois choses que la colonne « BL validés » ne peut pas dire à elle seule :
      - ce qui est REVENU (retour de BL), et qui explique un écart en faveur du client ;
      - ce qui est resté en BROUILLON — 45 bons pour 25 705 DT dans la base : de la marchandise
        sortie que rien ne constate, et la première cause d'un « livré moins que commandé » ;
      - ce qui a été ANNULÉ, qui explique pourquoi une commande paraît non honorée.
    """
    out = {}
    for r in frappe.db.sql(
            """SELECT customer AS cle, docstatus, is_return,
                      SUM(grand_total) AS total, COUNT(*) AS nb
               FROM `tabDelivery Note` WHERE customer IS NOT NULL
               GROUP BY customer, docstatus, is_return""", as_dict=True):
        etat = ("annules" if r.docstatus == 2
                else "brouillons" if r.docstatus == 0
                else "retours" if r.is_return else "livres")
        e = out.setdefault(r.cle, {})
        poste = e.setdefault(etat, {"total": 0.0, "nb": 0})
        poste["total"] = flt(poste["total"] + flt(r.total), PRECISION)
        poste["nb"] += int(r.nb or 0)
    return out


def reglements() -> dict:
    """Encaissements reçus du client (Payment Entry validées, sens Recevoir)."""
    return _somme("Payment Entry", "party", "docstatus = 1 AND payment_type = 'Receive'"
                                            " AND party_type = 'Customer'",
                  champ_montant="paid_amount")


def journal() -> dict:
    """Écritures de journal touchant le compte client -> {client: (net, nb)}.

    ⚠️ LE SENS COMPTE : sur un compte de tiers, un CRÉDIT diminue la créance — il agit comme un
    règlement (avoir, régularisation, perte). On rend donc `credit - debit`, homogène avec les
    encaissements, et non l'inverse.
    """
    rows = frappe.db.sql(
        """SELECT jea.party AS cle, SUM(jea.credit - jea.debit) AS total,
                  COUNT(DISTINCT jea.parent) AS nb
           FROM `tabJournal Entry Account` jea
           INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
           WHERE je.docstatus = 1 AND jea.party_type = 'Customer' AND jea.party IS NOT NULL
           GROUP BY jea.party""", as_dict=True)
    return {r.cle: (flt(r.total, PRECISION), int(r.nb or 0)) for r in rows}


def avances() -> dict:
    """Avances -> {client: {"non_affectee": x, "sur_commande": y}}.

    Deux choses différentes, et l'écran doit les distinguer :
      - `non_affectee` : de l'argent reçu qui ne pointe sur RIEN. C'est le plus suspect ;
      - `sur_commande` : posé sur une commande, pas encore sur une facture. C'est normal tant que
        la facture n'existe pas.
    """
    out = {}
    for r in frappe.db.sql(
            """SELECT party AS cle, SUM(unallocated_amount) AS total FROM `tabPayment Entry`
               WHERE docstatus = 1 AND payment_type = 'Receive' AND party_type = 'Customer'
                 AND unallocated_amount > 0 GROUP BY party""", as_dict=True):
        out.setdefault(r.cle, {})["non_affectee"] = flt(r.total, PRECISION)
    for r in frappe.db.sql(
            """SELECT pe.party AS cle, SUM(per.allocated_amount) AS total
               FROM `tabPayment Entry Reference` per
               INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
               WHERE pe.docstatus = 1 AND pe.party_type = 'Customer'
                 AND per.reference_doctype = 'Sales Order' GROUP BY pe.party""", as_dict=True):
        out.setdefault(r.cle, {})["sur_commande"] = flt(r.total, PRECISION)
    return out


def ignores() -> dict:
    """{client: motif} — les clients dont on a décidé que l'écart ne se corrigerait pas."""
    if not frappe.db.exists("DocType", DOCTYPE_IGNORE):
        return {}
    return {r.client: (r.motif or "")
            for r in frappe.get_all(DOCTYPE_IGNORE, fields=["client", "motif"],
                                    limit_page_length=0)}


def ecart_significatif(delta, seuil=None) -> bool:
    """Au-delà du seuil, il y a quelque chose à regarder."""
    return abs(flt(delta)) > (TOLERANCE if seuil is None else flt(seuil))


def _clients(groupe=None, type_client=None, recherche=None, avec_bl=None) -> list:
    """La liste des clients à examiner, filtrée EN BASE.

    ⚠️ FILTRER APRÈS AVOIR TOUT CALCULÉ SERAIT UNE ERREUR : les agrégats portent sur 10 000
    pièces, mais la liste des clients, elle, se réduit ici — c'est elle qui borne la page.
    """
    conditions, params = ["c.disabled = 0"], {}
    if groupe:
        conditions.append("c.customer_group = %(groupe)s")
        params["groupe"] = groupe
    if type_client:
        conditions.append("c.customer_type = %(type_client)s")
        params["type_client"] = type_client
    if recherche:
        # Un seul champ de recherche pour le nom ET le téléphone : l'utilisateur tape ce qu'il a
        # sous la main. Les espaces des numéros (« 26 130 274 ») sont retirés des deux côtés,
        # sinon chercher « 26130274 » ne trouve rien.
        params["q"] = "%%%s%%" % (recherche or "").strip()
        params["qtel"] = "%%%s%%" % "".join((recherche or "").split())
        tel = " OR ".join(
            "REPLACE(REPLACE(IFNULL(c.`%s`, ''), ' ', ''), '-', '') LIKE %%(qtel)s" % champ
            for champ in CHAMPS_TELEPHONE)
        conditions.append("(c.name LIKE %%(q)s OR c.customer_name LIKE %%(q)s OR %s)" % tel)
    return frappe.db.sql(
        """SELECT c.name, c.customer_name, c.customer_group, c.customer_type,
                  COALESCE(NULLIF(c.custom_liste_telephone, ''), c.mobile_no) AS telephone
           FROM `tabCustomer` c WHERE %s ORDER BY c.customer_name""" % " AND ".join(conditions),
        params, as_dict=True)


def lignes(groupe=None, type_client=None, recherche=None, seulement_ecarts=0,
           masquer_ignores=1) -> list:
    """Une ligne par client, avec ses trois totaux et ses deux deltas."""
    cdes, bls, regl, jrn = commandes(), bons_de_livraison(), reglements(), journal()
    av, ign = avances(), ignores()
    # Lus UNE fois : `lignes` boucle sur des milliers de clients, et un get_single_value par
    # ligne rechargerait le réglage autant de fois.
    seuils = tolerances()
    livr = livraisons_detail()
    pe_reprise = paiements_ouverture()
    vent = ventilation(exclure=pe_reprise)
    rep_pay, rep_jrn = reprise_paiements(), reprise_journal()

    out = []
    for c in _clients(groupe, type_client, recherche):
        cde_total, cde_nb = cdes.get(c.name, (0.0, 0))
        bl_total, bl_nb = bls.get(c.name, (0.0, 0))
        pay_total, pay_nb = regl.get(c.name, (0.0, 0))
        jrn_net, jrn_nb = jrn.get(c.name, (0.0, 0))
        avance = av.get(c.name, {})

        rep_p_total, rep_p_nb = rep_pay.get(c.name, (0.0, 0))
        rep_j_total, rep_j_nb = rep_jrn.get(c.name, (0.0, 0))
        reprise = flt(rep_p_total + rep_j_total, PRECISION)

        # Ce que le client a effectivement soldé : ses règlements PLUS ce que le journal a passé
        # en sa faveur (avoirs, régularisations, pertes). Les additionner est le seul moyen de
        # comparer honnêtement à ses commandes.
        regle = flt(pay_total + jrn_net, PRECISION)
        ligne = {
            "client": c.name,
            "nom": c.customer_name or c.name,
            "groupe": c.customer_group or "",
            "type": c.customer_type or "",
            "telephone": c.telephone or "",
            "commandes": cde_total, "nb_commandes": cde_nb,
            "bl": bl_total, "nb_bl": bl_nb,
            "a_des_bl": bool(bl_nb),
            "livraisons": livr.get(c.name, {}),
            "paiements": pay_total, "nb_paiements": pay_nb,
            "journal": jrn_net, "nb_journal": jrn_nb,
            "regle": regle,
            "avance_non_affectee": flt(avance.get("non_affectee"), PRECISION),
            "avance_sur_commande": flt(avance.get("sur_commande"), PRECISION),
            "reprise": reprise,
            # ⚠️ LA REPRISE SORT DU DELTA. Ces règlements soldent des factures d'ouverture, pas
            # des commandes de cet ERP : les compter ferait paraître surpayés des clients
            # parfaitement à jour.
            "delta_paiement": flt(regle - reprise - cde_total, PRECISION),
            "delta_bl": flt(bl_total - cde_total, PRECISION),
            "ignore": c.name in ign,
            "motif": ign.get(c.name, ""),
        }

        # LE DÉTAIL PAR TYPE, trié du plus gros au plus petit — c'est ainsi qu'on lit une
        # ventilation : par ce qui pèse.
        cats = sorted(vent.get(c.name, []), key=lambda x: -abs(x["total"]))
        # Le journal du client, MOINS ce qui relève de la reprise : sinon la même écriture
        # compterait dans les deux catégories.
        jrn_courant = flt(jrn_net - rep_j_total, PRECISION)
        jrn_nb_courant = max(0, jrn_nb - rep_j_nb)
        if reprise:
            cats.append({"cle": CLE_REPRISE, "mode": "Reprise d’historique", "compte": "",
                         "groupe": "", "libelle": "Reprise d’historique — soldes d’avant la "
                                                  "migration (factures d’ouverture)",
                         "encaisse": True, "total": reprise, "nb": rep_p_nb + rep_j_nb})
        jrn_net, jrn_nb = jrn_courant, jrn_nb_courant
        if jrn_net:
            # L'écriture de journal est un type de règlement à part entière : réduction
            # accordée sur commande, avoir, régularisation. Elle n'a ni mode ni compte de
            # destination, d'où sa catégorie propre.
            cats.append({"cle": CLE_JOURNAL, "mode": "Écriture de journal", "compte": "",
                         "groupe": "", "libelle": "Écriture de journal — réduction, avoir, "
                                                  "régularisation",
                         # Décision utilisateur 04/09/2026 : une écriture de journal sur un
                         # compte client SOLDE réellement la créance — remise accordée, avoir,
                         # régularisation. Elle compte donc comme encaissée.
                         "encaisse": True, "total": jrn_net, "nb": jrn_nb})
        ligne["ventilation"] = cats
        # Le total de la ventilation DOIT égaler la colonne « Réglé » : c'est le contrôle que
        # l'utilisateur fait à l'œil, et un écran qui ne s'additionne pas ne se croit pas.
        ligne["total_ventile"] = flt(sum(x["total"] for x in cats), PRECISION)
        # ⚠️ CE QUI EST VRAIMENT ARRIVÉ. « Réglé » compte tout, y compris les dettes non payées
        # (71 706 DT au total) et les pertes : des pièces qui soldent une commande sans qu-un
        # dinar ait bougé. Les distinguer est le seul moyen de savoir ce qu-on a encaissé.
        # `encaisse_reel` : ce qui SOLDE la créance, cash ou non — la retenue à la source et
        # l'écriture de journal en font partie. Le nom du champ est historique.
        ligne["encaisse_reel"] = flt(sum(x["total"] for x in cats if x["encaisse"]), PRECISION)
        ligne["non_encaisse"] = flt(ligne["regle"] - ligne["encaisse_reel"], PRECISION)
        ligne["ecart_paiement"] = ecart_significatif(ligne["delta_paiement"], seuils["montant"])
        ligne["ecart_bl"] = ecart_significatif(ligne["delta_bl"], seuils["bl"])
        ligne["en_ecart"] = ligne["ecart_paiement"] or ligne["ecart_bl"]

        # Un client sans aucune pièce n'a rien à dire : il encombrerait la liste sans jamais
        # rien révéler (613 clients sans groupe, la plupart n'ont jamais rien commandé).
        if not (cde_nb or bl_nb or pay_nb or jrn_nb):
            continue
        if frappe.utils.cint(masquer_ignores) and ligne["ignore"]:
            continue
        if frappe.utils.cint(seulement_ecarts) and not ligne["en_ecart"]:
            continue
        out.append(ligne)
    return out


# ---------------------------------------------------------------- ventilation

# ⚠️ `account_type` NE SERT À RIEN ICI. Dans ce plan comptable, TOUS les comptes de destination
# sont typés « Bank » ou « Cash » — y compris « Dettes », « Chèques », « Perte de non paiement »
# et « Livraison Aramex ». S'y fier ferait passer 71 706 DT de dettes non payées pour de
# l'argent encaissé. C'est le GROUPE PARENT qui tranche, et lui seul (mesuré le 04/09/2026).
GROUPE_BANQUE = "Comptes bancaires - A&S"
GROUPE_CAISSE = "Liquidités - A&S"
GROUPE_CREANCE = "Liste créance - A&S"
GROUPE_IMPOTS = "Actifs d'Impôts - A&S"

#: Ce que devient le groupe dans le libellé d'une catégorie. Un groupe inconnu garde son nom :
#: une nouvelle famille de comptes apparaît alors d'elle-même, sans toucher au code.
LIBELLE_GROUPE = {
    GROUPE_BANQUE: "encaissé en banque",
    GROUPE_CAISSE: "en caisse",
    GROUPE_CREANCE: "en attente, pas encore encaissé",
    "Charges Indirectes - A&S": "perte assumée",
    GROUPE_IMPOTS: "versé au Trésor pour notre compte",
}

#: Les groupes qui SOLDENT RÉELLEMENT la créance du client. Le drapeau ne dit pas « c'est du
#: cash » mais « le client ne doit plus cela » — c'est la question que pose l'écran.
#:
#: ⚠️ LA RETENUE À LA SOURCE EN FAIT PARTIE (décision utilisateur 04/09/2026). Le client la verse
#: au Trésor pour notre compte : il a payé, nous recevons un crédit d'impôt au lieu d'espèces.
#: La ranger en « attente » faisait paraître 4 862 DT impayés sur 154 pièces alors que rien
#: n'est dû. Même raisonnement que pour l'écriture de journal, admise la veille.
#:
#: Ce qui reste dehors est une promesse, pas un règlement : un chèque en portefeuille, une dette
#: portée au compte de créance, une perte assumée.
GROUPES_ENCAISSES = (GROUPE_BANQUE, GROUPE_CAISSE, GROUPE_IMPOTS)

CLE_JOURNAL = "journal"
CLE_REPRISE = "reprise"

#: ⚠️ LA REPRISE D'HISTORIQUE N'EST PAS UNE VENTE DE CET ERP. Onze factures d'ouverture
#: (31 322 DT) portent le solde que les clients devaient AVANT la migration ; vingt et un
#: paiements les soldent, et treize écritures passent par le compte temporaire d'ouverture.
#: Ces règlements n'ont, par construction, AUCUNE commande en face : les compter dans la
#: comparaison ferait paraître surpayés des clients parfaitement à jour. Ils sortent donc du
#: delta et s'affichent sur leur propre ligne (décision utilisateur 04/09/2026).
VOUCHER_OUVERTURE = "Opening Entry"


def paiements_ouverture() -> set:
    """Les Payment Entry qui soldent une facture d'OUVERTURE. -> {noms}."""
    return {r[0] for r in frappe.db.sql(
        """SELECT DISTINCT pe.name
           FROM `tabPayment Entry Reference` per
           INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
           INNER JOIN `tabSales Invoice` si ON si.name = per.reference_name
           WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive'
             AND pe.party_type = 'Customer'
             AND per.reference_doctype = 'Sales Invoice' AND si.is_opening = 'Yes'""")}


def reprise_paiements() -> dict:
    """{client: (total, nb)} — les règlements qui soldent une facture d'ouverture."""
    noms = paiements_ouverture()
    if not noms:
        return {}
    ph = ",".join(["%s"] * len(noms))
    return {r.cle: (flt(r.total, PRECISION), int(r.nb or 0)) for r in frappe.db.sql(
        f"""SELECT party AS cle, SUM(paid_amount) AS total, COUNT(*) AS nb
            FROM `tabPayment Entry` WHERE name IN ({ph}) GROUP BY party""",
        tuple(noms), as_dict=True)}


def reprise_journal() -> dict:
    """{client: (net, nb)} — les écritures de REPRISE de solde, reconnues par leur type.

    ⚠️ LE COMPTE D'OUVERTURE NE PEUT PAS SERVIR DE MARQUEUR. Une première version reconnaissait
    aussi les écritures dont la CONTREPARTIE est « Compte temporaire - compte d'overture ». Or ce
    compte sert de compte de passage pour des avoirs et des ajustements tout ordinaires :
    « Reliquat avoir paiement », « Ajustement Erreur Néjib pour les osmoseurs »… Sur LIMPID'EAU,
    la règle rangeait ainsi 2 621,303 DT d'avoirs en reprise d'historique, et l'écran annonçait
    un impayé de 2 621 chez un client dont les comptes tombent juste à 0,200 près — ce que le
    bandeau « Totaux cohérents » de sa fiche disait pourtant déjà (constaté le 04/09/2026).

    Seul `voucher_type = 'Opening Entry'` désigne une vraie reprise. Le nom d'un compte ne prouve
    rien de l'intention de l'écriture.
    """
    return {r.cle: (flt(r.total, PRECISION), int(r.nb or 0)) for r in frappe.db.sql(
        """SELECT jea.party AS cle, SUM(jea.credit - jea.debit) AS total,
                  COUNT(DISTINCT jea.parent) AS nb
           FROM `tabJournal Entry Account` jea
           INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
           WHERE je.docstatus = 1 AND jea.party_type = 'Customer' AND jea.party IS NOT NULL
             AND je.voucher_type = %s
           GROUP BY jea.party""", (VOUCHER_OUVERTURE,), as_dict=True)}


def _groupes_de_comptes() -> dict:
    """{compte: groupe parent} — une requête, pas une par ligne."""
    return {r.name: r.parent_account or ""
            for r in frappe.get_all("Account", filters={"is_group": 0},
                                    fields=["name", "parent_account"], limit_page_length=0)}


def categorie(mode, compte, groupe) -> dict:
    """La catégorie d'un règlement : son mode ET l'endroit où l'argent a atterri.

    Les deux ensemble, jamais l'un sans l'autre. « Chèque » ne dit pas s'il a été encaissé ;
    « compte bancaire » ne dit pas par quel moyen. C'est leur croisement qui produit les
    indicateurs demandés — chèques encaissés, chèques en portefeuille, espèces en caisse,
    espèces versées en banque — et il en produira d'autres tout seul si un mode ou un compte
    apparaît.
    """
    mode = (mode or "Sans mode").strip()
    suffixe = LIBELLE_GROUPE.get(groupe, (groupe or "compte inconnu").replace(" - A&S", ""))
    return {
        "cle": "%s|%s" % (mode, compte or ""),
        "mode": mode,
        "compte": compte or "",
        "groupe": groupe or "",
        "libelle": "%s — %s" % (mode, suffixe),
        "encaisse": groupe in GROUPES_ENCAISSES,
    }


def ventilation(exclure=None) -> dict:
    """{client: [catégorie…]} — le détail des règlements, par mode et par destination.

    `exclure` : les Payment Entry à ne pas ventiler ici — les règlements de reprise, qui ont
    leur propre catégorie. Sans quoi ils apparaîtraient deux fois.
    """
    groupes = _groupes_de_comptes()
    exclure = exclure or set()
    condition, params = "", ()
    if exclure:
        condition = " AND name NOT IN (%s)" % ",".join(["%s"] * len(exclure))
        params = tuple(exclure)
    out = {}
    for r in frappe.db.sql(
            """SELECT party AS cle, mode_of_payment AS mode, paid_to AS compte,
                      SUM(paid_amount) AS total, COUNT(*) AS nb
               FROM `tabPayment Entry`
               WHERE docstatus = 1 AND payment_type = 'Receive' AND party_type = 'Customer'
                 AND party IS NOT NULL""" + condition + """
               GROUP BY party, mode_of_payment, paid_to""", params, as_dict=True):
        cat = categorie(r.mode, r.compte, groupes.get(r.compte, ""))
        cat.update({"total": flt(r.total, PRECISION), "nb": int(r.nb or 0)})
        out.setdefault(r.cle, []).append(cat)
    return out
