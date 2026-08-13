"""Regles de date de comptabilisation des depenses mensuelles.

Regle metier : une facture mensuelle recue en debut de mois suivant est
comptabilisee au DERNIER JOUR de la periode qu'elle couvre
(ex. facture recue le 05-07-2026 pour juin -> ecriture datee du 30-06-2026).

On s'appuie sur la periode deja deduite par `mail.classifier.extract_period`
(format "YYYY-MM"), plus fiable qu'un simple "mois de reception - 1".
"""
from __future__ import annotations

import calendar
from datetime import date


def period_end_date(period: str):
    """'2026-06' -> date(2026, 6, 30). Retourne None si period est vide/invalide."""
    if not period:
        return None
    try:
        y, m = period.split("-")
        y, m = int(y), int(m)
        if not (1 <= m <= 12):
            return None
        return date(y, m, calendar.monthrange(y, m)[1])
    except (ValueError, AttributeError):
        return None
