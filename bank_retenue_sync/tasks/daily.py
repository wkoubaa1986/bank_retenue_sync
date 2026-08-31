"""Taches planifiees.

Chaque tache est gardee par le coupe-circuit `Bank Retenue Sync Settings.enabled`, isolee par un
try/except global et journalisee : un incident cote service bancaire ne doit jamais faire echouer
le scheduler du site.

Les taches qui pilotent un navigateur via tej-bank-service (verification bancaire, factures
email, carte, certificats RAS) sont en DEUX temps : le tick planifie (`<nom>`) met le vrai
travail (`<nom>_job`) en file sur la queue `long` — cf. `_dispatch` pour le pourquoi.

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


def _dispatch(nom: str, timeout: int = 3600):
    """Le tick du scheduler ne fait que METTRE EN FILE le vrai travail, sur la queue `long`.

    POURQUOI : les jobs de frequence « Cron » partent sur la queue default de Frappe, tuee a
    300 s (`ScheduledJobType.get_queue_name` ne reserve la queue long qu'aux frequences
    « Long »/« Maintenance »). Or un passage bancaire complet pilote un navigateur a travers
    tej-bank-service : plusieurs minutes des que le portail rame. Constate en prod le
    2026-08-21 : TOUS les passages du jour tues en plein vol (« Task exceeded maximum timeout
    value (300 seconds) »), import parfois commite mais classification et creations jamais
    atteintes. Le tick, lui, revient en quelques millisecondes.

    `job_id` + `deduplicate` : un passage encore en cours ou deja en file n'est pas double par
    le tick suivant — sept crons par jour ne peuvent pas s'empiler quand la banque est lente.
    """
    if not _enabled():
        return None
    frappe.enqueue("bank_retenue_sync.tasks.daily." + nom + "_job",
                   queue="long", timeout=timeout,
                   job_id="brs-" + nom, deduplicate=True)
    return "en file (long)"


def factures_email():
    """Factures recues par email : Total, Aramex, note d'honoraire comptable.

    Chacune est lue sur son PDF (extraction OpenAI) et comptabilisee avec ses montants REELS —
    HT, TVA, timbre, retenue a la source. C'est ce qui la rend superieure a une regle calendaire,
    qui devrait deviner ces composantes. L'idempotence tient au numero de reference periodise
    (« Facture Aramex MM-YYYY »), donc relire la meme boite ne cree rien deux fois.
    """
    return _dispatch("factures_email")


def factures_email_job():
    from bank_retenue_sync import orchestrator

    return _safe("factures_email", lambda: orchestrator.run_email_ingestion())


def paiements_carte():
    """Paiements de la carte technologique, lus au releve de CARTE et non au releve bancaire.

    Une fois par jour suffit : la carte sert a la publicite en ligne, dont les debits arrivent
    par vagues de quelques lignes. L'idempotence tient au numero de paiement, donc un passage
    supplementaire ne cree jamais de doublon.
    """
    return _dispatch("paiements_carte")


def paiements_carte_job():
    from bank_retenue_sync import orchestrator

    return _safe("paiements_carte", lambda: orchestrator.run_cartes())


def verification_bancaire():
    """Passage de verification : releve + solde + identification + ecritures qui en decoulent.

    Tourne SEPT fois par jour — 5h, 7h, 9h, 11h, 13h, 15h et 17h (5h et 7h ajoutees le
    2026-08-21). Chaque passage est complet et idempotent : l'import n'insere que l'inconnu,
    la classification reprend tout le registre, et
    l'ecriture mensuelle de frais est recalculee depuis zero puis refaite seulement si son total
    a change. Un passage sans nouveaute ne laisse aucune trace comptable.
    """
    return _dispatch("verification_bancaire")


def verification_bancaire_job():
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


def depots_tej():
    """Depots de certificats EMIS que TEJ n'a pas encore analyses.

    ⚠️ C'EST LA SECONDE MOITIE DE L'EMISSION, ET ELLE NE PEUT PAS ETRE SYNCHRONE. Le clic sur
    « Valider » enregistre un depot ; le certificat et sa reference n'existent que lorsque TEJ
    l'analyse, quand il veut. Faire attendre le job de creation bloquerait le worker unique du
    service pour tous les autres flux — le contrat le dit explicitement et rend, pour cette
    raison, un corps de suivi tout pret.

    Lecture seule : la route de statut ne resoumet rien, la rappeler est sans risque. Plusieurs
    passages par jour, parce qu'un fournisseur attend son certificat et qu'un depot analyse le
    matin n'a aucune raison d'attendre le lendemain.
    """
    from bank_retenue_sync.tej import emis

    return _safe("depots_tej", lambda: emis.verifier_depots())


def export_emis():
    """L'export des certificats EMIS, regenere une fois par jour sur le portail.

    Le recap des retenues d'achat lit le dernier export que le service detient : un certificat
    cree A LA MAIN sur le portail n'y apparaissait qu'a la prochaine soumission reelle depuis
    l'app — c'est-a-dire jamais, tant que personne n'emettait. Un passage quotidien suffit :
    les certificats manuels se comptent par mois, et chaque regeneration coute un job
    Playwright sur le worker unique du service.
    """
    return _dispatch("export_emis")


def export_emis_job():
    from bank_retenue_sync.tej import emis

    return _safe("export_emis", lambda: emis.certificats_emis(rafraichir=True))


def certificats_ras():
    """Certificats de retenue a la source recus du portail TEJ.

    Une fois par jour, et sans demander de nouveau scraping : le portail est alimente par nos
    CLIENTS, quelques fois par mois. L'empreinte du fichier fait le reste — un export identique a
    celui de la veille s'arrete avant tout traitement. Le rapprochement, lui, est rejoue a chaque
    passage : une facture saisie hier peut expliquer un certificat d'avant-hier.
    """
    return _dispatch("certificats_ras")


def certificats_ras_job():
    from bank_retenue_sync import orchestrator

    return _safe("certificats_ras", lambda: orchestrator.run_certificats_ras())
