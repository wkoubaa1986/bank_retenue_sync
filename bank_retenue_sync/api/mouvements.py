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


# ═══════════════════════════════════════════════════════════════════════════════════════════
# DEUX ARBITRAGES A LA MAIN, DEPUIS LA LIGNE (demande utilisateur 30/10/2026)
# ═══════════════════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def basculer_ecart_credit_fournisseur(cle: str, motif: str = "") -> dict:
    """La BANQUE a debite MOINS que l'ecriture (erreur de saisie bancaire, cheque partiellement
    honore...) : le manque est reporte en CREDIT FOURNISSEUR, par une ecriture
    Dr banque / Cr fournisseur du manque, et le mouvement est rattache aux deux pieces.

    Cas reel : cheque 4001056 (MAYAR DE COMMERCE), 4 841,000 comptabilises, 841,100 debites. Le
    paiement et l'identification sont justes, c'est le releve qui est faux : la banque corrigera
    (ou un nouveau paiement reglera le reste), et ce debit futur trouvera le credit fournisseur.
    Ensemble, l'ecriture d'origine et le correctif font exactement le debit du releve : ecart nul."""
    _guard(write=True)
    frappe.only_for("Accounts Manager")
    import re as _re

    from bank_retenue_sync.encaissement.pending import BANK_ACCOUNT, COMPANY
    from bank_retenue_sync.expenses import journal as J

    doc = frappe.get_doc(DOCTYPE, cle)
    if doc.sens != "Debit":
        frappe.throw(_("Seul un débit du relevé peut être basculé en crédit fournisseur."))
    if doc.document_type != "Journal Entry" or not doc.document_name \
            or frappe.db.get_value("Journal Entry", doc.document_name, "docstatus") != 1:
        frappe.throw(_("Le mouvement doit d'abord être identifié sur une écriture de journal validée."))
    je = frappe.get_doc("Journal Entry", doc.document_name)
    banque = flt(doc.debit, 3)
    comptabilise = flt(C._montant_bancaire("Journal Entry", je.name, "Debit"), 3)
    manque = round(comptabilise - banque, 3)
    if manque <= 0.005:
        frappe.throw(_("La banque n'a pas débité moins que l'écriture ({0} débités pour {1} "
                       "comptabilisés) : rien à reporter.").format(banque, comptabilise))

    # Le fournisseur : la ligne Créditeurs de l'écriture, sinon « Fournisseur : X » de sa remarque.
    party = next((a.party for a in je.accounts if a.party_type == "Supplier" and a.party), None)
    if not party:
        m = _re.search(r"Fournisseur\s*:\s*(.+)", je.user_remark or "")
        nom = m.group(1).strip() if m else ""
        party = nom if nom and frappe.db.exists("Supplier", nom) else \
            (frappe.db.get_value("Supplier", {"supplier_name": nom}) if nom else None)
    if not party:
        frappe.throw(_("Aucun fournisseur sur l'écriture {0} : créez sa fiche ou rattachez le "
                       "mouvement à la main.").format(je.name))
    crediteurs = frappe.db.get_value("Company", COMPANY, "default_payable_account") or "Créditeurs - A&S"
    motif = " ".join(str(motif or "").split())
    remarque = _("Écart bancaire sur « {0} » ({1}) du {2} : la banque a débité {3} pour {4} "
                 "comptabilisés ({5}). Le manque, {6}, est reporté en crédit fournisseur {7} — "
                 "il sera soldé quand la banque corrigera, ou par un nouveau paiement.").format(
        doc.operation, doc.reference or "—", frappe.utils.formatdate(doc.date), banque,
        comptabilise, je.name, manque, party)
    if motif:
        remarque += " " + _("Motif : {0}.").format(motif)
    remarque += " " + _("Basculé depuis l'identification bancaire par {0}.").format(frappe.session.user)
    correctif = J.build_journal_entry(COMPANY, doc.date, [
        {"account": BANK_ACCOUNT, "debit": manque},
        {"account": crediteurs, "credit": manque, "party_type": "Supplier", "party": party},
    ], remark=remarque, cheque_no=(PREFIXE_ECART_BANCAIRE + doc.operation)[:140], cheque_date=doc.date)
    correctif.flags.ignore_permissions = True
    correctif.insert()
    correctif.submit()
    registry.marquer_lien(
        cle, [{"doctype": "Journal Entry", "name": je.name},
              {"doctype": "Journal Entry", "name": correctif.name}],
        _("écart bancaire de {0} reporté en crédit fournisseur {1} ({2})").format(manque, party, correctif.name)
        + (" — " + motif if motif else ""),
        frappe.session.user)
    frappe.db.commit()
    C.run(persist=True)
    row = frappe.db.get_value(DOCTYPE, cle, ["statut", "raison", "ecart"], as_dict=True)
    return {"ecriture": correctif.name, "fournisseur": party, "manque": manque,
            "statut": row.statut, "raison": row.raison, "ecart": flt(row.ecart, 3)}


@frappe.whitelist()
def dettes_clients_en_attente(cle: str) -> dict:
    """Pour « Rattacher à une dette client » : le virement et les dettes clients en attente
    (paiements sur « Dettes - A&S »), le client que le libellé suggère en tête."""
    _guard()
    from bank_retenue_sync.bank import ecarts as E
    from bank_retenue_sync.encaissement import party, pending

    doc = frappe.get_doc(DOCTYPE, cle)
    credit = flt(doc.credit, 3)
    dettes = pending.get_pending_dettes()
    clients = sorted({d["party"] for d in dettes})
    suggere = None
    try:
        ident = party.identify(doc.operation, clients, aliases=party.manual_aliases(), ai_resolver=None)
        suggere = ident.get("customer")
    except Exception:
        pass
    out = []
    for d in dettes:
        out.append({"name": d["name"], "client": d["party"], "sales_order": d.get("sales_order"),
                    "reference_no": d.get("reference_no"), "date": str(d.get("posting_date") or ""),
                    "montant": flt(d["paid_amount"], 3), "suggere": d["party"] == suggere,
                    "ecart": round(credit - flt(d["paid_amount"], 3), 3)})
    out.sort(key=lambda x: (not x["suggere"], abs(x["ecart"]) > E.tolerance(credit), abs(x["ecart"]),
                            x["client"], x["date"]))
    return {"mouvement": {"cle": doc.name, "date": doc.date, "operation": doc.operation,
                          "reference": doc.reference, "montant": credit},
            "dettes": out, "client_suggere": suggere, "tolerance": E.tolerance(credit)}


@frappe.whitelist()
def rattacher_dette_client(cle: str, dettes, motif: str = "") -> dict:
    """Un virement client que le libellé ne permet pas d'identifier -> l'operateur designe la ou
    les dettes qu'il solde. On construit et SOUMET l'Encaissement Paiement correspondant, comme le
    ferait le flux automatique (`matching.match_virements`) : le server script bascule chaque
    dette de « Dettes - A&S » vers la banque avec la reference du virement, et le mouvement
    devient Identifie au reclassement. Le libelle sera reconnu la prochaine fois (alias appris
    depuis le paiement cree)."""
    _guard(write=True)
    frappe.only_for("Accounts Manager")
    from bank_retenue_sync.encaissement import allocation, builder, matching, pending

    doc = frappe.get_doc(DOCTYPE, cle)
    if doc.sens != "Credit":
        frappe.throw(_("Seul un crédit du relevé (virement reçu) solde une dette client."))
    ref = (doc.reference or "").strip()
    if not ref:
        frappe.throw(_("Ce mouvement n'a pas de référence bancaire : impossible de le porter sur un encaissement."))
    if ref in pending.consumed_bank_keys()["virement"]:
        frappe.throw(_("La référence {0} est déjà portée par un encaissement.").format(ref))
    noms = json.loads(dettes) if isinstance(dettes, str) else list(dettes or [])
    en_attente = {d["name"]: d for d in pending.get_pending_dettes()}
    choisies = [en_attente[n] for n in noms if n in en_attente]
    if not choisies or len(choisies) != len(noms):
        frappe.throw(_("Choisissez une ou plusieurs dettes encore en attente."))
    clients = {d["party"] for d in choisies}
    if len(clients) > 1:
        frappe.throw(_("Un virement solde les dettes d'UN seul client ({0} choisis).").format(len(clients)))
    client = choisies[0]["party"]
    credit = flt(doc.credit, 3)
    repartition = allocation.allocate(credit, choisies)
    if not repartition["lignes"]:
        frappe.throw(_("Impossible de répartir {0} sur les dettes choisies : {1}")
                     .format(credit, repartition.get("raison") or "—"))
    lignes = []
    for ligne in repartition["lignes"]:
        d = ligne["dette"]
        if not pending.dette_matches_schedule(d.get("sales_order"), d["paid_amount"]):
            frappe.throw(_("La commande {0} n'a pas d'échéance « Dette non payée » de {1} : le "
                           "traitement de l'encaissement ignorerait la dette {2} en silence. "
                           "Corrigez l'échéancier avant de rattacher.")
                         .format(d.get("sales_order"), d["paid_amount"], d["name"]))
        lignes.append({"ref_paiement": d["name"], "bl": d.get("sales_order"),
                       "valeur": d["paid_amount"], "part": ligne["part"], "emmeteur": client})
    lot = {"client": client, "n_virement": ref, "banque": matching.BANQUE, "date": doc.date,
           "total": repartition["total_alloue"], "credit_bancaire": credit,
           "mode": repartition["mode"], "methode_client": "manuel", "lignes": lignes}
    enc = builder.build_encaissement(virement_lots=[lot], insert=True)
    motif = " ".join(str(motif or "").split())
    enc.add_comment("Info", _("🔗 Rattaché depuis l'identification bancaire par {0} : virement « {1} » "
                              "({2}) du {3}, {4} → dette(s) {5} de {6}.{7}").format(
        frappe.session.user, doc.operation, ref, frappe.utils.formatdate(doc.date), credit,
        ", ".join(l["ref_paiement"] for l in lignes), client, (" " + motif) if motif else ""))
    enc.flags.ignore_permissions = True
    enc.submit()
    frappe.db.commit()
    C.run(persist=True)
    row = frappe.db.get_value(DOCTYPE, cle, ["statut", "raison", "ecart"], as_dict=True)
    return {"encaissement": enc.name, "client": client, "dettes": [l["ref_paiement"] for l in lignes],
            "total": repartition["total_alloue"], "reliquat": repartition.get("reliquat", 0),
            "statut": row.statut, "raison": row.raison, "ecart": flt(row.ecart, 3)}


#: Numero de reference des ecritures correctives posees par `basculer_ecart_credit_fournisseur`.
PREFIXE_ECART_BANCAIRE = "Écart bancaire — "


def echeancier_restaure(rows: list, encaissement: str, dettes: list, due_date=None) -> tuple:
    """Ce qu'il faut retirer et remettre dans l'echeancier d'une commande pour DEFAIRE un
    virement : les lignes « Virement » que cet encaissement y a ecrites s'en vont, et chaque dette
    retrouve sa ligne « Dette non payée » du montant d'origine. Fonction pure.

    `rows`   : [{name, mode_of_payment, payment_amount, description}] de la commande ;
    `dettes` : [{valeur}] les dettes de cette commande portees par le virement.
    -> (noms des lignes a retirer, lignes a ajouter)."""
    a_retirer = [r["name"] for r in (rows or [])
                 if r.get("mode_of_payment") == "Virement"
                 and encaissement and encaissement in (r.get("description") or "")]
    a_ajouter = [{"mode_of_payment": "Dette non payée", "payment_amount": round(flt(d["valeur"]), 3),
                  "due_date": due_date,
                  "description": "Dette rétablie : rattachement au virement défait (encaissement %s)" % encaissement}
                 for d in (dettes or []) if flt(d.get("valeur")) > 0]
    return a_retirer, a_ajouter


def _document_supprime(doctype: str, nom: str):
    """(nom de la ligne de corbeille, donnees) du document supprime, ou (None, None)."""
    dd = frappe.get_all("Deleted Document",
                        filters={"deleted_doctype": doctype, "deleted_name": nom, "restored": 0},
                        fields=["name", "data"], order_by="creation desc", limit_page_length=1)
    if not dd:
        return None, None
    return dd[0].name, json.loads(dd[0].data)


def _restaurer_paiement_supprime(nom: str, renommages: dict = None):
    """Le paiement de dette que le traitement de l'encaissement a SUPPRIME revient tel qu'il
    etait (meme contenu, meme statut soumis), sous un nouveau nom : c'est ce que fait la
    restauration de la corbeille de Frappe, sans exiger le role System Manager. `renommages`
    : {facture supprimee -> {"reference_doctype", "reference_name"}} : l'imputation est
    redirigee (vers la commande, le temps que la facture d'origine soit recreee et la reprenne
    en avance)."""
    ligne, data = _document_supprime("Payment Entry", nom)
    if not data:
        return None
    # Le traitement l'avait ANNULE avant de le supprimer : la corbeille le garde en docstatus 2.
    # On le recree en brouillon, a l'identique, puis on le soumet.
    for champ in ("name", "amended_from", "creation", "modified", "owner", "modified_by", "__islocal"):
        data.pop(champ, None)
    data["docstatus"] = 0
    for table in ("references", "deductions", "taxes"):
        for r in data.get(table) or []:
            for champ in ("name", "parent", "creation", "modified", "owner", "modified_by"):
                r.pop(champ, None)
    for r in data.get("references") or []:
        cible = (renommages or {}).get(r.get("reference_name"))
        if cible:
            r["reference_doctype"] = cible["reference_doctype"]
            r["reference_name"] = cible["reference_name"]
            for champ in ("due_date", "bill_no", "total_amount", "outstanding_amount", "payment_term"):
                r.pop(champ, None)
    doc = frappe.get_doc(data)
    doc.flags.ignore_permissions = True
    doc.flags.ignore_links = True
    doc.flags.from_restore = True
    doc.insert()
    doc.reload()          # les hooks d'insertion ont pu re-ecrire la piece : on repart du dernier etat
    doc.submit()
    frappe.db.set_value("Deleted Document", ligne, "restored", 1, update_modified=False)
    doc.add_comment("Info", _("Rétabli depuis la corbeille : était {0}, supprimé par le traitement d'un "
                              "encaissement dont le rattachement a été défait.").format(nom))
    return doc.name


def _refaire_dette_en_place(copie, ligne):
    """Le paiement de dette que le traitement avait BASCULE en place (compte banque, reference du
    virement) redevient une dette : meme contenu, compte « Dettes - A&S », reference = la commande
    (c'est ce que `pending.get_pending_dettes` attend). -> nom du nouveau paiement."""
    from bank_retenue_sync.encaissement.pending import DETTE_ACCOUNT

    copie.paid_to = DETTE_ACCOUNT
    copie.paid_to_account_currency = frappe.db.get_value("Account", DETTE_ACCOUNT, "account_currency") \
        or copie.paid_to_account_currency
    copie.reference_no = ligne.get("bl") or copie.reference_no
    copie.docstatus = 0
    copie.flags.ignore_permissions = True
    copie.flags.ignore_links = True
    copie.insert()
    copie.reload()
    copie.submit()
    copie.add_comment("Info", _("Dette refaite : le traitement d'un encaissement l'avait basculée sur "
                                "la banque, le rattachement du virement a été défait."))
    return copie.name


@frappe.whitelist()
def defaire_rattachement(cle: str, motif: str = "") -> dict:
    """DEFAIT un arbitrage : le mouvement redevient ce qu'il etait avant (orphelin, ou identifie
    avec son ecart), et les pieces touchees sont remises en l'etat (30/10/2026).

    Deux cas :
      - virement rattache a une dette client (par le bouton ou par le flux automatique) : les
        paiements bancaires nes de l'encaissement pour ce virement sont annules, le paiement de
        dette d'origine revient de la corbeille, l'echeancier de la commande retrouve sa ligne
        « Dette non payée », et l'encaissement est annule (s'il ne portait que ce virement) ou
        allege de ce lot ;
      - ecart bancaire reporte en credit fournisseur : le correctif est annule et le
        rattachement manuel retire.
    Refus si un paiement bancaire issu de l'encaissement a ete impute depuis sur une facture."""
    _guard(write=True)
    frappe.only_for("Accounts Manager")
    from bank_retenue_sync.encaissement.pending import BANK_ACCOUNT, DETTE_ACCOUNT

    doc = frappe.get_doc(DOCTYPE, cle)
    motif = " ".join(str(motif or "").split())
    qui = frappe.session.user
    trace = []

    if doc.lien_manuel:
        correctifs = [p["name"] for p in registry.pieces_du_lien(doc.lien_pieces)
                      if p["doctype"] == "Journal Entry"
                      and str(frappe.db.get_value("Journal Entry", p["name"], "cheque_no") or "")
                      .startswith(PREFIXE_ECART_BANCAIRE)]
        if not correctifs:
            frappe.throw(_("Ce rattachement manuel n'est pas un report en crédit fournisseur : "
                           "utilisez « Détacher »."))
        for nom in correctifs:
            je = frappe.get_doc("Journal Entry", nom)
            if je.docstatus == 1:
                je.flags.ignore_permissions = True
                je.flags.ignore_links = True
                je.cancel()
            trace.append(_("correctif {0} annulé").format(nom))
        registry.retirer_lien(cle)
        frappe.db.commit()
        C.run(persist=True)
        row = frappe.db.get_value(DOCTYPE, cle, ["statut", "raison", "ecart"], as_dict=True)
        return {"defait": "credit_fournisseur", "trace": trace, "statut": row.statut,
                "raison": row.raison, "ecart": flt(row.ecart, 3)}

    # Frais de LC affectes a un fournisseur : l'ecriture « Frais LC — … » s'annule, l'operation
    # entiere (commissions + TVA de meme reference) redevient a affecter.
    if doc.categorie == R.CATEGORIE_LC and doc.document_type == "Journal Entry" and doc.document_name \
            and str(frappe.db.get_value("Journal Entry", doc.document_name, "cheque_no") or "") \
            .startswith(PREFIXE_FRAIS_LC):
        je = frappe.get_doc("Journal Entry", doc.document_name)
        if je.docstatus == 1:
            je.flags.ignore_permissions = True
            je.flags.ignore_links = True
            je.cancel()
        je.add_comment("Info", _("↩ Affectation des frais de LC défaite par {0}.{1}").format(
            qui, (" " + motif) if motif else ""))
        trace.append(_("écriture Frais de LC {0} annulée").format(je.name))
        frappe.db.commit()
        C.run(persist=True)
        row = frappe.db.get_value(DOCTYPE, cle, ["statut", "raison", "ecart"], as_dict=True)
        return {"defait": "frais_lc", "trace": trace, "statut": row.statut, "raison": row.raison,
                "ecart": flt(row.ecart, 3)}

    if doc.document_type != "Encaissement Paiement" or not doc.document_name:
        frappe.throw(_("Rien à défaire : ce mouvement n'est porté ni par un encaissement ni par "
                       "un report en crédit fournisseur."))
    enc = frappe.get_doc("Encaissement Paiement", doc.document_name)
    if enc.docstatus != 1:
        frappe.throw(_("L'encaissement {0} n'est pas soumis.").format(enc.name))
    ref = (doc.reference or "").strip()
    tetes = [l for l in (enc.get("dette_client") or []) if (l.get("n_chèque") or "").strip() == ref]
    lignes = [l for l in (enc.get("dettes_a_encaisser") or []) if (l.get("n_chèque") or "").strip() == ref]
    if not tetes or not lignes:
        frappe.throw(_("L'encaissement {0} ne porte pas le virement {1} dans ses dettes : rien à "
                       "défaire ici.").format(enc.name, ref or "—"))
    client = tetes[0].client

    # 1. Les paiements bancaires nes de l'encaissement pour ce virement. Le traitement a pu
    #    aussi annuler, supprimer et RECREER la facture de la commande pour y imputer ce paiement :
    #    cette facture-la est GARDEE (meme commande, memes lignes, meme date que l'originale — la
    #    recreer une seconde fois serait fragile et inutile) ; c'est le paiement de dette retabli
    #    qui viendra la solder, comme l'original soldait l'originale. Une facture imputee qui
    #    n'est pas de ce client arrete tout : on ne touche pas a ce qu'on n'a pas fait.
    #    ⚠️ LES NOMS NE PROUVENT RIEN : Frappe rend le numero d'une piece supprimee si elle etait
    #    la derniere de sa serie, et le traitement recree la piece sous le MEME nom. Seule la
    #    corbeille (ligne posterieure a l'encaissement) dit si le paiement de dette a ete supprime.
    pes = frappe.get_all("Payment Entry",
                         filters={"party_type": "Customer", "party": client, "paid_to": BANK_ACCOUNT,
                                  "docstatus": 1, "reference_no": ["like", "%%%s%%" % ref]},
                         pluck="name")
    factures_par_commande: dict = {}
    for nom in pes:
        for si in frappe.get_all("Payment Entry Reference",
                                 filters={"parent": nom, "reference_doctype": "Sales Invoice"},
                                 pluck="reference_name"):
            info = frappe.db.get_value("Sales Invoice", si, ["customer", "docstatus"], as_dict=True)
            if not info or info.customer != client or info.docstatus != 1:
                frappe.throw(_("Le paiement {0} est imputé sur la facture {1}, qui n'est pas une "
                               "facture validée de ce client : défaites d'abord cette imputation.")
                             .format(nom, si))
            for so in frappe.get_all("Sales Invoice Item", filters={"parent": si},
                                     pluck="sales_order", distinct=True):
                if so:
                    factures_par_commande.setdefault(so, si)
    supprimes = {l.ref_paiement for l in lignes
                 if frappe.db.exists("Deleted Document", {"deleted_doctype": "Payment Entry",
                                                          "deleted_name": l.ref_paiement,
                                                          "creation": [">=", enc.creation]})}
    en_place = {}
    for nom in pes:
        pe = frappe.get_doc("Payment Entry", nom)
        if nom not in supprimes and any(l.ref_paiement == nom for l in lignes):
            en_place[nom] = frappe.copy_doc(pe)      # bascule en place : on le refera a partir de la
        pe.flags.ignore_permissions = True
        pe.flags.ignore_links = True          # l'encaissement le designe encore : c'est voulu
        pe.cancel()
        trace.append(_("paiement bancaire {0} annulé").format(nom))

    # 2. Les paiements de dette d'origine reviennent : de la corbeille (imputes a la facture
    #    actuelle de leur commande, a defaut a la commande), ou refaits a partir du paiement
    #    bascule en place.
    restaures = {}
    for l in lignes:
        if l.ref_paiement in en_place:
            nouveau = _refaire_dette_en_place(en_place[l.ref_paiement], l)
            restaures[l.ref_paiement] = nouveau
            trace.append(_("dette {0} (basculée en place) refaite sous {1}").format(l.ref_paiement, nouveau))
            continue
        # Une dette ENCORE VIVANTE sous ce nom (docstatus 1 sur « Dettes - A&S ») n'a rien a
        # restaurer, meme si la corbeille porte une ligne a ce nom : Frappe rend le numero d'une
        # piece supprimee, et une dette recreee plus tard l'a repris (vu sur dev le 30/10/2026).
        etat_dette = frappe.db.get_value("Payment Entry", l.ref_paiement, ["docstatus", "paid_to"], as_dict=True)
        if etat_dette and etat_dette.docstatus == 1 and etat_dette.paid_to == DETTE_ACCOUNT:
            restaures[l.ref_paiement] = l.ref_paiement
            continue
        redirections = {}
        _ligne, data = _document_supprime("Payment Entry", l.ref_paiement)
        for r in (data or {}).get("references") or []:
            if r.get("reference_doctype") == "Sales Invoice" and not frappe.db.exists("Sales Invoice", r.get("reference_name")):
                so = l.get("bl")
                if so and factures_par_commande.get(so):
                    redirections[r["reference_name"]] = {"reference_doctype": "Sales Invoice",
                                                         "reference_name": factures_par_commande[so]}
                elif so and frappe.db.exists("Sales Order", so):
                    redirections[r["reference_name"]] = {"reference_doctype": "Sales Order",
                                                         "reference_name": so}
                else:
                    frappe.throw(_("La dette {0} imputait la facture {1}, supprimée, et aucune commande "
                                   "n'est connue pour la porter : à rétablir à la main.")
                                 .format(l.ref_paiement, r.get("reference_name")))
        nouveau = _restaurer_paiement_supprime(l.ref_paiement, redirections)
        if nouveau:
            restaures[l.ref_paiement] = nouveau
            trace.append(_("dette {0} rétablie sous {1}{2}").format(
                l.ref_paiement, nouveau,
                (" " + _("(imputée à {0})").format(", ".join(v["reference_name"] for v in redirections.values())))
                if redirections else ""))
        elif frappe.db.get_value("Payment Entry", l.ref_paiement, "docstatus") == 1:
            restaures[l.ref_paiement] = l.ref_paiement       # jamais supprimee : rien a faire
        else:
            frappe.throw(_("Le paiement de dette {0} n'est ni en base ni dans la corbeille : "
                           "impossible de le rétablir.").format(l.ref_paiement))

    # 3. L'echeancier de chaque commande retrouve sa ligne « Dette non payée ».
    par_commande: dict = {}
    for l in lignes:
        if l.get("bl") and frappe.db.exists("Sales Order", l.bl):
            par_commande.setdefault(l.bl, []).append({"valeur": l.valeur})
    for so_nom, dettes in par_commande.items():
        so = frappe.get_doc("Sales Order", so_nom)
        rows = [{"name": r.name, "mode_of_payment": r.mode_of_payment,
                 "payment_amount": r.payment_amount, "description": r.description}
                for r in so.payment_schedule]
        a_retirer, a_ajouter = echeancier_restaure(rows, enc.name, dettes, due_date=doc.date)
        so.payment_schedule = [r for r in so.payment_schedule if r.name not in a_retirer]
        for a in a_ajouter:
            so.append("payment_schedule", a)
        for i, r in enumerate(so.payment_schedule, 1):
            r.idx = i
        so.flags.ignore_permissions = True
        so.flags.ignore_validate_update_after_submit = True
        so.save()
        trace.append(_("échéancier de {0} rétabli ({1} ligne(s) « Dette non payée »)")
                     .format(so_nom, len(a_ajouter)))

    # 4. L'encaissement : annule s'il ne portait que ce virement, allege sinon.
    autres = (enc.get("chèques_a_encaisser") or []) + (enc.get("traite_bancaire_a_encaissées") or []) \
        + (enc.get("livraison_aramex_a_encaisser") or []) \
        + [l for l in (enc.get("dette_client") or []) if (l.get("n_chèque") or "").strip() != ref]
    commentaire = _("↩ Rattachement du virement « {0} » ({1}) défait par {2} : {3}.{4}").format(
        doc.operation, ref, qui, " ; ".join(trace), (" " + motif) if motif else "")
    if not autres:
        enc.add_comment("Info", commentaire)
        enc.flags.ignore_permissions = True
        enc.cancel()
        trace.append(_("encaissement {0} annulé").format(enc.name))
    else:
        for l in tetes:
            frappe.db.delete("Liste des Dettes client", {"name": l.name})
        for l in lignes:
            frappe.db.delete("Liste Dettes", {"name": l.name})
        enc.add_comment("Info", commentaire + " " + _("(encaissement groupé : seul ce virement en est retiré)"))
        trace.append(_("virement retiré de l'encaissement groupé {0}").format(enc.name))

    frappe.db.commit()
    C.run(persist=True)
    row = frappe.db.get_value(DOCTYPE, cle, ["statut", "raison", "ecart"], as_dict=True)
    return {"defait": "dette_client", "trace": trace, "restaures": restaures,
            "statut": row.statut, "raison": row.raison, "ecart": flt(row.ecart, 3)}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# FRAIS DE LETTRE DE CREDIT : affectes a la main a un fournisseur (decision utilisateur 30/10/2026)
# ═══════════════════════════════════════════════════════════════════════════════════════════
# Les commissions d'ouverture / annulation / projet de LC et la commission SWIFT, avec leurs TVA,
# partagent une reference TF… le meme jour. L'utilisateur veut : les suivre PAR FOURNISSEUR (quel
# poids par rapport au cout d'achat), recuperer leur TVA dans la declaration, sans double
# comptage, et SANS peser sur le resultat tant que la marchandise n'est pas achetee. Donc PAS une
# avance fournisseur (un paiement, qui viendrait en moins de la prochaine facture) ni une charge
# du mois : une ecriture Dr « Frais de LC en attente » (actif, cf. expenses/frais_lc) + Dr TVA
# deductible / Cr banque, le fournisseur inscrit dessus. Le dossier mensuel lit ce compte avec
# les charges (sa TVA part en deductible), et le cumul mensuel des frais l'ignore (categorie
# frais_lc, cf. rules.py). A la reception, un bon de frais d'approche videra le compte dans la
# valeur des articles.

from bank_retenue_sync.expenses.frais_lc import PREFIXE_FRAIS_LC, compte_frais_lc as _compte_frais_lc


def _lignes_lc(doc) -> list:
    """Les lignes du registre de la meme operation de LC que `doc` : meme reference, meme jour,
    categorie frais_lc (commissions et TVA)."""
    return frappe.get_all(DOCTYPE, filters={"reference": doc.reference, "date": doc.date,
                                            "categorie": R.CATEGORIE_LC, "ignore_manuel": 0},
                          fields=["name", "operation", "regle", "debit", "statut", "document_name"],
                          order_by="debit desc")


def ventiler_frais_lc(lignes: list) -> dict:
    """Hors-taxe et TVA d'une operation de LC a partir de ses lignes (regle tva_bancaire = TVA).
    Fonction pure. -> {ht, tva, total}."""
    tva = round(sum(flt(l.get("debit")) for l in lignes if l.get("regle") == "tva_bancaire"), 3)
    total = round(sum(flt(l.get("debit")) for l in lignes), 3)
    return {"ht": round(total - tva, 3), "tva": tva, "total": total}


def pourcentages_frais_lc(frais: dict, achats: dict) -> list:
    """{fournisseur: {ht, tva, nb}} + {fournisseur: achats TTC} -> lignes triees par poids.
    Fonction pure. Le pourcentage est frais HT / achats ; None quand il n'y a pas d'achat."""
    out = []
    for f, v in (frais or {}).items():
        achat = flt((achats or {}).get(f), 3)
        out.append({"fournisseur": f, "frais_ht": flt(v.get("ht"), 3), "tva": flt(v.get("tva"), 3),
                    "nb": v.get("nb", 0), "achats": achat,
                    "pourcentage": round(100.0 * flt(v.get("ht")) / achat, 2) if achat else None})
    out.sort(key=lambda x: (-(x["pourcentage"] or 0), -x["frais_ht"]))
    return out


@frappe.whitelist()
def frais_lc_groupe(cle: str) -> dict:
    """Pour le dialogue « Affecter à un fournisseur (LC) » : les lignes de l'operation et sa
    ventilation."""
    _guard()
    doc = frappe.get_doc(DOCTYPE, cle)
    lignes = _lignes_lc(doc)
    return {"mouvement": {"cle": doc.name, "date": doc.date, "operation": doc.operation,
                          "reference": doc.reference},
            "lignes": lignes, **ventiler_frais_lc(lignes), "compte": _compte_frais_lc()}


@frappe.whitelist()
def affecter_frais_lc(cle: str, supplier: str, motif: str = "") -> dict:
    """Affecte une operation de LC a un fournisseur : ecriture Dr « Frais de LC » (HT) + Dr TVA 19 %
    / Cr banque, citant la reference bancaire (c'est elle que l'identification retrouve pour tout
    le groupe) et le fournisseur (« Fournisseur : X », la convention que la caisse ecrit deja)."""
    _guard(write=True)
    frappe.only_for("Accounts Manager")
    from bank_retenue_sync.encaissement.pending import BANK_ACCOUNT, COMPANY
    from bank_retenue_sync.expenses import fees, journal as J

    if not supplier or not frappe.db.exists("Supplier", supplier):
        frappe.throw(_("Choisissez un fournisseur."))
    doc = frappe.get_doc(DOCTYPE, cle)
    lignes = _lignes_lc(doc)
    if not lignes:
        frappe.throw(_("Ce mouvement n'est pas une opération de lettre de crédit (catégorie frais_lc)."))
    deja = [l for l in lignes if l.statut == "Identifie" and l.document_name]
    if deja:
        frappe.throw(_("Cette opération est déjà comptabilisée ({0}).").format(deja[0].document_name))
    v = ventiler_frais_lc(lignes)
    motif = " ".join(str(motif or "").split())
    detail = " ; ".join("%s %s" % (l.operation, flt(l.debit, 3)) for l in lignes)
    remarque = _("Frais de lettre de crédit — Fournisseur : {0} — Réf. banque {1} du {2} — {3} — "
                 "HT {4}, TVA {5}, total {6} — affecté depuis l'identification bancaire par {7}.").format(
        supplier, doc.reference, frappe.utils.formatdate(doc.date), detail, v["ht"], v["tva"],
        v["total"], frappe.session.user)
    if motif:
        remarque += " " + motif
    lignes_je = [{"account": _compte_frais_lc(), "debit": v["ht"]},
                 {"account": fees.FEE_TVA_ACCOUNT, "debit": v["tva"]},
                 {"account": BANK_ACCOUNT, "credit": v["total"]}]
    je = J.build_journal_entry(COMPANY, doc.date, lignes_je, remark=remarque,
                               cheque_no=(PREFIXE_FRAIS_LC + "%s — %s" % (supplier, doc.reference))[:140],
                               cheque_date=doc.date)
    je.flags.ignore_permissions = True
    je.insert()
    je.submit()
    frappe.db.commit()
    C.run(persist=True)
    row = frappe.db.get_value(DOCTYPE, cle, ["statut", "raison", "ecart"], as_dict=True)
    return {"ecriture": je.name, "fournisseur": supplier, **v, "statut": row.statut,
            "raison": row.raison, "ecart": flt(row.ecart, 3)}


@frappe.whitelist()
def frais_lc_par_fournisseur(date_from=None, date_to=None) -> dict:
    """Le poids des frais de LC par fournisseur sur la periode : frais HT affectes / achats
    (factures d'achat validees) du fournisseur, en %."""
    _guard()
    import re as _re

    date_from = date_from or frappe.utils.get_year_start(frappe.utils.nowdate())
    date_to = date_to or frappe.utils.nowdate()
    compte = _compte_frais_lc()
    frais: dict = {}
    for je in frappe.get_all("Journal Entry",
                             filters={"docstatus": 1, "cheque_no": ["like", PREFIXE_FRAIS_LC + "%"],
                                      "posting_date": ["between", [date_from, date_to]]},
                             fields=["name", "posting_date", "user_remark", "cheque_no"]):
        m = _re.search(r"Fournisseur\s*:\s*(.+?)\s+—", je.user_remark or "")
        fournisseur = m.group(1).strip() if m else je.cheque_no[len(PREFIXE_FRAIS_LC):].split(" — ")[0]
        lignes = frappe.get_all("Journal Entry Account", filters={"parent": je.name},
                                fields=["account", "debit_in_account_currency as debit"])
        ht = round(sum(flt(l.debit) for l in lignes if l.account == compte), 3)
        tva = round(sum(flt(l.debit) for l in lignes if "19%" in (l.account or "") or "7%" in (l.account or "")), 3)
        f = frais.setdefault(fournisseur, {"ht": 0.0, "tva": 0.0, "nb": 0, "ecritures": []})
        f["ht"] = round(f["ht"] + ht, 3)
        f["tva"] = round(f["tva"] + tva, 3)
        f["nb"] += 1
        f["ecritures"].append(je.name)
    achats = {}
    if frais:
        for r in frappe.get_all("Purchase Invoice",
                                filters={"docstatus": 1, "supplier": ["in", list(frais)],
                                         "posting_date": ["between", [date_from, date_to]]},
                                fields=["supplier", "sum(grand_total) as total"], group_by="supplier"):
            achats[r.supplier] = flt(r.total, 3)
    lignes = pourcentages_frais_lc(frais, achats)
    for l in lignes:
        l["ecritures"] = frais[l["fournisseur"]]["ecritures"]
    return {"date_from": str(date_from), "date_to": str(date_to), "compte": compte, "lignes": lignes,
            "total_ht": round(sum(l["frais_ht"] for l in lignes), 3),
            "total_tva": round(sum(l["tva"] for l in lignes), 3)}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# BORDEREAU INCOMPLET : un cheque depose mais jamais inscrit a l'encaissement (30/10/2026)
# ═══════════════════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def cheques_manquants(cle: str) -> dict:
    """Pour « Compléter le bordereau » : le manque du bordereau et les cheques en portefeuille
    (« Chèques - A&S », pas encore dans un encaissement), ceux du montant manquant en tete."""
    _guard()
    from bank_retenue_sync.encaissement import pending

    doc = frappe.get_doc(DOCTYPE, cle)
    m = {"operation": doc.operation, "reference": doc.reference, "debit": flt(doc.debit),
         "credit": flt(doc.credit), "date": doc.date}
    rule = R.find_rule(m)
    bon = C._cle_bancaire(rule, m, R.extract_numero(rule, m)) if rule else None
    manque = flt(doc.ecart, 3)
    out = []
    for p in pending.get_pending_cheques():
        out.append({"name": p["name"], "client": p.get("party"), "n_cheque": p.get("reference_no"),
                    "date": str(p.get("posting_date") or p.get("reference_date") or ""),
                    "montant": flt(p["paid_amount"], 3), "exact": abs(flt(p["paid_amount"], 3) - manque) < 0.0005})
    out.sort(key=lambda x: (not x["exact"], abs(x["montant"] - manque), x["date"]))
    return {"mouvement": {"cle": doc.name, "date": doc.date, "operation": doc.operation,
                          "bon": bon, "encaissement": doc.document_name, "manque": manque},
            "cheques": out[:100]}


@frappe.whitelist()
def completer_bordereau(cle: str, paiements, motif: str = "") -> dict:
    """Inscrit le ou les cheques choisis dans un Encaissement Paiement (bordereau du mouvement,
    statut Versé) et le soumet : le traitement bascule chaque cheque du portefeuille vers la
    banque, et le bordereau est enfin complet au reclassement."""
    _guard(write=True)
    frappe.only_for("Accounts Manager")
    from bank_retenue_sync.encaissement import builder, pending

    doc = frappe.get_doc(DOCTYPE, cle)
    info = cheques_manquants(cle)
    bon = info["mouvement"]["bon"]
    if not bon:
        frappe.throw(_("Ce mouvement ne porte pas de n° de bordereau."))
    noms = json.loads(paiements) if isinstance(paiements, str) else list(paiements or [])
    en_portefeuille = {p["name"]: p for p in pending.get_pending_cheques()}
    choisis = [en_portefeuille[n] for n in noms if n in en_portefeuille]
    if not choisis or len(choisis) != len(noms):
        frappe.throw(_("Choisissez un ou plusieurs chèques encore en portefeuille."))
    total = round(sum(flt(p["paid_amount"], 3) for p in choisis), 3)
    manque = info["mouvement"]["manque"]
    if abs(total - manque) > max(1.0, 0.005 * abs(manque)):
        frappe.throw(_("Les chèques choisis font {0}, le bordereau en manque {1}.").format(total, manque))
    rows = [{"ref_paiement": p["name"], "statut": "Versé", "bon_remise": bon,
             "n_cheque": p.get("reference_no"), "valeur": flt(p["paid_amount"], 3),
             "emmeteur": p.get("party"), "date": doc.date} for p in choisis]
    enc = builder.build_encaissement(cheque_rows=rows, insert=True)
    motif = " ".join(str(motif or "").split())
    enc.add_comment("Info", _("➕ Bordereau {0} complété depuis l'identification bancaire par {1} : "
                              "chèque(s) {2} ({3}) crédités le {4} mais jamais inscrits.{5}").format(
        bon, frappe.session.user, ", ".join(p["name"] for p in choisis), total,
        frappe.utils.formatdate(doc.date), (" " + motif) if motif else ""))
    enc.flags.ignore_permissions = True
    enc.submit()
    frappe.db.commit()
    C.run(persist=True)
    row = frappe.db.get_value(DOCTYPE, cle, ["statut", "raison", "ecart"], as_dict=True)
    return {"encaissement": enc.name, "cheques": [p["name"] for p in choisis], "total": total,
            "statut": row.statut, "raison": row.raison, "ecart": flt(row.ecart, 3)}


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


# ─── déclenchements manuels et journal des passages (14/10/2026) ─────────────

@frappe.whitelist()
def apercu_reglements() -> dict:
    """Ce que « Régler les dettes » ferait MAINTENANT (essai à blanc, rien n'est écrit) : les factures
    Aramex / notes d'honoraire appariées à un virement émis du même montant, et celles qui attendent."""
    _guard()
    from bank_retenue_sync.bank import registry
    from bank_retenue_sync.expenses import reglement
    from bank_retenue_sync.orchestrator import _movements_geres

    lignes = reglement.process_reglements(_movements_geres(registry.registry_as_movements()), insert=False)
    return {"lignes": lignes, "a_regler": sum(1 for l in lignes if l.get("status") == "a regler")}


@frappe.whitelist(methods=["POST"])
def regler_dettes() -> dict:
    """« Régler les dettes » à la main : la même étape que la vérification bancaire, tout de suite."""
    _guard(write=True)
    from bank_retenue_sync.orchestrator import run_reglements

    lignes = run_reglements(insert=1)
    return {"lignes": lignes, "regles": sum(1 for l in lignes if l.get("status") == "regle"),
            "erreurs": [l for l in lignes if l.get("status") == "error"]}


@frappe.whitelist(methods=["POST"])
def lancer_verification() -> dict:
    """Un passage complet de vérification bancaire, maintenant — le même que les 7 passages planifiés,
    journalisé de même. En file sur la queue longue ; un passage déjà en cours n'est pas doublé."""
    _guard(write=True)
    frappe.only_for("System Manager")
    from bank_retenue_sync.tasks import passages

    passages.surveiller()
    frappe.enqueue("bank_retenue_sync.tasks.daily.verification_bancaire_job", queue="long", timeout=3600,
                   job_id="brs-verification_bancaire", deduplicate=True, declencheur="manuel (%s)" % frappe.session.user)
    return {"statut": "en file"}


@frappe.whitelist()
def dernieres_verifications(limite: int = 10) -> list:
    """Les derniers passages de vérification bancaire : statut, heures, étapes, erreurs."""
    _guard()
    from bank_retenue_sync.tasks import passages

    return passages.derniers(limite)
