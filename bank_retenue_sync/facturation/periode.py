"""Le mois, et rien d'autre : bornes, libelle, liste des mois offerts a l'ecran.

Toute la cloture mensuelle tourne autour d'une seule cle, `YYYY-MM`. La garder dans un module
a part evite qu'elle se reinvente dans chaque onglet — et evite surtout la divergence classique
entre un onglet qui borne au dernier jour du mois et un autre qui borne au premier du suivant.

Fonctions pures : ni frappe, ni base. Elles se testent directement.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin",
           "juillet", "août", "septembre", "octobre", "novembre", "décembre"]

_CLE = re.compile(r"^(\d{4})-(\d{2})$")


def normaliser(mois: str | None, aujourdhui: date | None = None) -> str:
    """« 2026-07 » -> « 2026-07 ». Vide ou illisible -> le mois PRECEDENT.

    ⚠️ LE DEFAUT EST LE MOIS PRECEDENT, PAS LE MOIS COURANT. On clot un mois quand il est fini :
    ouvrir la page le 3 du mois pour y trouver trois jours de facturation n'aurait aucun sens.
    C'est deja le choix de `get_invoices_pdf.py` et de la page Facturation Auto.
    """
    m = _CLE.match((mois or "").strip())
    if m:
        annee, numero = int(m.group(1)), int(m.group(2))
        if 1 <= numero <= 12:
            return "%04d-%02d" % (annee, numero)
    ref = aujourdhui or date.today()
    return cle(*precedent(ref.year, ref.month))


def cle(annee: int, numero: int) -> str:
    return "%04d-%02d" % (annee, numero)


def eclater(mois: str) -> tuple[int, int]:
    m = _CLE.match(mois)
    if not m:
        raise ValueError("mois illisible : %r" % (mois,))
    return int(m.group(1)), int(m.group(2))


def precedent(annee: int, numero: int) -> tuple[int, int]:
    return (annee - 1, 12) if numero == 1 else (annee, numero - 1)


def suivant(annee: int, numero: int) -> tuple[int, int]:
    return (annee + 1, 1) if numero == 12 else (annee, numero + 1)


def bornes(mois: str) -> tuple[str, str]:
    """-> (premier jour, dernier jour), en chaines ISO. Bornes INCLUSIVES des deux cotes."""
    annee, numero = eclater(mois)
    fin = calendar.monthrange(annee, numero)[1]
    return "%04d-%02d-01" % (annee, numero), "%04d-%02d-%02d" % (annee, numero, fin)


def libelle(mois: str) -> str:
    """« 2026-07 » -> « juillet 2026 »."""
    annee, numero = eclater(mois)
    return "%s %d" % (MOIS_FR[numero - 1], annee)


def courant(aujourdhui: date | None = None) -> str:
    """La cle du mois EN COURS (« 2026-10 » le 30/10/2026)."""
    ref = aujourdhui or date.today()
    return cle(ref.year, ref.month)


def est_en_cours(mois: str, aujourdhui: date | None = None) -> bool:
    """Le mois n'est pas fini : on peut le CONSULTER, pas le clore ni l'envoyer."""
    return normaliser(mois, aujourdhui) >= courant(aujourdhui)


def derniers(combien: int = 18, aujourdhui: date | None = None,
             avec_courant: bool = False) -> list[dict]:
    """Les N derniers mois clos, du plus recent au plus ancien. Pour le selecteur de l'ecran.

    `avec_courant` ajoute en tete le mois EN COURS, marque « (en cours) » (demande utilisateur
    30/10/2026 : voir les frais de LC du 29/10 sans attendre la fin du mois). Le defaut de
    `normaliser` reste le mois precedent, et la cloture refuse un mois en cours."""
    ref = aujourdhui or date.today()
    out = []
    if avec_courant:
        c = courant(ref)
        out.append({"cle": c, "libelle": libelle(c) + " (en cours)", "en_cours": True})
    annee, numero = precedent(ref.year, ref.month)
    for _ in range(max(1, combien)):
        out.append({"cle": cle(annee, numero), "libelle": libelle(cle(annee, numero))})
        annee, numero = precedent(annee, numero)
    return out
