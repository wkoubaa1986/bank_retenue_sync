"""Moteur generique de depenses declenchees par le releve bancaire.

Une depense recurrente est une LIGNE DE CONFIGURATION (`BRS Depense Recurrente`), pas du code : le
declencheur (mots-cles, montant), l'imputation (comptes, TVA) et la cle d'idempotence y sont
decrits. Ajouter un salaire ou changer un loyer se fait donc depuis l'interface.

CE MODULE NE REECRIT RIEN DE L'EXISTANT
---------------------------------------
Les cinq `process_*` de `orchestrator.py` et les six constructeurs de `expenses/journal.py`
restent en place et inchanges : ce moteur vit a cote et ne traite que les regles parametrees. C'est
ce qui garantit qu'aucun des 68 tests existants ne bouge.

Il reutilise en revanche leurs briques : `journal.build_journal_entry` (avec son controle
d'equilibre `UnbalancedEntry`), `journal._company_of`, `journal._auto_submit_enabled`.

L'IDEMPOTENCE A DEUX CLES
-------------------------
`Journal Entry.cheque_no` (« Salaire Koubaâ Néjib 06-2026 ») ne suffit pas : un flux sans periode
— la recharge de carte, plusieurs fois par mois sans identifiant propre — n'a pas de numero de
reference unique. La REFERENCE BANCAIRE (FT...) comble ce trou. Chaque ecriture produite porte
« Réf. banque <reference> » dans son libelle, ce qui la rend detectable au run suivant.
"""
from __future__ import annotations

import calendar
from datetime import date

import frappe
from frappe.utils import flt

from bank_retenue_sync.bank import rules as R
from bank_retenue_sync.bank.movements import _norm_op
from bank_retenue_sync.expenses import journal

SETTINGS = "Bank Retenue Sync Settings"
TABLE_FIELD = "depenses_recurrentes"


# ------------------------------------------------------------------ configuration

def load_rules(only=None) -> list:
    """Lignes de depense recurrente ACTIVES. Repli sur `defaults.py` si l'app n'est pas installee
    ou si la table est vide -> le moteur reste testable sans base."""
    rows = []
    try:
        if frappe.db and frappe.db.exists("DocType", SETTINGS):
            doc = frappe.get_cached_doc(SETTINGS)
            rows = [r.as_dict() for r in (doc.get(TABLE_FIELD) or [])]
    except Exception:
        rows = []
    if not rows:
        from bank_retenue_sync.expenses import defaults
        rows = defaults.as_rows()
    rows = [r for r in rows if r.get("actif")]
    if only:
        only = {only} if isinstance(only, str) else set(only)
        rows = [r for r in rows if r.get("cle") in only]
    return rows


def seed_defaults(overwrite: bool = False) -> int:
    """Amorce la table des Settings avec `defaults.py`. Les lignes deja presentes (par `cle`) sont
    conservees : l'utilisateur a pu ajuster un montant, on ne le repasse jamais dessus."""
    from bank_retenue_sync.expenses import defaults

    doc = frappe.get_single(SETTINGS)
    existantes = {r.cle for r in (doc.get(TABLE_FIELD) or []) if r.cle}
    n = 0
    for row in defaults.as_rows():
        if row["cle"] in existantes and not overwrite:
            continue
        doc.append(TABLE_FIELD, row)
        n += 1
    if n:
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return n


# ------------------------------------------------------------------ appariement

def rule_matches(row: dict, m: dict) -> tuple:
    """Ce mouvement releve-t-il de cette ligne de configuration ? -> (bool, raison si non).

    Les deux criteres sont CUMULATIFS quand ils sont tous deux renseignes : le montant fige d'un
    salaire et le nom dans le libelle se confirment mutuellement, ce qui neutralise l'instabilite
    de casse des noms constatee en base.
    """
    if not (m.get("debit") or 0):
        return False, "mouvement au credit"

    bank_rule = (row.get("bank_rule") or "").strip()
    if bank_rule:
        rule = R.RULES_BY_KEY.get(bank_rule)
        if not rule or not R.matches(rule, m):
            return False, "regle bancaire %s non applicable" % bank_rule

    motifs = [x.strip() for x in (row.get("motifs_libelle") or "").split(",") if x.strip()]
    montant = flt(row.get("montant"), 3)
    if not motifs and not montant:
        return False, "ligne de configuration sans critere (ni montant ni mots-cles)"

    if motifs:
        op = _norm_op(m.get("operation"))
        if not all(_norm_op(x) in op for x in motifs):
            return False, "mots-cles absents du libelle"
    if montant:
        if abs(flt(m.get("debit"), 3) - montant) > flt(row.get("tolerance") or 0.01, 3):
            return False, "montant different de l'attendu"
    return True, ""


def find_rules_for(m: dict, rows=None) -> list:
    """Toutes les lignes applicables. Plus d'une = ambiguite, jamais tranchee automatiquement."""
    return [r for r in (rows if rows is not None else load_rules()) if rule_matches(r, m)[0]]


# ------------------------------------------------------------------ construction

def _add_months(d: date, n: int) -> tuple:
    """(mois, annee) apres n mois."""
    total = (d.year * 12 + d.month - 1) + n
    return total % 12 + 1, total // 12


def build_reference(row: dict, m: dict) -> str:
    """Numero de reference du Journal Entry, a partir du modele de la ligne."""
    d = m.get("date") or date.today()
    pas = {"Bimestriel": 2, "Trimestriel": 3}.get(row.get("periodicite") or "", 1)
    mm2, yyyy2 = _add_months(d, pas)
    # Le mois PRECEDENT : certaines depenses sont reglees pour la periode ecoulee — l'honoraire
    # comptable du 25/05 porte « 04-2026 ». Sans ces jetons, sa reference designerait le mois du
    # paiement, et l'idempotence classerait deux notes differentes sous la meme cle.
    mm_prec, yyyy_prec = _add_months(d, -1)
    jour = row.get("jour_reference") or d.day
    return (row.get("template_reference") or row.get("libelle") or row.get("cle")).format(
        mm="%02d" % d.month, yyyy="%04d" % d.year,
        mm2="%02d" % mm2, yyyy2="%04d" % yyyy2,
        mm_prec="%02d" % mm_prec, yyyy_prec="%04d" % yyyy_prec,
        jour="%02d" % jour,
        reference=(m.get("reference") or "").strip(),
        libelle=row.get("libelle") or "",
    )


def build_lines(row: dict, montant: float) -> list:
    """Lignes de l'ecriture. Ordre repris des saisies existantes : le paiement en tete, la TVA au
    milieu, la charge en dernier (cf. expenses/journal.py)."""
    montant = flt(montant, 3)
    company = journal._company_of(row["compte_charge"])
    cc = frappe.db.get_value("Company", company, "cost_center") if frappe.db else None

    taux = flt(row.get("taux_tva") or 0)
    tva = flt(montant - montant / (1 + taux / 100.0), 3) if taux else 0.0
    ht = flt(montant - tva, 3)

    lines = [{"account": row["compte_banque"], "credit": montant}]
    if tva and row.get("compte_tva"):
        lines.append({"account": row["compte_tva"], "debit": tva, "cost_center": cc})
    else:
        ht = montant
    lines.append({"account": row["compte_charge"], "debit": ht, "cost_center": cc})
    return lines


# ------------------------------------------------------------------ traitement

def _deja_comptabilise(row: dict, m: dict, reference_je: str, context) -> str:
    """Nom du Journal Entry deja existant pour ce mouvement, ou None.

    Ordre : la reference BANCAIRE d'abord (la plus forte, elle identifie l'operation elle-meme),
    puis le numero de reference metier.
    """
    mode = row.get("idempotence") or "Les deux"
    ref_banque = (m.get("reference") or "").strip().upper()

    # ⚠️ LE REGISTRE FAIT FOI AVANT TOUT INDEX. Un mouvement deja RATTACHE a une piece
    # (classification « Identifié », document_name pose) est deja comptabilise — meme si la
    # piece ne porte ni la reference bancaire ni le cheque_no du gabarit, cas de toute saisie
    # anterieure au registre. Le 20/08/2026, le backfill d'avril a fait recomptabiliser deux
    # recharges de carte Total (ACC-JV-2026-00612/00613) que le registre savait pourtant deja
    # liees aux JV d'avril : 500 DT de doublons sur la banque.
    if ref_banque:
        lie = frappe.db.get_value(
            "BRS Bank Movement",
            {"reference": ref_banque, "document_name": ["is", "set"]},
            "document_name")
        if lie:
            return lie

    if mode in ("Les deux", "Reference bancaire") and ref_banque and context:
        noms = (context.je_par_reference or {}).get(ref_banque) or []
        if noms:
            return noms[0]
    if mode in ("Les deux", "Numero de reference") and context:
        nom = (context.cheque_no_index or {}).get(reference_je)
        if nom:
            return nom
    return None


def process_rule(row: dict, movements: list, context=None, insert: bool = True) -> list:
    """Traite une ligne de configuration sur une liste de mouvements.

    Contrat de retour identique aux `process_*` de l'orchestrateur : [{flux, ref, status, ...}]
    avec status parmi created | skipped | regle_ambigue | error.
    """
    out = []
    toutes = load_rules()
    for m in movements or []:
        ok, _ = rule_matches(row, m)
        if not ok:
            continue

        concurrentes = [r for r in toutes if r.get("cle") != row.get("cle")
                        and rule_matches(r, m)[0]]
        if concurrentes:
            # Deux regles pour un meme mouvement : on ne tranche pas au hasard, on signale.
            out.append({"flux": row["cle"], "ref": (m.get("reference") or ""),
                        "status": "regle_ambigue",
                        "regles": [row["cle"]] + [r["cle"] for r in concurrentes]})
            continue

        reference_je = build_reference(row, m)
        existant = _deja_comptabilise(row, m, reference_je, context)
        if existant:
            out.append({"flux": row["cle"], "ref": reference_je, "status": "skipped",
                        "je": existant})
            continue

        try:
            montant = flt(m.get("debit"), 3)
            lines = build_lines(row, montant)
            remark = "%s\nRéf. banque %s" % (reference_je, (m.get("reference") or "").strip())
            je = journal.build_journal_entry(
                journal._company_of(row["compte_charge"]), m.get("date"), lines,
                remark=remark, cheque_no=reference_je, cheque_date=m.get("date"),
                mode_of_payment=row.get("mode_paiement") or None)
            if not insert:
                out.append({"flux": row["cle"], "ref": reference_je, "status": "created",
                            "je": "(dry-run)", "montant": montant})
                continue
            je.insert(ignore_permissions=True)
            if journal._auto_submit_enabled():
                je.submit()
            out.append({"flux": row["cle"], "ref": reference_je, "status": "created",
                        "je": je.name, "montant": montant})
        except Exception as e:
            out.append({"flux": row["cle"], "ref": reference_je, "status": "error",
                        "error": str(e)[:160]})
    return out


def process_all_rules(movements: list, context=None, insert: bool = True, only=None) -> list:
    rows = load_rules(only=only)
    out = []
    for row in rows:
        out += process_rule(row, movements, context=context, insert=insert)
    return out
