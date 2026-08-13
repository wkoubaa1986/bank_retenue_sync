"""Versements d'especes en banque : caisse 'Sabah Espèces - A&S' -> compte Zitouna.

Un credit bancaire 'VERSEMENT ESPECES ... AGENCE <x>' correspond a un depot de la recette en
liquide. Il ne se traite PAS comme un encaissement client (aucun compte d'attente, aucune Payment
Entry) : c'est un simple virement interne, saisi en ECRITURE DE JOURNAL
`Dr STE430127B - Zitouna / Cr Sabah Espèces`.

Trois issues possibles par mouvement (cf. `match_versements`) :
  - `reference`  : une ecriture correspond ET porte deja la reference bancaire -> rien a faire.
  - `annotation` : une ecriture correspond mais SANS reference -> on ajoute 'Ref : <reference>'.
  - `creation`   : aucune ecriture -> on en cree une en BROUILLON.
Plus deux cas ou l'on ne touche a rien : plusieurs ecritures candidates (ambigu), ou reference
deja portee par une Payment Entry (le depot etait en fait un reglement client en especes, cf.
'VERSEMENT ESPECES AGENCE DENDEN' 175 DT = ACC-PAY-2026-03787).

/!\ `user_remark` n'est pas `allow_on_submit` : annoter une ecriture DEJA SOUMISE passe par une
ecriture directe en base. C'est volontairement limite au LIBELLE — jamais un montant, un compte
ni une date — et en AJOUT, jamais en ecrasement. `annotate=False` desactive completement ce
comportement et se contente de signaler.
"""
from __future__ import annotations

import re
from datetime import timedelta

import frappe

from bank_retenue_sync.encaissement.pending import BANK_ACCOUNT, COMPANY

CASH_ACCOUNT = "Sabah Espèces - A&S"
COST_CENTER = "Principal - A&S"
# La date de valeur bancaire peut decaler d'un jour ou deux la saisie comptable.
_FENETRE_JOURS = 3


def is_versement_espece(m: dict) -> bool:
    """Credit bancaire correspondant a un depot d'especes."""
    if not m.get("credit"):
        return False
    op = (m.get("operation") or "").upper()
    return "VERSEMENT" in op and ("ESPECE" in op or "ESPÈCE" in op)


def _reference_presente(je: dict, reference: str) -> bool:
    """La reference bancaire figure-t-elle deja dans le libelle de l'ecriture ?
    On regarde `user_remark` ET `cheque_no` : la convention observee est 'Ref : TT26195DXDH5'
    dans le remark, mais rien n'interdit de l'avoir mise dans le numero de reference."""
    champs = " ".join(str(je.get(f) or "") for f in ("user_remark", "remark", "cheque_no"))
    return bool(reference) and reference.upper() in champs.upper()


def find_journal_entries(date, montant: float, fenetre: int = _FENETRE_JOURS) -> list:
    """Ecritures de journal (brouillon ou soumises) qui debitent la banque et creditent la caisse
    du meme montant, a `fenetre` jours pres. Le couple de comptes ET le montant doivent coller :
    un simple rapprochement par montant confondrait deux depots du meme jour."""
    if not date:
        return []
    rows = frappe.db.sql(
        """
        select je.name, je.posting_date, je.docstatus, je.cheque_no, je.user_remark, je.remark
        from `tabJournal Entry` je
        join `tabJournal Entry Account` banque
             on banque.parent = je.name and banque.account = %(banque)s and banque.debit > 0
        join `tabJournal Entry Account` caisse
             on caisse.parent = je.name and caisse.account = %(caisse)s and caisse.credit > 0
        where je.docstatus != 2
          and je.company = %(company)s
          and je.posting_date between %(debut)s and %(fin)s
          and abs(banque.debit - %(montant)s) < 0.005
          and abs(caisse.credit - %(montant)s) < 0.005
        """,
        {"banque": BANK_ACCOUNT, "caisse": CASH_ACCOUNT, "company": COMPANY,
         "debut": date - timedelta(days=fenetre), "fin": date + timedelta(days=fenetre),
         "montant": montant},
        as_dict=1,
    )
    return rows


def match_versements(movements, booked=None, je_finder=None) -> tuple:
    """Confronte les depots d'especes du releve aux ecritures de journal existantes.

    `booked` : references bancaires deja portees par une Payment Entry (cf.
               pending.bank_refs_already_booked) -> le depot etait un reglement client, pas un
               versement de caisse.
    `je_finder(date, montant) -> list` injectable pour les tests.

    Retourne (actions, diagnostics)."""
    je_finder = je_finder or find_journal_entries
    booked = booked or set()
    actions, diag = [], []

    for m in movements or []:
        if not is_versement_espece(m):
            continue
        ref = (m.get("reference") or "").strip()
        montant = round(m.get("credit") or 0.0, 3)

        if ref and ref in booked:
            diag.append({"type": "espece", "ref": ref, "credit": montant,
                         "operation": m.get("operation"),
                         "reason": "reglement client en especes (Payment Entry existante), "
                                   "pas un versement de caisse"})
            continue

        candidats = je_finder(m.get("date"), montant)
        if len(candidats) > 1:
            diag.append({"type": "espece", "ref": ref, "credit": montant,
                         "ecritures": [c["name"] for c in candidats],
                         "reason": "plusieurs ecritures caisse->banque du meme montant : "
                                   "annotation non tranchee"})
            continue

        if not candidats:
            actions.append({"kind": "creation", "reference": ref, "montant": montant,
                            "date": m.get("date"), "operation": m.get("operation")})
            continue

        je = candidats[0]
        if _reference_presente(je, ref):
            actions.append({"kind": "reference", "journal_entry": je["name"], "reference": ref,
                            "montant": montant, "date": m.get("date")})
        else:
            actions.append({"kind": "annotation", "journal_entry": je["name"], "reference": ref,
                            "montant": montant, "date": m.get("date"),
                            "docstatus": je["docstatus"],
                            "user_remark": je.get("user_remark") or ""})
    return actions, diag


def _libelle(operation: str) -> str:
    """'VERSEMENT ESPECES RECETTE AGENCE AOUINA' -> 'Versement espèce - Recette AGENCE AOUINA'.
    Reprend la forme des saisies manuelles ('Versement espèce -Recette')."""
    reste = re.sub(r"^\s*VERSEMENT\s+ESP[EÈ]CES?\s*", "", (operation or "").upper()).strip()
    return ("Versement espèce - " + reste.title()) if reste else "Versement espèce"


def build_journal_entry(action: dict, insert: bool = True):
    """Ecriture de journal BROUILLON `Dr banque / Cr caisse` pour un depot non saisi."""
    doc = frappe.new_doc("Journal Entry")
    doc.voucher_type = "Journal Entry"
    doc.company = COMPANY
    doc.posting_date = action["date"]
    doc.cheque_no = _libelle(action.get("operation"))
    doc.cheque_date = action["date"]
    doc.user_remark = "%s\nRef : %s" % (_libelle(action.get("operation")), action["reference"])
    doc.append("accounts", {"account": BANK_ACCOUNT, "debit_in_account_currency": action["montant"],
                            "cost_center": COST_CENTER})
    doc.append("accounts", {"account": CASH_ACCOUNT, "credit_in_account_currency": action["montant"],
                            "cost_center": COST_CENTER})
    if insert:
        doc.insert(ignore_permissions=True)      # BROUILLON : jamais submit
    return doc


def annotate_journal_entry(action: dict) -> bool:
    """Ajoute 'Ref : <reference bancaire>' au libelle d'une ecriture existante.

    Sur une ecriture SOUMISE, `user_remark` n'est pas modifiable par la voie normale : on ecrit
    donc en base (`db_set`), en AJOUTANT une ligne au texte existant. Le champ `remark` (celui
    qu'ERPNext recalcule a la validation et qui s'affiche) est mis a jour de la meme facon, sinon
    l'ecran continuerait d'afficher l'ancien libelle. Aucun montant, compte ni date n'est touche."""
    doc = frappe.get_doc("Journal Entry", action["journal_entry"])
    ajout = "Ref : %s" % action["reference"]
    if ajout.upper() in (doc.user_remark or "").upper():
        return False
    nouveau = ((doc.user_remark or "").rstrip() + "\n" + ajout).strip()
    if doc.docstatus == 0:
        doc.user_remark = nouveau
        doc.save(ignore_permissions=True)
        return True
    doc.db_set("user_remark", nouveau, update_modified=False)
    if doc.remark:
        doc.db_set("remark", (doc.remark.rstrip() + "\n" + ajout).strip(), update_modified=False)
    return True


def apply_actions(actions: list, insert: bool = True, annotate: bool = True) -> dict:
    """Execute les actions de `match_versements`. `annotate=False` laisse les ecritures
    existantes intactes (mode constat)."""
    out = {"reference": 0, "annotation": 0, "creation": 0, "ecritures": [], "erreurs": []}
    for a in actions:
        try:
            if a["kind"] == "reference":
                out["reference"] += 1
            elif a["kind"] == "annotation" and annotate:
                if annotate_journal_entry(a):
                    out["annotation"] += 1
                    out["ecritures"].append(a["journal_entry"])
            elif a["kind"] == "creation" and insert:
                doc = build_journal_entry(a, insert=True)
                out["creation"] += 1
                out["ecritures"].append(doc.name)
        except Exception as e:
            out["erreurs"].append({"action": a["kind"],
                                   "ref": a.get("reference"), "erreur": str(e)[:160]})
    return out
