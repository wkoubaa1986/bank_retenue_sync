"""Tous les leasings en « Charges remboursement véhicules » (demande utilisateur 30/10/2026).

Changan et Cenntro etaient declares sur « Frais de Déplacement » : cinq mois d'echeances (juin a
octobre 2026) y ont ete comptabilises, a cote de CHERY et FIAT qui etaient au bon compte.

  1. Les contrats de type Leasing des reglages passent sur le compte des vehicules.
  2. Chaque echeance de 2026 encore sur l'ancien compte est REFAITE a l'identique sur le bon
     compte, A SA DATE : l'ancienne est annulee (gardee en trace), la nouvelle porte la meme
     date, la meme reference et la meme remarque — l'identification bancaire suit, et chaque
     mois montre ses leasings au bon compte (situation mensuelle, dossier, grand livre).

Pas d'ecriture de reclassement datee du jour : elle gonflait les charges vehicules du mois en
cours de cinq mois d'echeances (vu sur la situation mensuelle le 30/10/2026). Les mois deja
transmis au comptable gardent leurs totaux ; seul le compte des pieces change — a lui re-envoyer
si besoin. Perimetre : l'exercice 2026 ; une assurance citant la reference du contrat n'est pas
une echeance. Idempotent : rien n'est refait si aucune ligne ne reste sur l'ancien compte.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

COMPTE_VEHICULES = "Charges remboursement véhicules - A&S"
ANCIEN_COMPTE = "Frais de Déplacement - A&S"
#: Perimetre : l'exercice 2026 seulement (meme plancher que les controles de retenue).
PLANCHER = "2026-01-01"


def execute():
    if not frappe.db.exists("Account", COMPTE_VEHICULES) or not frappe.db.exists("Account", ANCIEN_COMPTE):
        return
    for contrat in _basculer_contrats():
        for e in _ecritures_du_contrat(contrat):
            _refaire(e.name)
    frappe.db.commit()


def _basculer_contrats() -> list:
    """Les contrats Leasing des reglages -> compte des vehicules. Rend les contrats concernes."""
    from bank_retenue_sync.expenses import contrats as K

    if not frappe.db.exists("DocType", K.SETTINGS):
        return []
    doc = frappe.get_doc(K.SETTINGS)
    touches = []
    for row in doc.get(K.TABLE_FIELD) or []:
        if (row.type or "") != "Leasing":
            continue
        if row.compte_charge != COMPTE_VEHICULES:
            row.compte_charge = COMPTE_VEHICULES
        touches.append(row.as_dict())
    doc.flags.ignore_permissions = True
    doc.save()
    return touches


def _ecritures_du_contrat(contrat: dict) -> list:
    """Les echeances validees de 2026 du contrat qui debitent encore l'ancien compte."""
    ref = (contrat.get("reference_bancaire") or "").strip()
    if not ref:
        return []
    return frappe.db.sql("""
        select je.name, je.posting_date, sum(a.debit_in_account_currency) as montant
        from `tabJournal Entry` je
        join `tabJournal Entry Account` a on a.parent = je.name
        where je.docstatus = 1 and a.account = %s and a.debit_in_account_currency > 0
          and je.posting_date >= %s
          and (je.user_remark like %s or ifnull(je.cheque_no, '') like %s)
          and ifnull(je.cheque_no, '') not like 'Assurance%%'
        group by je.name, je.posting_date order by je.posting_date""",
        (ANCIEN_COMPTE, PLANCHER, "%%%s%%" % ref, "%%%s%%" % (contrat.get("libelle") or ref)),
        as_dict=True)


def _refaire(nom: str) -> str:
    """Refait une echeance a l'identique, A SA DATE, sur le compte des vehicules ; l'ancienne est
    annulee et gardee. Le registre bancaire suit la nouvelle."""
    ancienne = frappe.get_doc("Journal Entry", nom)
    nouvelle = frappe.copy_doc(ancienne)
    for a in nouvelle.accounts:
        if a.account == ANCIEN_COMPTE and flt(a.debit_in_account_currency) > 0:
            a.account = COMPTE_VEHICULES
    nouvelle.posting_date = ancienne.posting_date
    nouvelle.cheque_no = ancienne.cheque_no
    nouvelle.cheque_date = ancienne.cheque_date
    nouvelle.user_remark = (ancienne.user_remark or "") + "\nRefaite sur « %s » (remplace %s)" % (
        COMPTE_VEHICULES, ancienne.name)
    nouvelle.flags.ignore_permissions = True
    nouvelle.insert()
    nouvelle.submit()
    ancienne.flags.ignore_permissions = True
    ancienne.flags.ignore_links = True
    ancienne.cancel()
    if frappe.db.table_exists("BRS Bank Movement"):
        frappe.db.sql("""update `tabBRS Bank Movement` set document_name = %s
                         where document_type = 'Journal Entry' and document_name = %s""",
                      (nouvelle.name, ancienne.name))
    return nouvelle.name
