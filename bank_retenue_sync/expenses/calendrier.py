"""Depenses declenchees par le CALENDRIER, et non par la banque.

Certaines depenses sont connues d'avance : les salaires partent 2 jours avant la fin du mois, le
loyer le 15 tous les deux mois. Les comptabiliser en attendant le releve ferait perdre plusieurs
jours, et le releve ne dit de toute facon pas a qui va un « VIR TN AUTRE BQ ».

On les cree donc PAR ANTICIPATION, et chaque ecriture s'accompagne d'un ordre de paiement
(`expenses/ordres.py`) que le rapprochement bancaire viendra solder. L'ecriture dit ce qu'on doit,
l'ordre dit si c'est parti.

Toujours en BROUILLON : anticiper n'est pas affirmer.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.expenses import engine, journal, ordres

DECLENCHEUR_CALENDRIER = "Calendrier"


def fin_de_mois(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def date_declenchement(row: dict, annee: int, mois: int):
    """Date a laquelle l'ecriture doit etre creee pour ce mois, ou None si la regle n'en definit pas."""
    jours_avant = int(row.get("jours_avant_fin_mois") or 0)
    if jours_avant:
        return fin_de_mois(date(annee, mois, 1)) - timedelta(days=jours_avant)
    jour = int(row.get("jour_declenchement") or 0)
    if jour:
        dernier = calendar.monthrange(annee, mois)[1]
        return date(annee, mois, min(jour, dernier))
    return None


def mois_concerne(row: dict, mois: int) -> bool:
    """Ce mois fait-il partie du rythme de la regle ?

    Le mois d'ancrage cale les periodicites non mensuelles : un loyer bimestriel ancre en juin
    tombe en juin, aout, octobre... et pas en juillet.
    """
    periodicite = row.get("periodicite") or "Mensuel"
    pas = {"Mensuel": 1, "Bimestriel": 2, "Trimestriel": 3}.get(periodicite)
    if not pas:
        return False
    if pas == 1:
        return True
    ancre = int(row.get("mois_ancre") or 0)
    if not ancre:
        return False        # sans ancrage, on ne devine pas le rythme : la regle ne se declenche pas
    return (mois - ancre) % pas == 0


def regles_calendaires(rows=None) -> list:
    return [r for r in (rows if rows is not None else engine.load_rules())
            if (r.get("declencheur") or "Banque") == DECLENCHEUR_CALENDRIER]


def echeances_du_jour(jour, rows=None) -> list:
    """Regles calendaires dont la date de declenchement est `jour`."""
    jour = getdate(jour)
    out = []
    for row in regles_calendaires(rows):
        if not mois_concerne(row, jour.month):
            continue
        d = date_declenchement(row, jour.year, jour.month)
        if d and d == jour:
            out.append((row, d))
    return out


def process_calendrier(jour=None, insert: bool = True, rows=None, rattrapage: bool = False) -> list:
    """Cree les ecritures anticipees du jour, chacune avec son ordre de paiement.

    `rattrapage=True` traite aussi les echeances du mois deja passees et non comptabilisees —
    utile apres une interruption du scheduler, ou pour amorcer l'historique.
    Contrat de retour identique aux autres `process_*` : [{flux, ref, status, ...}].
    """
    jour = getdate(jour or frappe.utils.nowdate())
    rows = rows if rows is not None else engine.load_rules()

    if rattrapage:
        candidats = []
        for row in regles_calendaires(rows):
            if not mois_concerne(row, jour.month):
                continue
            d = date_declenchement(row, jour.year, jour.month)
            if d and d <= jour:
                candidats.append((row, d))
    else:
        candidats = echeances_du_jour(jour, rows)

    out = []
    for row, d in candidats:
        reference = engine.build_reference(row, {"date": d, "reference": ""})
        # Idempotence : le numero de reference porte la periode, il ne peut y avoir qu'une
        # ecriture par regle et par mois.
        if frappe.db.exists("Journal Entry", {"cheque_no": reference}):
            out.append({"flux": row["cle"], "ref": reference, "status": "skipped"})
            continue
        try:
            montant = flt(row.get("montant"), 3)
            if not montant:
                out.append({"flux": row["cle"], "ref": reference, "status": "error",
                            "error": "declencheur calendaire sans montant attendu : "
                                     "impossible d'anticiper l'ecriture"})
                continue
            # L'ecriture anticipee credite le compte d'ATTENTE, pas la banque : tant que le
            # virement n'est pas au releve, rien ne prouve qu'il est parti. C'est le
            # rapprochement (ordres.confirmer_par_banque) qui la recree sur la banque.
            anticipee = dict(row)
            if row.get("compte_attente"):
                anticipee["compte_banque"] = row["compte_attente"]
            lines = engine.build_lines(anticipee, montant)
            je = journal.build_journal_entry(
                journal._company_of(row["compte_charge"]), d, lines,
                remark="%s\n(ecriture anticipee : confirmation bancaire attendue)" % reference,
                cheque_no=reference, cheque_date=d,
                mode_of_payment=row.get("mode_paiement") or None)
            if not insert:
                out.append({"flux": row["cle"], "ref": reference, "status": "created",
                            "je": "(dry-run)", "montant": montant, "date": str(d)})
                continue
            je.insert(ignore_permissions=True)
            # L'ecriture anticipee suit le meme reglage que les autres : soumise si
            # `auto_submit_journal_entries` est coche. Elle credite le compte d'ATTENTE, donc la
            # soumettre n'affirme rien sur la banque — c'est `reglement.py` qui la basculera.
            if journal._auto_submit_enabled():
                je.submit()
            ordre = ordres.creer_ordre(
                libelle=reference, montant=montant, date_prevue=d, journal_entry=je.name,
                type_depense=row.get("type"), beneficiaire=row.get("libelle"),
                compte_banque=row.get("compte_banque"), source_regle=row["cle"],
                periode=d.strftime("%Y-%m"))
            out.append({"flux": row["cle"], "ref": reference, "status": "created",
                        "je": je.name, "ordre": ordre.name if ordre else None,
                        "montant": montant, "date": str(d)})
        except Exception as e:
            out.append({"flux": row["cle"], "ref": reference, "status": "error",
                        "error": str(e)[:160]})
    return out
