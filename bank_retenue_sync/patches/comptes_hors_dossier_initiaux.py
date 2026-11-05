"""Facturation mensuelle : le Compte Wassim Euros est le premier compte de financement hors dossier
(demande utilisateur 05/11/2026 — les dépenses payées en euros ne vont pas au dossier du comptable).
Ne fait rien si la liste a déjà été réglée à la main ou si le compte n'existe pas. Idempotent."""
import frappe

REGLAGE = "Bank Retenue Sync Settings"
COMPTE = "Compte Wassim Euros - A&S"


def execute():
    if not frappe.db.exists("Account", COMPTE):
        return
    if frappe.get_all("BRS Compte Hors Dossier", filters={"parenttype": REGLAGE, "parentfield": "comptes_hors_dossier"}, limit=1):
        return
    doc = frappe.get_single(REGLAGE)
    doc.append("comptes_hors_dossier", {"account": COMPTE})
    doc.flags.ignore_permissions = True
    doc.flags.ignore_mandatory = True
    doc.save()
