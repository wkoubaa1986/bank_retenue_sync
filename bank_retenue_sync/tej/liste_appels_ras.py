"""A RECLAMER — les certificats de retenue a la source manquants depuis plus de 45 jours (15/10/2026).

La premiere version creait une LISTE D'APPELS le mardi. Decision de l'utilisateur le soir meme : pas de
liste — la retenue a la source s'appelle et se trace DANS SA PAGE (/app/retenue-source-vente, onglet
« A reclamer »), la ou Salma voit le probleme : les pieces, les montants, le telephone, la derniere
relance. Ce module ne fait que dire QUI appeler et POURQUOI ; la page fait le reste, et le tableau des
appels compte les appels traces ([RELANCE-RAS] | Telephone) dans la file « Retenue a la source » du mardi.

QUI Y ENTRE — un client par ligne, avec toutes ses pieces :
- une retenue que NOUS avons comptabilisee et que le portail ne justifie pas (`orphelines`, verdict
  « sans certificat ») depuis plus de `JOURS` jours ;
- une dette de 1 % du TTC que le client n'a PAS declaree (`retenues_probables`, non declaree) depuis
  plus de `JOURS` jours — la retenue probable qui n'existe nulle part tant qu'il ne depose pas.
QUI N'Y ENTRE PAS : un client deja relance (SMS, e-mail ou appel trace [RELANCE-RAS]) depuis moins de
`JOURS` jours — on ne harcele pas, on revient dans 45 jours. Il reste visible, grise, avec la date.
"""
from __future__ import annotations

import re

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

JOURS = 45
MARQUEUR = "[RELANCE-RAS]"


def _texte_piece(p: dict) -> str:
    return "%s du %s : retenue %s DT — %s" % (p["piece"], getdate(p["date"]).strftime("%d/%m/%Y"), p["montant"], p["motif"])


def regrouper(pieces: list, relances_recentes: set) -> dict:
    """{client: [pieces]} sans les clients relances recemment. PURE. `pieces` : [{customer, piece, date, montant, motif}]."""
    out = {}
    for p in pieces:
        if not p.get("customer") or p["customer"] in relances_recentes:
            continue
        out.setdefault(p["customer"], []).append(p)
    return out


def pieces_a_reclamer(today, jours: int = JOURS) -> list:
    """Les pieces dont le certificat manque depuis plus de `jours` jours, dans les deux sens."""
    from bank_retenue_sync.tej import orphelines, retenues_probables

    today = getdate(today)
    limite = add_days(today, -jours)
    out = []
    for l in orphelines.inventaire(depuis="%s-01-01" % today.year).get("lignes", []):
        if l.get("verdict") == "sans certificat" and l.get("posting_date") and getdate(l["posting_date"]) <= limite:
            out.append({"customer": l.get("customer"), "piece": l.get("sales_invoice") or l.get("sales_order") or l.get("name"),
                        "date": l["posting_date"], "montant": flt(l.get("montant"), 3),
                        "motif": "retenue comptabilisee chez nous, aucun certificat au portail"})
    for l in retenues_probables.detecter(depuis="%s-01-01" % today.year):
        if not l.get("declaree") and l.get("dette_date") and getdate(l["dette_date"]) <= limite:
            out.append({"customer": l["customer"], "piece": l["piece"], "date": l["dette_date"], "montant": flt(l["retenue"], 3),
                        "motif": "1 %% du TTC laisse en dette (%s DT sur %s), retenue non declaree" % (l["retenue"], flt(l["grand_total"], 3))})
    return out


def dernieres_relances() -> dict:
    """{client: {date, type, note}} de la derniere relance RAS tracee (SMS, e-mail ou telephone)."""
    out = {}
    for r in frappe.db.sql("""select reference_name, creation, content from tabComment where reference_doctype = 'Customer'
                              and content like %s order by creation desc""", ("%" + MARQUEUR + "%",), as_dict=1):
        if r.reference_name in out:
            continue
        bouts = [b.strip() for b in (r.content or "").split("|")]
        out[r.reference_name] = {"date": r.creation, "type": bouts[1] if len(bouts) > 1 else "", "note": bouts[2] if len(bouts) > 2 else ""}
    return out


def relances_recentes(today, jours: int = JOURS, dernieres: dict = None) -> set:
    """Clients deja relances pour un certificat depuis moins de `jours` jours."""
    limite = add_days(getdate(today), -jours)
    return {c for c, r in (dernieres if dernieres is not None else dernieres_relances()).items() if getdate(r["date"]) >= limite}


def clients_a_reclamer(today=None, jours: int = JOURS) -> list:
    """Pour l'onglet « A reclamer » et la file du mardi : [{customer, customer_name, tax_id, telephones, pieces,
    total, depuis, derniere_relance, a_appeler}] — `a_appeler` faux quand une relance date de moins de `jours` jours."""
    today = getdate(today or nowdate())
    dernieres = dernieres_relances()
    recentes = relances_recentes(today, jours, dernieres)
    out = []
    for customer, ps in regrouper(pieces_a_reclamer(today, jours), set()).items():
        client = frappe.db.get_value("Customer", customer, ["customer_name", "tax_id", "custom_liste_telephone", "mobile_no"], as_dict=True) or frappe._dict()
        tels = [t.strip() for t in re.split(r"[\n,;/]+", (client.custom_liste_telephone or "") + "\n" + (client.mobile_no or "")) if t.strip()]
        ps = sorted(ps, key=lambda p: str(p["date"]))
        out.append({"customer": customer, "customer_name": client.customer_name or customer, "tax_id": client.tax_id,
                    "telephones": list(dict.fromkeys(tels)), "pieces": [{**p, "texte": _texte_piece(p)} for p in ps],
                    "total": round(sum(p["montant"] for p in ps), 3), "depuis": ps[0]["date"],
                    "derniere_relance": dernieres.get(customer), "a_appeler": customer not in recentes})
    out.sort(key=lambda c: (not c["a_appeler"], str(c["depuis"])))
    return out


def tracer_appel(customer: str, resume: str, note: str = "") -> str:
    """L'appel passe depuis l'onglet : le meme commentaire [RELANCE-RAS] | Telephone | … que l'onglet Factures,
    pour que l'historique, la « derniere relance » et le tableau des appels le voient."""
    if not customer or not frappe.db.exists("Customer", customer):
        frappe.throw("client inconnu")
    texte = " — ".join(x.strip() for x in (resume or "", note or "") if x and x.strip())
    doc = frappe.get_doc({"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "Customer",
                          "reference_name": customer, "content": "%s | Téléphone | %s" % (MARQUEUR, texte)})
    doc.insert(ignore_permissions=True)
    return doc.name
