"""BILAN HEBDOMADAIRE DE LA RETENUE A LA SOURCE VENTE (15/10/2026) — lundi 08:00, par e-mail.

Jusqu'ici il fallait OUVRIR la page pour savoir : 3 certificats « sans piece » attendaient depuis
mai, 6 retenues restaient sans certificat, et personne n'avait demande un nouvel export du portail
depuis le 8 septembre. Un bilan qui arrive tout seul change la nature de la page : elle n'est plus
le seul endroit ou l'on decouvre ce qui attend.

CINQ RUBRIQUES, UNE SEULE QUESTION : qu'est-ce qui appelle une action cette semaine ?
- retenues comptabilisees SANS certificat depuis plus de `JOURS_SANS_CERTIFICAT` jours (a reclamer) ;
- certificats declares par les clients SANS ecriture chez nous (a regulariser : credit d'impot) ;
- ecarts certificat / ecriture hors timbre (a arbitrer) ;
- retenues PROBABLES deguisees en dette (a loger) ;
- fraicheur de l'export du portail (ce que le reste vaut).
"""
from __future__ import annotations

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

JOURS_SANS_CERTIFICAT = 45     # delai moyen ecriture -> certificat : 10 j, maximum vu : 98
TIMBRE = 0.015                 # l'ecart du timbre fiscal de 1 DT a 1 % : 0,010-0,011


def _sans_certificat(today, annee_min: int) -> list:
    from bank_retenue_sync.tej import orphelines

    inv = orphelines.inventaire(depuis="%s-01-01" % annee_min)
    limite = add_days(getdate(today), -JOURS_SANS_CERTIFICAT)
    return [l for l in inv.get("lignes", []) if l.get("verdict") == "sans certificat"
            and l.get("posting_date") and getdate(l["posting_date"]) <= limite]


def _sans_piece() -> list:
    """Le client a declare, rien ne le porte chez nous — meme definition que les KPI de l'onglet : un
    certificat « Manually Matched » sans ecriture n'est PAS rapproche."""
    return [c for c in frappe.get_all("Retenue Certificate", filters={"hors_perimetre": 0},
                                      fields=["name", "numero_chez_declarant", "declarant", "customer", "date_paiement",
                                              "montant_retenue", "match_status", "payment_entry", "match_raison"],
                                      order_by="date_paiement")
            if c.match_status == "Sans piece" or (c.match_status in ("Auto Matched", "Manually Matched") and not c.payment_entry)]


def _ecarts() -> list:
    return [c for c in frappe.get_all("Retenue Certificate", filters={"hors_perimetre": 0},
                                      fields=["name", "numero_chez_declarant", "declarant", "customer", "date_paiement",
                                              "montant_retenue", "ecart_piece", "payment_entry"], order_by="date_paiement")
            if abs(flt(c.ecart_piece, 3)) > TIMBRE]


def _fraicheur() -> dict:
    run = frappe.get_all("BRS Sync Run", filters={"kind": "tej_certificats_recus", "status": "Success"},
                         fields=["started_at", "rows_received", "rows_created"], order_by="started_at desc", limit_page_length=1)
    dernier = frappe.db.sql("select max(date_creation_certificat) from `tabRetenue Certificate`")[0][0]
    return {"export": run[0].started_at if run else None, "dernier_certificat": dernier,
            "lignes": run[0].rows_received if run else 0}


def composer(today=None) -> dict:
    """Les rubriques, telles quelles : la mise en forme et l'envoi viennent apres. Chaque rubrique est
    isolee — une rubrique en erreur ne doit pas faire taire les autres."""
    from bank_retenue_sync.tej import retenues_probables

    today = getdate(today or nowdate())
    annee = today.year
    out = {"date": str(today), "annee": annee, "erreurs": []}
    for cle, fn in (("sans_certificat", lambda: _sans_certificat(today, annee)), ("sans_piece", _sans_piece),
                    ("ecarts", _ecarts), ("probables", lambda: retenues_probables.detecter(depuis="%s-01-01" % annee)),
                    ("fraicheur", _fraicheur)):
        try:
            out[cle] = fn()
        except Exception as e:
            out[cle] = [] if cle != "fraicheur" else {}
            out["erreurs"].append("%s : %s" % (cle, str(e)[:160]))
    out["a_faire"] = sum(len(out[k]) for k in ("sans_certificat", "sans_piece", "ecarts", "probables"))
    return out


def _dt(v) -> str:
    return "%s DT" % ("{:,.3f}".format(flt(v, 3)).replace(",", " "))


def _jj(v) -> str:
    return getdate(v).strftime("%d/%m/%Y") if v else "—"


def _tableau(titre: str, entetes: list, lignes: list, vide: str) -> str:
    esc = frappe.utils.escape_html
    corps = "".join("<tr>%s</tr>" % "".join("<td style='padding:3px 8px;border-bottom:1px solid #eee'>%s</td>" % c for c in l) for l in lignes)
    if not lignes:
        corps = "<tr><td colspan='%d' style='color:#888;padding:3px 8px'>%s</td></tr>" % (len(entetes), esc(vide))
    return ("<h3 style='margin:18px 0 6px'>%s <span style='color:#888;font-weight:normal'>(%d)</span></h3>"
            "<table style='border-collapse:collapse;font-size:13px'><tr>%s</tr>%s</table>"
            % (esc(titre), len(lignes), "".join("<th style='text-align:left;padding:3px 8px;border-bottom:2px solid #ccc'>%s</th>" % esc(e) for e in entetes), corps))


def rendre(b: dict, url: str) -> str:
    """Le corps HTML du courriel. PURE (hors escape)."""
    esc = frappe.utils.escape_html
    f = b.get("fraicheur") or {}
    age = (getdate(b["date"]) - getdate(f["export"])).days if f.get("export") else None
    fraicheur = ("Dernier export du portail : <b>%s</b> (%s lignes), dernier certificat connu : <b>%s</b>%s"
                 % (_jj(f.get("export")), f.get("lignes") or 0, _jj(f.get("dernier_certificat")),
                    " — <span style='color:#c0392b'>plus de 14 jours, les « sans certificat » ci-dessous sont peut-etre deja declares</span>" if age and age > 14 else ""))
    parts = ["<p>Retenue a la source vente — bilan du %s (exercice %s). <b>%d</b> point(s) appellent une action. "
             "<a href='%s'>Ouvrir la page</a>.</p><p style='font-size:13px'>%s</p>" % (_jj(b["date"]), b["annee"], b["a_faire"], esc(url), fraicheur)]
    parts.append(_tableau("Retenues comptabilisees sans certificat depuis plus de %d jours — a reclamer au client" % JOURS_SANS_CERTIFICAT,
                          ["Date", "Client", "Retenue", "Piece", "Ecriture"],
                          [[_jj(l.get("posting_date")), esc(l.get("customer") or ""), _dt(l.get("montant")), esc(l.get("sales_invoice") or l.get("sales_order") or ""), esc(l.get("name") or "")]
                           for l in b.get("sans_certificat", [])], "aucune"))
    parts.append(_tableau("Certificats declares par les clients sans ecriture chez nous — credit d'impot a regulariser",
                          ["Date", "Declarant", "Client", "Retenue", "Pourquoi"],
                          [[_jj(c.get("date_paiement")), esc(c.get("declarant") or ""), esc(c.get("customer") or "?"), _dt(c.get("montant_retenue")), esc((c.get("match_raison") or "")[:90])]
                           for c in b.get("sans_piece", [])], "aucun"))
    parts.append(_tableau("Ecarts certificat / ecriture hors timbre — a arbitrer",
                          ["Date", "Declarant", "Retenue", "Ecart", "Ecriture"],
                          [[_jj(c.get("date_paiement")), esc(c.get("declarant") or ""), _dt(c.get("montant_retenue")), _dt(c.get("ecart_piece")), esc(c.get("payment_entry") or "")]
                           for c in b.get("ecarts", [])], "aucun"))
    probables = b.get("probables", [])
    parts.append(_tableau("Dettes de 1 % du TTC dont le client a DECLARE la retenue — a loger sur la piece",
                          ["Piece", "Client", "TTC", "Dette = retenue", "Certificat"],
                          [[esc(l.get("piece") or l.get("sales_invoice")), esc(l.get("customer_name") or l.get("customer")), _dt(l.get("grand_total")), _dt(l.get("retenue")),
                            esc((l.get("certificat") or {}).get("numero_chez_declarant") or "")]
                           for l in probables if l.get("declaree")], "aucune"))
    parts.append(_tableau("Dettes de 1 % du TTC NON declarees au portail — retenue probable, a reclamer au client",
                          ["Piece", "Client", "TTC", "Dette = retenue", "Depuis"],
                          [[esc(l.get("piece") or l.get("sales_invoice")), esc(l.get("customer_name") or l.get("customer")), _dt(l.get("grand_total")), _dt(l.get("retenue")), _jj(l.get("dette_date"))]
                           for l in probables if not l.get("declaree")], "aucune"))
    liste = b.get("liste") or {}
    if liste.get("created"):
        parts.append("<p>📞 Liste d'appels <b>%s</b> creee : <b>%d</b> client(s) a appeler pour leur certificat (onglet Appels, file « Retenue a la source »).</p>"
                     % (esc(liste.get("name") or ""), liste.get("count_clients") or 0))
    elif liste:
        parts.append("<p style='color:#888'>Liste d'appels « Retenue a la source » : %s.</p>" % esc(liste.get("raison") or "rien a creer"))
    if b.get("erreurs"):
        parts.append("<p style='color:#c0392b'>Rubriques en erreur : %s</p>" % esc(" ; ".join(b["erreurs"])))
    return "".join(parts)


def destinataires() -> list:
    from bank_retenue_sync.orchestrator import _system_managers

    return [e for e in (frappe.db.get_value("User", u, "email") for u in _system_managers()) if e]


def envoyer(today=None, liste: dict = None) -> dict:
    b = composer(today)
    b["liste"] = liste
    dest = destinataires()
    if not dest:
        return {"envoye": False, "raison": "aucun System Manager avec e-mail", "a_faire": b["a_faire"]}
    frappe.sendmail(recipients=dest, subject="[ERPNext] Retenue a la source vente — %d point(s) cette semaine" % b["a_faire"],
                    message=rendre(b, frappe.utils.get_url("/app/retenue-source-vente")))
    return {"envoye": True, "destinataires": dest, "a_faire": b["a_faire"]}
