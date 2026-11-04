"""Analyse du compte « Economiq Aqua Solution - A&S » — l'argent qu'Economiq encaisse pour nous.

LE CIRCUIT, tel que les comptes sont sortis chaque mois :

    debit   Economiq Aqua Solution   chaque reglement qu'Economiq encaisse chez NOS clients,
                                     sur les ventes qu'il realise pour Aqua World (Payment Entry
                                     portee a ce compte)
    credit  Economiq Aqua Solution   « Ventes Economiq MM/AAAA » — la ligne de l'ecriture de
                                     bilan du mois (`ecriture.creer`), egale aux ventes de la
                                     section Economiq du bilan

Une fois l'ecriture du mois passee, tout ce qu'Economiq a encaisse a ete repris : le compte doit
REVENIR A ZERO. La projection le verifie avant meme que l'ecriture existe :

    solde projete = solde du compte aujourd'hui − ventes des mois dont l'ecriture reste a passer

Zero, ou un ecart qu'on explique commande par commande.

⚠️ L'ECRITURE CREDITE LA VENTE DU BILAN, PAS CE QU'ECONOMIQ A ENCAISSE. La « vente » du bilan est
le total des articles : elle ne compte ni le timbre fiscal (1,000 ajoute au total de la commande)
ni la TVA facturee EN SUS d'un prix hors taxe (commandes de societes). Le client, lui, paie le
total de la commande. La difference reste au compte, mois apres mois, sans que rien ne la
signale : 9,700 au 04/11/2026 — quatre timbres (mai, juin, aout ×2) et 5,700 de TVA sur
SAL-ORD-2026-02501 (juillet). C'est de l'argent qu'Economiq detient et que l'ecriture ne lui
reprend pas.

LECTURE SEULE : rien n'est ecrit.
"""
from __future__ import annotations

import re
from collections import defaultdict

PRECISION = 3

#: Le compte analyse — celui que credite l'ecriture de bilan (`ecriture.COMPTE_PARTENAIRE`).
COMPTE = "Economiq Aqua Solution - A&S"

#: Premier mois analyse. ⚠️ CHOISI, PAS DEVINE : c'est le premier mois dont l'ecriture de bilan
#: credite ce compte des « Ventes Economiq » du bilan (ACC-JV-2026-00429, mai 2026), et le compte
#: etait a 0,000 au 30/04/2026 (constate sur la base de prod du 04/11/2026). Avant, les ecritures
#: le soldaient autrement (« Commission », « Balance activité installation ») : les rapprocher
#: commande par commande du bilan d'aujourd'hui n'aurait pas de sens. Ce qui precede est resume
#: dans le solde d'ouverture.
DEBUT = "2026-05"

#: Une piece citee dans la remarque d'une ligne d'ecriture : « … [réf: ACC-PAY-2026-03392] ».
_REF_PIECE = re.compile(r"ACC-PAY-\d{4}-\d+")

# Natures des mouvements hors bilan.
LETTRE = "lettre"
HORS_BILAN = "hors_bilan"
NON_AFFECTE = "non_affecte"
AUTRE = "autre"

# Causes d'ecart d'une commande.
TAXES = "taxes"
TROP_PERCU = "trop_percu"
AILLEURS = "ailleurs"
NON_ENCAISSE = "non_encaisse"
PARTIEL = "partiel"


def _arr(v) -> float:
    return round(float(v or 0), PRECISION)


def montant(v) -> str:
    """1234.5 -> « 1 234,500 ». Le format des ecrans et du rapport."""
    v = _arr(v)
    signe = "-" if v < 0 else ""
    entier, dec = ("%.3f" % abs(v)).split(".")
    groupes = []
    while entier:
        groupes.insert(0, entier[-3:])
        entier = entier[:-3]
    return "%s%s,%s" % (signe, " ".join(groupes), dec)


# ------------------------------------------------------------------ le rapprochement (pur)

def cause(commande: dict, ecart: float) -> dict:
    """Pourquoi l'encaisse d'une commande ne vaut pas sa vente au bilan. Fonction pure.

    `commande` : {vente, encaisse, total_ttc, taxes_en_sus: [{libelle, montant}], paiements:
    [{compte, mode, montant}] (reglements portes AILLEURS que sur ce compte), en_cours}.
    """
    ecart = _arr(ecart)
    if ecart > 0:
        sur_prix = _arr(_arr(commande.get("total_ttc")) - _arr(commande.get("vente")))
        taxes = [t for t in commande.get("taxes_en_sus") or [] if _arr(t.get("montant"))]
        if taxes and abs(ecart - sur_prix) <= 0.001:
            return {"code": TAXES, "texte": "Taxes en sus du prix, encaissées par Economiq et "
                    "absentes de la vente du bilan : %s" % " + ".join(
                        "%s %s" % (t.get("libelle") or "taxe", montant(t.get("montant")))
                        for t in taxes)}
        return {"code": TROP_PERCU, "texte": "Encaissé au-delà de la vente du bilan "
                "(commande à %s TTC)" % montant(commande.get("total_ttc"))}
    ailleurs = [p for p in commande.get("paiements") or [] if _arr(p.get("montant"))]
    if ailleurs:
        return {"code": AILLEURS, "texte": "Réglée ailleurs que chez Economiq : %s" % ", ".join(
            "%s %s → %s" % (montant(p.get("montant")), p.get("mode") or "?", p.get("compte") or "?")
            for p in ailleurs)}
    if not _arr(commande.get("encaisse")):
        return {"code": NON_ENCAISSE, "texte": "Pas encore encaissée par Economiq"
                if commande.get("en_cours") else "Rien d’encaissé sur ce compte"}
    return {"code": PARTIEL, "texte": "Encaissée en partie"}


def rapprocher(ouverture: float, mois: list, mouvements: list) -> dict:
    """Le compte, mois par mois et commande par commande, et sa projection. Fonction pure.

    `mois` : [{mois, libelle, en_cours, ventes, ecritures: [noms des ecritures de bilan du
    mois], commandes: [{sales_order, customer, vente, total_ttc, taxes_en_sus, paiements}],
    aqua_world: [commandes comptees cote Aqua World], ecartees: [commandes ecartees]}].

    `mouvements` : les lignes du compte depuis `DEBUT`, [{date, voucher_type, voucher_no, debit,
    credit, remarque, commandes: [{sales_order, montant}] (reglement ventile par commande),
    non_affecte}].

    L'IDENTITE QUI REND LE CHIFFRE VERIFIABLE :

        solde projete = ouverture + Σ ecarts des mois + Σ mouvements hors bilan

    ou l'ecart d'un mois vaut encaisse − credite (ecriture passee) ou encaisse − ventes (ecriture
    a passer). `coherent` dit si elle tient au millime ; si elle ne tient pas, un mouvement a
    echappe au classement et le chiffre ne doit pas etre presente comme explique.
    """
    ouverture = _arr(ouverture)
    mouvements = list(mouvements or [])
    du_mois = {}                      # commande -> mois de bilan
    for m in mois or []:
        for c in m.get("commandes") or []:
            du_mois[c["sales_order"]] = m["mois"]
    bilan_de = {je: m["mois"] for m in mois or [] for je in m.get("ecritures") or []}
    cote_aqua = {so: m["mois"] for m in mois or [] for so in m.get("aqua_world") or []}
    ecartees = {so for m in mois or [] for so in m.get("ecartees") or []}

    # 1. Lettrage : une ligne d'ecriture qui cite un reglement porte a ce compte le reprend.
    #    (mai 2026 : « Remboursement paiement … [réf: ACC-PAY-2026-03392] », 211,000.)
    regles = {mv["voucher_no"] for mv in mouvements
              if mv.get("voucher_type") == "Payment Entry" and _arr(mv.get("debit"))}
    lettres = set()
    for mv in mouvements:
        if mv.get("voucher_type") == "Journal Entry" and _arr(mv.get("credit")):
            lettres.update(r for r in _REF_PIECE.findall(mv.get("remarque") or "") if r in regles)

    encaisse = defaultdict(float)
    pieces = defaultdict(list)
    credite = defaultdict(float)
    hors = []

    def hors_bilan(mv, montant_signe, nature, texte, sales_order=None):
        hors.append({"date": mv.get("date"), "voucher_type": mv.get("voucher_type"),
                     "voucher_no": mv.get("voucher_no"), "montant": _arr(montant_signe),
                     "nature": nature, "texte": texte, "sales_order": sales_order})

    for mv in mouvements:
        debit, credit = _arr(mv.get("debit")), _arr(mv.get("credit"))
        nom, genre = mv.get("voucher_no"), mv.get("voucher_type")
        if genre == "Journal Entry":
            cites = [r for r in _REF_PIECE.findall(mv.get("remarque") or "") if r in lettres]
            if cites and credit:
                hors_bilan(mv, debit - credit, LETTRE, "Reprend %s" % ", ".join(cites))
            elif nom in bilan_de:
                credite[bilan_de[nom]] = _arr(credite[bilan_de[nom]] + credit - debit)
            else:
                hors_bilan(mv, debit - credit, AUTRE, "Écriture hors bilan du partenaire")
            continue
        if genre == "Payment Entry" and debit:
            if nom in lettres:
                so = next((p.get("sales_order") for p in mv.get("commandes") or []
                           if p.get("sales_order")), None)
                hors_bilan(mv, debit, LETTRE, "Repris par une écriture qui le cite", so)
                continue
            for part in mv.get("commandes") or []:
                so, m = part.get("sales_order"), _arr(part.get("montant"))
                if so in du_mois:
                    encaisse[so] = _arr(encaisse[so] + m)
                    pieces[so].append({"payment_entry": nom, "date": mv.get("date"), "montant": m})
                elif so in cote_aqua:
                    hors_bilan(mv, m, HORS_BILAN, "Commande comptée côté Aqua World (%s) : "
                               "l’écriture ne reprend pas ce règlement" % cote_aqua[so], so)
                elif so in ecartees:
                    hors_bilan(mv, m, HORS_BILAN, "Commande écartée du bilan (tâche ouverte)", so)
                else:
                    hors_bilan(mv, m, HORS_BILAN, "Commande hors des ventes Economiq du bilan", so)
            reste = _arr(mv.get("non_affecte"))
            if reste:
                hors_bilan(mv, reste, NON_AFFECTE, "Règlement non affecté à une commande")
            continue
        hors_bilan(mv, debit - credit, AUTRE, "Mouvement hors bilan du partenaire")

    lignes_mois, commandes = [], []
    for m in mois or []:
        ventes = _arr(m.get("ventes"))
        enc = _arr(sum(encaisse[c["sales_order"]] for c in m.get("commandes") or []))
        a_passer = not (m.get("ecritures") or [])
        cred = 0.0 if a_passer else _arr(credite[m["mois"]])
        lignes_mois.append({
            "mois": m["mois"], "libelle": m.get("libelle") or m["mois"],
            "en_cours": bool(m.get("en_cours")), "ventes": ventes, "encaisse": enc,
            "credite": None if a_passer else cred, "ecritures": m.get("ecritures") or [],
            "a_passer": a_passer and bool(ventes or enc),
            "ecart": _arr(enc - (ventes if a_passer else cred)),
            "ecart_ecriture": 0.0 if a_passer else _arr(ventes - cred)})
        for c in m.get("commandes") or []:
            e = _arr(encaisse[c["sales_order"]] - _arr(c.get("vente")))
            if abs(e) <= 0.001:
                continue
            commandes.append({
                "sales_order": c["sales_order"], "customer": c.get("customer"),
                "mois": m["mois"], "libelle": m.get("libelle") or m["mois"],
                "vente": _arr(c.get("vente")), "encaisse": _arr(encaisse[c["sales_order"]]),
                "ecart": e, "pieces": pieces[c["sales_order"]],
                "cause": cause({**c, "encaisse": encaisse[c["sales_order"]],
                                "en_cours": m.get("en_cours")}, e)})

    solde = _arr(ouverture + sum(_arr(mv.get("debit")) - _arr(mv.get("credit"))
                                 for mv in mouvements))
    a_crediter = _arr(sum(l["ventes"] for l in lignes_mois if l["a_passer"]))
    projete = _arr(solde - a_crediter)
    explique = _arr(ouverture + sum(l["ecart"] for l in lignes_mois)
                    + sum(h["montant"] for h in hors))

    par_cause = defaultdict(float)
    for c in commandes:
        par_cause[c["cause"]["code"]] = _arr(par_cause[c["cause"]["code"]] + c["ecart"])
    ecriture = _arr(sum(l["ecart_ecriture"] for l in lignes_mois))
    if ecriture:
        par_cause["ecriture"] = ecriture
    hors_total = _arr(sum(h["montant"] for h in hors))
    if hors_total:
        par_cause["hors_bilan"] = hors_total
    if ouverture:
        par_cause["ouverture"] = ouverture

    return {
        "ouverture": ouverture,
        "mois": lignes_mois,
        "commandes": commandes,
        "hors_bilan": hors,
        "solde": solde,
        "a_crediter": a_crediter,
        "a_passer": [{"mois": l["mois"], "libelle": l["libelle"], "ventes": l["ventes"],
                      "en_cours": l["en_cours"]} for l in lignes_mois if l["a_passer"]],
        "projete": projete,
        "explique": explique,
        "coherent": abs(projete - explique) <= 0.001,
        "par_cause": dict(par_cause),
    }


# ------------------------------------------------------------------ lecture de la base

def _ventilation(debits: dict) -> dict:
    """Chaque reglement porte a ce compte, ventile par commande. -> {piece: [{sales_order, montant}]}

    Une reference a une facture est rapportee a ses commandes au prorata des articles — la meme
    regle que `economiq.commandes`, pour la meme raison : a la facturation, le reglement est
    realloue a la facture et perd sa reference a la commande.
    """
    import frappe
    from frappe.utils import flt

    if not debits:
        return {}
    refs = frappe.get_all("Payment Entry Reference",
                          filters={"parent": ["in", list(debits)], "docstatus": 1},
                          fields=["parent", "reference_doctype", "reference_name",
                                  "allocated_amount"], limit_page_length=0)
    factures = {r.reference_name for r in refs if r.reference_doctype == "Sales Invoice"}
    poids = defaultdict(lambda: defaultdict(float))
    if factures:
        for it in frappe.get_all("Sales Invoice Item",
                                 filters={"parent": ["in", list(factures)], "docstatus": 1},
                                 fields=["parent", "sales_order", "amount"],
                                 limit_page_length=0):
            if it.sales_order:
                poids[it.parent][it.sales_order] += flt(it.amount)
    out = defaultdict(list)
    for r in refs:
        alloue = _arr(r.allocated_amount)
        if r.reference_doctype == "Sales Order":
            out[r.parent].append({"sales_order": r.reference_name, "montant": alloue})
        elif r.reference_doctype == "Sales Invoice" and poids.get(r.reference_name):
            total = sum(poids[r.reference_name].values()) or 1.0
            for so, part in poids[r.reference_name].items():
                out[r.parent].append({"sales_order": so, "montant": _arr(alloue * part / total)})
        else:
            out[r.parent].append({"sales_order": None, "montant": alloue})
    return out


def _taxes_en_sus(commandes: list) -> dict:
    """Les taxes AJOUTEES au prix des commandes : timbre fiscal, TVA d'un prix hors taxe."""
    import frappe

    if not commandes:
        return {}
    out = defaultdict(list)
    for t in frappe.get_all("Sales Taxes and Charges",
                            filters={"parenttype": "Sales Order", "parent": ["in", commandes]},
                            fields=["parent", "account_head", "description", "tax_amount",
                                    "included_in_print_rate"],
                            order_by="idx asc", limit_page_length=0):
        if t.included_in_print_rate or not _arr(t.tax_amount):
            continue
        libelle = (t.account_head or t.description or "taxe").rsplit(" - ", 1)[0]
        out[t.parent].append({"libelle": libelle, "montant": _arr(t.tax_amount)})
    return out


def _mois_de(debut: str, fin: str) -> list:
    from bank_retenue_sync.facturation import periode

    annee, numero = periode.eclater(debut)
    out = []
    while periode.cle(annee, numero) <= fin and len(out) < 60:
        out.append(periode.cle(annee, numero))
        annee, numero = periode.suivant(annee, numero)
    return out


def analyse() -> dict:
    """Le compte du partenaire, depuis `DEBUT`, rapproche du bilan, avec sa projection."""
    import frappe
    from frappe.utils import nowdate

    from bank_retenue_sync.facturation import periode
    from bank_retenue_sync.partenaire import ecriture, economiq

    if not frappe.db.exists("Account", COMPTE):
        return {"disponible": False, "compte": COMPTE,
                "message": "Le compte {0} n’existe pas sur ce site.".format(COMPTE)}

    debut = periode.bornes(DEBUT)[0]
    cles = _mois_de(DEBUT, periode.courant())
    ouverture = _arr(frappe.db.sql(
        """select sum(debit) - sum(credit) from `tabGL Entry`
           where account = %s and is_cancelled = 0 and posting_date < %s""",
        (COMPTE, debut))[0][0])
    gl = frappe.db.sql(
        """select posting_date, voucher_type, voucher_no, debit, credit, remarks
           from `tabGL Entry` where account = %s and is_cancelled = 0 and posting_date >= %s
           order by posting_date asc, creation asc""", (COMPTE, debut), as_dict=True)

    debits = {g.voucher_no for g in gl if g.voucher_type == "Payment Entry" and _arr(g.debit)}
    ventilation = _ventilation(debits)
    mouvements = []
    for g in gl:
        mv = {"date": str(g.posting_date), "voucher_type": g.voucher_type,
              "voucher_no": g.voucher_no, "debit": _arr(g.debit), "credit": _arr(g.credit),
              "remarque": g.remarks or "", "commandes": [], "non_affecte": 0.0}
        if g.voucher_no in debits and mv["debit"]:
            parts = ventilation.get(g.voucher_no) or []
            mv["commandes"] = [p for p in parts if p["sales_order"]]
            mv["non_affecte"] = max(0.0, _arr(mv["debit"] - sum(p["montant"]
                                                                  for p in mv["commandes"])))
        mouvements.append(mv)

    mois = []
    for cle in cles:
        bilan = economiq._bilan(cle)
        if bilan is None:
            return {"disponible": False, "compte": COMPTE,
                    "message": "Le bilan de {0} est indisponible : l’analyse ne peut pas "
                               "rapprocher le compte.".format(periode.libelle(cle))}
        sections = {s.get("key"): s for s in bilan.get("sections") or []}
        eco = sections.get("economiq") or {}
        piece = ecriture.lire(cle)
        mois.append({
            "mois": cle, "libelle": periode.libelle(cle), "en_cours": periode.est_en_cours(cle),
            "ventes": _arr((eco.get("totals") or {}).get("vente")),
            "ecritures": ([piece["journal_entry"]] + list(piece.get("autres") or []))
            if piece else [],
            "commandes": [{
                "sales_order": o["name"], "customer": o.get("customer"),
                "vente": _arr((o.get("totals") or {}).get("vente")),
                "total_ttc": _arr(o.get("total_ttc")),
                "paiements": [{"compte": p.get("paid_to"), "mode": p.get("mode"),
                               "montant": _arr(p.get("amount"))}
                              for p in o.get("payments") or [] if p.get("paid_to") != COMPTE],
            } for o in eco.get("orders") or []],
            "aqua_world": [o["name"] for o in (sections.get("aqua_world") or {}).get("orders")
                           or []],
            "ecartees": list(bilan.get("ecartees") or []),
        })

    taxes = _taxes_en_sus([c["sales_order"] for m in mois for c in m["commandes"]])
    for m in mois:
        for c in m["commandes"]:
            c["taxes_en_sus"] = taxes.get(c["sales_order"]) or []

    out = rapprocher(ouverture, mois, mouvements)
    out.update({"disponible": True, "compte": COMPTE, "debut": debut,
                "debut_libelle": periode.libelle(DEBUT), "date": nowdate()})
    return out
