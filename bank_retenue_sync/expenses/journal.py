"""Construction des ecritures de journal (Journal Entry) pour les depenses mensuelles.

- `build_journal_entry()` : constructeur generique (equilibre verifie), n'INSERE pas.
- `create_total_journal_entry()` : ecriture de la facture Total (payee par la Carte Total).
- Aramex / declaration : viendront ici (memes briques).

Comportement de soumission : brouillon par defaut. Un parametre `auto_submit`
dans les Settings (une fois l'app installee) permettra de soumettre directement ;
tant que ce parametre n'existe pas, on reste en brouillon.
"""
from __future__ import annotations

import frappe

from datetime import timedelta

from frappe.utils import getdate

from bank_retenue_sync.expenses.dates import period_end_date
from bank_retenue_sync.mail.total_invoice import TotalInvoice

# --- Mapping comptable (valide avec l'utilisateur) -------------------------
# Total : paye immediatement par la Carte Total. TVA agregee sur le 19%
# (pas de compte 13% -> somme de toute la TVA sur ce compte).
TOTAL_CHARGE_ACCOUNT = "Frais de Déplacement - A&S"
TOTAL_TVA_ACCOUNT = "TVA 19% - A&S"
TOTAL_PAYMENT_ACCOUNT = "Carte Total - A&S"
TOTAL_MODE_OF_PAYMENT = "Carte de crédit"

# Aramex : facture reglee PLUS TARD depuis Zitouna -> credit = compte fournisseur
# (Crediteurs, tiers ARAMEX), pas de moyen de paiement a la creation.
# TVA a 7%, plus un droit de timbre.
ARAMEX_CHARGE_ACCOUNT = "Frais de Fret et d'Expédition - A&S"
ARAMEX_TVA_ACCOUNT = "TVA 7% - A&S"
ARAMEX_STAMP_ACCOUNT = "Timbre Fiscal - A&S"
ARAMEX_CREDIT_ACCOUNT = "Créditeurs - A&S"
ARAMEX_SUPPLIER = "ARAMEX"


def _period_mm_yyyy(d):
    """date -> 'MM-YYYY' (ex. 30/06/2026 -> '06-2026'), sinon ''."""
    return f"{d.month:02d}-{d.year:04d}" if d else ""


def _parse_iso_date(s):
    """'2026-06-30' -> date, sinon None."""
    from datetime import datetime
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


class UnbalancedEntry(Exception):
    pass


def _auto_submit_enabled() -> bool:
    """Parametre de soumission auto : Settings DocType si installe, sinon site_config
    (brs_auto_submit). Defaut False (brouillon)."""
    try:
        if frappe.db.exists("DocType", "Bank Retenue Sync Settings"):
            val = frappe.db.get_single_value("Bank Retenue Sync Settings", "auto_submit_journal_entries")
            if val is not None:
                return bool(val)
    except Exception:
        pass
    return bool(frappe.conf.get("brs_auto_submit"))


def _company_of(account: str) -> str:
    return frappe.db.get_value("Account", account, "company")


def build_journal_entry(company, posting_date, lines, remark=None,
                        cheque_no=None, cheque_date=None, mode_of_payment=None):
    """lines : [{account, debit?, credit?, party_type?, party?, cost_center?}, ...].
    Verifie l'equilibre Dr=Cr, construit le doc EN MEMOIRE (pas d'insert).

    cheque_no       -> "Numero de reference" (ex. periode '06-2026')
    cheque_date     -> "Date de Reference"
    mode_of_payment -> "Moyen de paiement" (ex. 'Carte de credit')
    """
    total_debit = round(sum(float(l.get("debit") or 0) for l in lines), 3)
    total_credit = round(sum(float(l.get("credit") or 0) for l in lines), 3)
    if abs(total_debit - total_credit) >= 0.01:
        raise UnbalancedEntry(f"Ecriture desequilibree: Dr={total_debit} != Cr={total_credit}")

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.posting_date = posting_date
    je.company = company
    if remark:
        je.user_remark = remark
    if cheque_no:
        je.cheque_no = cheque_no
    if cheque_date:
        je.cheque_date = cheque_date
    if mode_of_payment:
        je.mode_of_payment = mode_of_payment
    for l in lines:
        je.append("accounts", {
            "account": l["account"],
            "debit_in_account_currency": float(l.get("debit") or 0),
            "credit_in_account_currency": float(l.get("credit") or 0),
            "party_type": l.get("party_type"),
            "party": l.get("party"),
            "cost_center": l.get("cost_center"),
        })
    return je


def _attach(je, attachments):
    """attachments : [(filename, bytes), ...] -> fichiers prives sur l'ecriture."""
    from frappe.utils.file_manager import save_file
    for fname, content in attachments or []:
        save_file(fname, content, je.doctype, je.name, is_private=1)


def create_total_journal_entry(invoice: TotalInvoice, pdf=None, submit=None, insert=True,
                               email_date=None):
    """Ecriture de la facture Total.

    Dr Frais de Deplacement (HT) + Dr TVA 19% (toute la TVA) / Cr Carte Total (TTC).
    Datee au dernier jour de la periode (Date_facture).
    submit=None -> lit le parametre auto_submit (defaut brouillon).
    insert=False -> dry-run : renvoie le doc sans l'inserer (aucune ecriture DB).
    """
    company = _company_of(TOTAL_CHARGE_ACCOUNT)
    # Date de comptabilisation : dernier jour du mois PRECEDANT la reception de l'email
    # (convention utilisateur). Repli sur la date de facture puis sur la fin de periode
    # quand l'email n'est pas date.
    posting_date = (fin_mois_precedent(email_date) if email_date
                    else (invoice.invoice_date or period_end_date(invoice.period)))
    cc = frappe.db.get_value("Company", company, "cost_center")
    # Convention utilisateur : compte de paiement (Carte Total) EN PREMIER,
    # compte de charge TOUJOURS EN DERNIER ; TVA au milieu.
    lines = [
        {"account": TOTAL_PAYMENT_ACCOUNT, "credit": invoice.total_ttc},
        {"account": TOTAL_TVA_ACCOUNT, "debit": invoice.total_tva, "cost_center": cc},
        {"account": TOTAL_CHARGE_ACCOUNT, "debit": invoice.total_ht, "cost_center": cc},
    ]
    remark = f"Facture Total {invoice.invoice_no} ({invoice.period})"
    je = build_journal_entry(
        company, posting_date, lines, remark=remark,
        cheque_no=f"Facture Total {_period_mm_yyyy(invoice.invoice_date)}",   # ex. 'Facture Total 06-2026'
        cheque_date=invoice.invoice_date,
        mode_of_payment=TOTAL_MODE_OF_PAYMENT,
    )

    if not insert:
        return je   # dry-run

    je.insert(ignore_permissions=True)
    if pdf:
        _attach(je, [pdf])
    if submit is None:
        submit = _auto_submit_enabled()
    if submit:
        je.submit()
    return je


# Declaration fiscale mensuelle : payee au fisc depuis Zitouna a la DATE BANCAIRE.
# Mapping valide avec l'utilisateur :
DECL_BANK_ACCOUNT = "STE430127B - Zitouna - A&S"
DECL_LOYER_ACCOUNT = "Taxe Loyer - A&S"          # retenue 10% loyers (+ solde TVA si >0)
DECL_RS_ACHAT_ACCOUNT = "Retenue a la source achat - A&S"  # retenue 3% honoraires
DECL_TIMBRE_ACCOUNT = "Timbre Fiscal - A&S"
DECL_TCL_ACCOUNT = "T.C.L - A&S"
DECL_TVA_COLLECTEE_ACCOUNT = "TVA collectee - A&S"          # TVA A PAYER (solde debiteur)
DECL_IMPOT_ACCOUNT = "Impot sur revenu + CNSS - A&S"        # le reste (salaires, solidarite, 1%, TFP, FOPROLOS)


def compute_declaration_lines(extract: dict) -> dict:
    """Applique le mapping et renvoie {comptes: montants, total, impot}.

    Taxe Loyer = retenue 10% loyers uniquement.
    TVA collectee = TVA A PAYER (tva_solde) -> ligne dediee (0 s'il y a un credit de TVA).
    Impot + CNSS = le reste par difference.
    """
    total = round(float(extract.get("total_a_payer") or 0), 3)
    loyer = round(float(extract.get("rs_loyer_10") or 0), 3)
    tva_collectee = round(max(float(extract.get("tva_solde") or 0), 0.0), 3)
    # Retenue a la source achat = retenue 1% sur acquisitions >= 1000 DT (ligne 17 du formulaire)
    rs_achat = round(float(extract.get("rs_achat_1pct") or 0), 3)
    timbre = round(float(extract.get("timbre_fiscal") or 0), 3)
    tcl = round(float(extract.get("tcl") or 0), 3)
    impot = round(total - loyer - rs_achat - timbre - tcl - tva_collectee, 3)   # le reste
    return {"total": total, "loyer": loyer, "rs_achat": rs_achat, "timbre": timbre,
            "tcl": tcl, "tva_collectee": tva_collectee, "impot": impot}


# --- Ecriture d'ouverture : balancer le compte TVA sur la declaration ------
# Au dernier jour du mois, on aligne le solde ERP du compte TVA de reference sur
# la valeur de credit/report TVA de la declaration, via le "Compte temporaire d'ouverture".
TVA_PRIMARY_ACCOUNT = "TVA 19% - A&S"          # aligne sur la declaration (compte debiteur >0)
TVA_SECONDARY_ACCOUNTS = ("TVA 7% - A&S",)     # sous-comptes soldes vers 0 (verses au primaire)
TVA_OPENING_ACCOUNT = "Compte temporaire - compte  d'overture - A&S"


def account_balance(account: str, upto_date) -> float:
    """Solde (Debit - Credit) d'un compte jusqu'a une date incluse."""
    r = frappe.db.sql(
        """SELECT SUM(debit - credit) FROM `tabGL Entry`
           WHERE account=%s AND posting_date<=%s AND is_cancelled=0""",
        (account, upto_date),
    )
    return round(float(r[0][0] or 0), 3)


def create_tva_balancer_entry(period, target_value, submit=None, insert=True):
    """Ecriture 'Balancer TVA MM-YYYY' au dernier jour de la periode.

    Mecanique :
      1. les sous-comptes TVA secondaires (ex. TVA 7%) sont ramenes a 0 ;
      2. le compte TVA primaire (TVA 19%) est aligne sur `target_value`
         (= credit / report TVA de la declaration) ;
      3. la contrepartie (le vrai delta net) va au "Compte temporaire d'ouverture".

    Ajustement par compte = cible - solde ERP (au dernier jour du mois).
    Renvoie None si aucun ajustement. insert=False -> dry-run.
    """
    company = _company_of(TVA_PRIMARY_ACCOUNT)
    cc = frappe.db.get_value("Company", company, "cost_center")
    end = period_end_date(period)

    targets = {TVA_PRIMARY_ACCOUNT: round(float(target_value), 3)}
    for acc in TVA_SECONDARY_ACCOUNTS:
        targets[acc] = 0.0

    adjustments = {}
    for acc, tgt in targets.items():
        adj = round(tgt - account_balance(acc, end), 3)   # +adj -> Dr, -adj -> Cr
        if abs(adj) >= 0.001:
            adjustments[acc] = adj
    if not adjustments:
        return None

    lines = []
    for acc, adj in adjustments.items():
        if adj > 0:
            lines.append({"account": acc, "debit": adj, "cost_center": cc})
        else:
            lines.append({"account": acc, "credit": -adj, "cost_center": cc})

    opening = round(-sum(adjustments.values()), 3)   # contrepartie qui equilibre l'ecriture
    if abs(opening) >= 0.001:
        if opening > 0:
            lines.append({"account": TVA_OPENING_ACCOUNT, "debit": opening, "cost_center": cc})
        else:
            lines.append({"account": TVA_OPENING_ACCOUNT, "credit": -opening, "cost_center": cc})

    je = build_journal_entry(company, end, lines,
                             cheque_no=f"Balancer TVA {_period_mm_yyyy(end)}", cheque_date=end)
    if not insert:
        return je
    je.insert(ignore_permissions=True)
    if submit is None:
        submit = _auto_submit_enabled()
    if submit:
        je.submit()
    return je


def create_declaration_journal_entry(extract: dict, bank_date=None, bank_reference=None,
                                     pdf=None, submit=None, insert=True):
    """Ecriture de la declaration fiscale mensuelle.

    Cr Zitouna (total, a la DATE BANCAIRE du paiement) / Dr comptes fiscaux mappes.
    bank_date      : date reelle de paiement (depuis le service bancaire) ; a defaut, dernier
                     jour de la periode (a remplacer par la vraie date banque).
    bank_reference : reference du mouvement bancaire (ex. 'FT26202JQJH3'), ajoutee a la remarque.
    insert=False -> dry-run.
    """
    m = compute_declaration_lines(extract)
    company = _company_of(DECL_IMPOT_ACCOUNT)
    cc = frappe.db.get_value("Company", company, "cost_center")
    period = extract.get("period")
    posting_date = bank_date or period_end_date(period)

    # Cr banque en tete ; comptes fiscaux au debit ; on ignore les lignes a 0.
    lines = [{"account": DECL_BANK_ACCOUNT, "credit": m["total"]}]
    for account, amount in [
        (DECL_LOYER_ACCOUNT, m["loyer"]),
        (DECL_RS_ACHAT_ACCOUNT, m["rs_achat"]),
        (DECL_TIMBRE_ACCOUNT, m["timbre"]),
        (DECL_TCL_ACCOUNT, m["tcl"]),
        (DECL_TVA_COLLECTEE_ACCOUNT, m["tva_collectee"]),   # ligne presente seulement si TVA a payer
        (DECL_IMPOT_ACCOUNT, m["impot"]),
    ]:
        if abs(amount) >= 0.001:
            lines.append({"account": account, "debit": amount, "cost_center": cc})

    remark = f"Declaration comptable {period}"
    if bank_reference:
        remark += f" - Réf. banque {bank_reference}"
    je = build_journal_entry(
        company, posting_date, lines, remark=remark,
        cheque_no=f"Declaration comptable {_period_mm_yyyy(_parse_iso_date((period or '') + '-01'))}",
        cheque_date=posting_date,
    )
    if not insert:
        return je
    je.insert(ignore_permissions=True)
    if pdf:
        _attach(je, [pdf])
    if submit is None:
        submit = _auto_submit_enabled()
    if submit:
        je.submit()
    return je


# CNSS (charges sociales, trimestriel) : pilote par la BANQUE. A la detection d'un
# 'PAIEMENT PRELEVEMENT C.N.S.S', on comptabilise le debit : Cr Zitouna / Dr Impot+CNSS.
CNSS_BANK_ACCOUNT = "STE430127B - Zitouna - A&S"
CNSS_CHARGE_ACCOUNT = "Impot sur revenu + CNSS - A&S"


def cnss_quarter_label(d):
    """date de paiement -> 'QX-YYYY' du trimestre paye (T1 paye en avril, T2 en juillet...)."""
    q = (d.month - 1) // 3
    year = d.year
    if q == 0:            # paiement en janvier -> T4 de l'annee precedente
        q, year = 4, year - 1
    return f"Q{q}-{year}"


def cnss_quarter_file_token(d):
    """-> '2TR2026' pour retrouver le PDF DECL.CNSS du trimestre."""
    q = (d.month - 1) // 3
    year = d.year
    if q == 0:
        q, year = 4, year - 1
    return f"{q}TR{year}"


def create_cnss_journal_entry(movement: dict, attachments=None, submit=None, insert=True):
    """Ecriture CNSS a partir d'un mouvement bancaire 'PRELEVEMENT C.N.S.S'.
    Cr Zitouna (date + montant du debit) / Dr Impot + CNSS.
    attachments : liste de (nom, bytes) des justificatifs du trimestre (DECL.CNSS, RECAP,
    FICHE DE PAIE) issus de l'email du comptable. insert=False -> dry-run."""
    date = movement["date"]
    amount = round(float(movement["debit"]), 3)
    ref = movement.get("reference")
    quarter = cnss_quarter_label(date)

    company = _company_of(CNSS_CHARGE_ACCOUNT)
    cc = frappe.db.get_value("Company", company, "cost_center")
    lines = [
        {"account": CNSS_BANK_ACCOUNT, "credit": amount},
        {"account": CNSS_CHARGE_ACCOUNT, "debit": amount, "cost_center": cc},
    ]
    remark = f"Charge employeur CNSS {quarter}"
    if ref:
        remark += f" - Réf. banque {ref}"
    je = build_journal_entry(company, date, lines, remark=remark,
                             cheque_no=f"CNSS {quarter}", cheque_date=date)
    if not insert:
        return je
    je.insert(ignore_permissions=True)
    _attach(je, attachments)
    if submit is None:
        submit = _auto_submit_enabled()
    if submit:
        je.submit()
    return je


def create_declaration_with_balancer(extract: dict, bank_date=None, bank_reference=None,
                                     pdf=None, submit=None, insert=True):
    """Cree l'ecriture de declaration ET, automatiquement, l'ecriture 'Balancer TVA'
    du meme mois (si la declaration presente un credit de TVA reportable).
    Retourne (declaration_je, balancer_je | None)."""
    decl = create_declaration_journal_entry(
        extract, bank_date=bank_date, bank_reference=bank_reference,
        pdf=pdf, submit=submit, insert=insert,
    )
    balancer = None
    tva_credit = round(float(extract.get("tva_credit") or 0), 3)
    if tva_credit > 0:
        balancer = create_tva_balancer_entry(extract.get("period"), tva_credit,
                                             submit=submit, insert=insert)
    return decl, balancer


# Note d'honoraire du comptable : creee A LA RECEPTION contre un compte crediteur
# (Compte de decouvert bancaire), rapprochee au paiement bancaire ulterieurement.
# RS 3% suivie separement ; charge = HT + timbre sur "Charges Diverses".
HONORAIRE_CHARGE_ACCOUNT = "Charges Diverses - A&S"
HONORAIRE_TVA_ACCOUNT = "TVA 19% - A&S"
HONORAIRE_CREDIT_ACCOUNT = "Compte de découvert bancaire - A&S"
HONORAIRE_RS_ACCOUNT = "Retenue a la source achat - A&S"


def fin_mois_precedent(d):
    """Dernier jour du mois PRECEDANT `d`. ('2026-07-14' -> date(2026, 6, 30))

    Convention retenue avec l'utilisateur pour les factures fournisseur recues par email : la
    facture arrive en debut de mois suivant, l'ecriture est datee de la fin de la periode qu'elle
    couvre, c'est-a-dire le dernier jour du mois qui precede la RECEPTION de l'email.
    """
    if not d:
        return None
    d = getdate(d)
    premier = d.replace(day=1)
    return premier - timedelta(days=1)


def _prev_month_period(d):
    """date -> 'YYYY-MM' du mois PRECEDENT (la note de juillet concerne juin)."""
    if not d:
        return None
    y, m = d.year, d.month - 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


def create_honoraire_journal_entry(extract: dict, pdf=None, submit=None, insert=True):
    """Ecriture de la note d'honoraire comptable (a la reception).

    Cr Compte de decouvert (NET) + Cr Retenue a la source achat (RS 3%) /
    Dr TVA 19% + Dr Charges Diverses (HT + timbre, en dernier).
    Datee de la date de la note ; periode = mois precedent. insert=False -> dry-run.
    """
    inv_date = _parse_iso_date(extract.get("invoice_date"))
    period = _prev_month_period(inv_date)
    ht = float(extract.get("total_ht") or 0)
    tva = float(extract.get("total_tva") or 0)
    timbre = float(extract.get("timbre_fiscal") or 0)
    rs = float(extract.get("retenue_source") or 0)
    net = float(extract.get("net_a_payer") or 0)
    charge = round(ht + timbre, 3)

    company = _company_of(HONORAIRE_CHARGE_ACCOUNT)
    cc = frappe.db.get_value("Company", company, "cost_center")
    lines = [
        {"account": HONORAIRE_CREDIT_ACCOUNT, "credit": net},
        {"account": HONORAIRE_RS_ACCOUNT, "credit": rs},
        {"account": HONORAIRE_TVA_ACCOUNT, "debit": tva, "cost_center": cc},
        {"account": HONORAIRE_CHARGE_ACCOUNT, "debit": charge, "cost_center": cc},
    ]
    remark = f"Note d'honoraire comptable {period}"
    je = build_journal_entry(
        company, inv_date or period_end_date(period), lines, remark=remark,
        cheque_no=f"Note d'honoraire comptable {_period_mm_yyyy(period_end_date(period))}",
        cheque_date=inv_date,
    )
    if not insert:
        return je
    je.insert(ignore_permissions=True)
    if pdf:
        _attach(je, [pdf])
    if submit is None:
        submit = _auto_submit_enabled()
    if submit:
        je.submit()
    return je


def create_aramex_journal_entry(extract: dict, pdf=None, submit=None, insert=True,
                                email_date=None):
    """Ecriture de la facture Aramex a partir de l'extraction OpenAI (`extract`).

    Cr Crediteurs (tiers ARAMEX, TTC) / Dr TVA 7% / Dr Timbre Fiscal / Dr charge (HT, en dernier).
    Datee au dernier jour de la periode ; reglee ulterieurement depuis Zitouna (pas de
    moyen de paiement ici). insert=False -> dry-run.
    """
    inv_date = _parse_iso_date(extract.get("invoice_date"))
    company = _company_of(ARAMEX_CHARGE_ACCOUNT)
    cc = frappe.db.get_value("Company", company, "cost_center")
    ht = float(extract.get("total_ht") or 0)
    tva = float(extract.get("total_tva") or 0)
    stamp = float(extract.get("stamp_duty") or 0)
    ttc = float(extract.get("total_ttc") or 0)
    inv_no = extract.get("invoice_no") or ""
    period = f"{inv_date.year:04d}-{inv_date.month:02d}" if inv_date else None

    # credit en tete, charge en dernier (convention utilisateur).
    # Le droit de timbre est INTEGRE au compte de charge (Frais de Fret), pas de ligne dediee.
    charge = round(ht + stamp, 3)
    lines = [
        {"account": ARAMEX_CREDIT_ACCOUNT, "credit": ttc,
         "party_type": "Supplier", "party": ARAMEX_SUPPLIER},
        {"account": ARAMEX_TVA_ACCOUNT, "debit": tva, "cost_center": cc},
        {"account": ARAMEX_CHARGE_ACCOUNT, "debit": charge, "cost_center": cc},
    ]

    remark = f"Facture Aramex {inv_no} ({period})"
    # Dernier jour du mois precedant la reception de l'email (convention utilisateur),
    # repli sur la date de facture.
    posting_date = fin_mois_precedent(email_date) if email_date else (
        inv_date or period_end_date(period))
    je = build_journal_entry(
        company, posting_date, lines, remark=remark,
        cheque_no=f"Facture Aramex {_period_mm_yyyy(inv_date)}",
        cheque_date=inv_date,
        # pas de mode_of_payment : reglee plus tard depuis Zitouna
    )
    if not insert:
        return je
    je.insert(ignore_permissions=True)
    if pdf:
        _attach(je, [pdf])
    if submit is None:
        submit = _auto_submit_enabled()
    if submit:
        je.submit()
    return je
