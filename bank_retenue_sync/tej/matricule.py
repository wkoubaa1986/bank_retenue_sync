"""Normalisation du matricule fiscal tunisien. Aucune base, aucun reseau : pure et testable.

POURQUOI CE MODULE EXISTE
-------------------------
Le certificat TEJ porte le matricule du declarant sous sa forme compacte (`1298092B`), tandis que
`Customer.tax_id` est saisi a la main et arrive sous toutes les formes rencontrees en Tunisie :

    974450 / N / A / C / 000        1648235/B/A/M/000        1453894 Z
    925641 / W / A / M  / 000       1511801 C / A / M / 000  988239/K

Une egalite stricte echoue sur 100 % de ces cas. Normalises, ils se rejoignent — et le matricule
devient alors la SEULE cle EXACTE du flux : ni approximation, ni score, ni IA. Sur l'export reel,
elle identifie a elle seule 34 certificats 2026 sur 44.

LA LETTRE CLE N'EST PAS UN DETAIL
---------------------------------
Elle valide les chiffres (elle est calculee a partir d'eux) et distingue des societes qui, sans
elle, se confondraient. Un matricule sans lettre n'est donc jamais indexe : mieux vaut retomber
sur la raison sociale que rapprocher deux clients par une collision numerique.

... MAIS ELLE MANQUE PARFOIS COTE ERPNEXT
-----------------------------------------
« 1398789 / A / M / 000 » : le A est le code de categorie, la lettre cle (L) a saute a la saisie.
Impossible a distinguer d'un matricule normal — d'ou `chiffres()`, qui ne rend que le NUMERO du
contribuable. Ce numero ne sert jamais de cle a lui seul (voir ci-dessus), mais il permet deux
jugements plus faibles et tres utiles : deux matricules aux chiffres differents ne peuvent pas
designer le meme contribuable (`contredit`), et des chiffres identiques doublent d'une preuve
une raison sociale deja plausible (`meme_numero`).
"""
from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^0-9A-Z]")
_CLE = re.compile(r"^(\d{1,8})([A-Z])")
_CHIFFRES = re.compile(r"^(\d{1,8})")

# Suffixes de categorie (A/M/C/P/B/N/D + etablissement 000) : ils suivent la lettre cle et ne
# distinguent pas deux contribuables. On les ignore en s'arretant a la lettre cle.
LONGUEUR_CHIFFRES = 7


def normaliser(valeur) -> str:
    """« 974450 / N / A / C / 000 » -> « 0974450N ». None si aucune lettre cle lisible."""
    if valeur is None:
        return None
    compact = _NON_ALNUM.sub("", str(valeur).upper())
    m = _CLE.match(compact)
    if not m:
        return None
    chiffres, cle = m.group(1), m.group(2)
    if len(chiffres) > LONGUEUR_CHIFFRES + 1:
        return None                      # au-dela, ce n'est plus un matricule : on ne devine pas
    return chiffres.zfill(LONGUEUR_CHIFFRES) + cle


def chiffres(valeur) -> str:
    """« 1398789 / A / M / 000 » -> « 1398789 ». Le numero du contribuable, lettre cle exclue.

    Tolere l'absence de lettre — c'est tout l'interet : ce que `normaliser` refuse d'indexer reste
    ici comparable. A n'utiliser que pour les jugements ci-dessous, jamais comme cle.
    """
    if valeur is None:
        return None
    m = _CHIFFRES.match(_NON_ALNUM.sub("", str(valeur).upper()))
    if not m or len(m.group(1)) > LONGUEUR_CHIFFRES + 1:
        return None
    return m.group(1).zfill(LONGUEUR_CHIFFRES)


def contredit(a, b) -> bool:
    """Ces deux matricules designent-ils a coup sur des contribuables DIFFERENTS ?

    Ne pas connaitre l'un des deux n'est pas une contradiction : seul un numero different en est
    une. Sert a ecarter du rapprochement les clients que le matricule disqualifie, sans jamais
    prendre la decision inverse.
    """
    ca, cb = chiffres(a), chiffres(b)
    return bool(ca) and bool(cb) and ca != cb


def meme_numero(a, b) -> bool:
    """Meme numero de contribuable, quelle que soit la lettre cle (presente, absente ou fausse)."""
    ca, cb = chiffres(a), chiffres(b)
    return bool(ca) and ca == cb


def index(paires) -> dict:
    """[(matricule, valeur)] -> {matricule normalise: [valeur, ...]}.

    La valeur est une LISTE, jamais un scalaire : deux fiches client peuvent porter le meme
    matricule (doublons de saisie). Ecraser silencieusement l'une des deux rattacherait des
    certificats a un client arbitraire — l'ambiguite doit remonter, pas disparaitre.
    """
    out = {}
    for brut, valeur in paires:
        cle = normaliser(brut)
        if cle:
            out.setdefault(cle, []).append(valeur)
    return out


def index_par_numero(paires) -> dict:
    """[(matricule, valeur)] -> {numero du contribuable: [valeur, ...]}, lettre cle exclue.

    ⚠️ CE N'EST PAS UNE CLE, c'est un VIVIER. Deux fiches peuvent y tomber ensemble sans designer
    le meme contribuable (chiffres saisis de travers). Il sert a restreindre les candidats du fuzzy
    la ou `index` ne rend rien — et a trancher seulement quand une seule fiche s'y trouve, sous
    reserve de revue humaine (cf. `rapprochement.identifier_client`).
    """
    out = {}
    for brut, valeur in paires:
        num = chiffres(brut)
        if num:
            out.setdefault(num, []).append(valeur)
    return out


def meme_contribuable(a, b) -> bool:
    ka, kb = normaliser(a), normaliser(b)
    return bool(ka) and ka == kb
