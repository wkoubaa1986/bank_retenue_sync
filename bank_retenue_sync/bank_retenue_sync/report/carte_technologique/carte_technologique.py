"""Suivi de la carte technologique : ce qui la recharge, ce qui la depense, ce qui y reste.

POURQUOI UN SUIVI A PART
------------------------
La carte technologique est une TRESORERIE PARALLELE : elle est alimentee par un virement depuis
Zitouna (« CHARGEMENT CARTE TECHNOLOGIQUE » au releve) puis depensee en publicite Facebook, et ces
depenses-la ne paraissent JAMAIS au releve bancaire. Le rapprochement bancaire ne peut donc rien
en dire — d'ou ce suivi, qui lit le compte lui-meme.

Un solde de carte qui approche zero est une information operationnelle, pas comptable : c'est le
moment de la recharger avant que les campagnes ne s'arretent. C'est ce que la tuile « Solde
disponible » donne d'un coup d'oeil.

LES DEUX SENS N'ONT PAS LA MEME PREUVE
--------------------------------------
Une RECHARGE se prouve au releve : elle a une reference bancaire, et `bank/classify` la rapproche.
Une DEPENSE n'a que sa facture — rien en banque ne la confirme. La colonne « Rapproché » ne
concerne donc que les recharges ; l'afficher sur une depense laisserait croire a un controle qui
n'existe pas.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate

COMPTE_CARTE = "Carte technologique - A&S"


def execute(filters=None):
    filters = frappe._dict(filters or {})
    compte = filters.get("compte") or COMPTE_CARTE
    lignes = _mouvements(compte, filters)
    return _colonnes(), lignes, None, _graphique(lignes), _synthese(compte, lignes)


def _colonnes() -> list:
    return [
        {"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
        {"fieldname": "nature", "label": _("Nature"), "fieldtype": "Data", "width": 110},
        {"fieldname": "libelle", "label": _("Libellé"), "fieldtype": "Data", "width": 280},
        {"fieldname": "recharge", "label": _("Recharge"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "depense", "label": _("Dépense"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "solde", "label": _("Solde après"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "journal_entry", "label": _("Écriture"), "fieldtype": "Link",
         "options": "Journal Entry", "width": 170},
        {"fieldname": "rapproche", "label": _("Rapproché"), "fieldtype": "Data", "width": 110},
    ]


def _mouvements(compte: str, filters) -> list:
    conditions, params = ["gl.account = %(compte)s", "gl.is_cancelled = 0"], {"compte": compte}
    if filters.get("date_from"):
        conditions.append("gl.posting_date >= %(date_from)s")
        params["date_from"] = filters.date_from
    if filters.get("date_to"):
        conditions.append("gl.posting_date <= %(date_to)s")
        params["date_to"] = filters.date_to

    rows = frappe.db.sql(
        """select gl.posting_date, gl.voucher_type, gl.voucher_no,
                  round(sum(gl.debit), 3) as recharge, round(sum(gl.credit), 3) as depense
           from `tabGL Entry` gl
           where %s
           group by gl.voucher_type, gl.voucher_no, gl.posting_date
           order by gl.posting_date, gl.voucher_no""" % " and ".join(conditions),
        params, as_dict=1)
    if not rows:
        return []

    textes = _textes([r.voucher_no for r in rows])
    # Une recharge est rapprochee si un mouvement du releve pointe son ecriture. C'est le seul
    # controle possible ici : les depenses n'ont pas de contrepartie bancaire.
    rapproches = set(frappe.db.get_all(
        "BRS Bank Movement", filters={"document_name": ["in", [r.voucher_no for r in rows]]},
        pluck="document_name"))
    # ⚠️ Hors de la periode du registre, l'absence de rapprochement ne prouve RIEN : le releve
    # ne remonte pas jusque-la. Sans cette borne, 18 recharges de 2024-2025 ressortaient en
    # alerte rouge alors qu'elles sont simplement anterieures a l'import.
    debut, fin = frappe.db.sql(
        "select min(`date`), max(`date`) from `tabBRS Bank Movement`")[0]

    # Le solde progressif est calcule sur TOUTE l'histoire du compte, meme si un filtre de date
    # masque le debut : un solde qui repartirait de zero au 1er du mois serait faux.
    solde = _solde_avant(compte, rows[0].posting_date)
    out = []
    for r in rows:
        solde = round(solde + r.recharge - r.depense, 3)
        recharge = r.recharge > 0
        out.append({
            "date": r.posting_date,
            "nature": _("Recharge") if recharge else _("Dépense"),
            "libelle": textes.get(r.voucher_no) or r.voucher_no,
            "recharge": r.recharge or None,
            "depense": r.depense or None,
            "solde": solde,
            "journal_entry": r.voucher_no if r.voucher_type == "Journal Entry" else None,
            "rapproche": _rapproche(r, recharge, rapproches, debut, fin),
        })
    return out


def _rapproche(r, recharge: bool, rapproches: set, debut, fin) -> str:
    """« oui » / « non » / « » — et le vide n'est pas un oubli, c'est l'absence de preuve."""
    if not recharge:
        return ""
    if r.voucher_no in rapproches:
        return _("oui")
    if not debut or not (getdate(debut) <= getdate(r.posting_date) <= getdate(fin)):
        return _("hors registre")
    return _("non")


def _solde_avant(compte: str, jour) -> float:
    return flt(frappe.db.sql(
        """select sum(debit) - sum(credit) from `tabGL Entry`
           where account = %s and is_cancelled = 0 and posting_date < %s""",
        (compte, getdate(jour)))[0][0], 3)


def _textes(noms: list) -> dict:
    return {r.name: (r.cheque_no or r.user_remark or "").split("\n")[0][:140]
            for r in frappe.db.get_all("Journal Entry", filters={"name": ["in", noms]},
                                       fields=["name", "cheque_no", "user_remark"],
                                       limit_page_length=0)}


def _synthese(compte: str, lignes: list) -> list:
    """Le solde vient du COMPTE, pas des lignes affichees : un filtre de date ne doit pas
    changer l'argent disponible sur la carte."""
    total = frappe.db.sql(
        """select round(sum(debit), 3), round(sum(credit), 3), round(sum(debit) - sum(credit), 3)
           from `tabGL Entry` where account = %s and is_cancelled = 0""", compte)[0]
    recharges, depenses, solde = (flt(x, 3) for x in total)
    non_rapprochees = [l for l in lignes if l["rapproche"] == _("non")]
    tuiles = [
        {"value": solde, "label": _("Solde comptable"), "datatype": "Currency",
         "indicator": "Red" if solde < 200 else ("Orange" if solde < 500 else "Green")},
    ]
    # Le solde REEL vient du service : le confronter au solde comptable est le seul controle
    # possible sur ce compte, la carte n'ayant aucune contrepartie au releve bancaire. C'est lui
    # qui a revele deux paiements de juillet jamais comptabilises.
    tuiles += _tuiles_controle(solde)
    return tuiles + [
        {"value": flt(sum(l["depense"] or 0 for l in lignes), 3),
         "label": _("Dépensé sur la période"), "datatype": "Currency"},
        {"value": flt(sum(l["recharge"] or 0 for l in lignes), 3),
         "label": _("Rechargé sur la période"), "datatype": "Currency"},
        {"value": len(non_rapprochees), "label": _("Recharges non rapprochées"), "datatype": "Int",
         "indicator": "Red" if non_rapprochees else "Green"},
        {"value": recharges, "label": _("Total rechargé"), "datatype": "Currency"},
        {"value": depenses, "label": _("Total dépensé"), "datatype": "Currency"},
    ]


def _tuiles_controle(solde_comptable: float) -> list:
    """Solde reel, ecart, et surtout : combien de paiements du releve manquent aux livres.

    Non bloquant par contrat : le service peut etre injoignable (autre conteneur, scraping en
    cours). Le rapport doit rester lisible sans lui — on perd le controle, pas le suivi.

    L'ordre des tuiles suit l'ordre du raisonnement : le nombre d'operations manquantes vient
    AVANT l'ecart, parce qu'il l'explique. Un ecart de 620 DT avec un paiement non comptabilise
    n'est pas un frais bancaire a regulariser, c'est ce paiement — les lire dans l'autre sens
    conduit tout droit a inventer une charge et a masquer l'operation.
    """
    from bank_retenue_sync.bank import cartes

    try:
        etat = cartes.etat_controle()
    except Exception:
        return []
    r = etat["resume"]
    tuiles = [
        {"value": r["a_comptabiliser"], "label": _("Paiements carte non comptabilisés"),
         "datatype": "Int", "indicator": "Red" if r["a_comptabiliser"] else "Green"},
    ]
    if r["a_comptabiliser"]:
        tuiles.append({"value": r["montant_a_comptabiliser"], "label": _("Montant à comptabiliser"),
                       "datatype": "Currency", "indicator": "Red"})
    if etat.get("lu_le"):
        reel, ecart = flt(etat["solde_reel"], 3), flt(etat["ecart"], 3)
        tuiles += [
            {"value": reel, "label": _("Solde réel (carte)"), "datatype": "Currency",
             "indicator": "Red" if reel < 200 else "Green"},
            {"value": ecart, "label": _("Écart livres / carte"), "datatype": "Currency",
             "indicator": "Green" if abs(ecart) < 0.5 else "Red"},
            {"value": flt(etat.get("plafond_restant"), 3), "label": _("Plafond annuel restant"),
             "datatype": "Currency"},
        ]
    return tuiles


def _graphique(lignes: list) -> dict:
    """Le solde dans le temps : c'est la courbe qui dit quand recharger."""
    if not lignes:
        return None
    return {
        "data": {"labels": [str(l["date"]) for l in lignes],
                 "datasets": [{"name": _("Solde"), "values": [l["solde"] for l in lignes]}]},
        "type": "line", "colors": ["#2490ef"], "lineOptions": {"regionFill": 1},
    }
