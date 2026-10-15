"""LISTE D'APPELS « RETENUE A LA SOURCE » (15/10/2026) — les certificats manquants depuis plus de 45 jours.

Jusqu'ici, reclamer un certificat etait un geste isole (SMS / e-mail / telephone depuis l'onglet
Factures, 7 fois en aout, plus rien ensuite). La demande : en faire une LISTE D'APPELS comme les
autres, creee chaque lundi, dans le poste de travail de Salma, derriere la relance paiements.

QUI Y ENTRE — un client par ligne, avec toutes ses pieces :
- une retenue que NOUS avons comptabilisee et que le portail ne justifie pas (`orphelines`, verdict
  « sans certificat ») depuis plus de `JOURS` jours ;
- une dette de 1 % du TTC que le client n'a PAS declaree (`retenues_probables`, non declaree) depuis
  plus de `JOURS` jours — la retenue probable qui n'existe nulle part tant qu'il ne depose pas.
QUI N'Y ENTRE PAS : un client deja relance (liste de ce type, ou commentaire [RELANCE-RAS] d'un SMS,
e-mail ou appel) depuis moins de `JOURS` jours — on ne harcele pas, on revient dans 45 jours.

La ligne n'a pas d'echeancier d'entretien : le champ, obligatoire jusqu'ici, ne l'est plus (patch
customization_app ensure_liste_retenue_source). Pas de 2e appel automatique : la liste revient
d'elle-meme tant que le certificat manque.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

DOCTYPE = "Liste Appelle Entretien"
TYPE_LISTE = "Retenue à la source"
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


def relances_recentes(today, jours: int = JOURS) -> set:
    """Clients deja relances pour un certificat depuis moins de `jours` jours : liste de ce type (non annulee)
    ou commentaire [RELANCE-RAS] (SMS, e-mail, telephone depuis l'onglet Factures)."""
    limite = add_days(getdate(today), -jours)
    listes = set(frappe.db.sql_list("""select distinct a.client from `tabAppelle Client` a join `tabListe Appelle Entretien` l on l.name = a.parent
                                       where l.type_liste = %s and l.docstatus <> 2 and l.creation >= %s""", (TYPE_LISTE, limite)))
    commentaires = set(frappe.db.sql_list("""select distinct reference_name from tabComment where reference_doctype = 'Customer'
                                             and content like %s and creation >= %s""", ("%" + MARQUEUR + "%", limite)))
    return listes | commentaires


def creer(today=None, insert: bool = True) -> dict:
    """La liste du lundi. Un seul brouillon a la fois ; sans client, rien n'est cree."""
    today = getdate(today or nowdate())
    if frappe.db.exists(DOCTYPE, {"docstatus": 0, "type_liste": TYPE_LISTE}):
        return {"created": False, "raison": "un brouillon existe deja", "type": TYPE_LISTE}
    clients = regrouper(pieces_a_reclamer(today), relances_recentes(today))
    if not clients:
        return {"created": False, "raison": "aucun certificat manquant depuis plus de %d jours" % JOURS, "type": TYPE_LISTE, "count_clients": 0}
    if not insert:
        return {"created": False, "essai": True, "type": TYPE_LISTE, "count_clients": len(clients),
                "clients": {c: [_texte_piece(p) for p in ps] for c, ps in clients.items()}}
    doc = frappe.new_doc(DOCTYPE)
    for customer, ps in sorted(clients.items(), key=lambda kv: str(min(p["date"] for p in kv[1]))):
        client = frappe.db.get_value("Customer", customer, ["custom_liste_telephone", "customer_name", "tax_id"], as_dict=True) or frappe._dict()
        doc.append("clients", {
            "client": customer, "telephone": client.custom_liste_telephone, "secteur": "",
            "info": ("Certificat de retenue a la source manquant (%d piece(s), %s DT) — MF %s :\n%s\nDemander au client de deposer "
                     "sa retenue sur le portail TEJ (ou d'envoyer le certificat papier)."
                     % (len(ps), round(sum(p["montant"] for p in ps), 3), client.tax_id or "?", "\n".join("• " + _texte_piece(p) for p in ps))),
        })
    doc.type_liste, doc.date = TYPE_LISTE, today
    doc.nb_appels_restant, doc.nb_r_p, doc.nb_r_c = len(clients), 0, 0
    doc.titre = "%s %s — certificats manquants depuis plus de %d jours" % (TYPE_LISTE, today, JOURS)
    doc.flags.ignore_permissions = True
    doc.insert()
    return {"created": True, "type": TYPE_LISTE, "count_clients": len(clients), "name": doc.name}
