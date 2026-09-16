"""Paiements a venir : ce qui est DEJA PAYE mais pas encore SORTI de la banque.

Le cas (demande utilisateur 16/09/2026) : une TRAITE BANCAIRE emise depuis la caisse. Le
fournisseur est regle — le papier est parti — mais la banque ne debite qu'a l'ECHEANCE. Ce n'est
donc pas un « paiement a faire » (rien a virer) : c'est une sortie a venir, a une date connue,
qu'il faut voir pour la tresorerie et pour controler le releve le jour venu.

Source : les ordres de paiement (BRS Ordre de Paiement) de type « Traite bancaire », statut
« En attente », poses par customization_app.caisse_depenses a la saisie de la traite. Quand le
debit parait au releve dans la fenetre de l'echeance, l'identification bancaire passe l'ordre a
« Vire » et recree l'ecriture sur Zitouna : la ligne disparait d'elle-meme.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

DOCTYPE = "BRS Ordre de Paiement"
TYPE_TRAITE = "Traite bancaire"
DECOUVERT = "Compte de découvert bancaire - A&S"


def execute(filters=None):
    filters = frappe._dict(filters or {})
    lignes = _traites_a_echoir(filters)
    return _colonnes(), lignes, None, None, _synthese(lignes)


@frappe.whitelist()
def nb_a_venir() -> int:
    """Nombre de traites en attente d'echeance — pour un compteur de raccourci."""
    frappe.only_for(("System Manager", "Accounts Manager", "Accounts User"))
    return len(_traites_a_echoir(frappe._dict({})))


def _traites_a_echoir(filters) -> list:
    """`dans` : jours restants avant l'echeance (negatif = echeance passee sans debit vu au
    releve — c'est la qu'il faut regarder le releve, ou le rejet)."""
    if not frappe.db.exists("DocType", DOCTYPE):
        return []
    conditions = {"statut": "En attente", "type_depense": TYPE_TRAITE}
    if filters.get("date_from"):
        conditions["date_prevue"] = [">=", filters.date_from]
    rows = frappe.db.get_all(DOCTYPE, filters=conditions, limit_page_length=0,
                             order_by="date_prevue asc",
                             fields=["name", "libelle", "montant", "date_prevue",
                                     "beneficiaire", "journal_entry", "creation"])
    aujourdhui = getdate(nowdate())
    out = []
    for r in rows:
        if filters.get("date_to") and r.date_prevue and getdate(r.date_prevue) > getdate(filters.date_to):
            continue
        out.append({
            "echeance": r.date_prevue,
            "dans": (getdate(r.date_prevue) - aujourdhui).days if r.date_prevue else 0,
            "reference": r.libelle,
            "beneficiaire": r.beneficiaire or _("(fournisseur)"),
            "montant": flt(r.montant, 3),
            "journal_entry": r.journal_entry,
            "ordre": r.name,
            "contrepartie": DECOUVERT,
        })
    return out


def _colonnes() -> list:
    return [
        {"fieldname": "echeance", "label": _("Échéance"), "fieldtype": "Date", "width": 100},
        {"fieldname": "dans", "label": _("Dans (j)"), "fieldtype": "Int", "width": 80},
        {"fieldname": "reference", "label": _("Traite"), "fieldtype": "Data", "width": 360},
        {"fieldname": "beneficiaire", "label": _("Bénéficiaire"), "fieldtype": "Data",
         "width": 170},
        {"fieldname": "montant", "label": _("Montant"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "journal_entry", "label": _("Écriture"), "fieldtype": "Link",
         "options": "Journal Entry", "width": 170},
        {"fieldname": "ordre", "label": _("Ordre"), "fieldtype": "Link",
         "options": DOCTYPE, "width": 150},
        {"fieldname": "contrepartie", "label": _("En attente sur"), "fieldtype": "Link",
         "options": "Account", "width": 220},
    ]


def _synthese(lignes: list) -> list:
    total = flt(sum(l["montant"] for l in lignes), 3)
    passees = [l for l in lignes if l["dans"] < 0]
    sept_jours = [l for l in lignes if 0 <= l["dans"] <= 7]
    return [
        {"value": total, "label": _("À sortir"), "datatype": "Currency",
         "indicator": "Blue" if total else "Green"},
        {"value": flt(sum(l["montant"] for l in sept_jours), 3),
         "label": _("Sous 7 jours"), "datatype": "Currency",
         "indicator": "Orange" if sept_jours else "Green"},
        {"value": flt(sum(l["montant"] for l in passees), 3),
         "label": _("Échéance passée, débit non vu"), "datatype": "Currency",
         "indicator": "Red" if passees else "Green"},
    ]
