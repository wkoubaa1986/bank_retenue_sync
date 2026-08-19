"""Paiements a faire : les ecritures de NOTRE OUTIL qui ne sont pas encore passees par la banque.

LA REGLE, ENONCEE PAR L'UTILISATEUR
-----------------------------------
« Les virements a faire doivent contenir la reference des ecritures de journal creees a partir de
notre outil qui ne sont pas du compte Zitouna. »

Tout tient dans cette phrase :
  1. **creee par l'outil** — l'ecriture porte un NUMERO DE REFERENCE periodise que seul l'outil
     produit (« Facture Aramex 07-2026 », « Salaire Jamel Aloui 08-2026 »). C'est aussi la cle
     d'idempotence des flux, donc un identifiant deja fiable ;
  2. **pas du compte Zitouna** — tant que l'ecriture ne touche pas la banque, l'argent n'est pas
     parti : c'est un virement a faire. Le jour ou il part, `reglement.py` recree l'ecriture SUR
     Zitouna et la ligne disparait d'elle-meme.

Aucune autre condition. Pas d'ordre de paiement a maintenir, pas de FIFO, pas de statut a tenir
a jour : l'etat se lit dans la comptabilite elle-meme.

CE QUE CETTE VERSION A REMPLACE, ET POURQUOI
--------------------------------------------
La version precedente lisait le compte d'attente en FIFO **et** les soldes fournisseurs par tiers.
Elle etait juste mais illisible : elle melangeait des dettes commerciales anciennes (AQUA SERVICE,
JEGHAM, ESSAKIA) avec les virements que l'outil prepare. Or ces dettes-la ne sont pas des
virements a faire par l'outil — elles relevent des etats fournisseurs d'ERPNext.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

BANQUE = "STE430127B - Zitouna - A&S"

# Prefixes des numeros de reference produits par l'outil. Une ecriture qui n'en porte aucun n'a
# pas ete creee par nous : elle n'a rien a faire dans cette liste, meme si elle attend un
# paiement — c'est le sens de « a partir de notre outil ».
# ⚠️ « Facture Total » en est volontairement ABSENTE : elle se regle sur la CARTE prepayee, pas
# par un virement. L'y laisser affichait 988,100 a virer pour une depense deja financee.
PREFIXES = (
    "Facture Aramex",
    "Salaire",
    "Loyer",
    "Note d'honoraire comptable",
)

# La recharge de carte n'a pas de prefixe d'ecriture : elle ne vient pas d'une piece mais du
# solde REEL de la carte. Son libelle de type est donc declare a part.
TYPES_CARTE = _("Carte technologique")

TYPES = {
    "Facture Aramex": _("Aramex"),
    "Salaire": _("Salaire"),
    "Loyer": _("Loyer"),
    "Note d'honoraire comptable": _("Honoraire"),
}


def execute(filters=None):
    filters = frappe._dict(filters or {})
    lignes = _virements_a_faire(filters)
    lignes += _alimentation_carte(filters)
    lignes.sort(key=lambda l: (str(l["date"]), l["reference"]))
    return _colonnes(), lignes, None, _graphique(lignes), _synthese(lignes)


@frappe.whitelist()
def nb_a_faire() -> int:
    """Nombre de virements a faire — pour le COMPTEUR du raccourci de l'espace Comptabilite.

    Frappe n'affiche un badge que sur les raccourcis DocType (shortcut_widget.js) : celui-ci
    etant un rapport, notre patch client (public/js/paiements_a_faire_compteur.js) appelle
    cette methode. Memes roles que le rapport."""
    frappe.only_for(("System Manager", "Accounts Manager", "Accounts User"))
    filters = frappe._dict({})
    return len(_virements_a_faire(filters)) + len(_alimentation_carte(filters))


def _alimentation_carte(filters) -> list:
    """Ligne de recharge de la carte technologique, quand son solde passe sous le seuil.

    ⚠️ C'EST LA SEULE LIGNE SANS ECRITURE DE JOURNAL du tableau, et c'est assume : la recharge
    n'a pas encore ete decidee, donc rien ne la porte en comptabilite. Elle figure ici parce
    qu'un virement a faire reste un virement a faire, qu'une piece existe ou non — et parce que
    la voir a cote des autres est ce qui permet d'arbitrer la tresorerie.

    Le montant est une RECHARGE TYPE (1500 DT), pas ce qui manque pour repasser le seuil : un
    appoint de 555 DT laisserait la carte replonger sous le seuil des le premier debit Facebook,
    et le meme virement serait a refaire la semaine suivante. Il reste plafonne par le solde
    disponible en banque — un virement que le compte ne peut pas honorer n'est pas un paiement a
    faire, c'est un rejet a venir.
    """
    from bank_retenue_sync.bank import cartes

    if filters.get("type") and filters.type != TYPES_CARTE:
        return []
    try:
        r = cartes.recharge_a_faire()
    except Exception:
        # Service injoignable : le tableau reste lisible sans cette ligne.
        return []
    if not r.get("montant"):
        return []
    libelle = _("Alimentation carte technologique — solde {0} DT").format(r["solde_carte"])
    if r.get("plafonne"):
        libelle += _(" (plafonné au solde bancaire, recharge type {0} DT)").format(r["cible"])
    return [{
        "date": getdate(nowdate()),
        "retard": 0,
        "reference": libelle,
        "type_depense": TYPES_CARTE,
        "beneficiaire": _("Carte 5300 XXXX XXXX 7841"),
        "montant": r["montant"],
        "journal_entry": None,
        "contrepartie": cartes.COMPTE_CARTE,
        "brouillon": False,
    }]


def _colonnes() -> list:
    return [
        {"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
        {"fieldname": "retard", "label": _("Retard (j)"), "fieldtype": "Int", "width": 90},
        {"fieldname": "reference", "label": _("Référence"), "fieldtype": "Data", "width": 300},
        {"fieldname": "type_depense", "label": _("Type"), "fieldtype": "Data", "width": 110},
        {"fieldname": "beneficiaire", "label": _("Bénéficiaire"), "fieldtype": "Data",
         "width": 170},
        {"fieldname": "montant", "label": _("Montant à virer"), "fieldtype": "Currency",
         "width": 140},
        {"fieldname": "journal_entry", "label": _("Écriture"), "fieldtype": "Link",
         "options": "Journal Entry", "width": 170},
        {"fieldname": "contrepartie", "label": _("En attente sur"), "fieldtype": "Link",
         "options": "Account", "width": 220},
    ]


def _virements_a_faire(filters) -> list:
    conditions = ["je.docstatus < 2", "ifnull(je.cheque_no, '') != ''"]
    params = {"banques": _comptes_de_tresorerie()}
    if filters.get("date_from"):
        conditions.append("je.posting_date >= %(date_from)s")
        params["date_from"] = filters.date_from
    if filters.get("date_to"):
        conditions.append("je.posting_date <= %(date_to)s")
        params["date_to"] = filters.date_to
    if filters.get("type"):
        prefixe = next((p for p, lib in TYPES.items() if lib == filters.type), None)
        if not prefixe:
            return []
        conditions.append("je.cheque_no like %(prefixe)s")
        params["prefixe"] = prefixe + "%"

    # Le coeur de la regle : l'ecriture ne doit avoir AUCUNE ligne de tresorerie.
    # ⚠️ TOUS les comptes de banque et de caisse, pas seulement Zitouna : « Loyer wassim »
    # creditait `Compte TAWFIR - Banque Zitouna` et ressortait comme un virement a faire alors
    # que l'argent etait deja parti, d'un autre compte.
    # `not exists` plutot qu'une jointure exclue — une ecriture peut toucher la tresorerie sur
    # une seule de ses lignes, et il suffit d'une pour qu'elle soit reglee.
    rows = frappe.db.sql(
        """select je.name, je.posting_date, je.cheque_no, je.docstatus,
                  jea.account, jea.party, round(jea.credit_in_account_currency, 3) as montant
           from `tabJournal Entry` je
           join `tabJournal Entry Account` jea on jea.parent = je.name
           where %s
             and jea.credit_in_account_currency > 0
             and jea.account not in %%(banques)s
             and not exists (select 1 from `tabJournal Entry Account` b
                             where b.parent = je.name and b.account in %%(banques)s)
           order by je.posting_date""" % " and ".join(conditions), params, as_dict=1)

    aujourdhui = getdate(nowdate())
    out = []
    for r in rows:
        prefixe = next((p for p in PREFIXES if (r.cheque_no or "").startswith(p)), None)
        if not prefixe:
            continue
        out.append({
            "date": r.posting_date,
            "retard": (aujourdhui - getdate(r.posting_date)).days,
            "reference": r.cheque_no,
            "type_depense": TYPES.get(prefixe, prefixe),
            "beneficiaire": r.party or _("(interne)"),
            "montant": flt(r.montant, 3),
            "journal_entry": r.name,
            "contrepartie": r.account,
            "brouillon": r.docstatus == 0,
        })
    return out


def _comptes_de_tresorerie() -> list:
    """Tous les comptes de banque et de caisse : une depense qui en touche un est deja payee."""
    comptes = frappe.db.get_all("Account", filters={"account_type": ["in", ["Bank", "Cash"]]},
                                pluck="name")
    return comptes or [BANQUE]


def _synthese(lignes: list) -> list:
    """Trois chiffres, pas plus : ce qu'il faut virer, combien de virements, ce qui traine.

    Les controles de solde de l'ancienne version ont ete retires : ils repondaient a une question
    comptable (le compte d'attente est-il coherent ?) et non a la question posee ici, qui est
    « qu'est-ce que je dois virer aujourd'hui ? ».
    """
    total = flt(sum(l["montant"] for l in lignes), 3)
    retard = [l for l in lignes if flt(l["retard"]) > 7]
    return [
        {"value": total, "label": _("À virer"), "datatype": "Currency",
         "indicator": "Orange" if total else "Green"},
        {"value": len(lignes), "label": _("Virements à faire"), "datatype": "Int"},
        {"value": flt(sum(l["montant"] for l in retard), 3),
         "label": _("En retard (+7 j)"), "datatype": "Currency",
         "indicator": "Red" if retard else "Green"},
    ]


def _graphique(lignes: list) -> dict:
    if not lignes:
        return None
    par_type = {}
    for l in lignes:
        par_type[l["type_depense"]] = flt(par_type.get(l["type_depense"], 0) + l["montant"], 3)
    labels = sorted(par_type, key=lambda k: -par_type[k])
    return {
        "data": {"labels": labels,
                 "datasets": [{"name": _("À virer"), "values": [par_type[k] for k in labels]}]},
        "type": "bar", "colors": ["#e0a800"],
    }
