"""Cheques IMPAYES : le releve dit « Cheque repris <n°> » (debit), le paiement sort de la banque.

LE CYCLE (demande utilisateur 16/09/2026)
-----------------------------------------
Un cheque client est saisi en caisse (Payment Entry mode « Chèque », compte d'attente
« Chèques - A&S »), remis en banque (le releve crédite « ENC CHEQ »), puis — parfois — REVIENT :
le releve porte un debit « Cheque repris 0000260 » du meme montant. Jusqu'ici la ligne restait
« A verifier : categorise, aucune automatisation prevue » et la relance client ne voyait rien.

Ce flux :
  1. detecte les debits « Cheque repris » non encore rapproches ;
  2. retrouve LE paiement du cheque (n° ET montant, un seul candidat — sinon on ne fait rien et on
     le dit) parmi les cheques en portefeuille (« Chèques - A&S ») ou deja verses (Zitouna) ;
  3. le BASCULE sur « Chèques sans provision - A&S » : meme convention que la branche « Sans
     provision » du Server Script « Traitement des encaissement » — le paiement est annule puis
     supprime, et recree a l'identique (memes references, donc la facture / commande reste
     « payee » par ce cheque) sur le compte des impayes, a la DATE DU REJET, son libelle citant la
     reference bancaire du rejet (c'est elle que l'identification bancaire retrouve) ;
  4. lie le mouvement au nouveau paiement (statut « Identifie ») et trace le rejet sur le
     paiement ET sur la commande / facture — c'est la que le vendeur regarde.

La page « Relance Paiements Clients » (customization_app) lit les Payment Entry sur
« Chèques sans provision - A&S » : le client y apparait aussitot, avec le montant a recouvrer.

/!\ CE QUI N'EST PAS FAIT : le credit « ENC CHEQ » de la remise d'origine n'est pas touche. Si la
remise avait deja ete rapprochee (cheque « verse » sur Zitouna), la piece sur Zitouna disparait
avec ce basculement — c'est le choix de la convention maison (le compte de banque ne porte que
l'argent reellement encaisse), et le credit puis le debit du releve se neutralisent.
"""
from __future__ import annotations

import re

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.bank import rules as R

COMPTE_PORTEFEUILLE = "Chèques - A&S"
COMPTE_BANQUE = "STE430127B - Zitouna - A&S"
COMPTE_IMPAYES = "Chèques sans provision - A&S"
REGLE = "cheque_repris"
TOLERANCE = 0.005


def numero_normalise(valeur) -> str:
    """Chiffres seuls, sans zeros de tete : « 0000260 » et « 260-BIAT » designent le meme cheque."""
    digits = re.sub(r"\D", "", str(valeur or ""))
    return digits.lstrip("0") or ("0" if digits else "")


def _numeros_de(reference_no: str) -> set:
    """Tous les groupes de chiffres d'un libelle de paiement (« 0000260-BIAT / BR:90027933 »)."""
    return {numero_normalise(g) for g in re.findall(r"\d+", str(reference_no or "")) if g}


def est_impaye(m: dict) -> bool:
    rule = R.find_rule(m)
    return bool(rule is not None and rule.key == REGLE and flt(m.get("debit"), 3) > 0)


def trouver_paiement(numero, montant, candidats) -> tuple:
    """(paiement, raison) — FONCTION PURE. Le n° ET le montant doivent concorder, et UN SEUL
    candidat : un cheque revenu deux fois (re-presente) donnerait deux paiements du meme numero,
    on ne tranche pas."""
    num = numero_normalise(numero)
    if not num:
        return None, "aucun n° de cheque lisible dans le libelle"
    par_numero = [p for p in (candidats or []) if num in _numeros_de(p.get("reference_no"))]
    if not par_numero:
        return None, "aucun paiement par cheque ne porte le n° %s" % numero
    exacts = [p for p in par_numero if abs(flt(p.get("paid_amount"), 3) - flt(montant, 3)) <= TOLERANCE]
    if not exacts:
        return None, "le cheque %s existe (%s) mais pas au montant du rejet (%s)" % (
            numero, ", ".join("%s : %s" % (p["name"], flt(p.get("paid_amount"), 3)) for p in par_numero),
            flt(montant, 3))
    if len(exacts) > 1:
        return None, "%d paiements portent le cheque %s pour ce montant : %s — non tranche" % (
            len(exacts), numero, ", ".join(p["name"] for p in exacts))
    return exacts[0], ""


def candidats_pe() -> list:
    """Les paiements par cheque qui peuvent revenir impayes : en portefeuille ou deja verses."""
    return frappe.get_all(
        "Payment Entry",
        filters={"docstatus": 1, "payment_type": "Receive", "mode_of_payment": "Chèque",
                 "paid_to": ["in", [COMPTE_PORTEFEUILLE, COMPTE_BANQUE]]},
        fields=["name", "party", "party_name", "paid_amount", "reference_no", "paid_to",
                "posting_date"], limit_page_length=0)


def _pieces_liees(pe) -> list:
    """(doctype, nom) des pieces que le paiement couvre, commandes derriere les factures comprises."""
    out = []
    for r in pe.references or []:
        if (r.reference_doctype, r.reference_name) not in out:
            out.append((r.reference_doctype, r.reference_name))
        if r.reference_doctype == "Sales Invoice":
            for so in frappe.get_all("Sales Invoice Item",
                                     filters={"parent": r.reference_name, "sales_order": ["!=", ""]},
                                     pluck="sales_order", distinct=True):
                if ("Sales Order", so) not in out:
                    out.append(("Sales Order", so))
    return out


def basculer(pe_name: str, mouvement: dict, insert: bool = True) -> dict:
    """Le paiement du cheque passe sur « Chèques sans provision - A&S » a la date du rejet."""
    pe = frappe.get_doc("Payment Entry", pe_name)
    if pe.docstatus != 1:
        frappe.throw("Le paiement %s n'est pas valide." % pe_name)
    reference = (mouvement.get("reference") or "").strip()
    jour = getdate(mouvement.get("date"))
    nouveau = frappe.copy_doc(pe)
    nouveau.paid_to = COMPTE_IMPAYES
    nouveau.paid_to_account_currency = frappe.db.get_value(
        "Account", COMPTE_IMPAYES, "account_currency") or nouveau.paid_to_account_currency
    nouveau.posting_date = jour
    nouveau.reference_date = jour
    nouveau.reference_no = "%s / Impayé %s du %s" % (pe.reference_no or "", reference, jour)
    if hasattr(nouveau, "custom_remise_en_banque"):
        nouveau.custom_remise_en_banque = None
    if not insert:
        return {"status": "a basculer", "ancien": pe.name, "nouveau": "(dry-run)",
                "montant": flt(pe.paid_amount, 3), "party": pe.party_name or pe.party}
    pieces = _pieces_liees(pe)
    ancien = pe.name
    pe.flags.ignore_permissions = True
    pe.flags.ignore_links = True
    pe.cancel()
    pe.delete(ignore_permissions=True)
    nouveau.flags.ignore_permissions = True
    nouveau.insert(ignore_permissions=True)
    nouveau.submit()
    texte = ("❌ Chèque n° %s (%s DT) revenu IMPAYÉ le %s — rejet bancaire %s. Paiement %s "
             "remplacé par %s sur « %s » : à relancer (Relance Paiements Clients)."
             % (pe.reference_no or "", flt(pe.paid_amount, 3), jour, reference, ancien,
                nouveau.name, COMPTE_IMPAYES))
    for doctype, nom in [("Payment Entry", nouveau.name)] + pieces:
        try:
            frappe.get_doc({"doctype": "Comment", "comment_type": "Info",
                            "reference_doctype": doctype, "reference_name": nom,
                            "content": texte}).insert(ignore_permissions=True)
        except Exception:
            pass
    return {"status": "bascule", "ancien": ancien, "nouveau": nouveau.name,
            "montant": flt(pe.paid_amount, 3), "party": pe.party_name or pe.party}


def process_impayes(movements: list = None, insert: bool = True) -> list:
    """Point d'entree : pour chaque debit « Cheque repris » non rapproche, retrouve le cheque et le
    bascule en impaye. Rend un compte rendu par mouvement ; les cas douteux ne font rien."""
    from bank_retenue_sync.bank import registry

    if movements is None:
        movements = registry.registry_as_movements()
    out = []
    candidats = None
    for m in movements or []:
        if not est_impaye(m):
            continue
        cle = registry.movement_key(m)
        row = frappe.db.get_value("BRS Bank Movement", cle,
                                  ["statut", "document_name", "ignore_manuel"], as_dict=True) or {}
        if row.get("ignore_manuel") or (row.get("document_name") and row.get("statut") == "Identifie"):
            continue
        if candidats is None:
            candidats = candidats_pe()
        numero = R.extract_numero(R.find_rule(m), m)
        pe, raison = trouver_paiement(numero, m.get("debit"), candidats)
        base = {"flux": "impaye", "cle": cle, "date": str(m.get("date")), "numero": numero,
                "reference": m.get("reference"), "montant": flt(m.get("debit"), 3)}
        if not pe:
            out.append(dict(base, status="non trouve", raison=raison))
            continue
        try:
            res = basculer(pe["name"], m, insert=insert)
        except Exception as e:
            out.append(dict(base, status="error", paiement=pe["name"], error=str(e)[:160]))
            continue
        if insert and frappe.db.exists("BRS Bank Movement", cle):
            frappe.db.set_value("BRS Bank Movement", cle, {
                "statut": "Identifie", "document_type": "Payment Entry",
                "document_name": res["nouveau"], "montant_document": res["montant"], "ecart": 0,
                "raison": "chèque impayé : paiement %s basculé sur %s" % (res["ancien"], COMPTE_IMPAYES),
            }, update_modified=False)
            candidats = [c for c in candidats if c["name"] != pe["name"]]
        out.append(dict(base, **res))
    return out
