"""Dépenses en euros payées depuis « Compte Wassim Euros » — écran de l'espace Banque (demande du 02/11/2026).

Pub Google, Facebook, abonnements… sont réglés en euros depuis le compte personnel en devises. Chacune était une
écriture de journal multi-devises saisie à la main (ex. ACC-JV-2026-00608 : Frais de Marketing 620,338 DT /
Compte Wassim Euros 185,73 € au taux 3,34). Ici : plusieurs lignes d'un coup (date, libellé, montant en euros, compte
de charge), le taux EUR→TND du jour lu sur internet (modifiable), et à la validation UNE écriture de journal par
ligne, soumise : débit du compte de charge en dinars, crédit du compte en euros au taux saisi.

Le taux : d'abord un « Currency Exchange » ERPNext du jour s'il existe, sinon open.er-api.com (gratuit, sans clé,
connaît le TND ; frankfurter/BCE ne le connaît pas). En cas d'échec le taux se tape à la main.
"""
from __future__ import annotations

import json

import frappe
import requests
from frappe import _
from frappe.utils import flt, getdate, nowdate

COMPANY = "Aquaworld & Servicing"
COST_CENTER = "Principal - A&S"
COMPTE_EUROS = "Compte Wassim Euros - A&S"
COMPTE_CHARGE_DEFAUT = "Frais de Marketing - A&S"
ROLES = ("System Manager", "Accounts Manager", "Banque")
URL_TAUX = "https://open.er-api.com/v6/latest/EUR"
MAX_LIGNES = 50


# ── Règles pures ─────────────────────────────────────────────────────────────

def montant_tnd(eur: float, taux: float) -> float:
    """Contre-valeur en dinars, à 3 décimales (précision comptable). Pure."""
    return flt(flt(eur) * flt(taux), 3)


def verifier_lignes(lignes, taux: float, comptes_charge: set) -> list[str]:
    """Les erreurs qui empêchent la validation (vide = tout est bon). Pure.
    `lignes` : [{date, libelle, montant_eur, compte_charge}]."""
    erreurs = []
    if flt(taux) <= 0:
        erreurs.append(_("Le taux EUR → TND doit être supérieur à zéro."))
    if not lignes:
        erreurs.append(_("Aucune ligne à enregistrer."))
    if len(lignes or []) > MAX_LIGNES:
        erreurs.append(_("Au plus {0} lignes à la fois.").format(MAX_LIGNES))
    for i, l in enumerate(lignes or [], 1):
        if not l.get("date"):
            erreurs.append(_("Ligne {0} : date manquante.").format(i))
        if not (l.get("libelle") or "").strip():
            erreurs.append(_("Ligne {0} : libellé manquant.").format(i))
        if flt(l.get("montant_eur")) <= 0:
            erreurs.append(_("Ligne {0} : montant en euros manquant ou nul.").format(i))
        if l.get("compte_charge") not in comptes_charge:
            erreurs.append(_("Ligne {0} : compte de charge « {1} » inconnu ou non utilisable.").format(i, l.get("compte_charge")))
    return erreurs


def resume(lignes, taux: float) -> dict:
    """Totaux en euros et en dinars. Pure."""
    eur = flt(sum(flt(l.get("montant_eur")) for l in lignes or []), 2)
    return {"nb": len(lignes or []), "eur": eur, "tnd": montant_tnd(eur, taux)}


# ── Lecture ──────────────────────────────────────────────────────────────────

def _acces():
    frappe.only_for(ROLES)


def _comptes_charge() -> list[dict]:
    return frappe.get_all("Account", filters={"company": COMPANY, "root_type": "Expense", "is_group": 0, "disabled": 0},
                          fields=["name", "parent_account"], order_by="name")


@frappe.whitelist()
def contexte():
    """Ce qu'il faut pour peindre l'écran : comptes en euros, comptes de charge, défauts, dernières dépenses."""
    _acces()
    euros = frappe.get_all("Account", filters={"company": COMPANY, "account_currency": "EUR", "is_group": 0, "disabled": 0},
                           pluck="name", order_by="name")
    return {"comptes_euros": euros, "compte_euros": COMPTE_EUROS if COMPTE_EUROS in euros else (euros[0] if euros else None),
            "comptes_charge": _comptes_charge(), "compte_charge": COMPTE_CHARGE_DEFAUT, "aujourd_hui": nowdate(),
            "historique": historique()}


@frappe.whitelist()
def taux_eur_tnd():
    """Le taux du jour : Currency Exchange ERPNext daté d'aujourd'hui, sinon open.er-api.com."""
    _acces()
    jour = nowdate()
    ce = frappe.db.get_value("Currency Exchange", {"from_currency": "EUR", "to_currency": "TND", "date": jour},
                             "exchange_rate", order_by="creation desc")
    if flt(ce) > 0:
        return {"taux": flt(ce, 6), "source": _("Taux de change ERPNext du {0}").format(jour), "date": jour}
    try:
        r = requests.get(URL_TAUX, timeout=12)
        r.raise_for_status()
        d = r.json()
        taux = flt((d.get("rates") or {}).get("TND"))
        if d.get("result") != "success" or taux <= 0:
            raise ValueError("réponse sans taux TND")
    except Exception as e:
        frappe.throw(_("Taux introuvable sur internet ({0}) : saisissez-le à la main.").format(str(e)[:120]))
    return {"taux": flt(taux, 6), "source": "open.er-api.com (" + (d.get("time_last_update_utc") or jour) + ")", "date": jour}


@frappe.whitelist()
def historique(limite=20):
    """Les dernières sorties du compte en euros (écritures de journal validées)."""
    _acces()
    rows = frappe.db.sql("""select je.name, je.posting_date, je.cheque_no as libelle, jea.credit_in_account_currency as eur,
                                   jea.exchange_rate as taux, jea.credit as tnd,
                                   (select group_concat(distinct a2.account separator ', ') from `tabJournal Entry Account` a2
                                     where a2.parent = je.name and a2.debit > 0) as compte_charge
                            from `tabJournal Entry` je join `tabJournal Entry Account` jea on jea.parent = je.name
                            where je.docstatus = 1 and jea.account = %s and jea.credit_in_account_currency > 0
                            order by je.posting_date desc, je.creation desc limit %s""", (COMPTE_EUROS, int(limite)), as_dict=True)
    for r in rows:
        r.posting_date, r.eur, r.taux, r.tnd = str(r.posting_date), flt(r.eur, 2), flt(r.taux, 6), flt(r.tnd, 3)
    return rows


# ── Écriture ─────────────────────────────────────────────────────────────────

@frappe.whitelist(methods=["POST"])
def creer_depenses(lignes, taux, compte_euros=None, source=None):
    """Une écriture de journal multi-devises SOUMISE par ligne : débit charge (TND), crédit compte euros (EUR × taux).
    Tout ou rien : une ligne refusée annule les précédentes."""
    _acces()
    lignes = json.loads(lignes) if isinstance(lignes, str) else (lignes or [])
    taux = flt(taux)
    compte_euros = compte_euros or COMPTE_EUROS
    if frappe.db.get_value("Account", compte_euros, "account_currency") != "EUR":
        frappe.throw(_("{0} n'est pas un compte en euros.").format(compte_euros))
    erreurs = verifier_lignes(lignes, taux, {c.name for c in _comptes_charge()})
    if erreurs:
        frappe.throw("<br>".join(erreurs))
    crees = []
    for l in lignes:
        eur = flt(l["montant_eur"], 2)
        tnd = montant_tnd(eur, taux)
        libelle = (l["libelle"] or "").strip()
        je = frappe.get_doc({
            "doctype": "Journal Entry", "voucher_type": "Journal Entry", "company": COMPANY, "posting_date": getdate(l["date"]),
            "multi_currency": 1, "cheque_no": libelle[:140], "cheque_date": getdate(l["date"]),
            "user_remark": _("Dépense en euros : {0} — {1} € × {2} = {3} DT{4}").format(
                libelle, eur, taux, tnd, (" (" + source + ")") if source else ""),
            "accounts": [
                {"account": l["compte_charge"], "debit_in_account_currency": tnd, "cost_center": COST_CENTER,
                 "user_remark": libelle},
                {"account": compte_euros, "credit_in_account_currency": eur, "exchange_rate": taux, "cost_center": COST_CENTER,
                 "user_remark": libelle},
            ]})
        je.insert()
        je.submit()
        crees.append({"name": je.name, "date": str(je.posting_date), "libelle": libelle, "eur": eur, "tnd": tnd,
                      "compte_charge": l["compte_charge"]})
    return {"crees": crees, "resume": resume(lignes, taux), "taux": taux}
