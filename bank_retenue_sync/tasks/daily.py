"""Taches planifiees.

Chaque tache est gardee par le coupe-circuit `Bank Retenue Sync Settings.enabled`, isolee par un
try/except global et journalisee : un incident cote service bancaire ne doit jamais faire echouer
le scheduler du site.

Aucune tache ne SOUMET quoi que ce soit. Les ecritures restent en brouillon tant que
`auto_submit_journal_entries` n'est pas coche.
"""
from __future__ import annotations

import frappe


def _enabled() -> bool:
    try:
        return bool(frappe.db.get_single_value("Bank Retenue Sync Settings", "enabled"))
    except Exception:
        return False


def _safe(nom, fn):
    if not _enabled():
        return None
    try:
        out = fn()
        frappe.db.commit()
        return out
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(f"tache {nom} echouee\n{e}", "brs scheduler")
        return None


def factures_email():
    """Factures recues par email : Total, Aramex, note d'honoraire comptable.

    Chacune est lue sur son PDF (extraction OpenAI) et comptabilisee avec ses montants REELS —
    HT, TVA, timbre, retenue a la source. C'est ce qui la rend superieure a une regle calendaire,
    qui devrait deviner ces composantes. L'idempotence tient au numero de reference periodise
    (« Facture Aramex MM-YYYY »), donc relire la meme boite ne cree rien deux fois.
    """
    from bank_retenue_sync import orchestrator

    return _safe("factures_email", lambda: orchestrator.run_email_ingestion())


def paiements_carte():
    """Paiements de la carte technologique, lus au releve de CARTE et non au releve bancaire.

    Une fois par jour suffit : la carte sert a la publicite en ligne, dont les debits arrivent
    par vagues de quelques lignes. L'idempotence tient au numero de paiement, donc un passage
    supplementaire ne cree jamais de doublon.
    """
    from bank_retenue_sync import orchestrator

    return _safe("paiements_carte", lambda: orchestrator.run_cartes())


def verification_bancaire():
    """Passage de verification : releve + solde + identification + ecritures qui en decoulent.

    Tourne CINQ fois par jour (9h, 11h, 13h, 15h, 17h). Chaque passage est complet et
    idempotent : l'import n'insere que l'inconnu, la classification reprend tout le registre, et
    l'ecriture mensuelle de frais est recalculee depuis zero puis refaite seulement si son total
    a change. Un passage sans nouveaute ne laisse aucune trace comptable.
    """
    from bank_retenue_sync import orchestrator

    return _safe("verification_bancaire", lambda: orchestrator.run_verification_bancaire())


def sync_bancaire():
    """Export bancaire -> registre -> classification. Ne cree aucune ecriture."""
    from bank_retenue_sync import orchestrator
    return _safe("sync_bancaire", lambda: orchestrator.run_identification(refresh=True))


def depenses_recurrentes():
    """Depenses parametrees declenchees par le releve, en brouillon."""
    from bank_retenue_sync import orchestrator
    return _safe("depenses_recurrentes", lambda: orchestrator.run_depenses_recurrentes())


def depenses_calendaires():
    """Salaires et loyer : ecritures anticipees a date fixe, avec leur ordre de paiement."""
    from bank_retenue_sync import orchestrator
    return _safe("depenses_calendaires", lambda: orchestrator.run_calendrier())


def contrats_financement():
    """Echeances de pret et de leasing detectees au releve."""
    from bank_retenue_sync import orchestrator
    return _safe("contrats_financement", lambda: orchestrator.run_contrats())


def confirmation_ordres():
    """Confirme par la banque les ordres de paiement en attente."""
    from bank_retenue_sync import orchestrator
    return _safe("confirmation_ordres", lambda: orchestrator.run_ordres())


def audit_quotidien():
    """Rapport des depenses sans ecriture. Constat pur."""
    from bank_retenue_sync import orchestrator
    return _safe("audit_quotidien", lambda: orchestrator.run_audit_depenses())


def certificats_ras():
    """Certificats de retenue a la source recus du portail TEJ.

    Une fois par jour, et sans demander de nouveau scraping : le portail est alimente par nos
    CLIENTS, quelques fois par mois. L'empreinte du fichier fait le reste — un export identique a
    celui de la veille s'arrete avant tout traitement. Le rapprochement, lui, est rejoue a chaque
    passage : une facture saisie hier peut expliquer un certificat d'avant-hier.
    """
    from bank_retenue_sync import orchestrator

    return _safe("certificats_ras", lambda: orchestrator.run_certificats_ras())
