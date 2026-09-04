"""API de l'écran « Rapprochement client » (espace Banque).

Trois totaux par client — commandes TTC, BL validés, règlements reçus — et leurs écarts.
Aucune écriture comptable n'est produite ici : c'est un écran de CONSTAT. La seule chose que
l'API écrit, c'est la décision d'ignorer l'écart d'un client, avec son motif.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from bank_retenue_sync.clients import rapprochement as R

ROLES_LECTURE = ("System Manager", "Accounts Manager", "Accounts User")
ROLES_DECISION = ("System Manager", "Accounts Manager")


def _guard(decision: bool = False):
    frappe.only_for(list(ROLES_DECISION if decision else ROLES_LECTURE))


@frappe.whitelist()
def get_filtres() -> dict:
    _guard()
    return {
        "groupes": frappe.get_all("Customer Group", filters={"is_group": 0}, pluck="name",
                                  order_by="name"),
        "types": ["Company", "Individual"],
        "tolerances": R.tolerances(),
    }


@frappe.whitelist()
def get_data(groupe=None, type_client=None, recherche=None, seulement_ecarts=0,
             masquer_ignores=1, tri="delta_paiement", limite=300) -> dict:
    """Les lignes, triées, plus les totaux de la sélection ENTIÈRE.

    ⚠️ LES TOTAUX SE CALCULENT AVANT LA COUPE. Les additionner sur les 300 lignes affichées
    annoncerait un chiffre qui ne veut rien dire dès que la sélection est plus large.
    """
    _guard()
    lignes = R.lignes(groupe=groupe, type_client=type_client, recherche=recherche,
                      seulement_ecarts=seulement_ecarts, masquer_ignores=masquer_ignores)

    # Trier sur la VALEUR ABSOLUE : un client qui a trop payé est aussi anormal que celui qui
    # n'a pas assez payé, et l'utilisateur veut voir les deux en haut.
    cles = {"delta_paiement": lambda l: -abs(l["delta_paiement"]),
            "delta_bl": lambda l: -abs(l["delta_bl"]),
            "commandes": lambda l: -l["commandes"],
            "paiements": lambda l: -l["paiements"],
            "nom": lambda l: (l["nom"] or "").lower()}
    lignes.sort(key=cles.get(tri, cles["delta_paiement"]))

    totaux = {k: flt(sum(l[k] for l in lignes), R.PRECISION)
              for k in ("commandes", "bl", "paiements", "journal", "regle",
                        "delta_paiement", "delta_bl", "avance_non_affectee",
                        "avance_sur_commande", "encaisse_reel", "non_encaisse",
                        "reprise")}
    # Les états de livraison, cumulés sur la sélection entière — comme les autres totaux, ils se
    # calculent AVANT la coupe à 300 lignes.
    totaux_livraison = {}
    for l in lignes:
        for etat, v in (l.get("livraisons") or {}).items():
            e = totaux_livraison.setdefault(etat, {"total": 0.0, "nb": 0})
            e["total"] = flt(e["total"] + v["total"], R.PRECISION)
            e["nb"] += v["nb"]
    limite = frappe.utils.cint(limite) or 300
    return {
        "lignes": lignes[:limite],
        "nb": len(lignes),
        "tronque": len(lignes) > limite,
        "totaux": totaux,
        "en_ecart": sum(1 for l in lignes if l["en_ecart"]),
        "sans_bl": sum(1 for l in lignes if not l["a_des_bl"] and l["nb_commandes"]),
        "livraisons": totaux_livraison,
        "tolerances": R.tolerances(),
        "peut_decider": bool(set(frappe.get_roles()) & set(ROLES_DECISION)),
    }


@frappe.whitelist()
def detail(client) -> dict:
    """Les pièces d'UN client, pour comprendre son écart sans quitter l'écran."""
    _guard()
    if not frappe.db.exists("Customer", client):
        frappe.throw(_("Client introuvable"))
    return {
        "client": client,
        "commandes": frappe.get_all(
            "Sales Order", filters={"customer": client, "docstatus": 1},
            fields=["name", "transaction_date", "grand_total", "status", "delivery_status"],
            order_by="transaction_date desc", limit_page_length=200),
        "bl": frappe.get_all(
            "Delivery Note", filters={"customer": client, "docstatus": 1},
            fields=["name", "posting_date", "grand_total", "status"],
            order_by="posting_date desc", limit_page_length=200),
        "paiements": _paiements_detailles(client),
        "journal": frappe.db.sql(
            """SELECT je.name, je.posting_date, jea.debit, jea.credit, je.user_remark
               FROM `tabJournal Entry Account` jea
               INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
               WHERE je.docstatus = 1 AND jea.party_type = 'Customer' AND jea.party = %s
               ORDER BY je.posting_date DESC LIMIT 200""", (client,), as_dict=True),
    }


def _paiements_detailles(client) -> list:
    """Les règlements du client, CHACUN avec ce qu'il solde.

    ⚠️ UN RÈGLEMENT EST SOUVENT GROUPÉ. Le client paie 3 960 DT et la pièce couvre quatre
    factures, parfois à cheval sur deux mois. La ligne seule ne dit alors rien d'utile : on
    lit un montant sans savoir ce qu'il éteint, et un écart devient impossible à expliquer.
    Chaque paiement porte donc son détail, déplié à la demande.

    Une seule requête pour toutes les affectations : un `get_all` par paiement ferait 200
    allers-retours sur un gros client (131 règlements sur ECONOMIQ AQUA SOLUTIONS).
    """
    paiements = frappe.get_all(
        "Payment Entry",
        filters={"party": client, "party_type": "Customer", "docstatus": 1,
                 "payment_type": "Receive"},
        fields=["name", "posting_date", "paid_amount", "mode_of_payment", "paid_to",
                "reference_no", "unallocated_amount"],
        order_by="posting_date desc", limit_page_length=200)
    if not paiements:
        return []

    noms = [p.name for p in paiements]
    ph = ",".join(["%s"] * len(noms))
    refs = {}
    for r in frappe.db.sql(
            f"""SELECT per.parent, per.reference_doctype, per.reference_name,
                       per.allocated_amount, per.total_amount, per.outstanding_amount
                FROM `tabPayment Entry Reference` per
                WHERE per.parent IN ({ph}) AND per.docstatus < 2
                ORDER BY per.parent, per.idx""", tuple(noms), as_dict=True):
        refs.setdefault(r.parent, []).append({
            "doctype": r.reference_doctype,
            "nom": r.reference_name,
            "affecte": flt(r.allocated_amount, R.PRECISION),
            "total": flt(r.total_amount, R.PRECISION),
            "reste": flt(r.outstanding_amount, R.PRECISION),
        })
    for p in paiements:
        lignes = refs.get(p.name, [])
        p["affectations"] = lignes
        p["nb_affectations"] = len(lignes)
        # « Groupé » se lit sur le NOMBRE de pièces soldées, pas sur le montant : deux factures
        # de 30 DT sont un paiement groupé, un règlement de 4 000 DT sur une seule facture ne
        # l'est pas.
        p["groupe"] = len(lignes) > 1
    return paiements


@frappe.whitelist()
def ignorer(client, motif=None) -> dict:
    """Accepter l'écart d'un client. Le motif est OBLIGATOIRE.

    ⚠️ Sans motif, l'exclusion devient un trou de mémoire : six mois plus tard personne ne sait
    si l'écart avait été compris ou seulement masqué.
    """
    _guard(decision=True)
    motif = (motif or "").strip()
    if not motif:
        frappe.throw(_("Indique pourquoi l'écart de ce client est accepté."))
    if not frappe.db.exists("Customer", client):
        frappe.throw(_("Client introuvable"))
    if frappe.db.exists(R.DOCTYPE_IGNORE, client):
        doc = frappe.get_doc(R.DOCTYPE_IGNORE, client)
        doc.motif = motif
    else:
        doc = frappe.get_doc({"doctype": R.DOCTYPE_IGNORE, "client": client, "motif": motif})
    doc.auteur = frappe.session.user
    doc.date_ignore = frappe.utils.now_datetime()
    doc.flags.ignore_permissions = True
    doc.save()
    frappe.db.commit()
    return {"client": client, "ignore": True, "motif": motif}


@frappe.whitelist()
def reactiver(client) -> dict:
    """Remettre un client sous surveillance."""
    _guard(decision=True)
    if frappe.db.exists(R.DOCTYPE_IGNORE, client):
        frappe.delete_doc(R.DOCTYPE_IGNORE, client, ignore_permissions=True, force=True)
        frappe.db.commit()
    return {"client": client, "ignore": False}
