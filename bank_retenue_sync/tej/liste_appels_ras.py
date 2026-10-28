"""A RECLAMER — les certificats de retenue a la source manquants depuis plus de 45 jours (15/10/2026).

La premiere version creait une LISTE D'APPELS le mardi. Decision de l'utilisateur le soir meme : pas de
liste — la retenue a la source s'appelle et se trace DANS SA PAGE (/app/retenue-source-vente, onglet
« A reclamer »), la ou Salma voit le probleme : les pieces, les montants, le telephone, la derniere
relance. Ce module ne fait que dire QUI appeler et POURQUOI ; la page fait le reste, et le tableau des
appels compte les appels traces ([RELANCE-RAS] | Telephone) dans la file « Retenue a la source » du mardi.

QUI Y ENTRE — un client par ligne, avec toutes ses pieces, et POURQUOI on l'appelle (`type`) :
- `manquant` : une retenue que NOUS avons comptabilisee et que le portail ne justifie pas (`orphelines`,
  verdict « sans certificat »), ou une dette de 1 % du TTC que le client n'a PAS declaree
  (`retenues_probables`), depuis plus de `JOURS` jours — lui demander de deposer sa retenue ;
- `a_rectifier` : un certificat en ANOMALIE (retenue superieure a l'assiette : UBCI a saisi 629 DT en
  millimes pour une facture de 63 DT, 15/10/2026) — lui demander de rectifier le certificat ;
- `facture_inconnue` : un certificat declare sur une assiette qui ne correspond a aucune de nos factures
  (STIES, CGC, COMOSACC, BHM) — lui demander sur quelle facture il a declare.
Les deux derniers n'attendent pas 45 jours : l'erreur est deja la.
QUI N'Y ENTRE PAS : un client deja relance (SMS, e-mail ou appel trace [RELANCE-RAS]) depuis moins de
`JOURS` jours — on ne harcele pas, on revient dans 45 jours. Il reste visible, grise, avec la date.
"""
from __future__ import annotations

import re

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

JOURS = 45
MARQUEUR = "[RELANCE-RAS]"


TYPES = {"manquant": "Certificat manquant", "a_rectifier": "Certificat a rectifier", "facture_inconnue": "Facture a preciser"}


def _liens(pdf=None, facture=None, commande=None, ecriture=None) -> dict:
    """Ce que la carte peut ouvrir d'un clic (28/10/2026) : le PDF du certificat tel que le client l'a depose,
    la facture ou la commande qu'il vise, l'ecriture de retenue chez nous. Seules les cles renseignees sortent :
    une carte « facture a preciser » n'a, par definition, pas de facture."""
    return {k: v for k, v in (("pdf", pdf), ("facture", facture), ("commande", commande), ("ecriture", ecriture)) if v}


def _texte_piece(p: dict) -> str:
    return "%s du %s : retenue %s DT — %s" % (p["piece"], getdate(p["date"]).strftime("%d/%m/%Y"), p["montant"], p["motif"])


def regrouper(pieces: list, relances_recentes: set) -> dict:
    """{client: [pieces]} sans les clients relances recemment ; une piece sans fiche client est regroupee sous
    « ? <declarant> » (on sait qui appeler, pas sous quel nom il est chez nous). PURE."""
    out = {}
    for p in pieces:
        cle = p.get("customer") or ("? " + (p.get("declarant") or "inconnu"))
        if cle in relances_recentes:
            continue
        out.setdefault(cle, []).append(p)
    return out


def pieces_a_reclamer(today, jours: int = JOURS) -> list:
    """Les pieces a reclamer, dans les trois sens : certificat manquant depuis plus de `jours` jours, certificat
    en anomalie a rectifier, certificat sur une facture inconnue a preciser."""
    from bank_retenue_sync.tej import orphelines, retenues_probables

    today = getdate(today)
    limite = add_days(today, -jours)
    out = []
    for l in orphelines.inventaire(depuis="%s-01-01" % today.year).get("lignes", []):
        if l.get("verdict") == "sans certificat" and l.get("posting_date") and getdate(l["posting_date"]) <= limite:
            out.append({"customer": l.get("customer"), "piece": l.get("sales_invoice") or l.get("sales_order") or l.get("name"),
                        "date": l["posting_date"], "montant": flt(l.get("montant"), 3), "type": "manquant",
                        "motif": "retenue comptabilisee chez nous, aucun certificat au portail",
                        **_liens(facture=l.get("sales_invoice"), commande=l.get("sales_order"), ecriture=l.get("name"))})
    for l in retenues_probables.detecter(depuis="%s-01-01" % today.year):
        if not l.get("declaree") and l.get("dette_date") and getdate(l["dette_date"]) <= limite:
            out.append({"customer": l["customer"], "piece": l["piece"], "date": l["dette_date"], "montant": flt(l["retenue"], 3),
                        "type": "manquant", "motif": "1 %% du TTC laisse en dette (%s DT sur %s), retenue non declaree" % (l["retenue"], flt(l["grand_total"], 3)),
                        **_liens(facture=l["piece"] if l.get("piece_doctype") == "Sales Invoice" else None,
                                 commande=l["piece"] if l.get("piece_doctype") == "Sales Order" else None,
                                 ecriture=(l.get("dettes") or [{}])[0].get("dette_pe"))})
    for c in frappe.get_all("Retenue Certificate", filters={"hors_perimetre": 0, "etat_depot": ["in", ["Recue", "Rectifie"]]},
                            fields=["name", "numero_chez_declarant", "declarant", "declarant_matricule", "customer", "date_paiement",
                                    "montant_retenue", "total_brut", "anomalie", "anomalie_raison", "match_status", "payment_entry", "sales_invoice", "sales_order", "pdf_file"]):
        if c.anomalie:
            out.append({"customer": c.customer or _client_du_matricule(c.declarant_matricule), "declarant": c.declarant,
                        "piece": "certificat %s" % (c.numero_chez_declarant or c.name[:8]), "date": c.date_paiement,
                        "montant": flt(c.montant_retenue, 3), "type": "a_rectifier", "certificat": c.name,
                        **_liens(pdf=c.pdf_file, facture=c.sales_invoice, commande=c.sales_order, ecriture=c.payment_entry),
                        "motif": "certificat errone (%s) : assiette %s DT, retenue %s DT — le client doit le rectifier sur le portail"
                                 % ((c.anomalie_raison or "anomalie").split(":")[-1].strip()[:60], flt(c.total_brut, 3), flt(c.montant_retenue, 3))})
        elif c.match_status == "Sans piece" and not c.payment_entry and not c.sales_invoice and not c.sales_order:
            out.append({"customer": c.customer or _client_du_matricule(c.declarant_matricule), "declarant": c.declarant,
                        "piece": "certificat %s" % (c.numero_chez_declarant or c.name[:8]), "date": c.date_paiement,
                        "montant": flt(c.montant_retenue, 3), "type": "facture_inconnue", "certificat": c.name,
                        **_liens(pdf=c.pdf_file),
                        "motif": "certificat declare sur une assiette de %s DT qui ne correspond a aucune de nos factures — lui demander laquelle"
                                 % flt(c.total_brut, 3)})
    return out


def _client_du_matricule(matricule: str):
    """La fiche client au meme matricule (normalise), quand le rapprochement ne l'a pas posee (anomalie)."""
    from bank_retenue_sync.tej import matricule as M

    cle = M.normaliser(matricule)
    if not cle:
        return None
    for c in frappe.get_all("Customer", filters={"tax_id": ["like", "%" + M.chiffres(matricule) + "%"]}, fields=["name", "tax_id"]):
        if M.normaliser(c.tax_id) == cle:
            return c.name
    return None


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
        inconnu = customer.startswith("? ")
        client = frappe._dict() if inconnu else (frappe.db.get_value("Customer", customer, ["customer_name", "tax_id", "custom_liste_telephone", "mobile_no"], as_dict=True) or frappe._dict())
        tels = [t.strip() for t in re.split(r"[\n,;/]+", (client.custom_liste_telephone or "") + "\n" + (client.mobile_no or "")) if t.strip()]
        ps = sorted(ps, key=lambda p: str(p["date"]))
        types = sorted({p["type"] for p in ps}, key=list(TYPES).index)
        out.append({"customer": None if inconnu else customer, "customer_name": client.customer_name or customer[2:] if inconnu else (client.customer_name or customer),
                    "tax_id": client.tax_id or (ps[0].get("declarant_matricule") if inconnu else None),
                    "telephones": list(dict.fromkeys(tels)), "pieces": [{**p, "texte": _texte_piece(p), "libelle": TYPES[p["type"]]} for p in ps],
                    "types": types, "libelle": " + ".join(TYPES[t] for t in types),
                    "total": round(sum(p["montant"] for p in ps), 3), "depuis": ps[0]["date"],
                    "derniere_relance": dernieres.get(customer), "a_appeler": customer not in recentes})
    ordre = {"a_rectifier": 0, "facture_inconnue": 1, "manquant": 2}
    out.sort(key=lambda c: (not c["a_appeler"], min(ordre[t] for t in c["types"]), str(c["depuis"])))
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
