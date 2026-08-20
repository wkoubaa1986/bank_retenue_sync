"""Le tableau de bord du partenaire ECONOMIQ AQUA SOLUTIONS, en lecture seule.

⚠️ LE BILAN N'EST PAS RECALCULE ICI. `customization_app.bilan_vente.get_data` est deja la source
de verite des ventes, achats et benefices des deux societes — c'est ce que la page « Bilan Vente
Economiq Aqua Solution » affiche. Le script externe, lui, scrapait un Script Report devenu
obsolete. On appelle la meme methode : deux ecrans qui montrent un benefice different sur le meme
mois seraient pires que pas d'ecran du tout.

⚠️ CET ONGLET N'ECRIT RIEN. Ni ecriture de journal, ni paiement, ni annulation de piece. Le
portage de ces gestes (le JV du bilan, la logique de dette qui annule des Payment Entry validees)
vient apres, et separement : ce sont eux qui sont irreversibles.
"""
from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt

from bank_retenue_sync.facturation import periode
from bank_retenue_sync.partenaire import echeancier

PRECISION = 3

CLIENT = "ECONOMIQ AQUA SOLUTIONS"

# Classification des reglements, reprise telle quelle de `get_economiq_situation.py`.
MODE_DETTE = "Dette non payée"
MODE_PERTE = "Perte de paiement"
COMPTE_DETTES = "Dettes - A&S"


def _bilan(mois: str) -> dict | None:
    """Les deux sections du bilan, ou None si customization_app n'est pas installee."""
    try:
        get_data = frappe.get_attr("customization_app.bilan_vente.get_data")
    except Exception:
        return None
    try:
        return get_data(month=mois)
    except Exception:
        frappe.log_error(title="Bilan Economiq indisponible", message=frappe.get_traceback())
        return None


def _section(bilan: dict, cle: str) -> dict:
    for s in (bilan or {}).get("sections") or []:
        if s.get("key") == cle:
            t = s.get("totals") or {}
            return {"ventes": flt(t.get("vente"), PRECISION),
                    "achats": flt(t.get("achat"), PRECISION),
                    "benefice": flt(t.get("benefice"), PRECISION),
                    "especes": flt(t.get("especes"), PRECISION),
                    "reste": flt(t.get("reste"), PRECISION)}
    return {"ventes": 0.0, "achats": 0.0, "benefice": 0.0, "especes": 0.0, "reste": 0.0}


def commandes(mois: str) -> list:
    """Les commandes du partenaire sur le mois, avec leurs reglements classes paye / non paye.

    ⚠️ « DETTE NON PAYEE » NE VEUT PAS DIRE NON PAYE. Le mode sert aussi a enregistrer un
    reglement passe par un tiers (Aramex, par exemple) : c'est le COMPTE credite qui tranche.
    Seul un reglement porte au compte des dettes est une dette reelle.
    """
    debut, fin = periode.bornes(mois)
    ordres = frappe.get_all(
        "Sales Order",
        filters={"customer": CLIENT, "docstatus": 1,
                 "transaction_date": ["between", [debut, fin]]},
        fields=["name", "transaction_date", "status", "grand_total"],
        order_by="transaction_date asc", limit_page_length=0)
    if not ordres:
        return []

    noms = [o.name for o in ordres]
    refs = frappe.get_all("Payment Entry Reference",
                          filters={"reference_doctype": "Sales Order",
                                   "reference_name": ["in", noms], "docstatus": 1},
                          fields=["parent", "reference_name", "allocated_amount"],
                          limit_page_length=0)
    pieces = {}
    if refs:
        pieces = {p.name: p for p in frappe.get_all(
            "Payment Entry", filters={"name": ["in", list({r.parent for r in refs})],
                                      "docstatus": 1},
            fields=["name", "mode_of_payment", "paid_to", "posting_date", "reference_no"],
            limit_page_length=0)}

    def _ligne(pe_nom: str, piece, montant: float, via: str = "") -> dict:
        mode = piece.mode_of_payment or ""
        paye = not (mode == MODE_PERTE
                    or (mode == MODE_DETTE and (piece.paid_to or "") == COMPTE_DETTES))
        return {"payment_entry": pe_nom, "montant": flt(montant, PRECISION),
                "mode": mode, "compte": piece.paid_to or "", "paye": paye,
                "date": str(piece.posting_date or ""), "via": via}

    reglements = defaultdict(list)
    deja = defaultdict(set)     # commande -> PE deja comptees (par la reference directe)
    for r in refs:
        piece = pieces.get(r.parent)
        if not piece:
            continue
        reglements[r.reference_name].append(_ligne(r.parent, piece, r.allocated_amount))
        deja[r.reference_name].add(r.parent)

    # ⚠️ UNE COMMANDE FACTUREE PERD SES REGLEMENTS. A la facturation (auto ou soumission d'un
    # encaissement), la Payment Entry est REALLOUEE a la facture : la reference Sales Order
    # disparait et la commande paraissait « non encaissee » a tort. Decision utilisateur
    # 2026-08-20 : un paiement lie a la facture d'une commande compte comme encaisse sur la
    # commande. Une facture peut couvrir PLUSIEURS commandes (facturation auto groupee) :
    # l'allocation est repartie au prorata du poids des articles de chaque commande.
    items = frappe.get_all("Sales Invoice Item",
                           filters={"sales_order": ["in", noms], "docstatus": 1},
                           fields=["parent", "sales_order", "amount"], limit_page_length=0)
    if items:
        poids = defaultdict(lambda: defaultdict(float))     # facture -> commande -> montant
        for it in items:
            poids[it.parent][it.sales_order] = flt(
                poids[it.parent][it.sales_order] + flt(it.amount), PRECISION)
        refs_fact = frappe.get_all("Payment Entry Reference",
                                   filters={"reference_doctype": "Sales Invoice",
                                            "reference_name": ["in", list(poids)],
                                            "docstatus": 1},
                                   fields=["parent", "reference_name", "allocated_amount"],
                                   limit_page_length=0)
        pieces_fact = {p.name: p for p in frappe.get_all(
            "Payment Entry", filters={"name": ["in", list({r.parent for r in refs_fact})],
                                      "docstatus": 1},
            fields=["name", "mode_of_payment", "paid_to", "posting_date", "reference_no"],
            limit_page_length=0)} if refs_fact else {}
        for r in refs_fact:
            piece = pieces_fact.get(r.parent)
            if not piece:
                continue
            repartition = poids[r.reference_name]
            total_poids = sum(repartition.values()) or 1.0
            for so, part in repartition.items():
                if r.parent in deja[so]:
                    continue        # deja comptee par sa reference directe a la commande
                montant = flt(flt(r.allocated_amount) * part / total_poids, PRECISION)
                if montant:
                    reglements[so].append(_ligne(r.parent, piece, montant,
                                                 via=r.reference_name))

    out = []
    for o in ordres:
        lignes = reglements.get(o.name, [])
        encaisse = flt(sum(l["montant"] for l in lignes if l["paye"]), PRECISION)
        du = flt(sum(l["montant"] for l in lignes if not l["paye"]), PRECISION)
        out.append({
            "sales_order": o.name,
            "date": str(o.transaction_date or ""),
            "statut": o.status,
            "total": flt(o.grand_total, PRECISION),
            "encaisse": encaisse,
            "non_paye": du,
            "restant": flt(flt(o.grand_total, PRECISION) - encaisse, PRECISION),
            "reglements": lignes,
        })
    return out


def tableau(mois: str) -> dict:
    """Tout ce que l'onglet affiche pour un mois. -> dict."""
    mois = periode.normaliser(mois)
    debut, fin = periode.bornes(mois)
    annee, numero = periode.eclater(mois)

    bilan = _bilan(mois)
    if bilan is None:
        return {"mois": mois, "libelle": periode.libelle(mois),
                "periode": {"debut": debut, "fin": fin}, "disponible": False,
                "message": "Le bilan (customization_app.bilan_vente) n'est pas disponible sur ce bench."}

    aqua = _section(bilan, "aqua_world")
    partenaire = _section(bilan, "economiq")

    # ⚠️ LE MOIS ENREGISTRE FAIT FOI SUR LE BILAN RECALCULE. Une fois le mois arrete, son
    # echeancier a ete communique au partenaire : le recalculer parce qu'une piece est arrivee
    # depuis changerait un montant deja annonce. On lit donc ce qui est en base et on ne
    # recalcule que ce qui ne l'est pas encore.
    from bank_retenue_sync.partenaire import historique

    enregistre = historique.lire(mois)
    charges_libres = (enregistre or {}).get("charges") or []
    total_charges = flt(sum(c["montant"] for c in charges_libres), PRECISION)

    if enregistre and enregistre["valide"]:
        aqua, partenaire = enregistre["bilan"]["aqua"], enregistre["bilan"]["partenaire"]

    liste_commandes = commandes(mois)
    total_commandes = flt(sum(c["total"] for c in liste_commandes), PRECISION)
    solde = echeancier.solde_net(aqua["benefice"], partenaire["benefice"])
    ajustement = echeancier.ajustement(aqua["benefice"], partenaire["benefice"], total_charges)

    # ⚠️ L'ECRITURE DE BILAN PRIME SUR LE CALCUL. Sa ligne d'equilibre au debiteur est le meme
    # ajustement, mais assise sur les achats reels ; le calcul, lui, repose sur des achats
    # sous-evalues tant que `tabItem Price` est vide. Juin 2026 : 389,450 contre 652,000.
    from bank_retenue_sync.partenaire import ecriture as M_ecriture

    piece = M_ecriture.lire(mois)
    ajustement_calcule = ajustement
    if piece:
        ajustement = piece["ajustement"]

    if enregistre:
        total_commandes = enregistre["total_commandes"]
        solde = enregistre["solde_net"]
        ajustement = enregistre["ajustement"]
    if enregistre:
        versements = [{"date": e["date"], "montant": e["montant"], "note": e["note"],
                       "deduit": e["deduit"], "statut": e["statut"], "paye": e["paye"],
                       "reste": e["reste"]} for e in enregistre["echeances"]]
        report = enregistre["report"]
    else:
        versements, report = echeancier.ajuster(
            echeancier.brut(total_commandes, annee, numero), ajustement)

    return {
        "mois": mois,
        "libelle": periode.libelle(mois),
        "periode": {"debut": debut, "fin": fin},
        "disponible": True,
        "client": CLIENT,
        "bilan": {"aqua": aqua, "partenaire": partenaire},
        "charges_libres": charges_libres,
        "total_charges": total_charges,
        "total_commandes": total_commandes,
        "solde_net": solde,
        "ajustement": ajustement,
        "ajustement_calcule": ajustement_calcule,
        "ecriture": piece,
        "echeancier": versements,
        "report": report,
        "enregistre": bool(enregistre),
        "valide": bool((enregistre or {}).get("valide")),
        "journal_entry": (enregistre or {}).get("journal_entry"),
        "manquants": historique.continuite(mois),
        "commandes": liste_commandes,
        "totaux_commandes": {
            "nombre": len(liste_commandes),
            "total": flt(sum(c["total"] for c in liste_commandes), PRECISION),
            "encaisse": flt(sum(c["encaisse"] for c in liste_commandes), PRECISION),
            "non_paye": flt(sum(c["non_paye"] for c in liste_commandes), PRECISION),
            "restant": flt(sum(c["restant"] for c in liste_commandes), PRECISION),
        },
    }
