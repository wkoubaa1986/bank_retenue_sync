"""Detection / classification des emails sources (voir `sources.py`).

Fonctions pures et sans effet de bord : on passe (expediteur, sujet[, date])
et on obtient la source correspondante + la periode mensuelle deduite.
Testable hors Frappe.
"""
from __future__ import annotations

import re
import unicodedata

from bank_retenue_sync.mail.sources import SOURCES


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def _norm(s: str) -> str:
    return _strip_accents((s or "").lower()).strip()


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def extract_email(sender: str) -> str:
    """Extrait l'adresse d'un entete From ("Nom <x@y>" ou "x@y")."""
    if not sender:
        return ""
    m = _EMAIL_RE.search(sender)
    return m.group(0).lower() if m else sender.strip().lower()


def classify(sender: str, subject: str):
    """Retourne la 1re EmailSource qui matche (expediteur + mot-cle sujet), sinon None."""
    email = extract_email(sender)
    subj = _norm(subject)
    for src in SOURCES:
        if not any(pat in email for pat in src.senders):
            continue
        if src.subject_keywords and not any(_norm(k) in subj for k in src.subject_keywords):
            continue
        return src
    return None


# --- Deduction de la periode mensuelle "YYYY-MM" ---------------------------

_MONTHS = {
    # francais
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    # anglais
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def extract_period(subject: str, when=None):
    """Deduit le mois concerne au format "YYYY-MM".

    Ordre de priorite :
      1. periode numerique explicite dans le sujet : "2026-06", "06-2026", "06/2026" ;
      2. nom de mois (fr/en) + annee du sujet si presente, sinon annee de reception ;
      3. repli sur le mois de reception de l'email (`when`, datetime).
    Retourne None si rien n'est deductible.
    """
    subj = _norm(subject)

    # 1) periode numerique explicite (le separateur evite de confondre avec un
    #    numero client type "0060528998" ou un horodatage "20260719")
    m = re.search(r"\b(20\d{2})[-/](0[1-9]|1[0-2])\b", subj)          # YYYY-MM
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.search(r"\b(0[1-9]|1[0-2])[-/](20\d{2})\b", subj)          # MM-YYYY
    if m:
        return f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"

    # 2) nom de mois
    month = None
    for name, num in _MONTHS.items():
        if re.search(r"\b" + name + r"\b", subj):
            month = num
            break
    if month:
        ym = re.search(r"\b(20\d{2})\b", subj)
        year = int(ym.group(1)) if ym else (when.year if when is not None else None)
        if year:
            return f"{year:04d}-{month:02d}"

    # 3) repli : mois de reception
    if when is not None:
        return f"{when.year:04d}-{when.month:02d}"
    return None
