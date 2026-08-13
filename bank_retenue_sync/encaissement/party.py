"""Identification du CLIENT a partir du libelle d'un virement bancaire.

Le libelle bancaire n'est pas le nom ERPNext, et il est TRONQUE par la banque a ~30 caracteres :

    'VIR TN AUTRE BQ TAURUS SARL'        -> Customer 'STE TAURUS'
    'VIR TN AUTRE BQ AMBASSADE DE L'     -> Customer "Ambassade de l'Inde"      (tronque)
    'VIR TN AUTRE BQ BUSINESS HOTEL M'   -> 'BUSINESS HOTEL MANAGEMENT TUNIS - BHM'
    'VIR TN AUTRE BQ S O C O B A T'      -> 'SOCOBAT'                           (lettres eclatees)

Trois niveaux, du plus sur au plus couteux (cf. `identify`) :
  1. ALIAS APPRIS : libelle deja rencontre et tranche par un humain -> reponse certaine, gratuite.
  2. FUZZY : normalisation + score (prefixe / tokens / ratio). Couvre la majorite des cas.
  3. OpenAI (`ai.party_match`) : uniquement ce que 1 et 2 n'ont pas tranche.

Les alias ne sont stockes NULLE PART : ils sont RECONSTRUITS a chaque run depuis l'historique
des Payment Entry (cf. `learned_aliases`). C'est volontaire — l'app n'est pas installee sur le
site, donc pas de DocType disponible — et suffisant : le server script d'encaissement ecrit
`reference_no = '<reference bancaire> - <banque>'` sur la PE qu'il cree, donc chaque appariement
soumis redevient un alias au run suivant.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

import frappe

# Mots du libelle bancaire qui ne portent aucune information d'identite.
_BANK_NOISE = {
    "VIR", "VIREMENT", "VIRT", "TN", "MM", "AUTRE", "BQ", "BANQUE", "RECU", "RECEPTION",
    "EN", "FAVEUR", "DE", "DU", "DES", "LA", "LE", "LES", "ET", "AU", "AUX", "PAR", "POUR",
}
# Formes juridiques et civilites : presentes d'un cote, absentes de l'autre, elles faussent le score.
_LEGAL_NOISE = {
    "STE", "SOCIETE", "SOC", "SARL", "SUARL", "SA", "SAS", "SPA", "ETS", "ETABLISSEMENT",
    "MR", "M", "MME", "MLLE", "MONSIEUR", "MADAME", "DR", "SPH", "GROUPE", "GROUP",
}
_NOISE = _BANK_NOISE | _LEGAL_NOISE

_SEP_RE = re.compile(r"[^A-Za-z0-9]+")


def _strip_accents(s: str) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()


def _reglue_isolated(tokens: list) -> list:
    """Recolle les suites de lettres isolees : ['S','O','C','O','B','A','T'] -> ['SOCOBAT'].

    Certains libelles bancaires arrivent lettre par lettre. Sans recollage, chaque lettre est un
    token d'un caractere : le score par tokens devient du bruit et le prefixe ne peut plus matcher.
    Seuil a 3 lettres consecutives pour ne pas souder des initiales legitimes ('A & S')."""
    out, run = [], []
    for t in tokens:
        if len(t) == 1 and t.isalpha():
            run.append(t)
            continue
        if len(run) >= 3:
            out.append("".join(run))
        else:
            out.extend(run)
        run = []
        out.append(t)
    if len(run) >= 3:
        out.append("".join(run))
    else:
        out.extend(run)
    return out


def normalize(s: str, drop_noise: bool = True) -> str:
    """Forme comparable : sans accents, majuscules, ponctuation -> espaces, lettres eclatees
    recollees, mots vides retires."""
    tokens = [t for t in _SEP_RE.split(_strip_accents(s).upper()) if t]
    tokens = _reglue_isolated(tokens)
    if drop_noise:
        tokens = [t for t in tokens if t not in _NOISE]
    return " ".join(tokens)


def score(label: str, name: str) -> float:
    """Ressemblance libelle bancaire <-> nom de client, dans [0, 1].

    Le PREFIXE domine : la banque tronque le libelle a longueur fixe, donc le libelle normalise
    est tres souvent un prefixe strict du nom reel ('BUSINESS HOTEL M' / 'BUSINESS HOTEL
    MANAGEMENT'). Longueur minimale de 6 caracteres, sinon un libelle court ('DMT') matcherait
    n'importe quel nom commencant par les memes lettres."""
    a, b = normalize(label), normalize(name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= 6 and (b.startswith(a) or a.startswith(b)):
        return 0.95
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    # Un token entier commun et distinctif ('TAURUS', 'KEMVET') vaut mieux que le ratio global,
    # qui s'effondre des que l'un des deux noms porte des mots en plus.
    contain = max((len(t) for t in (ta & tb) if len(t) >= 4), default=0)
    contain = min(0.9, 0.6 + 0.05 * contain) if contain else 0.0
    return max(jaccard, contain, SequenceMatcher(None, a, b).ratio())


def learned_aliases(movements: list) -> dict:
    """{libelle normalise -> nom du Customer}, reconstruit depuis l'historique.

    Un mouvement dont la reference bancaire se retrouve dans le `reference_no` d'une Payment Entry
    soumise a DEJA ete rattache a un client par un humain : c'est une correspondance verifiee.
    On ne garde que les libelles sans ambiguite (un seul client observe).

    A n'alimenter qu'avec les mouvements du flux concerne (credits de virement) : un libelle
    'ENCAISSEMENT EFFET <n°>' est lui aussi rattache a un client, mais il ne reapparaitra jamais
    comme libelle de virement — l'indexer ne ferait que gonfler la table."""
    refs = {m["reference"]: m for m in movements or [] if m.get("reference")}
    if not refs:
        return {}
    rows = frappe.get_all(
        "Payment Entry",
        filters={"docstatus": 1, "party_type": "Customer", "reference_no": ["is", "set"]},
        fields=["party", "reference_no"],
    )
    by_label: dict = {}
    for r in rows:
        ref_no = (r["reference_no"] or "").strip()
        for ref, m in refs.items():
            if ref and ref in ref_no:
                by_label.setdefault(normalize(m["operation"]), set()).add(r["party"])
    return {label: next(iter(parties)) for label, parties in by_label.items() if len(parties) == 1}


def manual_aliases() -> dict:
    """Alias forces a la main, lus dans `site_config` (`brs_bank_aliases`) :
    {'<libelle ou fragment>': '<nom exact du Customer>'}. Sert aux cas que ni l'historique ni
    l'IA ne tranchent (client enregistre sous un nom sans rapport avec son compte bancaire)."""
    raw = frappe.conf.get("brs_bank_aliases") or {}
    return {normalize(k): v for k, v in raw.items() if k and v}


# Au-dessus : appariement retenu sans appel IA. En dessous : on demande a OpenAI.
AUTO_THRESHOLD = 0.80
# Ecart minimal avec le second candidat, sinon le choix n'est pas tranche (deux clients proches).
MARGIN = 0.08


def identify(operation: str, candidates: list, aliases: dict = None, ai_resolver=None) -> dict:
    """Identifie le client d'un virement.

    `candidates` : noms de Customer plausibles (ceux qui ont une dette en attente).
    `aliases`    : {libelle normalise -> Customer} (learned + manual), consulte en premier.
    `ai_resolver(operation, candidates) -> nom|None` : appele UNIQUEMENT si le fuzzy n'a pas
                  tranche. Injectable pour les tests, et pour pouvoir couper l'IA.

    Retourne {'customer', 'score', 'method', 'raison'} — `customer` a None si non tranche."""
    label = normalize(operation)
    aliases = aliases or {}
    if label in aliases and aliases[label] in candidates:
        return {"customer": aliases[label], "score": 1.0, "method": "alias", "raison": ""}

    ranked = sorted(((score(operation, c), c) for c in candidates), key=lambda x: -x[0])
    best = ranked[0] if ranked else (0.0, None)
    second = ranked[1][0] if len(ranked) > 1 else 0.0

    if best[0] >= AUTO_THRESHOLD and (best[0] - second) >= MARGIN:
        return {"customer": best[1], "score": round(best[0], 3), "method": "fuzzy", "raison": ""}

    ambigu = best[0] >= AUTO_THRESHOLD and (best[0] - second) < MARGIN
    if ai_resolver:
        # Restreindre la liste soumise a l'IA aux candidats un minimum plausibles garde le prompt
        # court ET borne le risque : le resolveur ne peut choisir que dans ce qu'on lui donne.
        shortlist = [c for s, c in ranked if s >= 0.25][:25] or [c for _, c in ranked[:25]]
        chosen = ai_resolver(operation, shortlist)
        if chosen and chosen in candidates:
            return {"customer": chosen, "score": round(best[0], 3), "method": "ai", "raison": ""}

    return {
        "customer": None,
        "score": round(best[0], 3),
        "method": "aucun",
        "raison": ("deux clients aussi proches (%s / %s)" % (ranked[0][1], ranked[1][1]))
        if ambigu and len(ranked) > 1
        else "aucun client suffisamment proche du libelle",
    }
