"""Specs declaratives des emails sources que bank_retenue_sync doit reconnaitre.

Chaque source = un type d'email recu sur la boite comptable
(aquaworld.servicing@gmail.com) que l'app doit savoir identifier pour, plus tard :
  - generer une depense mensuelle (facture Total, releve Aramex, declaration comptable) ;
  - rapprocher un virement bancaire (Aramex Payment Advice).

Ce module ne contient QUE la description des sources et leurs regles de
correspondance. La detection vit dans `classifier.py`, la recherche dans `finder.py`.

Les motifs `senders` / `subject_keywords` sont issus des emails reels observes
sur la boite (voir la reunion de mise en place). A ajuster ici si un expediteur
change, sans toucher au code de detection.
"""
from __future__ import annotations

from dataclasses import dataclass

# Roles : ce que l'app fera de l'email une fois la source detectee.
# (les traitements en aval seront implementes dans une iteration suivante)
ROLE_BANK_IDENTIFICATION = "bank_identification"   # virement -> identification bancaire
ROLE_MONTHLY_EXPENSE = "monthly_expense"           # facture / declaration -> depense mensuelle


@dataclass(frozen=True)
class EmailSource:
    key: str                        # identifiant stable
    label: str                      # libelle lisible
    role: str                       # ROLE_*
    senders: tuple                  # motifs (substring, minuscule) testes contre l'adresse expediteur
    subject_keywords: tuple = ()    # au moins un requis (accent-insensible) ; vide => expediteur suffit
    attachment_exts: tuple = ()     # extensions de PJ attendues (indicatif, pas un filtre bloquant)
    monthly: bool = True            # source attendue ~une fois par mois


SOURCES = (
    EmailSource(
        key="aramex_payment_advice",
        label="Aramex - Payment Advice (virement)",
        role=ROLE_BANK_IDENTIFICATION,
        senders=("e.aramex.com",),
        subject_keywords=("payment advice",),
        attachment_exts=(".pdf", ".xls"),
        monthly=False,   # peut arriver plusieurs fois par mois
    ),
    EmailSource(
        key="aramex_account_statement",
        label="Aramex - Account Statement (releve mensuel)",
        role=ROLE_MONTHLY_EXPENSE,
        senders=("e.aramex.com",),
        subject_keywords=("account statement",),
        attachment_exts=(".pdf",),
    ),
    EmailSource(
        key="aramex_invoice",
        label="Aramex - Facture mensuelle (E-INV)",
        role=ROLE_MONTHLY_EXPENSE,
        senders=("e.aramex.com",),
        # objet reel : "ARAMEX ACCOUNT NO: ... E-INV NO: 1900526448"
        subject_keywords=("e-inv",),
        attachment_exts=(".pdf",),   # PDF seul -> extraction via OpenAI
    ),
    EmailSource(
        key="total_invoice",
        label="TotalEnergies - Facture mensuelle",
        role=ROLE_MONTHLY_EXPENSE,
        # expediteur actuel : carte.tn@totalenergies.com ; total.tn/applicam.com = historique (2022)
        senders=("totalenergies.com", "total.tn", "applicam.com"),
        # "facture" cible la Facture Cartes Prepayees et exclut les "Bon de chargement"
        subject_keywords=("facture",),
        attachment_exts=(".zip",),
    ),
    EmailSource(
        key="comptable_declaration",
        label="Comptable - Declaration mensuelle",
        role=ROLE_MONTHLY_EXPENSE,
        senders=("belghithayman@gmail.com",),
        # Sujet non fiable cote comptable (ex. "Decaration Juin", fautes de frappe) :
        # tout email de cet expediteur est un candidat.
        subject_keywords=(),
    ),
)

SOURCES_BY_KEY = {s.key: s for s in SOURCES}
