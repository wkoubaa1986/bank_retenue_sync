"""API de la page « Identification bancaire ».

Toutes les valeurs affichees viennent d'ici : le JS ne fait que du rendu. Les ecritures creees le
sont toujours en BROUILLON.

/!\ La page ne soumet JAMAIS d'`Encaissement Paiement` : son server script (After Submit, cote
customization_app) supprime la Payment Entry d'origine dans la branche aramex. La soumission reste
une decision humaine, prise sur le document lui-meme.
"""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import flt

from bank_retenue_sync.bank import classify as C, registry, rules as R

DOCTYPE = registry.DOCTYPE
SETTINGS = "Bank Retenue Sync Settings"

_FIELDS = ["name as cle", "date", "operation", "reference", "debit", "credit", "montant", "sens",
           "categorie", "regle", "groupe", "statut", "raison", "document_type", "document_name",
           "montant_document", "ecart", "ignore_manuel", "ignore_motif", "note", "reglement",
           "lien_manuel", "lien_pieces", "lien_motif"]

# En deca de ce seuil, un ecart est repute correspondre aux frais bancaires preleves a la source.
SEUIL_ECART_DEFAUT = 5.0


def seuil_ecart() -> float:
    """Seuil d'alerte sur l'ecart de paiement, parametrable dans les Reglages."""
    try:
        v = frappe.db.get_single_value(SETTINGS, "seuil_ecart_paiement")
        if v:
            return flt(v, 3)
    except Exception:
        pass
    return SEUIL_ECART_DEFAUT

_SORTABLE = {"date", "montant", "operation", "reference", "categorie", "statut", "sens"}


def _guard(write: bool = False):
    if not frappe.has_permission(DOCTYPE, "write" if write else "read"):
        frappe.throw(_("Accès non autorisé"), frappe.PermissionError)


ECART_CHOIX = {
    "signale": "Écart au-delà du seuil",
    "mineur": "Écart sous le seuil (frais présumés)",
    "aucun": "Sans écart",
}


def _noms_par_ecart(choix: str, seuil: float) -> list:
    """Noms des mouvements correspondant au critere d'ecart.

    On passe par une pre-selection SQL plutot que par un filtre Frappe : le critere porte sur
    `abs(ecart)`, et l'operateur `not between` n'existe pas cote query builder. Le calcul reste
    fait EN BASE (et non en Python) et suit immediatement tout changement de seuil — un flag
    stocke, lui, resterait fige tant qu'on n'aurait pas reclasse.
    """
    conditions = {
        "signale": "abs(ecart) > %(s)s",
        "mineur": "ecart != 0 and abs(ecart) <= %(s)s",
        "aucun": "(ecart is null or ecart = 0)",
    }
    cond = conditions.get(choix)
    if not cond:
        return None
    rows = frappe.db.sql("select name from `tab%s` where %s" % (DOCTYPE, cond), {"s": seuil})
    return [r[0] for r in rows]


def _filters(date_from=None, date_to=None, statut=None, categorie=None, sens=None,
             search=None, ecart=None) -> tuple:
    filters, or_filters = {}, None
    if date_from and date_to:
        filters["date"] = ["between", [date_from, date_to]]
    elif date_from:
        filters["date"] = [">=", date_from]
    elif date_to:
        filters["date"] = ["<=", date_to]
    if statut:
        filters["statut"] = statut
    if categorie:
        filters["categorie"] = categorie
    if sens:
        filters["sens"] = sens
    if search:
        or_filters = {"operation": ["like", f"%{search}%"], "reference": ["like", f"%{search}%"]}
    if ecart:
        noms = _noms_par_ecart(ecart, seuil_ecart())
        if noms is not None:
            # Liste vide -> aucun resultat. Sans le repli, `in []` laisserait passer tout le monde.
            filters["name"] = ["in", noms or [""]]
    return filters, or_filters


@frappe.whitelist()
def get_filtres() -> dict:
    """Valeurs disponibles pour les listes deroulantes, et periode par defaut."""
    _guard()
    cats = frappe.db.get_all(DOCTYPE, fields=["distinct categorie as c"], limit_page_length=0)
    bornes = frappe.db.sql("select min(`date`), max(`date`) from `tab%s`" % DOCTYPE)[0]
    return {
        "categories": sorted({r.c for r in cats if r.c}),
        "statuts": ["Identifie", "Identifie non comptabilise", "Orphelin", "A verifier", "Ignore"],
        "sens": ["Debit", "Credit"],
        "date_min": bornes[0], "date_max": bornes[1],
        "ecarts": [{"valeur": k, "libelle": v} for k, v in ECART_CHOIX.items()],
        "seuil_ecart": seuil_ecart(),
    }


@frappe.whitelist()
def get_data(date_from=None, date_to=None, statut=None, categorie=None, sens=None, search=None,
             ecart=None, start=0, page_length=100, order_by="date", order_dir="desc") -> dict:
    """Lignes + KPI. Les KPI portent sur TOUT le filtre, pas sur la page affichee : sinon
    « ce qui reste » ne voudrait rien dire."""
    _guard()
    filters, or_filters = _filters(date_from, date_to, statut, categorie, sens, search, ecart)
    order_by = order_by if order_by in _SORTABLE else "date"
    order_dir = "asc" if str(order_dir).lower() == "asc" else "desc"

    rows = frappe.db.get_all(DOCTYPE, filters=filters, or_filters=or_filters, fields=_FIELDS,
                             order_by=f"`{order_by}` {order_dir}, creation desc",
                             start=frappe.utils.cint(start),
                             page_length=frappe.utils.cint(page_length) or 100)
    total = frappe.db.count(DOCTYPE, filters=filters) if not or_filters else len(
        frappe.db.get_all(DOCTYPE, filters=filters, or_filters=or_filters,
                          fields=["name"], limit_page_length=0))

    agg = frappe.db.get_all(DOCTYPE, filters=filters, or_filters=or_filters,
                            fields=["sens", "statut", "count(name) as nb", "sum(montant) as tot"],
                            group_by="sens, statut", limit_page_length=0)
    kpi = {}
    for a in agg:
        kpi.setdefault(a.sens or "Debit", {})[a.statut or "Orphelin"] = {
            "nb": a.nb, "montant": flt(a.tot, 3)}

    asof = frappe.db.sql("select max(`date`) from `tab%s`" % DOCTYPE)[0][0]

    # Ecarts de paiement au-dela du seuil, sur TOUT le filtre : c'est un compteur d'anomalies,
    # il perdrait son sens s'il ne portait que sur la page affichee.
    seuil = seuil_ecart()
    ecarts = frappe.db.get_all(
        DOCTYPE, filters=dict(filters, ecart=["!=", 0]), or_filters=or_filters,
        fields=["name", "ecart"], limit_page_length=0)
    hors_seuil = [e for e in ecarts if abs(flt(e.ecart, 3)) > seuil]

    return {"rows": rows, "total": total, "kpi": kpi, "asof": asof,
            "start": frappe.utils.cint(start), "seuil_ecart": seuil,
            "ecarts": {"nb": len(hors_seuil),
                       "montant": flt(sum(abs(flt(e.ecart, 3)) for e in hors_seuil), 3)}}


@frappe.whitelist()
def reclassifier(date_from=None, date_to=None) -> dict:
    """Rejoue la classification sur le registre. Ne touche pas a la banque : rapide et sans risque."""
    _guard(write=True)
    frappe.only_for("System Manager")
    return C.run(date_from=date_from, date_to=date_to, persist=True)


@frappe.whitelist()
def rafraichir(lookback_days=None) -> dict:
    """Declenche un nouvel export bancaire, l'ajoute au registre, puis reclasse.

    Lent (scraping Playwright de plusieurs minutes cote service) et non fatal : si le service est
    injoignable, le registre existant reste exploitable et l'erreur est rapportee.
    """
    _guard(write=True)
    frappe.only_for("System Manager")
    from bank_retenue_sync.bank import movements as mv

    out = {"export": None, "erreur": None}
    try:
        job = mv.refresh_movements(
            lookback_days=frappe.utils.cint(lookback_days) or None)
        out["export"] = job.get("_lookback_days")
        mouvements = mv.fetch_latest_movements()
        out.update(registry.upsert_movements(mouvements))
    except Exception as e:
        out["erreur"] = str(e)[:200]
        frappe.log_error(f"rafraichissement bancaire echoue\n{e}", "brs page")
    out.update(C.run(persist=True))
    return out


@frappe.whitelist()
def ignorer(cles, motif=None) -> dict:
    """Arbitrage humain : ces mouvements n'ont pas a etre rapproches."""
    _guard(write=True)
    cles = json.loads(cles) if isinstance(cles, str) and cles.startswith("[") else cles
    return {"maj": registry.marquer_ignore(cles, motif=motif, ignore=True)}


@frappe.whitelist()
def reactiver(cles) -> dict:
    _guard(write=True)
    cles = json.loads(cles) if isinstance(cles, str) and cles.startswith("[") else cles
    n = registry.marquer_ignore(cles, ignore=False)
    C.run(persist=True)
    return {"maj": n}


# ------------------------------------------------------------ rattachement manuel (06/10/2026)
# Quand aucune cle ne relie le mouvement a ses pieces (ou que plusieurs pieces le couvrent sans
# que la machine puisse le prouver), l'utilisateur les designe lui-meme. Comme « Ignorer », c'est
# un ARBITRAGE HUMAIN : la reclassification le respecte (classify.resoudre_lien_manuel).

def _cle_normalisee(v) -> str:
    return "".join(ch for ch in str(v or "").upper() if ch.isalnum())


def _qui_porte(exclure: str) -> dict:
    """{piece -> « 05/10 · LIBELLE »} : les pieces deja rattachees a un AUTRE mouvement."""
    out = {}
    for r in frappe.db.get_all(DOCTYPE, filters={"name": ["!=", exclure]}, limit_page_length=0,
                               fields=["name", "date", "operation", "document_name", "lien_pieces"]):
        noms = [p["name"] for p in registry.pieces_du_lien(r.lien_pieces)]
        if r.document_name:
            noms.append(r.document_name)
        for n in noms:
            out.setdefault(n, "%s · %s" % (frappe.utils.formatdate(r.date, "dd/MM"),
                                           (r.operation or "")[:40]))
    return out


@frappe.whitelist()
def candidats_rattachement(cle: str, jours: int = 10) -> dict:
    """Les pieces bancaires du meme sens a ± `jours` du mouvement, pour le dialogue « Rattacher » :
    celles qui CITENT la reference du mouvement d'abord, puis par montant et par date proches."""
    _guard()
    from bank_retenue_sync.bank import ecarts as E
    from bank_retenue_sync.expenses import lookup

    doc = frappe.get_doc(DOCTYPE, cle)
    jours = frappe.utils.cint(jours) or 10
    montant = flt(doc.montant, 3)
    ref = _cle_normalisee(doc.reference)
    deja = registry.pieces_du_lien(doc.lien_pieces)
    deja_noms = {p["name"] for p in deja}
    porte = _qui_porte(cle)
    pieces = lookup.pieces_bancaires(frappe.utils.add_days(doc.date, -jours),
                                     frappe.utils.add_days(doc.date, jours), marge=0)
    out = []
    for p in pieces:
        if p["sens"] != doc.sens and p["voucher_no"] not in deja_noms:
            continue
        cite = bool(ref) and len(ref) >= E.MIN_CLE_LEN and ref in _cle_normalisee(p.get("texte"))
        out.append({
            "doctype": p["voucher_type"], "name": p["voucher_no"], "date": p["posting_date"],
            "montant": flt(p["montant"], 3), "texte": (p.get("texte") or "")[:140],
            "party": p.get("party"), "cite": cite, "lie_a": porte.get(p["voucher_no"]),
            "coche": p["voucher_no"] in deja_noms,
            "jours": abs(frappe.utils.date_diff(p["posting_date"], doc.date)),
        })
    vus = {c["name"] for c in out}
    for p in deja:                       # une piece deja rattachee reste visible hors fenetre
        if p["name"] not in vus:
            info = piece_info(p["doctype"], p["name"])
            out.append(dict(info, coche=True, cite=False, lie_a=porte.get(p["name"]), jours=None))
    out.sort(key=lambda c: (not c["coche"], not c["cite"], bool(c["lie_a"]),
                            abs(c["montant"] - montant) > E.tolerance(montant),
                            c["jours"] if c["jours"] is not None else 99,
                            abs(c["montant"] - montant)))
    return {"mouvement": {"cle": doc.name, "date": doc.date, "operation": doc.operation,
                          "reference": doc.reference, "montant": montant, "sens": doc.sens,
                          "statut": doc.statut, "lien_motif": doc.lien_motif},
            "candidats": out[:60], "tolerance": E.tolerance(montant), "jours": jours}


@frappe.whitelist()
def piece_info(doctype: str, name: str) -> dict:
    """Une piece choisie a la main (hors suggestions) : son montant bancaire, sa date, son texte."""
    _guard()
    if doctype not in registry.DOCTYPES_RATTACHABLES:
        frappe.throw(_("Seules une écriture de journal ou un paiement se rattachent."))
    if not frappe.db.exists(doctype, name):
        frappe.throw(_("{0} {1} introuvable.").format(doctype, name))
    champs = (["posting_date", "docstatus", "cheque_no", "user_remark"] if doctype == "Journal Entry"
              else ["posting_date", "docstatus", "reference_no", "remarks", "party"])
    d = frappe.db.get_value(doctype, name, champs, as_dict=True)
    return {"doctype": doctype, "name": name, "date": d.posting_date, "docstatus": d.docstatus,
            "montant": C._montant_bancaire(doctype, name), "party": d.get("party"),
            "texte": " ".join(str(d.get(f) or "") for f in champs[2:4]).strip()[:140]}


@frappe.whitelist()
def rattacher(cle: str, pieces, motif: str) -> dict:
    """Rattache le mouvement a une ou plusieurs pieces, avec un motif. Reclasse ensuite."""
    _guard(write=True)
    pieces = registry.pieces_du_lien(pieces)
    motif = " ".join(str(motif or "").split())
    if not frappe.db.exists(DOCTYPE, cle):
        frappe.throw(_("Mouvement introuvable."))
    if not pieces:
        frappe.throw(_("Choisissez au moins une pièce."))
    if len(motif) < 5:
        frappe.throw(_("Dites en quelques mots pourquoi ces pièces correspondent au mouvement."))
    for p in pieces:
        etat = frappe.db.get_value(p["doctype"], p["name"], "docstatus")
        if etat is None:
            frappe.throw(_("{0} {1} introuvable.").format(p["doctype"], p["name"]))
        if etat == 2:
            frappe.throw(_("{0} est annulée : elle ne peut pas être rattachée.").format(p["name"]))
    porte = _qui_porte(cle)
    registry.marquer_lien(cle, pieces, motif, frappe.session.user)
    C.run(persist=True)
    row = frappe.db.get_value(DOCTYPE, cle, ["statut", "raison", "ecart"], as_dict=True)
    return {"statut": row.statut, "raison": row.raison, "ecart": flt(row.ecart, 3),
            # Une piece peut legitimement couvrir plusieurs prelevements : on previent, sans bloquer.
            "deja_ailleurs": {p["name"]: porte[p["name"]] for p in pieces if p["name"] in porte}}


@frappe.whitelist()
def detacher(cle: str) -> dict:
    """Retire le rattachement manuel : la machine reprend la main sur ce mouvement."""
    _guard(write=True)
    registry.retirer_lien(cle)
    C.run(persist=True)
    return {"statut": frappe.db.get_value(DOCTYPE, cle, "statut")}


@frappe.whitelist()
def creer_ecriture(cle: str) -> dict:
    """Cree en BROUILLON l'ecriture d'un mouvement orphelin dont une depense recurrente est
    parametree. Refuse les frais bancaires : ils ne se comptabilisent que par agregat journalier."""
    _guard(write=True)
    frappe.only_for("Accounts Manager")
    from bank_retenue_sync.expenses import engine

    doc = frappe.get_doc(DOCTYPE, cle)
    m = {"date": doc.date, "date_valeur": doc.date_valeur, "operation": doc.operation,
         "reference": doc.reference, "debit": flt(doc.debit, 3), "credit": flt(doc.credit, 3)}

    rule = R.find_rule(m)
    if rule and rule.action == R.ACTION_AGREGAT:
        frappe.throw(_("Les frais bancaires ne se comptabilisent pas à l'unité : "
                       "ils sont regroupés par jour ({0}).").format(doc.groupe or ""))
    # Une échéance est éclatée par la banque en principal, profit, assurance, TVA et timbre.
    # Comptabiliser une seule de ces lignes produirait une écriture partielle, donc fausse.
    if rule and rule.groupe == "echeance":
        frappe.throw(_("Cette ligne fait partie d'une échéance ({0}) que la banque prélève en "
                       "plusieurs débits du même jour. L'écriture doit porter le groupe entier, "
                       "pas cette seule ligne.").format(doc.groupe or ""))

    regles = engine.find_rules_for(m)
    if not regles:
        frappe.throw(_("Aucune dépense récurrente active ne correspond à ce mouvement. "
                       "Paramétrez-en une dans les réglages de l'application."))
    if len(regles) > 1:
        frappe.throw(_("Plusieurs règles revendiquent ce mouvement ({0}) : "
                       "levez l'ambiguïté avant de créer l'écriture.").format(
                           ", ".join(r.get("cle") for r in regles)))

    context = C.build_context([m])
    res = engine.process_rule(regles[0], [m], context=context, insert=True)
    frappe.db.commit()
    if not res:
        frappe.throw(_("Aucune écriture n'a pu être créée pour ce mouvement."))
    ligne = res[0]
    if ligne.get("status") == "error":
        frappe.throw(ligne.get("error") or _("Création impossible."))
    C.run(persist=True)
    return {"status": ligne.get("status"), "doctype": "Journal Entry", "name": ligne.get("je"),
            "ref": ligne.get("ref")}


@frappe.whitelist()
def get_groupe_frais(groupe: str) -> dict:
    """Detail d'un agregat journalier de frais bancaires."""
    _guard()
    rows = frappe.db.get_all(DOCTYPE, filters={"groupe": groupe}, fields=_FIELDS,
                             order_by="date asc", limit_page_length=0)
    return {"groupe": groupe, "lignes": rows,
            "total": flt(sum(flt(r.montant) for r in rows), 3)}


@frappe.whitelist()
def mesurer_ouverture(date_reference=None) -> dict:
    """Ecart constate a une date, a reporter dans le reglage d'ouverture.

    Ne modifie RIEN : c'est une mesure, la decision de la figer reste humaine.
    """
    _guard()
    from bank_retenue_sync.bank import ecarts as E

    date_reference = date_reference or E.ecart_ouverture().get("date")
    if not date_reference:
        frappe.throw(_("Renseignez d'abord « Rapprocher à partir du » dans les Réglages."))
    return {"date": date_reference, "ecart": E.mesurer_ecart_a(date_reference)}


@frappe.whitelist()
def get_ecarts(date_from=None, date_to=None) -> dict:
    """Rapprochement dans LES DEUX SENS : ce qui manque en banque, ce qui manque en compta.

    La page ne montrait que les mouvements sans piece. Une ecriture ERPNext sans mouvement
    bancaire — saisie en double, montant errone, operation qui n'est jamais passee — restait
    invisible, alors qu'elle pese autant sur l'ecart de solde.
    """
    _guard()
    from bank_retenue_sync.bank import ecarts as E

    out = E.rapprochement(date_from=date_from, date_to=date_to)
    # La decomposition porte sur la MEME periode que les listes : l'ecart affiche doit etre
    # celui que les lignes du dessous expliquent, sinon les deux tableaux se contredisent.
    periode = out.get("periode") or {}
    out["explication"] = E.explication_ecart(date_from=periode.get("du"),
                                             date_to=periode.get("au"))
    # La projection porte sur TOUT le reste-a-traiter, jamais sur la fenetre affichee : l'ecart
    # de solde, lui, court depuis l'origine, et un depot en circulation ne cesse pas de peser
    # parce qu'on a filtre les dates. Melanger les deux donnerait un chiffre qui ne veut rien dire.
    out["projection"] = E.projection_ecart(
        rapport=(out if not (date_from or date_to) else None))
    return out


@frappe.whitelist()
def download_ecarts(date_from=None, date_to=None):
    """Export Excel des deux listes d'ecart, sur deux blocs d'un meme onglet."""
    _guard()
    from frappe.utils.xlsxutils import make_xlsx

    from bank_retenue_sync.bank import ecarts as E

    r = E.rapprochement(date_from=date_from, date_to=date_to)
    data = [["PIÈCES ERPNEXT SANS MOUVEMENT BANCAIRE"],
            ["Date", "Type", "Pièce", "Sens", "Montant", "Statut", "Motif", "Libellé"]]
    data += [[str(p["posting_date"] or ""), p["voucher_type"], p["voucher_no"], p["sens"],
              flt(p["montant"], 3), p.get("statut_ecart") or "", p.get("motif") or "",
              (p.get("texte") or "")[:200]] for p in r["erpnext_sans_banque"]]
    data += [[], ["MOUVEMENTS BANCAIRES SANS PIÈCE ERPNEXT"],
             ["Date", "Sens", "Libellé", "Référence", "Montant", "Statut", "Motif", ""]]
    data += [[str(m["date"] or ""), m["sens"] or "", m["operation"] or "", m["reference"] or "",
              flt(m["montant"], 3), m.get("statut_ecart") or "", m.get("motif") or "", ""]
             for m in r["banque_sans_erpnext"]]

    frappe.response["filecontent"] = make_xlsx(data, "Écarts").getvalue()
    frappe.response["filename"] = "ecarts_banque_erpnext.xlsx"
    frappe.response["type"] = "binary"


@frappe.whitelist()
def download_excel(date_from=None, date_to=None, statut=None, categorie=None, sens=None, ecart=None,
                   search=None):
    """Export Excel du filtre courant (toutes les lignes, pas seulement la page affichee)."""
    _guard()
    from frappe.utils.xlsxutils import make_xlsx

    filters, or_filters = _filters(date_from, date_to, statut, categorie, sens, search, ecart)
    rows = frappe.db.get_all(DOCTYPE, filters=filters, or_filters=or_filters, fields=_FIELDS,
                             order_by="`date` asc", limit_page_length=0)
    entetes = ["Date", "Sens", "Libellé", "Référence", "Débit", "Crédit", "Catégorie", "Règle",
               "Groupe", "Statut", "Document", "Raison"]
    data = [entetes] + [[
        str(r.date or ""), r.sens or "", r.operation or "", r.reference or "",
        flt(r.debit, 3), flt(r.credit, 3), r.categorie or "", r.regle or "", r.groupe or "",
        r.statut or "", (f"{r.document_type} {r.document_name}" if r.document_name else ""),
        r.raison or "",
    ] for r in rows]

    frappe.response["filecontent"] = make_xlsx(data, "Mouvements bancaires").getvalue()
    frappe.response["filename"] = "identification_bancaire.xlsx"
    frappe.response["type"] = "binary"


@frappe.whitelist()
def get_solde(date_max=None, capture=False) -> dict:
    """Solde bancaire reel, solde ERPNext, et l'ecart entre les deux.

    `capture=True` declenche une NOUVELLE capture du portail (lente, et payante en appel OpenAI).
    Par defaut on rend le dernier releve archive : c'est ce que la page affiche a chaque ouverture.
    """
    _guard()
    from bank_retenue_sync.bank import solde as S

    out = {"erpnext": S.solde_erpnext(date_max), "banque": None, "ecart": None,
           "releve": None, "historique": [], "registre": S.flux_registre(date_max=date_max)}

    if frappe.utils.cint(capture):
        frappe.only_for("System Manager")
        try:
            out["capture"] = S.capturer_solde()
        except Exception as e:
            out["erreur"] = str(e)[:200]

    from bank_retenue_sync.bank import ecarts as E

    dernier = S.dernier_solde()
    if dernier:
        # L'ecart est recalcule ici, et non repris du releve : le solde ERPNext bouge a chaque
        # ecriture, alors que le releve est fige a sa date de capture.
        erpnext_a_la_date = S.solde_erpnext(dernier.date_solde)
        out["releve"] = dict(dernier)
        out["banque"] = flt(dernier.solde_banque, 3)
        out["erpnext_a_la_date"] = erpnext_a_la_date
        brut = flt(flt(dernier.solde_banque, 3) - erpnext_a_la_date, 3)
        # L'ecart AFFICHE est net de l'ouverture acceptee : le rapprochement ne suit que ce qui
        # se cree depuis la date de depart. Le brut reste servi, pour que rien ne soit masque.
        out["ecart_brut"] = brut
        out["ouverture"] = E.ecart_ouverture()
        out["ecart"] = E.ecart_net(brut)
    out["historique"] = [dict(h) for h in S.historique(limite=30)]
    out["reste_a_identifier"] = S.ecart_par_categorie()[:6]
    out["decomposition"] = S.decomposition_ecart(date_max=date_max)
    # Projection de l'ecart, servie AVEC le solde et non a la demande : elle doit figurer a cote
    # de l'ecart constate en permanence — un ecart brut ne dit pas s'il se resorbera de lui-meme.
    from bank_retenue_sync.bank import ecarts as E

    out["projection"] = E.projection_ecart()
    # Solde CALCULE : derniere capture + net du registre depuis sa derniere operation. Sert la
    # continuite quand la capture est en panne — le registre, lui, continue d'arriver. La page
    # ne l'affiche que s'il apporte quelque chose (mouvements posterieurs a la capture).
    try:
        out["derive"] = S.solde_derive(date_max=date_max)
    except Exception:
        out["derive"] = None
    return out


@frappe.whitelist()
def generer_descriptions(date_from=None, date_to=None) -> dict:
    """Genere et FIGE sur chaque mouvement la description de ce qu'il a REELLEMENT paye
    (client, facture, Total TTC, montant alloue, bordereau / n° de cheque, contrat...).

    C'est la meme logique que la colonne « Règlement » de la Facturation mensuelle
    (facturation/reglement.py), mais PERSISTEE dans le champ `reglement` du registre :
    visible sur la fiche du mouvement, dans les listes et les exports, sans recalcul.

    Idempotent et rejouable : une piece saisie apres coup enrichit le mouvement au
    passage suivant ; seul un texte qui CHANGE est reecrit (update_modified=False —
    la description est un derive, pas une modification du mouvement)."""
    _guard(write=True)
    frappe.only_for("System Manager")
    from bank_retenue_sync.facturation import reglement

    filters, _ignore = _filters(date_from=date_from, date_to=date_to)
    lignes = frappe.db.get_all(DOCTYPE, filters=filters, fields=_FIELDS + ["reglement as reglement_fige"],
                               order_by="`date` asc", limit_page_length=0)
    details = reglement.details_par_mouvement(lignes)
    maj = 0
    for l in lignes:
        detail = details.get(l["cle"])
        texte = reglement.texte(detail) if detail else ""
        if (l.get("reglement_fige") or "") != texte:
            frappe.db.set_value(DOCTYPE, l["cle"], "reglement", texte,
                                update_modified=False)
            maj += 1
    frappe.db.commit()
    return {"mouvements": len(lignes),
            "decrits": sum(1 for l in lignes if details.get(l["cle"])),
            "maj": maj}
