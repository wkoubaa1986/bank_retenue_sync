"""Frais de lettre de credit : le compte qui les porte (decision utilisateur 30/10/2026).

Tant que la marchandise n'est pas achetee, ces frais ne sont PAS une charge du mois : ce sont des
frais accessoires d'achat EN ATTENTE, portes par un compte d'ACTIF (prets et avances), que le bon
de frais d'approche videra dans la valeur des articles a la reception. Leur TVA, elle, est
deductible tout de suite : elle est sur sa propre ligne, et le dossier mensuel des charges lit ce
compte comme les autres (cf. facturation/charges.comptes_charges). Pas une avance fournisseur :
le fournisseur ne doit rien, il n'est qu'une etiquette pour mesurer le poids des frais.
"""
from __future__ import annotations

import frappe

#: Prefixe du numero de reference des ecritures d'affectation (lu par le rapport et par « Defaire »).
PREFIXE_FRAIS_LC = "Frais LC — "
#: Compte par defaut (cree a la premiere affectation) et son parent.
COMPTE_FRAIS_LC = "Frais de LC en attente - A&S"
COMPTE_PARENT_FRAIS_LC = "Prêts et avances (actif) - A&S"
#: Reglage facultatif de « Bank Retenue Sync Settings » qui remplace le compte par defaut.
CHAMP_REGLAGE = "frais_lc_compte"


def compte_frais_lc(creer: bool = True) -> str:
    """Le compte des frais de LC : celui des reglages s'il existe, sinon le compte par defaut,
    cree sous les prets et avances (actif) a la premiere affectation.

    ⚠️ Lire le reglage seulement si le champ existe : `get_single_value` sur un champ absent fait
    un `frappe.throw` dont le texte reste dans les messages de la reponse meme une fois
    l'exception rattrapee — l'utilisateur voyait « Field frais_lc_compte does not exist » alors
    que l'affectation avait reussi (30/10/2026)."""
    from bank_retenue_sync.encaissement.pending import COMPANY

    nom = None
    try:
        if frappe.get_meta("Bank Retenue Sync Settings").has_field(CHAMP_REGLAGE):
            nom = frappe.db.get_single_value("Bank Retenue Sync Settings", CHAMP_REGLAGE)
    except Exception:
        pass
    nom = nom or COMPTE_FRAIS_LC
    if frappe.db.exists("Account", nom) or not creer:
        return nom
    parent = COMPTE_PARENT_FRAIS_LC if frappe.db.exists("Account", COMPTE_PARENT_FRAIS_LC) \
        else frappe.db.get_value("Account", {"company": COMPANY, "is_group": 1, "root_type": "Asset",
                                             "account_name": ["like", "%Actifs Actuels%"]})
    compte = frappe.get_doc({"doctype": "Account", "account_name": nom.rsplit(" - ", 1)[0],
                             "parent_account": parent, "company": COMPANY, "is_group": 0,
                             "root_type": "Asset",
                             # Le type qu'un bon de frais d'approche accepte comme compte de frais.
                             "account_type": "Expenses Included In Valuation"})
    compte.flags.ignore_permissions = True
    compte.insert()
    return compte.name
