"""Pertes de non paiement : l'ecart entre ce qu'ERPNext attend et ce que la banque credite.

Un client vire le montant de sa facture ; la banque credite ce montant diminue de ses frais. Le
manque est une PERTE DE NON PAIEMENT — a condition qu'il reste dans la tolerance. Au-dela, ce n'est
plus un frais bancaire mais un impaye, et il ne faut surtout pas l'effacer par une ecriture.

MEME MECANIQUE QUE LES FRAIS BANCAIRES
--------------------------------------
Une SEULE ecriture par mois, refaite en entier a chaque fois qu'un nouvel ecart apparait, jusqu'a
la cloture. Le cumul est toujours recalcule depuis le releve, jamais incremente a partir de
l'ecriture existante : rejouer le meme mois ne double jamais le montant.

    Dr  Perte de non paiement - A&S    cumul des ecarts du mois
    Cr  STE430127B - Zitouna - A&S     idem

TOLERANCE : PLANCHER ET PLAFOND
-------------------------------
Reprise de la regle deja retenue pour l'allocation des dettes (`encaissement/allocation.py`) :
`min(max(1 DT, 0,5 %), 10 DT)`. Le plancher couvre les frais fixes de virement ; le PLAFOND evite
de pardonner un impaye significatif sur un gros montant. Un ecart hors tolerance ressort en
diagnostic et ne produit AUCUNE ecriture.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.encaissement.pending import BANK_ACCOUNT, COMPANY
from bank_retenue_sync.expenses import journal

PERTE_ACCOUNT = "Perte de non paiement - A&S"

# Memes bornes que l'allocation des dettes, pour que les deux flux s'accordent sur ce qu'est
# un ecart acceptable.
TOL_PLANCHER = 1.0
TOL_TAUX = 0.005
TOL_PLAFOND = 10.0


def tolerance(montant_attendu: float) -> float:
    return min(max(TOL_PLANCHER, flt(montant_attendu) * TOL_TAUX), TOL_PLAFOND)


@dataclass
class Ecart:
    date: object
    reference: str
    attendu: float
    recu: float
    ecart: float
    payment_entry: str = None
    party: str = None
    dans_tolerance: bool = True


@dataclass
class CumulPertes:
    periode: str
    cle: str
    ecarts: list = field(default_factory=list)
    total: float = 0.0
    hors_tolerance: list = field(default_factory=list)

    @property
    def jour(self):
        return max((e.date for e in self.ecarts), default=None)


def cle_mensuelle(periode: str) -> str:
    annee, mois = periode.split("-")
    return "Perte de non paiement %s-%s" % (mois, annee)


def ecarts_du_releve(movements: list, pe_finder=None) -> list:
    """Compare chaque credit bancaire a la Payment Entry qui le porte.

    `pe_finder(reference) -> dict|None` est injectable pour les tests. Par defaut, on cherche la
    Payment Entry soumise dont le `reference_no` cite la reference bancaire — c'est la convention
    posee par le server script d'encaissement (`'<ref> - Banque Zitouna'`).
    """
    pe_finder = pe_finder or _pe_par_reference
    out = []
    for m in movements or []:
        recu = flt(m.get("credit"), 3)
        ref = (m.get("reference") or "").strip()
        if not recu or not ref:
            continue
        pe = pe_finder(ref)
        if not pe:
            continue
        attendu = flt(pe.get("paid_amount"), 3)
        if not attendu:
            continue
        ecart = flt(attendu - recu, 3)
        if ecart <= 0:
            continue          # recu >= attendu : rien a constater en perte
        out.append(Ecart(date=getdate(m["date"]), reference=ref, attendu=attendu, recu=recu,
                         ecart=ecart, payment_entry=pe.get("name"), party=pe.get("party"),
                         dans_tolerance=ecart <= tolerance(attendu)))
    return out


def _pe_par_reference(reference: str):
    rows = frappe.db.get_all(
        "Payment Entry",
        filters={"docstatus": 1, "company": COMPANY,
                 "reference_no": ["like", "%%%s%%" % reference]},
        fields=["name", "party", "paid_amount", "posting_date"], limit_page_length=2)
    # Deux Payment Entry pour une meme reference bancaire : on ne tranche pas.
    return rows[0] if len(rows) == 1 else None


def cumul_mensuel(movements: list, periode: str, pe_finder=None) -> CumulPertes:
    """Cumul des ecarts du mois, recalcule depuis zero."""
    cumul = CumulPertes(periode=periode, cle=cle_mensuelle(periode))
    for e in ecarts_du_releve(movements, pe_finder):
        if "%04d-%02d" % (e.date.year, e.date.month) != periode:
            continue
        if not e.dans_tolerance:
            cumul.hors_tolerance.append(e)
            continue
        cumul.ecarts.append(e)
        cumul.total = flt(cumul.total + e.ecart, 3)
    return cumul


def is_enabled() -> bool:
    """⚠️ PIEGE : ce flux ferait DOUBLON avec l'ecriture de frais.

    `fees.cumul_mensuel(avec_pertes=True)` fond deja les deltas de paiement dans l'ecriture
    mensuelle de frais — c'est la pratique de l'utilisateur, verifiee sur ses saisies manuelles
    (« Perte de non paiement 9,896 | frais et TVA 74,392 | Total 84,288 »). Activer EN PLUS ce
    flux comptabiliserait les memes deltas une seconde fois.

    C'est sans consequence aujourd'hui : `process_pertes` n'a aucun appelant, ni dans
    `tasks/daily.py` ni dans `orchestrator.py`. Le reglage reste donc inerte — mais si on le
    cable un jour, il faudra passer `avec_pertes=False` au cumul des frais.
    """
    try:
        return bool(frappe.db.get_single_value(
            "Bank Retenue Sync Settings", "pertes_non_paiement_actives"))
    except Exception:
        return False


def build_journal_entry(cumul: CumulPertes, insert: bool = True):
    if not cumul.total:
        return None
    cc = frappe.db.get_value("Company", COMPANY, "cost_center")
    lines = [
        {"account": BANK_ACCOUNT, "credit": cumul.total},
        {"account": PERTE_ACCOUNT, "debit": cumul.total, "cost_center": cc},
    ]
    remark = "%s\n%d encaissements avec ecart\nRéf. banque %s" % (
        cumul.cle, len(cumul.ecarts),
        ", ".join(sorted({e.reference for e in cumul.ecarts})[:20]))
    je = journal.build_journal_entry(COMPANY, cumul.jour, lines, remark=remark,
                                     cheque_no=cumul.cle, cheque_date=cumul.jour)
    if insert:
        je.insert(ignore_permissions=True)      # BROUILLON
    return je


def sync_ecriture_mensuelle(movements: list, periode: str = None, insert: bool = True,
                            force: bool = False, pe_finder=None) -> dict:
    """Refait l'ecriture cumulative du mois si le total a change. Meme contrat que `fees`."""
    from bank_retenue_sync.expenses.fees import _supprimer_ecriture, periode_de

    periode = periode or periode_de(frappe.utils.nowdate())
    if not (force or is_enabled()):
        return {"periode": periode, "statut": "inactif",
                "raison": "flux desactive : cocher « Comptabiliser les pertes de non paiement »"}

    cumul = cumul_mensuel(movements, periode, pe_finder)
    existant = frappe.db.get_value(
        "Journal Entry", {"cheque_no": cumul.cle, "docstatus": ["<", 2]},
        ["name", "total_debit"], as_dict=True)

    base = {"periode": periode, "total": cumul.total,
            "hors_tolerance": [{"reference": e.reference, "ecart": e.ecart,
                                "attendu": e.attendu, "recu": e.recu,
                                "payment_entry": e.payment_entry}
                               for e in cumul.hors_tolerance]}

    if not cumul.total:
        return dict(base, statut="vide", je=existant.name if existant else None)
    if existant and abs(flt(existant.total_debit, 3) - cumul.total) < 0.005:
        return dict(base, statut="inchangee", je=existant.name)

    remplacee = None
    if existant:
        remplacee = "%s (%s)" % (existant.name, _supprimer_ecriture(existant.name))
    je = build_journal_entry(cumul, insert=insert)
    return dict(base, statut="remplacee" if remplacee else "cree",
                ecarts=len(cumul.ecarts), remplacee=remplacee,
                je=(je.name if insert and je else "(dry-run)"))


def process_pertes(movements: list, insert: bool = True, force: bool = False,
                   periodes=None, pe_finder=None) -> list:
    ecarts = ecarts_du_releve(movements, pe_finder)
    if not ecarts:
        return []
    if periodes is None:
        periodes = sorted({"%04d-%02d" % (e.date.year, e.date.month) for e in ecarts})
    return [sync_ecriture_mensuelle(movements, p, insert=insert, force=force, pe_finder=pe_finder)
            for p in periodes]
