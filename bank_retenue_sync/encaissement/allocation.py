"""Repartition d'un virement recu sur les dettes en attente d'un client.

Un virement client peut solder une dette, plusieurs d'un coup, ou une seule partiellement. Cette
etape est PUREMENT DETERMINISTE (aucun appel IA) : l'identite du payeur est une question de
langage, la repartition au millime est une question d'arithmetique.

Ordre de certitude decroissante (`allocate`) :
  1. exact    : une dette egale le montant recu
  2. subset   : un sous-ensemble de dettes dont la somme egale le montant recu
  3. partiel  : montant recu < total du -> imputation FIFO (dette la plus ancienne d'abord)
  4. excedent : montant recu > total du -> RIEN (diagnostic) : encaisser plus que le du signifie
                qu'une commande manque en base, pas qu'il faut repartir le surplus au hasard.

TOLERANCE (`tolerance`) : les frais de virement rognent le credit de ~1 DT (cas reel : 5 322,240
recus pour 5 323,230 dus). Dans la tolerance, la dette est consideree SOLDEE et on alloue son
montant PLEIN, pas le montant recu. C'est ce qui evite un residu de 0,99 DT tra1nant sur le
Sales Order, et c'est aussi ce que fait la saisie manuelle aujourd'hui (la PE porte le montant
comptable, pas le montant bancaire). L'ecart est remonte en diagnostic, jamais absorbe en silence.
"""
from __future__ import annotations

from itertools import combinations

# Plafond du nombre de combinaisons explorees : au-dela on renonce au subset exact plutot que de
# faire trainer un run quotidien. 2^18 = 262 144, largement au-dessus des volumes reels (le client
# le plus charge a une dizaine de dettes ouvertes).
_MAX_SUBSET_ITEMS = 18


def tolerance(amount: float) -> float:
    """Ecart admis entre le credit bancaire et le montant du.

    Plancher a 1 DT (frais de virement forfaitaires), puis 0,5 % pour les gros montants, mais
    PLAFONNE a 10 DT : sans plafond, 0,5 % d'un virement de 30 000 DT pardonnerait un impaye de
    150 DT, qui est un vrai paiement partiel et non un frais."""
    return min(max(1.0, 0.005 * abs(amount)), 10.0)


def _sum(dettes) -> float:
    return round(sum(d.get("paid_amount") or 0.0 for d in dettes), 3)


def allocate(amount: float, dettes: list) -> dict:
    """Repartit `amount` (credit bancaire) sur `dettes` (PE en attente sur 'Dettes - A&S',
    triees par anciennete). Chaque dette est un dict {name, paid_amount, reference_no, ...}.

    Retourne {'mode', 'lignes': [{'dette', 'part'}], 'total_alloue', 'ecart', 'raison'} —
    `lignes` vide si rien n'a pu etre tranche."""
    amount = round(float(amount or 0.0), 3)
    dettes = [d for d in (dettes or []) if (d.get("paid_amount") or 0) > 0]
    tol = tolerance(amount)
    if not dettes:
        return _none("aucune dette en attente pour ce client")

    total = _sum(dettes)

    # 1. une seule dette egale le montant : le cas courant, et le seul jamais ambigu.
    exacts = [d for d in dettes if abs(d["paid_amount"] - amount) <= tol]
    if len(exacts) == 1:
        d = exacts[0]
        return _ok("exact", [(d, d["paid_amount"])], amount)
    if len(exacts) > 1:
        return _none("plusieurs dettes du meme montant (%s) : lot non tranche"
                     % ", ".join(d["reference_no"] or d["name"] for d in exacts[:4]))

    # 2. sous-ensemble dont la somme fait le compte. On explore par taille croissante : un
    #    virement qui solde 2 factures est bien plus probable qu'une combinaison de 7 qui tombe
    #    juste par hasard. Deux solutions de meme taille = ambigu -> on ne tranche pas.
    if amount <= total + tol and len(dettes) <= _MAX_SUBSET_ITEMS:
        for taille in range(2, len(dettes) + 1):
            trouves = [c for c in combinations(dettes, taille)
                       if abs(_sum(c) - amount) <= tol]
            if len(trouves) == 1:
                combo = trouves[0]
                return _ok("groupe", [(d, d["paid_amount"]) for d in combo], amount)
            if len(trouves) > 1:
                return _none("%d combinaisons de %d dettes donnent %.3f : lot non tranche"
                             % (len(trouves), taille, amount))

    # 3. paiement partiel : imputation FIFO, la plus ancienne d'abord. Le server script fabrique
    #    tout seul la ligne 'Dette non payee' residuelle a partir de la part allouee.
    if amount < total - tol:
        lignes, reste = [], amount
        for d in dettes:
            if reste <= tol:
                break
            part = min(reste, d["paid_amount"])
            lignes.append((d, round(part, 3)))
            reste = round(reste - part, 3)
        if lignes:
            return _ok("partiel", lignes, amount)

    # 4. plus recu que du : ne rien ecrire.
    return _none("montant recu (%.3f) superieur au total des dettes en attente (%.3f)"
                 % (amount, total))


def _ok(mode: str, paires: list, amount: float) -> dict:
    total_alloue = round(sum(p for _, p in paires), 3)
    return {
        "mode": mode,
        "lignes": [{"dette": d, "part": round(p, 3)} for d, p in paires],
        "total_alloue": total_alloue,
        # ecart > 0 : la banque a credite MOINS que le montant comptable (frais de virement).
        "ecart": round(total_alloue - amount, 3),
        "raison": "",
    }


def _none(raison: str) -> dict:
    return {"mode": "aucun", "lignes": [], "total_alloue": 0.0, "ecart": 0.0, "raison": raison}
