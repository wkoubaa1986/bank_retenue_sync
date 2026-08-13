"""Registre persistant des mouvements bancaires (DocType `BRS Bank Movement`).

POURQUOI CE MODULE EXISTE
-------------------------
`bank/movements.fetch_latest_movements()` ne rend QUE le dernier fichier exporte, et chaque nouvel
export REMPLACE le precedent (cf. `movements.refresh_movements`). Sans memoire, deux consequences :
  - « ce qui reste a identifier » se limite a la fenetre courante, donc n'est pas une reponse ;
  - tout arbitrage humain (« ce mouvement n'a pas a etre rapproche ») serait perdu au refresh suivant.

Le registre est cette memoire. Il est alimente par upsert idempotent et n'ecrase JAMAIS ce qu'un
humain a decide.

LA CLE N'EST PAS LA REFERENCE BANCAIRE
--------------------------------------
Constat sur donnees reelles : `FT23291KR3ZG` porte A LA FOIS le credit `ENC CHEQ TN NUM 90017253`
(2 094,000) ET son debit `COM ENC CHEQUE TN` (2,856). Une commission et l'operation qui l'a generee
partagent la meme reference. Dedupliquer par reference seule les confondrait — et ruinerait le
regroupement journalier des frais bancaires, qui doit justement les distinguer.

La cle est donc l'empreinte de (date, reference, debit, credit). Le LIBELLE en est volontairement
exclu : sa casse varie d'un export a l'autre ('Encaiss cheque preavise' vs 'ENCAISS CHEQUE
PREAVISE'), ce qui creerait des doublons fantomes a chaque re-import.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter

import frappe
from frappe.utils import flt, now_datetime

DOCTYPE = "BRS Bank Movement"

# Champs poses par la classification. Regroupes ici parce que `apply_classifications` doit pouvoir
# les remettre a zero quand un mouvement cesse d'etre reconnu (une regle retiree, par exemple).
CLASSIFICATION_FIELDS = (
    "categorie", "regle", "groupe", "statut", "raison", "document_type", "document_name",
    "montant_document", "ecart",
)

# Champs qu'un humain renseigne et que la machine ne touche jamais.
HUMAN_FIELDS = ("ignore_manuel", "ignore_motif", "note")


def movement_key(m: dict) -> str:
    """Empreinte stable d'un mouvement : sha1 tronque de (date, reference, debit, credit)."""
    d = m.get("date")
    payload = "%s|%s|%.3f|%.3f" % (
        d.isoformat() if hasattr(d, "isoformat") else (str(d or "")),
        (m.get("reference") or "").strip().upper(),
        flt(m.get("debit"), 3),
        flt(m.get("credit"), 3),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def assign_keys(movements: list) -> list:
    """Rend [(cle, mouvement), ...] en desambiguisant les lignes STRICTEMENT identiques.

    Un releve peut porter deux fois la meme operation le meme jour pour le meme montant et la meme
    reference (deux commissions identiques, par exemple). Elles sont alors suffixees `-2`, `-3`...
    dans l'ordre d'apparition — stable, l'export etant trie par date.
    """
    seen = Counter()
    out = []
    for m in movements:
        base = movement_key(m)
        seen[base] += 1
        n = seen[base]
        out.append((base if n == 1 else f"{base}-{n}", m))
    return out


def _row_values(cle: str, m: dict) -> dict:
    """Champs FACTUELS d'un mouvement (ce que la banque dit), hors classification et hors humain."""
    from bank_retenue_sync.bank.movements import _norm_op

    debit, credit = flt(m.get("debit"), 3), flt(m.get("credit"), 3)
    return {
        "cle": cle,
        "date": m.get("date"),
        "date_valeur": m.get("date_valeur"),
        "operation": (m.get("operation") or "")[:1000],
        "operation_norm": _norm_op(m.get("operation"))[:140],
        "reference": (m.get("reference") or "").strip(),
        "debit": debit,
        "credit": credit,
        "montant": debit or credit,
        "sens": "Debit" if debit else "Credit",
    }


def upsert_movements(movements: list, snapshot_run: str = None, origine: str = "export") -> dict:
    """Insere les mouvements inconnus, rafraichit la trace de vue des connus.

    IDEMPOTENT. Sur une ligne deja connue, seuls `dernier_import` et `snapshot_run` bougent :
    ni la classification ni les champs humains ne sont retouches. Rejouer le meme export deux fois
    de suite ne cree donc aucun doublon et ne perd aucune decision.

    Retourne {"crees", "revus", "cles"}.
    """
    pairs = assign_keys(movements)
    if not pairs:
        return {"crees": 0, "revus": 0, "cles": []}

    cles = [c for c, _ in pairs]
    connus = set()
    # Requetes par lots : la clause IN d'un export de 4 mois depasse vite le confort de MariaDB.
    for i in range(0, len(cles), 500):
        lot = cles[i:i + 500]
        connus.update(
            r[0] for r in frappe.db.get_all(
                DOCTYPE, filters={"cle": ["in", lot]}, fields=["cle"], as_list=True,
                limit_page_length=0)
        )

    now = now_datetime()
    crees = revus = 0
    for cle, m in pairs:
        if cle in connus:
            frappe.db.set_value(DOCTYPE, cle, {
                "dernier_import": now,
                "snapshot_run": snapshot_run,
            }, update_modified=False)
            revus += 1
            continue
        doc = frappe.new_doc(DOCTYPE)
        doc.update(_row_values(cle, m))
        doc.statut = "Orphelin"          # tant que la classification n'est pas passee
        doc.origine = origine
        doc.snapshot_run = snapshot_run
        doc.premiere_vue = now
        doc.dernier_import = now
        doc.raw_payload = json.dumps(m, default=str, ensure_ascii=False)
        doc.insert(ignore_permissions=True)
        crees += 1
    return {"crees": crees, "revus": revus, "cles": cles}


def apply_classifications(classifications: list, snapshot_run: str = None) -> dict:
    """Ecrit le resultat du moteur de classification sur les lignes du registre.

    L'ARBITRAGE HUMAIN PRIME : toute ligne portant `ignore_manuel` est sautee. C'est le point ou
    l'utilisateur perdrait confiance dans l'outil si la machine repassait derriere lui.

    `classifications` : objets ou dicts portant au moins `cle`, `categorie`, `statut`.
    Retourne {"maj", "ignores", "absents"}.
    """
    maj = ignores = absents = 0
    for c in classifications:
        c = c if isinstance(c, dict) else c.__dict__
        cle = c.get("cle")
        if not cle:
            continue
        row = frappe.db.get_value(DOCTYPE, cle, ["name", "ignore_manuel"], as_dict=True)
        if not row:
            absents += 1
            continue
        if row.ignore_manuel:
            ignores += 1
            continue
        values = {f: c.get(f) for f in CLASSIFICATION_FIELDS}
        if snapshot_run:
            values["snapshot_run"] = snapshot_run
        frappe.db.set_value(DOCTYPE, cle, values, update_modified=False)
        maj += 1
    return {"maj": maj, "ignores": ignores, "absents": absents}


def registry_as_movements(date_from=None, date_to=None, categorie=None, sens=None) -> list:
    """Rend les lignes du registre AU FORMAT DICT NU de `bank/movements.py`.

    C'est la brique qui rend tout le code existant (matching, especes, classify, engine) utilisable
    HORS LIGNE : sans tej-bank-service joignable, le registre devient la source de mouvements.
    Indispensable ici, ou le microservice tourne dans un autre devcontainer.
    """
    filters = {}
    if date_from:
        filters.setdefault("date", ["between", [date_from, date_to or "2999-12-31"]])
    elif date_to:
        filters["date"] = ["<=", date_to]
    if categorie:
        filters["categorie"] = categorie
    if sens:
        filters["sens"] = sens
    rows = frappe.db.get_all(
        DOCTYPE, filters=filters, limit_page_length=0, order_by="date asc, creation asc",
        fields=["date", "date_valeur", "operation", "reference", "debit", "credit"])
    return [
        {
            "date": r.date,
            "date_valeur": r.date_valeur,
            "operation": r.operation or "",
            "reference": r.reference or "",
            "debit": flt(r.debit, 3),
            "credit": flt(r.credit, 3),
        }
        for r in rows
    ]


def import_from_bank_transactions(bank_account: str = None) -> dict:
    """Recupere les mouvements saisis via le module bancaire natif d'ERPNext.

    Le DocType `Bank Transaction` contient un import de releve Zitouna realise en 2023 puis
    abandonne (toutes les lignes sont restees `Pending`, rien n'a ete rapproche). Ce sont les
    SEULES donnees bancaires reelles disponibles hors tej-bank-service : elles servent de jeu
    d'essai au moteur de classification.

    LECTURE SEULE sur `tabBank Transaction` : rien n'y est modifie, le Bank Reconciliation Tool
    natif reste intact.
    """
    filters = {"docstatus": ["<", 2]}
    if bank_account:
        filters["bank_account"] = bank_account
    rows = frappe.db.get_all(
        "Bank Transaction", filters=filters, limit_page_length=0, order_by="date asc",
        fields=["date", "description", "reference_number", "deposit", "withdrawal"])
    movements = [
        {
            "date": r.date,
            "date_valeur": r.date,
            "operation": r.description or "",
            "reference": (r.reference_number or "").strip(),
            "debit": flt(r.withdrawal, 3),
            "credit": flt(r.deposit, 3),
        }
        for r in rows
    ]
    return upsert_movements(movements, origine="bank_transaction")


def marquer_ignore(cles, motif: str = None, ignore: bool = True) -> int:
    """Arbitrage humain : ces mouvements n'ont pas a etre rapproches (ou le redeviennent)."""
    if isinstance(cles, str):
        cles = [cles]
    n = 0
    for cle in cles:
        if not frappe.db.exists(DOCTYPE, cle):
            continue
        frappe.db.set_value(DOCTYPE, cle, {
            "ignore_manuel": 1 if ignore else 0,
            "ignore_motif": motif if ignore else None,
            "statut": "Ignore" if ignore else "Orphelin",
        })
        n += 1
    return n
