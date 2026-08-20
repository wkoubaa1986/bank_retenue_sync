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
           "montant_document", "ecart", "ignore_manuel", "ignore_motif", "note", "reglement"]

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
