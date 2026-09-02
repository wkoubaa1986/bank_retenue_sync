"""Orchestration : cree les ecritures de depense a partir des emails et de la banque.

Deux entrees whitelisted :
  - run_email_ingestion() : Total / Aramex / Note d'honoraire (declencheur = email recu)
  - run_bank_ingestion()  : Declaration / CNSS (declencheur = prelevement detecte en banque)

Idempotent : une ecriture n'est jamais recreee (cle = `cheque_no` du Journal Entry).
Ecritures creees en BROUILLON (sauf param auto_submit dans les Settings).
Chaque item est isole (try/except) : un echec n'arrete pas le lot.
"""
from __future__ import annotations

import re
import time

import frappe

from bank_retenue_sync.ai import party_match
from bank_retenue_sync.ai.declaration_extract import extract_declaration
from bank_retenue_sync.ai.invoice_extract import extract_honoraire, extract_invoice
from bank_retenue_sync.bank import movements as mv
from bank_retenue_sync.bank.movements import fetch_latest_movements, find_debits_by_keywords
from bank_retenue_sync.encaissement import builder, matching, party, pending
from bank_retenue_sync.expenses import journal
from bank_retenue_sync.expenses.dates import period_end_date
from bank_retenue_sync.mail.aramex_advice import parse_advice
from bank_retenue_sync.mail import config as mail_config
from bank_retenue_sync.mail.total_invoice import extract_invoice_files, parse_invoice_xlsx

# Plus aucun expediteur ni sujet en dur ici : tout vient de la table « Sources email » des
# Settings (cf. mail/config.py). C'etait la double verite a supprimer — `mail/sources.py`
# declarait les sources, mais l'orchestrateur reecrivait les memes chaines a cote.


def _total_de(je) -> float:
    """Total au debit d'une ecriture, y compris NON enregistree.

    `total_debit` n'est calcule qu'a la validation : sur un essai a blanc il vaut None, et le
    compte rendu affichait des montants vides — donc inverifiable, donc inutile.
    """
    from frappe.utils import flt

    # `build_journal_entry` renseigne `debit_in_account_currency`, pas `debit` : ce dernier n'est
    # derive qu'a la validation. Sommer `debit` rendait 0 sur tout essai a blanc.
    return round(flt(je.total_debit) or sum(
        flt(a.debit_in_account_currency) or flt(a.debit) for a in (je.accounts or [])), 3)


def _exists(cheque_no: str) -> bool:
    return bool(cheque_no) and bool(frappe.db.exists("Journal Entry", {"cheque_no": cheque_no}))


_PERIODE_RE = re.compile(r"(\d{2})-(\d{4})")


def _periodes_du_libelle(texte: str) -> set:
    """Periodes 'MM-YYYY' lisibles dans un libelle, quel que soit son format.

    Couvre les formes reelles : « au 30-06-2026 » -> {06-2026}, « du 28-02-2026 » -> {02-2026},
    « Facture ARAMEX 11-2023 » -> {11-2023}, « Facture ARAMEX -02-2024 » -> {02-2024}.
    """
    return {"%s-%s" % (m.group(1), m.group(2)) for m in _PERIODE_RE.finditer(texte or "")}


def _deja_comptabilise(periode: str, compte: str, sens: str = "credit",
                       marqueur: str = None) -> str:
    """Nom d'une ecriture couvrant DEJA `periode` ('YYYY-MM') sur `compte`, sinon None.

    POURQUOI L'EGALITE SUR `cheque_no` NE SUFFIT PAS
    ------------------------------------------------
    L'idempotence historique compare `cheque_no` a « Facture Total 06-2026 ». Or les saisies
    manuelles ne suivent aucun format stable — releve sur les ecritures reelles :
        « Facture TOTAL au 30-06-2026 », « Fac TOTAL au 31-03-2026 », « Facture TOTAL 05-2024 »,
        « Fac ARAMEX au 30-06-2026 », « Fac ARAMEX du 30-04-2026 », « Facture ARAMEX -02-2024 ».
    Aucune ne correspond a la cle generee : un premier vrai run recreerait DIX mois deja saisis.

    TROIS PIEGES MESURES SUR LES DONNEES, QUI DICTENT CE CRITERE
    -----------------------------------------------------------
    1. Le compte seul ne suffit PAS pour Aramex : `Frais de Fret et d'Expédition` recoit aussi
       Google Ads, SUNLINE, Frotec, Elestar, FASTHAUL. Sans le `marqueur`, avril 2026 se declarait
       « deja fait » en pointant la facture Google Ads. D'ou le marqueur de fournisseur.
    2. Aucune de ces lignes ne porte de `party` : l'appariement par tiers est impossible.
    3. La date de comptabilisation N'EST PAS la periode : « Fac ARAMEX du 28-02-2026 » est
       comptabilisee le 05/03/2026. On cherche donc dans une fenetre LARGE et c'est le LIBELLE
       qui tranche la periode ; la date de comptabilisation ne sert de repli que pour un libelle
       sans periode lisible.
    """
    from frappe.utils import add_days, get_last_day, getdate

    cible = _mmyyyy(periode)
    debut = add_days(periode + "-01", -40)
    fin = add_days(get_last_day(periode + "-01"), 60)
    colonne = "credit" if sens == "credit" else "debit"
    rows = frappe.db.sql(
        f"""
        select distinct gl.voucher_no, je.cheque_no, je.user_remark, je.posting_date
        from `tabGL Entry` gl
        join `tabJournal Entry` je on je.name = gl.voucher_no
        where gl.account = %(compte)s and gl.is_cancelled = 0
          and gl.voucher_type = 'Journal Entry'
          and gl.posting_date between %(debut)s and %(fin)s
          and gl.{colonne} > 0
        order by je.posting_date
        """, {"compte": compte, "debut": debut, "fin": fin}, as_dict=True)

    for r in rows:
        libelle = "%s %s" % (r.cheque_no or "", r.user_remark or "")
        if marqueur and marqueur.upper() not in libelle.upper():
            continue
        vues = _periodes_du_libelle(libelle)
        if vues:
            # Le libelle porte une periode : elle FAIT FOI. S'y fier evite de prendre l'ecriture
            # de fevrier, comptabilisee en mars, pour celle de mars.
            if cible in vues:
                return r.voucher_no
            continue
        if getdate(r.posting_date).strftime("%m-%Y") == cible:
            return r.voucher_no
    return None


def _periode_debut_gestion() -> str:
    """Plancher 'AAAA-MM' des Settings : en dessous, les depenses email ne creent rien (les
    mois anterieurs sont saisis a la main — cas reel : la note d'honoraire « 05-06-2026 »
    groupait deux mois sous un titre que l'idempotence ne reconnaissait pas, deux doublons
    crees puis supprimes le 19/08)."""
    try:
        return (frappe.db.get_single_value("Bank Retenue Sync Settings",
                                           "periode_debut_gestion") or "").strip()
    except Exception:
        return ""


def _movements_geres(movements) -> list:
    """Ecarte les mouvements ANTERIEURS au debut de gestion (`periode_debut_gestion`).

    POUR LES FLUX QUI CREENT DES PIECES — jamais pour la classification, qui doit continuer de
    montrer tout l'historique. Les mois anterieurs au plancher sont equilibres A LA MAIN (ecart
    d'ouverture accepte au 01/07/2026) : y creer une piece detruit un equilibre deja arbitre.

    Cas reel du 2026-08-21 : un backfill fait entrer au registre un versement d'especes du
    31/03 ; le flux especes, aveugle au plancher, cree `ACC-JV-2026-00621` (1 150 DT) date de
    MARS — en dev ET en prod, chacun par son propre run. La mecanique etait juste (caisse
    couverte, aucun doublon), mais l'ecriture est fausse PAR CONSTRUCTION : la periode
    n'appartient pas a l'app. Seuls les frais mensuels et les depenses email respectaient le
    plancher ; ce filtre l'impose a tous les flux de creation.
    """
    debut = _periode_debut_gestion()
    if not debut:
        return movements
    try:
        plancher = frappe.utils.getdate(debut + "-01")
    except Exception:
        return movements
    return [m for m in (movements or [])
            if m.get("date") and frappe.utils.getdate(m["date"]) >= plancher]


def _recharge_carte_importee(depuis) -> bool:
    """True si le run en cours a importe un CHARGEMENT de la carte technologique.

    `premiere_vue >= depuis` et non les `cles` de l'upsert : celles-ci contiennent aussi les
    mouvements simplement REVUS — une vieille recharge re-vue a chaque export redeclencherait
    la carte cinq fois par jour pour rien. La regle `chargement_carte` est posee par la
    classification, qui tourne dans le meme passage, avant cet appel."""
    return bool(frappe.db.exists("BRS Bank Movement", {
        "regle": "chargement_carte", "premiere_vue": [">=", depuis]}))


def _hors_perimetre(periode: str, depuis: str) -> bool:
    """`periode` ('YYYY-MM') est-elle anterieure au plancher `depuis` ?

    Sert a borner une reprise : la boite contient des factures bien plus anciennes que ce qu'on
    veut rejouer, et deux ecritures historiques portent une COQUILLE D'ANNEE dans leur libelle
    (« Fac TOTAL au 28-02-2025 » comptabilisee le 28/02/2026). Sur ces deux mois-la, et sur eux
    seuls, la detection par periode echoue et un doublon serait propose. Le plancher les ecarte.
    """
    return bool(depuis) and (periode or "") < depuis


def _reference_deja_citee(reference: str, date=None) -> str:
    """Nom d'une ecriture citant DEJA cette reference bancaire, sinon None.

    L'idempotence des flux bancaires reposait sur le seul `cheque_no` genere
    (« CNSS Q2-2026 », « Declaration comptable 06-2026 »). Or les saisies manuelles ne suivent
    pas ce format — reel : « Charge emploueyr CNSS Q2 -2026 » (ACC-JV-2026-00505) et
    « Declaration comptable 05-2025 » pour la periode 05-2026 (coquille d'annee systematique).
    Un run aurait donc recree ces ecritures en double.

    La reference bancaire, elle, est propre a l'operation et se retrouve dans le libelle de
    l'ecriture : c'est la cle sure. (A ne pas confondre avec les references de CONTRAT `LD…`,
    partagees par toutes les echeances — cf. bank/classify.)
    """
    from bank_retenue_sync.expenses import lookup

    ref = (reference or "").strip()
    if not ref:
        return None
    index = lookup.journal_entries_by_bank_reference([ref])
    noms = index.get(ref.upper()) or []
    return noms[0] if noms else None


# ------------------------- fraicheur des donnees bancaires -------------------------
# Chaque rafraichissement = un scraping Playwright de plusieurs minutes cote tej-bank-service.
# `run_all()` enchaine declaration -> CNSS -> encaissements, qui lisent tous la banque : sans
# memoisation on scraperait trois fois. On memoise par TTL plutot que par drapeau de run, pour que
# l'imbrication des entrees publiques n'ait pas d'importance (worker Frappe = process long).
_REFRESH_TTL = 1800          # 30 min : bien au-dela d'un run, bien en deca du rythme quotidien
_LAST_REFRESH = None         # time.monotonic() du dernier rafraichissement tente


def _ensure_fresh_bank_data(diagnostics: list = None, lookback_days: int = None) -> bool:
    """Declenche un nouvel export bancaire + la sync des bordereaux de remise, au plus une fois
    par `_REFRESH_TTL`. Retourne True si le rafraichissement a reussi.

    NON FATAL : en cas d'echec (service down, portail en rade), on note le diagnostic et on
    continue sur l'export existant — un rapprochement partiel vaut mieux qu'un job qui plante.
    L'appelant expose `bank_stale` pour que le resultat ne passe jamais pour complet a tort.

    `lookback_days` vaut None par defaut, et ce defaut compte : le passer FORCE une fenetre unique
    et desactive le repli sur `movements._EXPORT_WINDOWS` (cf. movements.py:221). L'ancienne valeur
    120 cumulait les deux defauts — elle n'est pas de la forme 7n-1, donc [today-120, today] fait
    121 jours = 17 semaines PLUS 2 jours de residu, exactement le cas ou le portail n'a rien a
    telecharger et ou le bot Playwright meurt sur 'Timeout waiting for event "download"'
    (cf. movements.py:201-205) — et sans repli possible.
    """
    global _LAST_REFRESH
    if _LAST_REFRESH is not None and (time.monotonic() - _LAST_REFRESH) < _REFRESH_TTL:
        return True
    _LAST_REFRESH = time.monotonic()
    ok = True
    for label, fn in (("mouvements", lambda: mv.refresh_movements(lookback_days=lookback_days)),
                      ("remises", mv.refresh_remises)):
        try:
            fn()
        except Exception as e:
            # Le kill du scheduler (JobTimeoutException) N'EST PAS un incident du service : le
            # ravaler ici laissait le job continuer dans un worker deja condamne (constate en
            # prod le 2026-08-21 : « refresh mouvements echoue | Task exceeded maximum timeout »
            # puis import a 13:06 dans un horse mort). On le laisse remonter pour que rq
            # constate l'echec proprement.
            if _est_kill_scheduler(e):
                raise
            ok = False
            if diagnostics is not None:
                diagnostics.append({"type": "refresh", "source": label, "reason": str(e)[:160]})
            frappe.log_error(f"bank_retenue_sync : refresh {label} echoue\n{e}", "brs refresh")
    return ok


def _est_kill_scheduler(e: Exception) -> bool:
    """True si l'exception est le timeout d'un job rq (kill), pas une erreur applicative."""
    try:
        from rq.timeouts import BaseTimeoutException
        return isinstance(e, BaseTimeoutException)
    except ImportError:
        return False


def _mmyyyy(period: str) -> str:
    """'2026-06' -> '06-2026'."""
    y, m = period.split("-")
    return f"{m}-{y}"


def _prev_month(period_or_date):
    """'YYYY-MM' -> mois precedent 'YYYY-MM'."""
    y, m = int(period_or_date[:4]), int(period_or_date[5:7])
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


# ============================== EMAIL-DRIVEN ==============================

def process_total(limit=None, insert: bool = True, depuis: str = None):
    """`insert=False` : essai a blanc, aucune ecriture creee (meme contrat que les autres flux).

    Utile avant un premier vrai run : les libelles historiques varient (« Fac TOTAL au »,
    « Facture TOTAL au ») alors que la cle d'idempotence est une egalite stricte, donc l'essai a
    blanc est le seul moyen de voir ce qui serait recree en double avant de l'ecrire.
    """
    out = []
    depuis = depuis or _periode_debut_gestion()
    for msg in mail_config.fetch("total_invoice", limit=limit):
        zip_att = mail_config.attachment_of("total_invoice", msg)
        if not zip_att:
            continue
        try:
            files = extract_invoice_files(zip_att[1])
            if not files["xlsx"]:
                continue
            inv = parse_invoice_xlsx(files["xlsx"][1])
            if not inv.period:
                continue
            if _hors_perimetre(inv.period, depuis):
                out.append({"flux": "total", "periode": inv.period, "status": "hors perimetre"})
                continue
            cheque = f"Facture Total {_mmyyyy(inv.period)}"
            deja = _exists(cheque) and cheque or _deja_comptabilise(
                inv.period, journal.TOTAL_PAYMENT_ACCOUNT, "credit", marqueur="TOTAL")
            if deja:
                out.append({"flux": "total", "ref": cheque, "status": "skipped", "je": deja})
                continue
            je = journal.create_total_journal_entry(
                inv, pdf=files["pdf"], insert=insert,
                email_date=mail_config.message_date(msg))
            out.append({"flux": "total", "ref": cheque, "status": "created",
                        "je": je.name if insert else "(dry-run)",
                        "montant": _total_de(je), "date": str(je.posting_date)})
        except Exception as e:
            out.append({"flux": "total", "subject": msg["subject"][:40], "status": "error", "error": str(e)[:120]})
    return out


def process_aramex(limit=None, insert: bool = True, depuis: str = None):
    """`insert=False` : essai a blanc. Attention, l'extraction OpenAI du PDF a lieu QUAND MEME —
    elle precede la decision de creer, donc un essai a blanc coute autant qu'un vrai run."""
    out = []
    depuis = depuis or _periode_debut_gestion()
    for msg in mail_config.fetch("aramex_invoice", limit=limit):
        pdf = mail_config.attachment_of("aramex_invoice", msg)
        if not pdf:
            continue
        # Filtre AVANT l'extraction : c'est elle qui coute un appel OpenAI par facture. La periode
        # n'est connue qu'apres extraction, mais la date de RECEPTION suffit a ecarter le vieux
        # (une facture de periode M arrive en M+1, donc jamais avant le 1er du mois M).
        recu = mail_config.message_date(msg)
        if depuis and recu and str(recu)[:7] < depuis:
            out.append({"flux": "aramex", "recu": str(recu), "status": "hors perimetre"})
            continue
        try:
            data = extract_invoice(pdf[1], extra_hint="Facture transport Aramex Tunisie, TND, TVA 7%, timbre.")
            period = (data.get("invoice_date") or "")[:7]
            if not re.match(r"\d{4}-\d{2}", period or ""):
                continue
            if _hors_perimetre(period, depuis):
                out.append({"flux": "aramex", "periode": period, "status": "hors perimetre"})
                continue
            cheque = f"Facture Aramex {_mmyyyy(period)}"
            deja = _exists(cheque) and cheque or _deja_comptabilise(
                period, journal.ARAMEX_CHARGE_ACCOUNT, "debit", marqueur="ARAMEX")
            if deja:
                out.append({"flux": "aramex", "ref": cheque, "status": "skipped", "je": deja})
                continue
            je = journal.create_aramex_journal_entry(
                data, pdf=pdf, insert=insert, email_date=mail_config.message_date(msg))
            out.append({"flux": "aramex", "ref": cheque, "status": "created",
                        "je": je.name if insert else "(dry-run)",
                        "montant": _total_de(je), "date": str(je.posting_date)})
        except Exception as e:
            out.append({"flux": "aramex", "subject": msg["subject"][:40], "status": "error", "error": str(e)[:120]})
    return out


def process_honoraire(limit=None, depuis: str = None):
    out = []
    depuis = depuis or _periode_debut_gestion()
    seen = set()
    src = mail_config.get_source("comptable_honoraire")
    # `pj_*` et non `ext` : plus bas, `ext = extract_honoraire(data)` reutilise ce nom.
    pj_ext = (src.get("extension") or ".pdf").lower()
    pj_motif = (src.get("motif_nom_piece_jointe") or "honoraire").lower()
    for msg in mail_config.fetch("comptable_honoraire", limit=limit):
        for name, data in msg["attachments"]:
            low = name.lower()
            if not (low.endswith(pj_ext) and pj_motif in low):
                continue
            # periode = 'MM YYYY' dans le nom de fichier -> pre-check idempotence sans OpenAI
            mo = re.search(r"(\d{2})\s+(\d{4})", name)
            if not mo:
                continue
            period = f"{mo.group(2)}-{mo.group(1)}"
            if _hors_perimetre(period, depuis):
                out.append({"flux": "honoraire", "periode": period, "status": "hors perimetre"})
                continue
            if period in seen:
                continue
            seen.add(period)
            cheque = f"Note d'honoraire comptable {_mmyyyy(period)}"
            if _exists(cheque):
                out.append({"flux": "honoraire", "ref": cheque, "status": "skipped"})
                continue
            try:
                ext = extract_honoraire(data)
                je = journal.create_honoraire_journal_entry(ext, pdf=(name, data), insert=True)
                out.append({"flux": "honoraire", "ref": je.cheque_no, "status": "created", "je": je.name})
            except Exception as e:
                out.append({"flux": "honoraire", "note": name, "status": "error", "error": str(e)[:120]})
    return out


# =============================== BANK-DRIVEN ===============================

def _find_decl_pdf(period, comptable_msgs):
    """Trouve le PDF de declaration du mois `period` (piece justificative du prelevement)."""
    src = mail_config.get_source("comptable_declaration")
    motif = (src.get("motif_nom_piece_jointe") or "decl.ste").lower()
    ext = (src.get("extension") or ".pdf").lower()
    token = f"{period[5:7]} {period[:4]}"     # '06 2026'
    for msg in comptable_msgs:
        for name, data in msg["attachments"]:
            low = name.lower()
            if low.startswith(motif) and low.endswith(ext) and token in low:
                return (name, data)
    return None


def process_declarations(insert: bool = True, movements: list = None):
    """Declencheur = le PRELEVEMENT en banque ; la creation exige la VERIFICATION par l'email du
    comptable (le PDF donne la ventilation, et son total doit egaler le debit au centime).

    La banque est lue dans le REGISTRE et non dans le dernier export : ce dernier est ecrase a
    chaque nouvel export, donc un prelevement anterieur a la derniere fenetre y est invisible.
    """
    from bank_retenue_sync.bank import registry

    out = []
    movements = _movements_geres(
        movements if movements is not None else registry.registry_as_movements())
    comptable_msgs = mail_config.fetch("comptable_declaration")
    for mov in find_debits_by_keywords(["PRELEVEMENT", "FINANCES"], movements=movements):
        period = _prev_month(str(mov["date"]))            # decl. payee le mois suivant
        cheque = f"Declaration comptable {_mmyyyy(period)}"
        deja = (cheque if _exists(cheque) else None) or _reference_deja_citee(mov.get("reference"))
        if deja:
            out.append({"flux": "declaration", "ref": cheque, "status": "skipped", "je": deja})
            continue
        pdf = _find_decl_pdf(period, comptable_msgs)
        if not pdf:
            out.append({"flux": "declaration", "ref": cheque, "status": "no_email_verif"})
            continue
        try:
            ext = extract_declaration(pdf[1])
            m = journal.compute_declaration_lines(ext)
            if abs(m["total"] - float(mov["debit"])) > 0.01:
                out.append({"flux": "declaration", "ref": cheque, "status": "amount_mismatch",
                            "decl_total": m["total"], "bank": mov["debit"]})
                continue
            decl, bal = journal.create_declaration_with_balancer(
                ext, bank_date=mov["date"], bank_reference=mov["reference"], pdf=pdf,
                insert=insert)
            out.append({"flux": "declaration", "ref": cheque, "status": "created",
                        "je": decl.name if insert else "(dry-run)",
                        "balancer": (bal.name if insert else "(dry-run)") if bal else None,
                        "total": m["total"], "date": str(mov["date"])})
        except Exception as e:
            out.append({"flux": "declaration", "ref": cheque, "status": "error", "error": str(e)[:120]})
    return out


def process_cnss(insert: bool = True, movements: list = None):
    """Meme principe que la declaration : declencheur bancaire, verification par email.
    Lit le REGISTRE (cf. `process_declarations`)."""
    from bank_retenue_sync.bank import registry

    out = []
    movements = _movements_geres(
        movements if movements is not None else registry.registry_as_movements())
    comptable_msgs = mail_config.fetch("comptable_cnss")
    for mov in find_debits_by_keywords(["PRELEVEMENT", "CNSS"], movements=movements):
        quarter = journal.cnss_quarter_label(mov["date"])
        cheque = f"CNSS {quarter}"
        deja = (cheque if _exists(cheque) else None) or _reference_deja_citee(mov.get("reference"))
        if deja:
            out.append({"flux": "cnss", "ref": cheque, "status": "skipped", "je": deja})
            continue
        token = journal.cnss_quarter_file_token(mov["date"])     # '2TR2026'
        atts, verif = None, None
        for msg in comptable_msgs:
            pdfs = [(n, d) for n, d in msg["attachments"] if n.lower().endswith(".pdf")]
            if any(token.lower() in n.lower().replace(" ", "") for n, _ in pdfs):
                atts, verif = pdfs, msg["subject"]
                break
        if not verif:
            out.append({"flux": "cnss", "ref": cheque, "status": "no_email_verif"})
            continue
        try:
            je = journal.create_cnss_journal_entry(mov, attachments=atts, insert=insert)
            out.append({"flux": "cnss", "ref": cheque, "status": "created",
                        "je": je.name if insert else "(dry-run)", "date": str(mov["date"])})
        except Exception as e:
            out.append({"flux": "cnss", "ref": cheque, "status": "error", "error": str(e)[:120]})
    return out


# =========================== ENCAISSEMENTS RECUS ===========================

def process_encaissements(insert=True, refresh=True, use_ai=True, movements=None):
    """Rapprochement des ENCAISSEMENTS recus (cote CREDIT) -> brouillon Encaissement Paiement.
    Reprend la logique de matching de outil-facturation-erpnext (cle = reference/n° d'operation),
    mais avec l'acces banque automatique tej-bank-service et la SAISIE (creation du brouillon).

    - cheques  : credit 'ENC CHEQ <bon>' -> remise PDF (OpenAI) -> PE en attente 'Chèques - A&S'.
    - traites  : credit 'ENCAISSEMENT EFFET <n°>' -> PE en attente 'Traite Bancaire - A&S'.
    - aramex   : advice email (net) -> credit bancaire -> PE en attente 'Livraison Aramex - A&S'.
    - virements: credit 'VIR ... <payeur>' -> client identifie -> dettes 'Dettes - A&S' soldees.

    Idempotent : ne reprend que les PE non encore encaissees (pending.already_encaissed_refs) ;
    un run a vide ne cree aucun document. Brouillon : l'utilisateur revoit puis soumet (le server
    script customization_app compense compte d'attente -> banque).

    `refresh=True` declenche d'abord un nouvel export bancaire + la sync des bordereaux : sans ca on
    travaille sur le dernier fichier exporte, qui peut avoir des semaines de retard. Le resultat
    porte `bank_data_asof` / `bank_stale` pour qu'un run a vide sur donnees perimees se signale.

    `use_ai=False` coupe l'appel OpenAI d'identification des payeurs : le flux virement se limite
    alors aux alias appris et au score fuzzy (aucun cout, moins de couverture)."""
    out = {"cheques": 0, "traites": 0, "aramex": 0, "virements": 0, "encaissement": None,
           "bank_data_asof": None, "bank_stale": True, "diagnostics": []}
    if refresh:
        _ensure_fresh_bank_data(out["diagnostics"])
    # Source par defaut : le REGISTRE, et non `/export/latest`. Le dernier export ne couvre que sa
    # propre fenetre, alors que le registre accumule tout ce qui a ete vu — un depot d'il y a trois
    # mois reste rapprochable. `movements=` permet d'injecter une liste (tests, rejeu cible).
    if movements is None:
        from bank_retenue_sync.bank import registry as _reg
        movements = _movements_geres(_reg.registry_as_movements() or fetch_latest_movements())
    asof = mv.movements_asof(movements)
    out["bank_data_asof"] = asof.isoformat() if asof else None
    out["bank_stale"] = mv.is_stale(movements)
    if out["bank_stale"]:
        out["diagnostics"].append({
            "type": "stale", "asof": out["bank_data_asof"],
            "reason": "export bancaire perime : les depots recents ne peuvent pas etre apparies"})
    exclude = pending.already_encaissed_refs()
    pend_chq = pending.get_pending_cheques(exclude)
    pend_tra = pending.get_pending_traites(exclude)
    pend_ara = pending.get_pending_aramex(exclude)
    # Le vivier des dettes reunit DEUX comptes d'attente : 'Dettes - A&S' et les reliquats sans
    # numero de suivi restes sur 'Livraison Aramex - A&S' apres une remise Aramex partielle. Ces
    # derniers n'etaient atteignables par aucun flux (cf. get_pending_dettes_aramex).
    pend_det = pending.get_pending_dettes(exclude) + pending.get_pending_dettes_aramex(exclude)

    # Payment Advice Aramex (email, piece jointe Excel .xls) -> net a rapprocher.
    # Source isolee : un email indisponible (IMAP down, cle de chiffrement...) ne doit PAS
    # bloquer les flux cheques/traites (qui ne dependent pas de l'email).
    # On cherche par EXPEDITEUR seul (pas de filtre sujet, formats variables) et on retient les
    # pieces jointes Excel (.xls) = les Payment Advice (les emails E-INV sont des PDF de facture).
    advices = []
    try:
        for msg in mail_config.fetch("aramex_payment_advice"):
            att = mail_config.attachment_of("aramex_payment_advice", msg)
            if not att:
                continue
            try:
                advices.append(parse_advice(att[1]))
            except Exception as e:
                out["diagnostics"].append({"type": "aramex", "reason": f"advice illisible: {str(e)[:80]}"})
    except Exception as e:
        out["diagnostics"].append({"type": "aramex", "reason": f"email advice indisponible: {str(e)[:80]}"})

    # Cles bancaires deja encaissees, lues UNE fois : ecarte les depots de la fenetre d'export
    # deja traites (sinon on repaie une extraction OpenAI par bordereau a chaque run).
    consumed = pending.consumed_bank_keys()
    chq_rows, d1 = matching.match_cheques(movements, pend_chq, consumed=consumed["cheque"])
    tra_rows, d2 = matching.match_traites(movements, pend_tra, consumed=consumed["traite"])
    ara_rows, d3 = matching.match_aramex(movements, pend_ara, advices, consumed=consumed["aramex"])
    # Cheques encaisses a l'unite ('Encaiss cheque preavise <n°>') : meme compte d'attente que les
    # remises, donc memes PE en attente et meme table de destination.
    pre_rows, d5 = matching.match_cheques_preavises(movements, pend_chq, consumed=consumed["cheque"])
    chq_rows = chq_rows + pre_rows

    # Virements clients : l'identification du payeur passe par les alias appris, puis le fuzzy,
    # puis OpenAI en dernier recours seulement (cf. encaissement.party.identify).
    # Alias appris sur les seuls credits de virement : un libelle 'ENCAISSEMENT EFFET <n°>' est
    # certes rattache a un client, mais il ne se representera jamais comme libelle de virement.
    virements = [m for m in movements if matching.is_virement_credit(m)]
    aliases = dict(party.learned_aliases(virements))
    aliases.update(party.manual_aliases())
    booked = pending.bank_refs_already_booked([m.get("reference") for m in virements])
    resolver = party_match.resolver(out["diagnostics"]) if use_ai else None
    vir_lots, d4 = matching.match_virements(movements, pend_det, consumed=consumed["virement"],
                                            booked=booked, ai_resolver=resolver, aliases=aliases)
    out["diagnostics"] += d1 + d2 + d3 + d4 + d5

    doc = builder.build_encaissement(chq_rows, tra_rows, ara_rows, vir_lots, insert=insert)
    if doc:
        out.update(cheques=len(chq_rows), traites=len(tra_rows), aramex=len(ara_rows),
                   virements=len(vir_lots),
                   encaissement=(doc.name if insert else "(dry-run)"))
        if insert:
            # Les ecarts Aramex (frais, toleres, deltas, sans piece) emis par match_aramex sont
            # rattaches au brouillon : les BLOQUANTS empechent sa soumission tant qu'un humain
            # n'a pas resolu (cf. encaissement/ecarts.py, decision utilisateur 2026-08-18).
            from bank_retenue_sync.encaissement import ecarts as _ecarts
            out["ecarts"] = _ecarts.persister(doc.name, out["diagnostics"])
    frappe.db.commit()
    return out


# ================================ ENTREES ================================

@frappe.whitelist()
def run_email_ingestion():
    """Total + Aramex + Note d'honoraire (declencheur email)."""
    frappe.only_for("System Manager")
    res = process_total() + process_aramex() + process_honoraire()
    frappe.db.commit()
    return res


@frappe.whitelist()
def run_bank_ingestion(refresh=True):
    """Declaration + CNSS (declencheur banque)."""
    frappe.only_for("System Manager")
    diag = []
    if refresh:
        _ensure_fresh_bank_data(diag)
    res = process_declarations() + process_cnss()
    frappe.db.commit()
    # meme forme que les autres lignes du rapport, pour ne pas casser les lecteurs du resultat
    return res + [{"flux": "refresh", "status": "error", "source": d["source"], "error": d["reason"]}
                  for d in diag]


@frappe.whitelist()
def run_encaissements(refresh=True, use_ai=True):
    """Rapprochement + saisie des encaissements recus en brouillon
    (cheques / traites / Aramex / virements clients)."""
    frappe.only_for("System Manager")
    return process_encaissements(refresh=refresh, use_ai=frappe.utils.cint(use_ai))


def process_versements_especes(insert=True, annotate=True, refresh=False):
    """Depots d'especes en banque (caisse Sabah -> Zitouna) : verifie que chaque credit
    'VERSEMENT ESPECES' a bien son ecriture de journal, complete la reference bancaire manquante,
    et cree en BROUILLON celles qui n'existent pas.

    `annotate=False` : mode constat, aucune ecriture existante n'est touchee."""
    from bank_retenue_sync.encaissement import especes
    out = {"reference": 0, "annotation": 0, "creation": 0, "ecritures": [],
           "erreurs": [], "diagnostics": []}
    if refresh:
        _ensure_fresh_bank_data(out["diagnostics"])
    # Meme source que les encaissements : le REGISTRE, pas `/export/latest` — le dernier export
    # ne couvre que sa fenetre, alors qu'un depot plus ancien reste rapprochable. Idempotent de
    # bout en bout (reference deja portee -> `reference`, ecriture existante -> au pire une
    # annotation deja presente ignoree), donc rejouable a chaque verification.
    from bank_retenue_sync.bank import registry as _reg
    movements = _movements_geres(_reg.registry_as_movements() or fetch_latest_movements())
    depots = [m for m in movements if especes.is_versement_espece(m)]
    # Un 'VERSEMENT ESPECES' peut aussi etre un client reglant en liquide au guichet : dans ce cas
    # une Payment Entry porte deja la reference, et il n'y a pas de mouvement de caisse a saisir.
    booked = pending.bank_refs_already_booked([m.get("reference") for m in depots])
    actions, diag = especes.match_versements(depots, booked=booked)
    out["diagnostics"] += diag
    out.update({k: v for k, v in especes.apply_actions(
        actions, insert=insert, annotate=annotate).items()})
    frappe.db.commit()
    return out


@frappe.whitelist()
def run_versements_especes(annotate=True):
    """Rapprochement des versements d'especes : constat + completion des references + creation
    des ecritures manquantes en brouillon."""
    frappe.only_for("System Manager")
    return process_versements_especes(annotate=frappe.utils.cint(annotate))


@frappe.whitelist()
def run_audit(cheques=False):
    """Rapport des FAUSSES/ABSENTES saisies : confronte le detail recu (advice Aramex, et
    bordereaux de remise si cheques=True) aux Payment Entry ERPNext. Ne cree/modifie rien.
    `cheques=True` declenche l'extraction OpenAI des remises (cout)."""
    frappe.only_for("System Manager")
    from bank_retenue_sync.encaissement import audit
    movements = fetch_latest_movements()
    advices = []
    try:
        for msg in mail_config.fetch("aramex_payment_advice"):
            att = mail_config.attachment_of("aramex_payment_advice", msg)
            if att:
                advices.append(parse_advice(att[1]))
    except Exception:
        pass
    rep = {"aramex": audit.audit_aramex(movements, advices)}
    if cheques:
        rep["cheques"] = audit.audit_cheques(movements)
    return rep


@frappe.whitelist()
def run_identification(refresh=False, date_from=None, date_to=None):
    """Identification bancaire : alimente le registre des mouvements et les classe.

    NE CREE AUCUNE ECRITURE. C'est la tache d'inventaire qui repond a « qu'est-ce qui est
    rapproche, qu'est-ce qui reste » — le contenu de la page Identification bancaire.

    `refresh=False` par defaut : sans nouvel export, on reclasse le registre existant, ce qui est
    rapide et sans dependance au service.
    """
    frappe.only_for("System Manager")
    from bank_retenue_sync.bank import classify, registry

    out = {"refresh": None, "crees": 0, "revus": 0}
    if frappe.utils.cint(refresh):
        diag = []
        _ensure_fresh_bank_data(diag)
        out["refresh"] = diag or "ok"
        try:
            out.update(registry.upsert_movements(fetch_latest_movements()))
        except Exception as e:
            out["refresh"] = f"export illisible : {str(e)[:120]}"
    out.update(classify.run(date_from=date_from, date_to=date_to, persist=True))
    return out


@frappe.whitelist()
def run_depenses_recurrentes(insert=True, only=None):
    """Depenses parametrees dans les Settings (salaires, loyer, recharge de carte, echeances de
    pret) declenchees par leur apparition au releve. Ecritures en BROUILLON.

    Lit le REGISTRE et non le service : la tache reste utilisable sans tej-bank-service joignable.
    """
    frappe.only_for("System Manager")
    from bank_retenue_sync.bank import classify, registry
    from bank_retenue_sync.expenses import engine

    movements = _movements_geres(registry.registry_as_movements())
    context = classify.build_context(movements)
    res = engine.process_all_rules(movements, context=context,
                                   insert=frappe.utils.cint(insert), only=only)
    frappe.db.commit()
    return res


@frappe.whitelist()
def run_audit_depenses(date_from=None, date_to=None):
    """Verification des depenses : quels debits bancaires n'ont aucune ecriture en face.
    Ne cree ni ne modifie rien."""
    frappe.only_for("System Manager")
    from bank_retenue_sync.bank import classify, registry
    from bank_retenue_sync.expenses import audit

    movements = registry.registry_as_movements(date_from=date_from, date_to=date_to)
    rapport = audit.audit_depenses(movements, context=classify.build_context(movements))
    return {"resume": audit.resume(rapport), "detail": rapport}


@frappe.whitelist()
def _auto_submit_encaissement(resultat):
    """Soumission automatique du brouillon cree par la verification (decision utilisateur
    2026-08-18) : SEULEMENT si l'option `encaissement_auto_submit` des Settings est cochee ET
    qu'aucun ecart bloquant ne reste a traiter. Sinon le brouillon attend l'humain — et le hook
    before_submit (encaissement/ecarts.py) re-verifie de toute facon au moment de soumettre."""
    nom = (resultat or {}).get("encaissement")
    if not nom or nom == "(dry-run)":
        return None
    if not frappe.utils.cint(frappe.db.get_single_value(
            "Bank Retenue Sync Settings", "encaissement_auto_submit")):
        return "option désactivée : soumission manuelle"
    bloquants = frappe.db.count("BRS Ecart Encaissement",
                                {"encaissement": nom, "bloquant": 1, "statut": "À traiter"})
    if bloquants:
        return "bloquée : {0} écart(s) à résoudre".format(bloquants)
    frappe.get_doc("Encaissement Paiement", nom).submit()
    return "soumis automatiquement"


def run_verification_bancaire(capture_solde=True, ecritures=True):
    """UN passage de verification bancaire, tel qu'il tourne cinq fois par jour.

    L'ENCHAINEMENT, ET POURQUOI IL EST DANS CET ORDRE
    -------------------------------------------------
      1. **releve** : nouvel export, insere au registre (`upsert_movements` est idempotent, un
         passage sans nouveaute ne cree rien) ;
      2. **solde** : capture du portail, archivee — c'est elle qui donne l'ecart du jour, et elle
         n'a de sens qu'apres l'import, pour etre comparee au meme instant ;
      3. **identification** : classification de TOUT le registre, pas seulement des nouveautes —
         une piece saisie entre deux passages rapproche des mouvements deja connus ;
      4. **creations declenchees par le releve** (seulement si du nouveau a ete importe) :
         brouillons d'encaissement (soumission humaine), versements d'especes, declaration
         fiscale et CNSS (avec verification par l'email du comptable), depenses recurrentes
         actives, echeances de contrats, reglement des dettes Aramex/honoraire au virement
         emis — puis une reclassification pour lier les pieces creees ;
      5. **ecritures** : les frais bancaires et les ecarts de paiement du mois sont recalcules
         depuis zero et l'ecriture mensuelle est refaite si le total a bouge.

    L'etape 4 ne part QUE si de nouveaux mouvements sont apparus : sans releve neuf, il n'y a par
    construction aucun frais nouveau, et refaire l'ecriture serait une annulation pour rien.

    Rien n'est cree pour un frais isole : l'ecriture est MENSUELLE et cumulative, recalculee a
    chaque passage. Rejouer la tache dix fois dans la journee laisse donc une seule ecriture,
    toujours a jour — c'est la propriete qui rend sept passages quotidiens sans danger.
    """
    frappe.only_for("System Manager")
    from bank_retenue_sync.bank import registry, solde as S
    from bank_retenue_sync.expenses import fees, ordres, reglement

    debut_run = frappe.utils.now_datetime()
    out = run_identification(refresh=True)

    if frappe.utils.cint(capture_solde):
        try:
            out["solde"] = S.capturer_solde()
        except Exception as e:
            if _est_kill_scheduler(e):
                raise
            # Un solde illisible ne doit jamais faire perdre l'import ni la classification :
            # ce sont eux qui portent le travail de la journee. Et capture en panne n'est pas
            # solde perdu : si l'export des mouvements est passe, le solde du jour se DEDUIT de
            # la derniere capture + le net du registre depuis sa derniere operation.
            derive = None
            try:
                derive = S.solde_derive()
            except Exception:
                pass
            out["solde"] = {"erreur": str(e)[:200], "derive": derive}

    # Decision utilisateur 2026-08-18 : la verification enchaine elle-meme les creations
    # declenchees par le releve — brouillons d'ENCAISSEMENT (cheques/traites/aramex/virements),
    # versements d'especes, depenses recurrentes actives, echeances de contrats.
    # ⚠️ SANS CONDITION sur `crees` (correctif du 2026-08-21). L'ancienne garde `if out.get
    # ("crees")` supposait que tout mouvement nouveau arrivait PAR CE RUN. Faux en pratique :
    # un rafraichissement manuel depuis la page (qui classe mais ne cree jamais) ou un run tue
    # en plein vol laisse des mouvements au registre qu'aucun passage suivant ne traite — cas
    # reel du virement SPH Khamsa (1 812,700 DT, FT26232TQ2GZ) importe le 20/08 a 20h08 et reste
    # orphelin alors que la dette existait. Le registre fait foi : chaque passage tente les
    # creations, et l'idempotence (cles bancaires consommees — brouillons compris —, idempotence
    # par reference, appariement d'echeances) garantit qu'un passage sans nouveaute ne cree rien.
    # La SOUMISSION des encaissements reste humaine — before_submit bloque tant que des ecarts
    # sont a resoudre. `refresh=False` partout : le refresh vient d'etre tente ci-dessus, et son
    # echec n'est pas une raison de laisser dormir le registre.
    # Chaque etape est isolee : son echec n'empeche ni les suivantes ni l'ecriture de frais.
    out["encaissements"] = out["depenses"] = out["contrats"] = out["especes"] = None
    out["declarations"] = out["cnss"] = out["reglements"] = None
    if frappe.db.count("BRS Bank Movement"):     # registre vide = rien a creer, pas d'exception
        try:
            out["encaissements"] = process_encaissements(insert=True, refresh=False)
            # Decision utilisateur 2026-08-18 (2e volet) : si l'option « validation automatique »
            # est cochee, un brouillon SANS ecart bloquant est soumis dans la foulee ; avec
            # ecarts, il reste en brouillon jusqu'a resolution humaine.
            out["encaissements"]["soumission"] = _auto_submit_encaissement(out["encaissements"])
        except Exception as e:
            out["encaissements"] = {"erreur": str(e)[:200]}
        # Versements d'especes (decision utilisateur 2026-08-19) : meme declencheur que les
        # encaissements. L'ecriture creee suit `auto_submit_journal_entries` (cf. especes.py) ;
        # les cas douteux (ambiguite, reglement client au guichet) ne creent jamais rien.
        # Declaration fiscale et CNSS (decision utilisateur 2026-08-19) : leur declencheur est
        # le PRELEVEMENT au registre, ils appartiennent donc a cette chaine — ils n'etaient
        # atteignables que par run_all() (manuel). La creation reste conditionnee au PDF du
        # comptable (`no_email_verif` sinon) et, pour la declaration, a l'egalite au centime
        # avec le debit bancaire ; un email indisponible fait echouer la seule etape concernee.
        for cle, fn in (("especes", lambda: process_versements_especes(
                            insert=True, annotate=True, refresh=False)),
                        ("declarations", lambda: process_declarations(insert=True)),
                        ("cnss", lambda: process_cnss(insert=True)),
                        ("depenses", lambda: run_depenses_recurrentes(insert=True)),
                        ("contrats", lambda: run_contrats(insert=True)),
                        # CONFIRMATION DES ORDRES DE PAIEMENT (decision utilisateur
                        # 2026-08-31) : des que la charge SORT au releve, l'ordre passe a
                        # « Vire » et son ecriture ANTICIPEE — posee sur le compte d'attente —
                        # est recreee sur la BANQUE avec la reference du virement. Attendre
                        # 17h30 laissait les salaires « orphelins » toute la journee alors que
                        # l'ecriture existait depuis le 29.
                        # ⚠️ AVANT « reglements » : ici l'ordre porte le lien vers l'ecriture ET
                        # vers le mouvement, la ou `reglement` n'apparie que par le MONTANT. Le
                        # laisser passer en premier eviterait qu'un debit de salaire solde par
                        # erreur une dette fournisseur du meme montant.
                        ("ordres", lambda: ordres.confirmer_par_banque(
                            _movements_geres(registry.registry_as_movements()))),
                        # Reglement des dettes Aramex / honoraire (decision utilisateur
                        # 2026-08-19) : quand le virement emis parait au releve, l'ecriture
                        # d'attente est remplacee par son equivalent sur la banque. En DERNIER :
                        # il lit le registre (a jour) et ses ambiguites sont refusees, donc
                        # rejouable a chaque passage. N'etait atteignable que par run_all().
                        ("reglements", lambda: reglement.process_reglements(
                            _movements_geres(registry.registry_as_movements()), insert=True))):
            try:
                out[cle] = fn()
            except Exception as e:
                out[cle] = {"erreur": str(e)[:200]}
        # Les pieces creees a l'instant doivent apparaitre LIEES sur la page identification
        # (document, montant comptabilise, mention brouillon) sans attendre le passage suivant :
        # une passe de reclassification, sans nouvel export.
        try:
            reclass = run_identification(refresh=False)
            out["reclassification"] = {k: reclass.get(k) for k in ("revus",)}
        except Exception as e:
            out["reclassification"] = {"erreur": str(e)[:200]}

    # Decision utilisateur 2026-08-21 : un CHARGEMENT de la carte technologique detecte au
    # releve declenche IMMEDIATEMENT la mise a jour de la carte (releve carte + ecritures de
    # ses paiements), au lieu d'attendre le cron dedie de 9h40. La recharge n'arrive jamais par
    # hasard : elle repond a des paiements en attente sur la carte — c'est donc le moment exact
    # ou son releve a du nouveau. `run_cartes` est idempotent (numero de paiement) : au pire le
    # releve carte n'a pas encore bouge et le passage ne cree rien.
    out["carte"] = None
    if _recharge_carte_importee(debut_run):
        try:
            out["carte"] = run_cartes(insert=True, refresh=True)
        except Exception as e:
            if _est_kill_scheduler(e):
                raise
            out["carte"] = {"erreur": str(e)[:200]}

    # Meme logique que l'etape 4 : l'ecriture mensuelle est un CUMUL recalcule depuis le
    # registre et refaite seulement si son total a change — la rejouer sans mouvement neuf ne
    # produit rien. La conditionner a `crees` laissait des frais importes par un autre canal
    # sans ecriture jusqu'au prochain run « chanceux ».
    out["ecritures"] = None
    if frappe.utils.cint(ecritures) and frappe.db.count("BRS Bank Movement"):
        try:
            out["ecritures"] = fees.process_fees(registry.registry_as_movements(), insert=True)
        except Exception as e:
            out["ecritures"] = {"erreur": str(e)[:200]}
        # ⚠️ L'IDENTIFICATION (etape 3) EST PASSEE AVANT CETTE ECRITURE. La page affichait donc
        # l'ecriture de frais telle qu'elle etait AU DEBUT du passage — « Identifie non
        # comptabilise / ecriture en brouillon, a soumettre » alors qu'elle venait d'etre creee
        # et soumise deux lignes plus haut. L'utilisateur voyait ses frais « encore en
        # brouillon » apres identification, et devait attendre le passage suivant (jusqu'a 2 h)
        # pour que la pastille tombe juste (constate le 02/09/2026, ACC-JV-2026-00687 soumise,
        # affichee en brouillon).
        # Meme remede qu'a l'etape 4 : une reclassification apres coup, sans nouvel export.
        # Conditionnee au fait que l'ecriture ait REELLEMENT bouge — un passage qui ne trouve
        # rien de neuf ne paie pas une classification de plus.
        if _ecriture_de_frais_a_bouge(out.get("ecritures")):
            try:
                reclass = run_identification(refresh=False)
                out["reclassification_frais"] = {k: reclass.get(k) for k in ("revus",)}
            except Exception as e:
                out["reclassification_frais"] = {"erreur": str(e)[:200]}

    _marquer_synchro(refresh_ok=_export_mouvements_ok(out.get("refresh")))
    frappe.db.commit()
    return out


# Les seuls etats ou l'ecriture mensuelle a change de forme au grand livre. « inchangee » et
# « vide » ne touchent a rien : reclassifier derriere eux serait du travail pur.
_FRAIS_ETATS_MOUVANTS = ("cree", "remplacee", "soumise")


def _ecriture_de_frais_a_bouge(resultat) -> bool:
    """L'ecriture mensuelle de frais a-t-elle ete creee, refaite ou soumise a l'instant ?

    `process_fees` rend une LISTE (un element par periode traitee) ; un echec rend un dict
    d'erreur. On ne veut declencher la reclassification que sur un vrai changement.
    """
    if isinstance(resultat, dict):
        resultat = [resultat]
    if not isinstance(resultat, list):
        return False
    return any(isinstance(r, dict) and r.get("statut") in _FRAIS_ETATS_MOUVANTS
               for r in resultat)


def _export_mouvements_ok(refresh) -> bool:
    """True si l'export des MOUVEMENTS a reussi — c'est lui que l'alerte surveille.

    `refresh` vaut "ok", une chaine d'erreur ("export illisible : ..."), ou la liste des
    diagnostics de `_ensure_fresh_bank_data`. Un echec des seules REMISES ne compte pas comme
    une panne : les mouvements continuent d'arriver, le registre vit — constate le 2026-08-21,
    ou le scraper de remises etait casse (« Champ identifiant introuvable ») pendant que
    l'export de mouvements passait."""
    if refresh == "ok":
        return True
    if isinstance(refresh, list):
        return not any(d.get("source") == "mouvements" for d in refresh)
    return False


# Au-dela de ce silence (aucune synchro bancaire reussie), on alerte : sept passages par jour,
# 24 h sans succes = au moins cinq echecs consecutifs, ce n'est plus un caprice du portail.
_ALERTE_SYNCHRO_HEURES = 24


def _marquer_synchro(refresh_ok: bool):
    """Trace la derniere synchro bancaire reussie et alerte quand la panne s'installe.

    Constat prod du 2026-08-21 : le login Zitouna echouait depuis le 20/08 au soir et personne
    ne l'a su avant de chercher un virement manquant. La panne se lisait pourtant dans les
    Error Log — mais personne ne les lit tous les jours. D'ou ce marqueur persistant :
      - refresh reussi -> `derniere_synchro_bancaire` = maintenant, drapeau d'alerte remis a 0 ;
      - refresh echoue depuis plus de `_ALERTE_SYNCHRO_HEURES` -> UNE notification Desk aux
        System Managers (le drapeau `alerte_synchro_envoyee` evite de notifier a chaque passage ;
        il se re-arme au premier succes).
    Jamais bloquant : une erreur ici ne doit pas faire echouer la verification elle-meme.
    """
    try:
        if refresh_ok:
            frappe.db.set_single_value("Bank Retenue Sync Settings", {
                "derniere_synchro_bancaire": frappe.utils.now_datetime(),
                "alerte_synchro_envoyee": 0,
            })
            return
        if frappe.utils.cint(frappe.db.get_single_value(
                "Bank Retenue Sync Settings", "alerte_synchro_envoyee")):
            return
        derniere = frappe.db.get_single_value(
            "Bank Retenue Sync Settings", "derniere_synchro_bancaire")
        if derniere and frappe.utils.time_diff_in_hours(
                frappe.utils.now_datetime(), derniere) < _ALERTE_SYNCHRO_HEURES:
            return
        depuis = frappe.utils.format_datetime(derniere) if derniere else "plus de 24 h"
        for user in _system_managers():
            frappe.get_doc({
                "doctype": "Notification Log",
                "for_user": user,
                "type": "Alert",
                "subject": "Synchronisation bancaire en panne",
                "email_content": (
                    "Aucun export bancaire n'a reussi depuis {0}. Le portail ou la session "
                    "Zitouna de tej-bank-service est probablement a verifier (profil a "
                    "re-semer ?). Les rapprochements continuent sur le registre existant, "
                    "mais sans mouvements nouveaux.".format(depuis)),
            }).insert(ignore_permissions=True)
        frappe.db.set_single_value(
            "Bank Retenue Sync Settings", "alerte_synchro_envoyee", 1)
    except Exception as e:
        frappe.log_error(f"marquage synchro bancaire echoue\n{e}", "brs refresh")


def _system_managers() -> list:
    """Utilisateurs actifs porteurs du role System Manager (Administrator exclu : personne ne
    lit ses notifications)."""
    porteurs = set(frappe.get_all("Has Role", filters={"role": "System Manager",
                                                       "parenttype": "User"}, pluck="parent"))
    actifs = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"},
                            pluck="name")
    return [u for u in actifs if u in porteurs and u != "Administrator"]


@frappe.whitelist()
def run_cartes(insert=True, refresh=True, depuis=None):
    """Paiements de la carte technologique : releve de carte -> ecritures.

    Ces depenses ne paraissent JAMAIS au releve bancaire — la carte est une tresorerie parallele,
    alimentee par un virement puis depensee en publicite. Le rapprochement bancaire ne peut donc
    rien en dire : c'est le releve de CARTE, chez tej-bank-service, qui fait foi.

    Reprend a la DERNIERE ecriture portee au compte de la carte, et non depuis l'origine. La date
    n'est qu'un raccourci : l'idempotence reelle tient au numero de reference du paiement, deja
    utilise par les saisies manuelles historiques.
    """
    frappe.only_for("System Manager")
    from bank_retenue_sync.bank import cartes

    out = {"refresh": None}
    if frappe.utils.cint(refresh):
        try:
            out["refresh"] = cartes.refresh_cartes().get("status")
        except Exception as e:
            # Le dernier export reste exploitable : on le dit, on ne s'arrete pas.
            out["refresh"] = "export indisponible : %s" % str(e)[:120]
        try:
            # Le SOLDE aussi : sans capture fraiche, `sync_frais_carte` comparerait le comptable
            # du jour a un solde d'hier et fabriquerait un faux frais (ou en raterait un vrai).
            cartes.refresh_solde()
        except Exception as e:
            out["refresh_solde"] = "capture indisponible : %s" % str(e)[:120]
    # `depuis` non renseigne -> reprise a la derniere ecriture ; le passer explicitement a vide
    # relache la borne pour un rattrapage, l'idempotence par numero restant le garde-fou.
    out["ecritures"] = cartes.process_cartes(
        insert=frappe.utils.cint(insert),
        **({"depuis": depuis} if depuis is not None else {}))
    # La regularisation vient APRES les paiements : tant qu'une operation du releve n'est pas
    # comptabilisee, l'ecart n'est pas un frais, c'est cette operation.
    try:
        out["frais"] = cartes.sync_frais_carte(insert=frappe.utils.cint(insert))
    except Exception as e:
        out["frais"] = {"statut": "erreur", "detail": str(e)[:160]}
    # L'alerte vient APRES les ecritures : le solde reel est celui de la carte, mais on veut que
    # les paiements du jour soient deja comptabilises quand l'utilisateur ouvre la notification —
    # sinon il lit une alerte sans pouvoir en verifier la cause dans ses comptes.
    #
    # Un essai a blanc (`insert=0`) n'alerte personne : une notification est un effet de bord
    # visible, et le bouton « Essai a blanc » du rapport promet le contraire. Elle consommerait en
    # plus l'unique alerte du jour, si bien que la vraie tache de 9h40 se tairait ensuite.
    if frappe.utils.cint(insert):
        try:
            out["alerte"] = cartes.alerte_recharge()
        except Exception as e:
            out["alerte"] = {"statut": "erreur", "detail": str(e)[:160]}
    else:
        out["alerte"] = {"statut": "essai a blanc : aucune alerte posee"}
    frappe.db.commit()
    return out


@frappe.whitelist()
def run_certificats_ras(refresh=0, insert=1, pdf=1, limite_pdf=20, use_ai=0):
    """Certificats de retenue a la source RECUS : portail TEJ -> certificats -> rapprochement.

    Le seul flux ou la piece comptable existe DEJA le plus souvent : la retenue est saisie a la
    main depuis des annees. Ce passage ne la refait pas, il la CONFRONTE au portail et repond a
    deux questions qu'on ne pouvait pas poser : toutes les retenues declarees par nos clients
    sont-elles dans nos comptes, et en avons-nous le justificatif ?

    `insert=0` est un essai a blanc integral : aucun certificat, aucun rapprochement, aucun PDF.
    `refresh=1` demande un nouveau scraping au portail — plusieurs minutes, donc jamais par defaut.
    """
    frappe.only_for("System Manager")
    from bank_retenue_sync.tej import certificats, pdf as pdf_module, rapprochement

    insert = frappe.utils.cint(insert)
    out = {"statut": "ok"}
    if not frappe.db.get_single_value("Bank Retenue Sync Settings", "enabled"):
        return {"statut": "desactive", "detail": "coupe-circuit general"}
    if not frappe.db.get_single_value("Bank Retenue Sync Settings", "enable_tej_sync"):
        # Avant tout appel reseau : un flux desactive ne doit pas solliciter le portail.
        return {"statut": "desactive", "detail": "synchronisation TEJ desactivee"}

    out["ingestion"] = certificats.synchroniser(refresh=frappe.utils.cint(refresh), insert=insert)
    if out["ingestion"].get("statut") == "export inchange" and insert:
        # L'export n'a pas bouge, mais le rapprochement, lui, depend de nos ecritures : une
        # facture saisie hier peut rapprocher un certificat d'avant-hier.
        out["ingestion"]["note"] = "rapprochement rejoue malgre l'export inchange"

    out["rapprochement"] = {k: v for k, v in rapprochement.rapprocher(
        insert=insert, use_ai=frappe.utils.cint(use_ai)).items() if k != "detail"}

    if frappe.utils.cint(pdf):
        try:
            out["pdf"] = {k: v for k, v in pdf_module.traiter(
                limite=frappe.utils.cint(limite_pdf), insert=insert).items() if k != "detail"}
            out["pdf_deplaces"] = pdf_module.deplacer_vers_facture(insert=insert)["deplaces"]
        except Exception as e:
            # Le portail peut refuser un PDF sans que le rapprochement, lui, soit invalide.
            out["pdf"] = {"statut": "erreur", "detail": str(e)[:160]}

    if insert:
        frappe.db.set_single_value("Bank Retenue Sync Settings", "last_tej_sync",
                                   frappe.utils.now_datetime())
        frappe.db.commit()
    return out


@frappe.whitelist()
def run_calendrier(jour=None, rattrapage=False):
    """Depenses a date fixe : salaires (2 jours avant la fin du mois), loyer (le 15, tous les
    deux mois). Cree l'ecriture en BROUILLON et l'ordre de paiement qui l'accompagne.

    Anticipe la banque : c'est `run_ordres()` qui confirmera ensuite que le virement est parti.
    """
    frappe.only_for("System Manager")
    from bank_retenue_sync.expenses import calendrier

    res = calendrier.process_calendrier(jour=jour, rattrapage=frappe.utils.cint(rattrapage))
    frappe.db.commit()
    return res


@frappe.whitelist()
def run_contrats(insert=True):
    """Echeances de pret et de leasing detectees au releve.

    Une ecriture par echeance, a quatre lignes (deux credits bancaires distincts), a l'identique
    des saisies manuelles. Le contrat est identifie par le TOTAL du jour, les deux prets partageant
    le meme libelle bancaire.
    """
    frappe.only_for("System Manager")
    from bank_retenue_sync.bank import classify, registry
    from bank_retenue_sync.expenses import contrats

    movements = _movements_geres(registry.registry_as_movements())
    context = classify.build_context(movements)
    # L'appariement de GROUPE (reference, jour) est l'idempotence des deux flux : il reconnait les
    # saisies manuelles, dont le libelle ne ressemble a aucune cle generee. Sans lui, une reference
    # de contrat citee par toutes les echeances passees ferait passer chaque nouvelle pour faite.
    context.echeances = classify.apparier_echeances(movements, context)
    insert = frappe.utils.cint(insert)
    res = contrats.process_contrats(movements, context=context, insert=insert)
    res += contrats.process_leasings(movements, context=context, insert=insert)
    frappe.db.commit()
    return res


@frappe.whitelist()
def run_ordres():
    """Confronte les ordres de paiement en attente aux debits du releve.

    Ne cree ni ne modifie aucune ecriture : seuls les ordres passent a « Vire ». Un ordre encore
    en attente apres sa date prevue remonte en alerte.
    """
    frappe.only_for("System Manager")
    from bank_retenue_sync.bank import registry
    from bank_retenue_sync.expenses import ordres

    res = ordres.confirmer_par_banque(registry.registry_as_movements())
    res["alertes"] = [dict(o) for o in ordres.alertes()]
    frappe.db.commit()
    return res


@frappe.whitelist()
def run_reglements(insert=True, only=None):
    """Regle les dettes fournisseur restees sur leur compte d'attente : au virement emis du meme
    montant, l'ecriture est annulee, supprimee, et recreee sur la banque avec la reference du
    virement — a DATE DE COMPTABILISATION INCHANGEE (cf. expenses/reglement).
    Couvre les factures Aramex (Crediteurs) et les notes d'honoraire (compte de decouvert).
    """
    frappe.only_for("Accounts Manager")
    from bank_retenue_sync.bank import registry
    from bank_retenue_sync.expenses import reglement

    res = reglement.process_reglements(
        _movements_geres(registry.registry_as_movements()),
        insert=frappe.utils.cint(insert), only=only)
    frappe.db.commit()
    return res


@frappe.whitelist()
def run_ecritures_frais():
    """Ecritures mensuelles cumulatives (frais bancaires + deltas de paiement) recalculees
    depuis le registre. Meme geste que l'etape 5 de la verification bancaire, declenchable a la
    main — la verification ne la fait que si son propre import a rapporte du nouveau."""
    frappe.only_for("System Manager")
    from bank_retenue_sync.bank import registry
    from bank_retenue_sync.expenses import fees
    res = fees.process_fees(registry.registry_as_movements(), insert=True)
    frappe.db.commit()
    return res


@frappe.whitelist()
def run_all():
    return {
        "email": run_email_ingestion(),
        "bank": run_bank_ingestion(),
        "encaissements": run_encaissements(),
        "identification": run_identification(),
        "calendrier": run_calendrier(),
        "contrats": run_contrats(),
        "ordres": run_ordres(),
        # APRES l'identification : le reglement lit le registre, qui doit etre a jour.
        "reglements": run_reglements(),
    }
