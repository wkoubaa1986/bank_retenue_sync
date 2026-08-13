"""Detection des FAUSSES / ABSENTES saisies (controle qualite).

Le detail RECU (Payment Advice Aramex, bordereau de remise cheque) est la verite terrain : on le
confronte, ligne par ligne, aux Payment Entry ERPNext. Non destructif -> produit un rapport
d'anomalies (ne cree/modifie rien).

Categories d'anomalie :
  - 'recu_non_saisi'  : une livraison/cheque encaisse(e) en banque mais SANS PE en ERPNext
                        (argent recu, non enregistre) -> le cas typique de fausse saisie.
  - 'montant_errone'  : PE trouvee mais montant != detail recu.
  - 'credit_sans_advice' : virement Aramex detecte en banque sans email advice correspondant.

Rapprochement par n° de suivi Aramex (== reference_no 'Aramex N: <suivi>') ou n° de cheque
(== reference_no '<n°>-<banque>'), meme cle que le rapprochement.
"""
from __future__ import annotations

import frappe

from bank_retenue_sync.bank import movements as mv
from bank_retenue_sync.encaissement.matching import _load_remise, _norm_num

COMPANY = "Aquaworld & Servicing"
TOL = 0.01


def _find_pe_by_number(number: str) -> list:
    """PE soumises (tout compte) dont reference_no contient `number`."""
    if not number:
        return []
    return frappe.get_all(
        "Payment Entry",
        filters={"reference_no": ["like", f"%{number}%"], "company": COMPANY, "docstatus": 1},
        fields=["name", "paid_to", "paid_amount", "party", "mode_of_payment", "reference_no"],
    )


def audit_aramex(movements, advices, pe_finder=None) -> list:
    """Confronte chaque virement Aramex detecte (credit 'ARAMEX' == net d'un advice) au detail de
    l'advice. `pe_finder(number)->list` injectable pour les tests. Retourne la liste d'anomalies."""
    pe_finder = pe_finder or _find_pe_by_number
    anomalies = []
    adv_by_net = {}
    for adv in advices or []:
        adv_by_net.setdefault(round(adv.net_total, 3), []).append(adv)

    for m in movements or []:
        if not m.get("credit") or "ARAMEX" not in (m.get("operation") or "").upper():
            continue
        amt = round(m["credit"], 3)
        cands = adv_by_net.get(amt)
        if not cands:
            anomalies.append({"flux": "aramex", "categorie": "credit_sans_advice",
                              "credit": amt, "ref_bancaire": m.get("reference"),
                              "detail": "virement Aramex detecte mais aucun email advice au meme montant"})
            continue
        for l in cands[0].lines:
            ref = (getattr(l, "reference", "") or "").strip()
            # La PE enregistre le montant BRUT paye par le client (invoice_amount). Aramex reverse
            # net = brut - retenue a la source ; comparer la PE au net serait faux (la RS n'est
            # pas une erreur). On compare donc au BRUT.
            gross = round(getattr(l, "invoice_amount", 0) or 0, 3)
            rs = round(getattr(l, "withholding_tax", 0) or 0, 3)
            # lignes d'ajustement (TRFREVENUE..., ref non numerique ou brut <= 0) -> ignorees
            if not ref or _norm_num(ref) == "0" or gross <= 0:
                continue
            pes = pe_finder(ref)
            if not pes:
                anomalies.append({"flux": "aramex", "categorie": "recu_non_saisi",
                                  "suivi": ref, "montant": gross, "ref_bancaire": m.get("reference"),
                                  "detail": "livraison payee par Aramex mais AUCUNE PE en ERPNext"})
                continue
            pe = pes[0]
            if abs((pe["paid_amount"] or 0) - gross) >= TOL:
                anomalies.append({"flux": "aramex", "categorie": "montant_errone",
                                  "suivi": ref, "pe": pe["name"],
                                  "montant_brut_advice": gross, "montant_pe": pe["paid_amount"],
                                  "retenue_source": rs,
                                  "detail": "montant de la PE != montant brut Aramex (hors retenue source)"})
    return anomalies


def audit_cheques(movements, remise_loader=None, pe_finder=None) -> list:
    """Confronte chaque cheque des bordereaux de remise (credit 'ENC CHEQ <bon>') au PE ERPNext.
    ATTENTION : declenche l'extraction OpenAI des remises (cout) -> opt-in."""
    remise_loader = remise_loader or _load_remise
    pe_finder = pe_finder or _find_pe_by_number
    anomalies, seen = [], set()
    for m in mv.find_credits_by_keywords(mv.ENC_CHEQUE_KEYWORDS, movements=movements):
        bon = mv.find_bon_number(m["operation"])
        if not bon or bon in seen:
            continue
        seen.add(bon)
        try:
            remise = remise_loader(bon)
        except Exception:
            continue  # remise indisponible : signale par le flux de rapprochement, pas ici
        for c in remise.get("cheques", []) or []:
            num, montant = c.get("numero_cheque"), c.get("montant")
            pes = pe_finder(num)
            if not pes:
                anomalies.append({"flux": "cheque", "categorie": "recu_non_saisi",
                                  "bon": bon, "cheque": num, "montant": montant,
                                  "detail": "cheque depose (bordereau) mais AUCUNE PE en ERPNext"})
                continue
            pe = pes[0]
            if montant is not None and abs((pe["paid_amount"] or 0) - montant) >= TOL:
                anomalies.append({"flux": "cheque", "categorie": "montant_errone",
                                  "cheque": num, "pe": pe["name"],
                                  "montant_remise": montant, "montant_pe": pe["paid_amount"],
                                  "detail": "montant de la PE != montant du bordereau"})
    return anomalies
