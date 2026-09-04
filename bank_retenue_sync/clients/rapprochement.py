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
    """BL VALIDÉS uniquement — c'est la question posée : « a-t-il des BL validés ? »."""
    return _somme("Delivery Note", "customer", "docstatus = 1")


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

    out = []
    for c in _clients(groupe, type_client, recherche):
        cde_total, cde_nb = cdes.get(c.name, (0.0, 0))
        bl_total, bl_nb = bls.get(c.name, (0.0, 0))
        pay_total, pay_nb = regl.get(c.name, (0.0, 0))
        jrn_net, jrn_nb = jrn.get(c.name, (0.0, 0))
        avance = av.get(c.name, {})

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
            "paiements": pay_total, "nb_paiements": pay_nb,
            "journal": jrn_net, "nb_journal": jrn_nb,
            "regle": regle,
            "avance_non_affectee": flt(avance.get("non_affectee"), PRECISION),
            "avance_sur_commande": flt(avance.get("sur_commande"), PRECISION),
            "delta_paiement": flt(regle - cde_total, PRECISION),
            "delta_bl": flt(bl_total - cde_total, PRECISION),
            "ignore": c.name in ign,
            "motif": ign.get(c.name, ""),
        }
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
