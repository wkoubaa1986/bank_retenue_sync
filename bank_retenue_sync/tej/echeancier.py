"""L'échéancier de la commande suit la dette quand une retenue à la source la réduit.

POURQUOI CE MODULE EXISTE
-------------------------
Quand un certificat TEJ est logé dans une facture soldée par une dette (`paiements.ajuster`), la
pièce « Dette non payée » est REPRISE à la baisse : 331,00 de dette deviennent 327,29 de dette +
3,71 de retenue. La comptabilité est juste. Mais l'échéancier de la COMMANDE, lui, gardait sa
ligne d'origine « Dette non payée 331,00 ».

Or le Server Script « Traitement des encaissement » apparie la ligne de détail d'un Encaissement
Paiement à l'échéance par ÉGALITÉ STRICTE :

    if iterm.mode_of_payment == "Dette non payée" and iterm.payment_amount == ipay.valeur:

327,29 ≠ 331,00 : la ligne serait ignorée EN SILENCE, et le flux virement de l'app, qui vérifie
cette égalité en amont (`pending.dette_matches_schedule`), abandonne le lot entier. Cas réel du
08/09/2026 : virement BUSINESS HOTEL MANAGEMENT de 497,970 resté « Orphelin » alors que le client
était reconnu et ses trois dettes trouvées.

CE QU'ON FAIT
-------------
On ne touche QUE la ligne de dette, et seulement si elle est seule de son espèce ET porte
exactement la dette reprise : un échéancier composite ou déjà retouché se rectifie à la main.
La part retenue est posée en face sur une ligne du mode de retenue (114 commandes en portent déjà
une), pour que l'échéancier continue de sommer au total de la commande — ERPNext le refuse sinon
à la première réouverture. Même patron que `clients.dettes_perdues._rafraichir_echeancier`.

Un échec ici ne doit JAMAIS faire échouer la régularisation comptable qui l'a déclenché : les
écritures sont justes, c'est l'échéancier qui est en retard, et on le dit.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

PRECISION = 3
TOLERANCE = 0.005


# ------------------------------------------------------------------ fonctions pures

def plan(lignes: list, avant: float, apres: float, part: float, mode_dette: str,
         mode_ras: str) -> dict:
    """Ce qu'il faut changer dans l'échéancier. Pure : c'est elle qu'on teste.

    `lignes` : [{name, mode_of_payment, payment_amount}] de la commande.
    `avant` / `apres` : la dette telle qu'elle était et telle qu'elle est désormais.
    `part` : ce qui est passé en retenue (= avant - apres dans le cas nominal).
    """
    avant, apres, part = flt(avant, PRECISION), flt(apres, PRECISION), flt(part, PRECISION)
    dettes = [l for l in lignes if l.get("mode_of_payment") == mode_dette]
    if len(dettes) != 1:
        return {"ok": False, "raison": "échéancier composite (%d ligne(s) « %s ») : à rectifier "
                                       "à la main" % (len(dettes), mode_dette)}
    ligne = dettes[0]
    if abs(flt(ligne.get("payment_amount"), PRECISION) - avant) >= TOLERANCE:
        return {"ok": False, "raison": "la ligne de dette de l'échéancier (%s) ne porte pas la "
                                       "dette reprise (%s) : à rectifier à la main"
                % (flt(ligne.get("payment_amount"), PRECISION), avant)}
    if part <= 0.001:
        return {"ok": False, "raison": "aucune part retenue : rien à changer"}
    retenues = [l for l in lignes if l.get("mode_of_payment") == mode_ras]
    retenue = retenues[0] if len(retenues) == 1 else None
    if len(retenues) > 1:
        return {"ok": False, "raison": "plusieurs lignes de retenue dans l'échéancier : à "
                                       "rectifier à la main"}
    return {
        "ok": True,
        "dette": {"name": ligne.get("name"), "avant": avant, "apres": apres,
                  "supprimer": apres <= 0.001},
        "retenue": {"name": retenue.get("name") if retenue else None,
                    "avant": flt(retenue.get("payment_amount"), PRECISION) if retenue else 0.0,
                    "apres": flt((flt(retenue.get("payment_amount")) if retenue else 0.0) + part,
                                 PRECISION)},
    }


def commandes_parmi(reference_no: str, references: list, est_commande, commandes_des_factures) -> list:
    """Les commandes que porte une pièce de dette, dans l'ordre où on les trouve.

    La convention maison met la commande dans `reference_no` ; à défaut on lit les références de
    la pièce (une commande directement, ou les commandes des factures qu'elle solde).
    `est_commande(nom) -> bool` et `commandes_des_factures([factures]) -> [commandes]` injectés.
    """
    vues, out = set(), []

    def _ajouter(nom):
        if nom and nom not in vues:
            vues.add(nom)
            out.append(nom)

    ref = (reference_no or "").strip()
    if ref and est_commande(ref):
        _ajouter(ref)
    factures = []
    for r in references or []:
        if r.get("reference_doctype") == "Sales Order":
            _ajouter(r.get("reference_name"))
        elif r.get("reference_doctype") == "Sales Invoice":
            factures.append(r.get("reference_name"))
    if factures:
        for so in commandes_des_factures(factures) or []:
            _ajouter(so)
    return out


# ------------------------------------------------------------------ accès base

def _lignes(commande: str) -> list:
    return frappe.get_all("Payment Schedule",
                          filters={"parent": commande, "parenttype": "Sales Order"},
                          fields=["name", "idx", "mode_of_payment", "payment_amount", "due_date"],
                          order_by="idx", limit_page_length=0)


def _commandes_des_factures(factures: list) -> list:
    if not factures:
        return []
    return [r.sales_order for r in frappe.get_all(
        "Sales Invoice Item", filters={"parent": ["in", factures], "sales_order": ["is", "set"]},
        fields=["sales_order"], distinct=True, order_by="sales_order")]


def commandes_de(pe) -> list:
    """Les commandes d'une Payment Entry (document ou nom)."""
    if isinstance(pe, str):
        pe = frappe.get_doc("Payment Entry", pe)
    return commandes_parmi(pe.reference_no,
                           [{"reference_doctype": r.reference_doctype,
                             "reference_name": r.reference_name} for r in (pe.references or [])],
                           lambda nom: bool(frappe.db.exists("Sales Order", nom)),
                           _commandes_des_factures)


def _appliquer(commande: str, p: dict, mode_ras: str, motif: str) -> dict:
    d, r = p["dette"], p["retenue"]
    if d["supprimer"]:
        frappe.db.delete("Payment Schedule", {"name": d["name"]})
    else:
        frappe.db.set_value("Payment Schedule", d["name"],
                            {"payment_amount": d["apres"],
                             "description": "Dette après retenue à la source (%s)" % motif},
                            update_modified=False)
    pose = None
    if r["name"]:
        frappe.db.set_value("Payment Schedule", r["name"], {"payment_amount": r["apres"]},
                            update_modified=False)
    else:
        lignes = _lignes(commande)
        pose = frappe.get_doc({
            "doctype": "Payment Schedule",
            "parent": commande, "parenttype": "Sales Order", "parentfield": "payment_schedule",
            "docstatus": 1,
            "idx": (max((l.idx for l in lignes), default=0) + 1),
            "due_date": frappe.utils.nowdate(),
            "mode_of_payment": mode_ras,
            "payment_amount": r["apres"],
            "description": "Retenue à la source du client (%s)" % motif,
        })
        pose.flags.ignore_permissions = True
        pose.insert()
    return {"commande": commande, "ok": True, "dette": d, "retenue": r,
            "ligne_posee": pose.name if pose else None}


def suivre(pe, avant: float, apres: float, part: float, mode_dette: str, mode_ras: str,
           motif: str = "") -> list:
    """Après la reprise d'une dette : aligne l'échéancier de chaque commande de la pièce.

    Ne lève JAMAIS. -> [{commande, ok, raison | dette, retenue}] ; les refus sont journalisés
    pour qu'un humain rectifie, la comptabilité restant juste.
    """
    out = []
    try:
        commandes = commandes_de(pe)
    except Exception as e:  # noqa: BLE001
        frappe.log_error(str(e)[:1000], "Échéancier RAS : commandes introuvables")
        return [{"commande": None, "ok": False, "raison": str(e)[:200]}]
    if not commandes:
        return [{"commande": None, "ok": False, "raison": "la pièce ne porte aucune commande"}]
    for commande in commandes:
        try:
            p = plan(_lignes(commande), avant, apres, part, mode_dette, mode_ras)
            if not p["ok"]:
                frappe.log_error("%s : %s" % (commande, p["raison"]),
                                 "Échéancier RAS non aligné")
                out.append({"commande": commande, **p})
                continue
            out.append(_appliquer(commande, p, mode_ras, motif))
        except Exception as e:  # noqa: BLE001
            frappe.log_error("%s : %s" % (commande, str(e)[:1000]), "Échéancier RAS : échec")
            out.append({"commande": commande, "ok": False, "raison": str(e)[:200]})
    return out


# ------------------------------------------------------------------ réparation de l'existant

def diagnostic(mode_dette: str = None, mode_ras: str = None, commandes: list = None) -> list:
    """Les dettes en attente dont l'échéancier est resté au brut alors qu'une retenue a été
    comptabilisée sur leurs factures. Lecture seule.

    Une dette est retenue si : sa commande porte UNE ligne de dette ≠ montant de la dette, et la
    différence est exactement la somme des retenues (mode RAS) imputées aux mêmes factures.
    Les autres écarts (règlements partiels, reliquats) ne sont pas de ce ressort.
    """
    from bank_retenue_sync.tej import paiements as P, rapprochement as R

    mode_dette = mode_dette or P.mode_dette()
    mode_ras = mode_ras or R.mode_ras()
    filtres = {"docstatus": 1, "payment_type": "Receive", "party_type": "Customer",
               "mode_of_payment": mode_dette}
    cas = []
    for pe in frappe.get_all("Payment Entry", filters=filtres,
                             fields=["name", "party", "reference_no", "paid_amount"],
                             order_by="posting_date, name", limit_page_length=0):
        doc = frappe.get_doc("Payment Entry", pe.name)
        for commande in commandes_de(doc):
            if commandes and commande not in commandes:
                continue
            lignes = _lignes(commande)
            dettes = [l for l in lignes if l.mode_of_payment == mode_dette]
            if len(dettes) != 1:
                continue
            ligne = flt(dettes[0].payment_amount, PRECISION)
            dette = flt(pe.paid_amount, PRECISION)
            if abs(ligne - dette) < TOLERANCE or ligne < dette:
                continue
            factures = [r.reference_name for r in doc.references
                        if r.reference_doctype == "Sales Invoice"]
            ras = _retenues_sur(factures, mode_ras) if factures else 0.0
            ecart = flt(ligne - dette, PRECISION)
            if ras and abs(ecart - ras) < TOLERANCE:
                cas.append({"dette_pe": pe.name, "client": pe.party, "commande": commande,
                            "echeancier": ligne, "dette": dette, "retenue": ras,
                            "factures": factures})
    return cas


def _retenues_sur(factures: list, mode_ras: str) -> float:
    return flt(frappe.db.sql("""select coalesce(sum(per.allocated_amount), 0)
                                from `tabPayment Entry Reference` per
                                join `tabPayment Entry` pe on pe.name = per.parent
                                where pe.docstatus = 1 and pe.mode_of_payment = %s
                                  and per.reference_doctype = 'Sales Invoice'
                                  and per.reference_name in %s""",
                             (mode_ras, tuple(factures)))[0][0], PRECISION)


@frappe.whitelist()
def reparer(insert=False, commandes=None) -> dict:
    """Aligne les échéanciers en retard. `insert=False` par défaut : on regarde avant d'écrire.

    `commandes` : limiter à ces commandes (liste ou nom). Réservé aux gestionnaires système.
    """
    frappe.only_for("System Manager")
    from bank_retenue_sync.tej import paiements as P, rapprochement as R

    if isinstance(commandes, str):
        commandes = [c.strip() for c in commandes.split(",") if c.strip()]
    mode_dette, mode_ras = P.mode_dette(), R.mode_ras()
    cas = diagnostic(mode_dette, mode_ras, commandes)
    faits = []
    for c in cas:
        if not frappe.utils.cint(insert):
            faits.append({**c, "statut": "essai à blanc"})
            continue
        p = plan(_lignes(c["commande"]), c["echeancier"], c["dette"], c["retenue"],
                 mode_dette, mode_ras)
        if not p["ok"]:
            faits.append({**c, "statut": "refusé", "raison": p["raison"]})
            continue
        faits.append({**c, "statut": "aligné",
                      **_appliquer(c["commande"], p, mode_ras, "réparation %s" % c["dette_pe"])})
    return {"cas": len(cas), "faits": faits, "essai_a_blanc": not frappe.utils.cint(insert)}
