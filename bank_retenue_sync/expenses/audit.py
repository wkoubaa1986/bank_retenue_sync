"""Verification des depenses : l'audit INVERSE.

`encaissement/audit.py` verifie que ce qui a ete RECU est bien saisi. Rien ne faisait l'inverse
pour les charges : « ce debit bancaire n'a AUCUNE ecriture en face ». C'est pourtant la question
qui compte pour une sortie d'argent — un encaissement oublie se voit tot ou tard sur le solde
client, une depense oubliee ne se voit nulle part.

NE CREE ET NE MODIFIE RIEN. Comme son modele, ce module constate et nomme.
"""
from __future__ import annotations

from frappe.utils import flt

from bank_retenue_sync.bank import classify as C, rules as R
from bank_retenue_sync.expenses import engine, fees, lookup

# Categories de mouvements dont l'absence d'ecriture n'a rien d'anormal : elles sont soit
# comptabilisees par un autre chemin, soit hors perimetre assume.
_HORS_AUDIT = {"encaissement", "caisse"}


def audit_depenses(movements: list, context=None, je_finder=None, rows=None) -> dict:
    """Confronte chaque DEBIT bancaire aux ecritures ERPNext.

    Retourne un dict de listes, une par categorie d'anomalie :

      debit_sans_ecriture  : debit classe, aucune ecriture creditant la banque en face
      debit_non_classe     : aucune regle bancaire ne reconnait le libelle (la regle manque)
      regle_ambigue        : plusieurs lignes de depense recurrente revendiquent le mouvement
      montant_errone       : une ecriture correspond, mais pas pour le meme montant
      doublon_ecriture     : plusieurs ecritures candidates pour un meme debit
      frais_a_regrouper    : frais bancaires en attente d'agregat journalier
    """
    je_finder = je_finder or lookup.find_journal_entries_by_bank_line
    context = context if context is not None else C.build_context(movements)
    rows = rows if rows is not None else engine.load_rules()

    out = {k: [] for k in ("debit_sans_ecriture", "debit_non_classe", "regle_ambigue",
                           "montant_errone", "doublon_ecriture", "frais_a_regrouper")}

    for m in movements or []:
        debit = flt(m.get("debit"), 3)
        if not debit:
            continue
        base = {"date": m.get("date"), "operation": m.get("operation"),
                "reference": (m.get("reference") or "").strip(), "debit": debit}

        rule = R.find_rule(m)
        if rule is None:
            out["debit_non_classe"].append(
                dict(base, raison="libelle inconnu : aucune regle ne le reconnait"))
            continue
        if rule.categorie in _HORS_AUDIT:
            continue

        # Les frais bancaires ne se comptabilisent jamais a l'unite : leur controle porte sur le
        # groupe du jour, pas sur la ligne.
        if rule.action == R.ACTION_AGREGAT:
            out["frais_a_regrouper"].append(dict(base, categorie=rule.categorie,
                                                 sous_categorie=rule.sous_categorie))
            continue

        concurrentes = engine.find_rules_for(m, rows)
        if len(concurrentes) > 1:
            out["regle_ambigue"].append(
                dict(base, regles=[r.get("cle") for r in concurrentes]))
            continue

        # Une ecriture citant la reference bancaire vaut preuve de saisie.
        ref = base["reference"].upper()
        if ref and (context.je_par_reference or {}).get(ref):
            continue

        candidats = je_finder(m.get("date"), debit, sens="credit") or []
        if len(candidats) > 1:
            out["doublon_ecriture"].append(
                dict(base, ecritures=[c["name"] for c in candidats][:5]))
            continue
        if candidats:
            continue

        # Rien du meme montant : y a-t-il une ecriture proche, du meme jour, d'un montant voisin ?
        # Un ecart signale plus surement une erreur de saisie qu'une absence.
        proches = [c for c in (je_finder(m.get("date"), debit, sens="credit", fenetre=1) or [])]
        if proches:
            out["montant_errone"].append(
                dict(base, ecritures=[c["name"] for c in proches][:5]))
            continue

        out["debit_sans_ecriture"].append(
            dict(base, categorie=rule.categorie, regle=rule.key,
                 creable=bool(concurrentes),
                 raison=("regle de depense parametree : l'ecriture peut etre creee"
                         if concurrentes else "aucune ecriture en face")))

    return out


def resume(rapport: dict) -> dict:
    """Compteurs et montants par categorie d'anomalie."""
    return {
        k: {"nb": len(v), "montant": round(sum(flt(x.get("debit")) for x in v), 3)}
        for k, v in rapport.items()
    }
