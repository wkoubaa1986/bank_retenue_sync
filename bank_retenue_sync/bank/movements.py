"""Recuperation et lecture des mouvements bancaires depuis tej-bank-service.

L'export (XLSX) a les colonnes :
    Date Operation | Date valeur | Operation | Reference | Debit | Credit

Sert a :
  - dater les ecritures de declaration (paiement PRELEVEMENT du fisc) ;
  - rapprocher les virements Aramex (Credit == net du Payment Advice).

Config lue depuis site_config (repli si le DocType Settings n'est pas installe) :
  brs_service_url (defaut http://tej-bank-service:8080), brs_service_token.
"""
from __future__ import annotations

import io
import time
from datetime import date, datetime, timedelta

import frappe
import requests


def _base_url() -> str:
    # Settings DocType prioritaire s'il existe, sinon site_config
    url = None
    if frappe.db and frappe.db.exists("DocType", "Bank Retenue Sync Settings"):
        url = frappe.db.get_single_value("Bank Retenue Sync Settings", "service_url")
    return (url or frappe.conf.get("brs_service_url") or "http://tej-bank-service:8080").rstrip("/")


def _token() -> str:
    if frappe.db and frappe.db.exists("DocType", "Bank Retenue Sync Settings"):
        try:
            doc = frappe.get_cached_doc("Bank Retenue Sync Settings")
            t = doc.get_password("service_token", raise_exception=False)
            if t:
                return t
        except Exception:
            pass
    return frappe.conf.get("brs_service_token")


def _headers() -> dict:
    h = {"Accept": "application/json"}
    tok = _token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _num(v) -> float:
    if v in (None, ""):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


def _to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    # `%d-%m-%Y` EN DERNIER, et c'est deliberé : l'ISO `%Y-%m-%d` doit gagner sur les chaines
    # ambigues du type « 2026-07-15 ». Le format tiret jour-mois vient du portail TEJ, dont
    # l'export des certificats date ses lignes « 08-07-2026 » ; sans lui la date etait perdue
    # en silence (`None`), donc la ligne muette.
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _col(row: dict, *names):
    """Lecture tolerante d'une colonne (accents/variantes)."""
    for n in names:
        if n in row:
            return row[n]
    low = {str(k).lower(): k for k in row}
    for n in names:
        k = low.get(n.lower())
        if k is not None:
            return row[k]
    return None


def fetch_latest_movements() -> list:
    """Retourne la liste des mouvements du dernier export, normalises :
    [{date, date_valeur, operation, reference, debit, credit}, ...]."""
    r = requests.get(_base_url() + "/banque/mouvements/export/latest", headers=_headers(), timeout=60)
    r.raise_for_status()
    return _parse_movements_xlsx(r.content)


def _parse_movements_xlsx(content: bytes) -> list:
    """Normalise un export XLSX de mouvements. Partage entre `/latest` et un fichier nomme."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    out = []
    for row in rows[1:]:
        d = {header[i]: row[i] for i in range(min(len(header), len(row)))}
        if not any(v is not None and str(v).strip() for v in d.values()):
            continue
        out.append({
            "date": _to_date(_col(d, "Date Opération", "Date Operation")),
            "date_valeur": _to_date(_col(d, "Date valeur")),
            "operation": str(_col(d, "Opération", "Operation") or "").strip(),
            "reference": str(_col(d, "Référence", "Reference") or "").strip(),
            "debit": _num(_col(d, "Débit", "Debit")),
            "credit": _num(_col(d, "Crédit", "Credit")),
        })
    return out


def movements_asof(movements: list = None):
    """Date du mouvement le plus recent de l'export (= fraicheur des donnees bancaires), ou None.
    `fetch_latest_movements` ne rend QUE le dernier fichier exporte : sans declenchement d'un
    nouvel export il peut dater de plusieurs semaines, et le rapprochement rend 0 en silence."""
    src = movements if movements is not None else fetch_latest_movements()
    dates = [m["date"] for m in src if m.get("date")]
    return max(dates) if dates else None


def business_days_between(start: date, end: date) -> int:
    """Nombre de jours OUVRES strictement apres `start` et jusqu'a `end` inclus (0 si end <= start).
    Le portail bancaire ne produit rien le week-end : un vendredi reste frais le lundi suivant."""
    if end <= start:
        return 0
    days = 0
    cur = start
    while cur < end:
        cur += timedelta(days=1)
        if cur.weekday() < 5:          # 0-4 = lundi..vendredi
            days += 1
    return days


def is_stale(movements: list = None, max_age_days: int = 2) -> bool:
    """True si l'export bancaire est trop vieux pour un rapprochement du jour (ou vide).

    L'age se compte en jours OUVRES : en calendaire, un export arrete au vendredi paraitrait
    perime des le lundi (3 jours) alors qu'aucun mouvement n'a pu paraitre entre-temps -> tous
    les lundis se signalaient perimes a tort."""
    asof = movements_asof(movements)
    return asof is None or business_days_between(asof, date.today()) > max_age_days


# ==================== Rafraichissement des sources (jobs de scraping) ====================
# tej-bank-service scrape le portail bancaire en tache de fond :
#   POST /jobs/...            -> 202 {job_id, status, poll}
#   GET  /jobs/{job_id}       -> {status, progress:{step,pct}, result:{artifacts,details}, error}
# Statuts terminaux : succeeded / failed / cancelled.

_TERMINAL = ("succeeded", "failed", "cancelled")


def start_job(path: str, body=None) -> str:
    """Declenche un job de scraping et retourne son job_id."""
    r = requests.post(_base_url() + path, headers=_headers(), json=body, timeout=60)
    r.raise_for_status()
    job_id = (r.json() or {}).get("job_id")
    if not job_id:
        raise ValueError(f"{path} : reponse sans job_id ({r.text[:120]})")
    return job_id


def wait_job(job_id: str, timeout: int = 900, interval: int = 5) -> dict:
    """Attend la fin d'un job. Retourne le job (dict) si 'succeeded'.
    Leve sur 'failed'/'cancelled' et sur depassement du timeout (jamais d'attente non bornee)."""
    deadline = time.monotonic() + timeout
    while True:
        r = requests.get(_base_url() + f"/jobs/{job_id}", headers=_headers(), timeout=30)
        r.raise_for_status()
        job = r.json() or {}
        status = job.get("status")
        if status in _TERMINAL:
            if status != "succeeded":
                raise RuntimeError(f"job {job_id} {status} : {job.get('error') or 'sans detail'}")
            return job
        if time.monotonic() >= deadline:
            step = (job.get("progress") or {}).get("step")
            raise TimeoutError(f"job {job_id} toujours '{status}' apres {timeout}s (etape : {step})")
        time.sleep(interval)


def export_movements(lookback_days: int, timeout: int = 900) -> dict:
    """Un export de mouvements sur [today - lookback_days, today], attendu jusqu'a son terme."""
    date_to = date.today()
    date_from = date_to - timedelta(days=lookback_days)
    job_id = start_job("/jobs/banque/mouvements/export", {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        # meme fenetre relancee le meme jour -> le service peut sauter le scraping
        "idempotency_key": f"mvt-{date_from.isoformat()}-{date_to.isoformat()}",
    })
    return wait_job(job_id, timeout=timeout)


# Fenetres d'export, de la plus large a la plus etroite (cf. `refresh_movements`).
# Chaque valeur vaut 7n-1 jours : la plage [today-N, today] fait alors 7n jours PLEINS, donc le
# service la decoupe en tranches de 7 jours SANS residu. Un residu de 1-2 jours peut tomber sur un
# week-end : le portail n'a alors rien a telecharger et le bot Playwright meurt sur
# 'Timeout waiting for event "download"' (observe le 2026-08-02 sur la tranche 04/04 -> 05/04).
_EXPORT_WINDOWS = (118, 55, 27, 13)


# Au-dela de ce retard (en jours), le registre est repute vieux : on retente les fenetres
# larges en premier pour reconstituer l'historique avant les donnees du jour.
_REGISTRE_FRESH_MAX_AGE = 3


def _ordered_windows(last_movement_date=None) -> tuple:
    """Ordre d'essai des fenetres d'export, selon la fraicheur du REGISTRE.

    Tous les flux lisent le registre (`registry_as_movements`), qui CUMULE les exports —
    `/export/latest` n'est plus qu'un repli quand le registre est vide. Une fenetre etroite ne
    fait donc rien disparaitre : elle limite seulement ce que CE run peut apprendre de neuf.

    D'ou l'ordre adaptatif (constat prod du 2026-08-21 : le portail en rade faisait bruler
    ~4 minutes sur 118 j puis 55 j avant d'atteindre la fenetre courte qui, elle, passait — et le
    job planifie mourait avant) :
      - registre frais (retard <= _REGISTRE_FRESH_MAX_AGE j) : 13 j D'ABORD — il ne manque que
        quelques jours, la petite fenetre suffit et reussit plus souvent ;
      - registre vieux ou vide : large d'abord, pour recouvrir le trou.
    """
    if last_movement_date:
        try:
            age = (date.today() - frappe.utils.getdate(last_movement_date)).days
        except Exception:
            age = None
        if age is not None and age <= _REGISTRE_FRESH_MAX_AGE:
            return tuple(sorted(_EXPORT_WINDOWS))
    return _EXPORT_WINDOWS


def _last_registered_date():
    """Date du dernier mouvement au registre, None si registre vide ou table absente."""
    try:
        return frappe.db.sql("select max(date) from `tabBRS Bank Movement`")[0][0]
    except Exception:
        return None


def refresh_movements(lookback_days: int = None, timeout: int = 900) -> dict:
    """Declenche un export des mouvements sur une fenetre glissante et attend sa fin.

    Le portail est capricieux : une tranche sans aucun mouvement fait echouer tout l'export. On
    reessaie donc sur plusieurs fenetres plutot que de ne rien rapporter, dans l'ordre choisi par
    `_ordered_windows` (etroite d'abord si le registre est frais, large d'abord sinon).
    `lookback_days` force une fenetre unique (pas de repli).
    """
    windows = (lookback_days,) if lookback_days else _ordered_windows(_last_registered_date())
    last_error = None
    for days in windows:
        try:
            job = export_movements(days, timeout=timeout)
            job["_lookback_days"] = days
            return job
        except (RuntimeError, TimeoutError) as e:
            last_error = e
            # Le retour a la ligne n'est pas cosmetique : `frappe.log_error` ne permute titre et
            # message que si le titre est multiligne. Sur une seule ligne, ce texte partait dans le
            # champ Data `method` (140 car. max) et la journalisation levait une exception QUI
            # SORTAIT DE CE `except` — les fenetres de repli (55, 27, 13 j) n'etaient jamais
            # essayees et un export recuperable devenait fatal.
            frappe.log_error(f"export mouvements sur {days} j echoue\n{e}", "brs refresh")
    raise RuntimeError(f"aucune fenetre d'export n'a abouti ({windows}) : {last_error}")


def export_range(date_from, date_to, timeout: int = 1800) -> dict:
    """Export sur une plage EXPLICITE (et non 'aujourd'hui moins N jours').

    Le service decoupe la plage en tranches de 7 jours. Une plage dont le nombre de jours INCLUS
    n'est pas un multiple de 7 laisse un residu, qui peut tomber sur un week-end : le portail n'a
    alors rien a telecharger et le bot meurt sur 'Timeout waiting for event "download"'. On aligne
    donc, en reculant `date_from` si necessaire — jamais en avancant, pour ne pas amputer la
    periode demandee.
    """
    jours = (date_to - date_from).days + 1
    if jours % 7:
        date_from = date_from - timedelta(days=7 - (jours % 7))
    job_id = start_job("/jobs/banque/mouvements/export", {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "idempotency_key": f"mvt-{date_from.isoformat()}-{date_to.isoformat()}",
    })
    job = wait_job(job_id, timeout=timeout)
    job["_date_from"], job["_date_to"] = date_from, date_to
    return job


def job_artifacts(job: dict) -> list:
    """Noms de fichiers produits par un job d'export."""
    arts = ((job or {}).get("result") or {}).get("artifacts") or []
    out = []
    for a in arts:
        nom = a if isinstance(a, str) else (a.get("filename") or a.get("name") or "")
        if nom:
            out.append(nom.rsplit("/", 1)[-1])
    return out


def fetch_movements_file(filename: str) -> list:
    """Lit un fichier d'export NOMME, et non `/latest`.

    Indispensable des qu'on enchaine plusieurs exports : chaque nouvel export remplace `/latest`,
    donc lire `/latest` apres coup ne rendrait que la derniere tranche et perdrait les autres.
    """
    r = requests.get(_base_url() + f"/banque/mouvements/export/{filename}",
                     headers=_headers(), timeout=120)
    r.raise_for_status()
    return _parse_movements_xlsx(r.content)


def releves_disponibles() -> list:
    """Les releves PDF deja telecharges par le service. -> [{filename, periode, size_bytes}]"""
    r = requests.get(_base_url() + "/banque/releves", headers=_headers(), timeout=60)
    r.raise_for_status()
    return r.json() or []


def releve_pdf(periode: str, timeout: int = 1800) -> bytes:
    """Le releve MENSUEL de la banque, en PDF. `periode` au format « YYYY-MM ».

    ⚠️ CE N'EST PAS L'EXPORT DES MOUVEMENTS. `/jobs/banque/mouvements/export` rend un tableur de
    lignes, utile pour alimenter le registre ; le comptable, lui, attend le releve tel que la
    banque l'edite — le document qui fait foi, avec ses soldes d'ouverture et de cloture. Le
    service a une route dediee.

    ⚠️ ET ON NE RETELECHARGE PAS CE QUI EST DEJA LA. Le job pilote le portail de la banque : le
    declencher pour un mois deja archive coute plusieurs minutes pour rien.
    """
    periode = (periode or "").strip()
    deja = {r.get("periode") for r in releves_disponibles()}
    if periode not in deja:
        wait_job(start_job("/jobs/banque/releve/pdf",
                           {"periode": periode, "idempotency_key": "releve-%s" % periode}),
                 timeout=timeout)
    r = requests.get(_base_url() + f"/banque/releves/{periode}/pdf",
                     headers=_headers(), timeout=180)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise ValueError("le service n'a pas rendu un PDF pour le releve %s" % periode)
    return r.content


def refresh_remises(timeout: int = 900) -> dict:
    """Telecharge les bordereaux de remise de cheques absents du service (les deja presents sont
    sautes cote service) : sans ca, une remise recente n'a pas de PDF a extraire."""
    return wait_job(start_job("/jobs/banque/remises-cheques/sync"), timeout=timeout)


def _norm_op(s: str) -> str:
    """Majuscule + suppression des points/espaces : 'PAIEMENT PRELEVEMENT C.N.S.S' -> 'PAIEMENTPRELEVEMENTCNSS'."""
    return "".join(c for c in (s or "").upper() if c.isalnum())


# Libelles bancaires reels (BIAT) :
#   Declaration fiscale -> 'PAIEMENT PRELEVEMENT MIN DES FINANCES'
#   CNSS                -> 'PAIEMENT PRELEVEMENT C.N.S.S'
# => on distingue par le LIBELLE en plus du montant.
DECLARATION_KEYWORDS = ("PRELEVEMENT", "FINANCES")
CNSS_KEYWORDS = ("PRELEVEMENT", "CNSS")


def find_payment(total: float, keywords, tol: float = 0.01):
    """Mouvement au DEBIT == total dont le libelle contient TOUS les mots-cles (normalises).
    Retourne le mouvement (dict) ou None."""
    kws = [_norm_op(k) for k in keywords]
    for m in fetch_latest_movements():
        if not m["debit"] or abs(m["debit"] - total) >= tol:
            continue
        op = _norm_op(m["operation"])
        if all(k in op for k in kws):
            return m
    return None


def find_declaration_payment(total: float, tol: float = 0.01):
    """Paiement de la declaration fiscale : Debit == total ET libelle ~ 'PRELEVEMENT ... FINANCES'
    (exclut le prelevement CNSS de meme nature)."""
    return find_payment(total, DECLARATION_KEYWORDS, tol)


def find_cnss_payment(total: float, tol: float = 0.01):
    """Paiement CNSS : Debit == total ET libelle ~ 'PRELEVEMENT ... C.N.S.S'."""
    return find_payment(total, CNSS_KEYWORDS, tol)


def find_debits_by_keywords(keywords, movements: list = None) -> list:
    """Tous les mouvements au DEBIT dont le libelle contient TOUS les mots-cles (normalises).
    Utile pour piloter par la banque (ex. detecter les prelevements CNSS sans connaitre le montant).

    `movements` est injectable et doit l'etre : par defaut cette fonction lit le dernier export du
    service, qui est ECRASE a chaque nouvel export et vide quand le service est injoignable. La
    memoire longue est le REGISTRE (`bank/registry.py`) — c'est lui que les appelants passent.
    """
    kws = [_norm_op(k) for k in keywords]
    out = []
    for m in (movements if movements is not None else fetch_latest_movements()):
        if m["debit"] and all(k in _norm_op(m["operation"]) for k in kws):
            out.append(m)
    return out


def find_credit_by_amount(amount: float, keyword: str = None, tol: float = 0.01):
    """Mouvement au CREDIT egal a `amount` (ex. virement Aramex). keyword optionnel
    filtre le libelle. Retourne le mouvement (dict) ou None."""
    for m in fetch_latest_movements():
        if m["credit"] and abs(m["credit"] - amount) < tol:
            if keyword and keyword.upper() not in m["operation"].upper():
                continue
            return m
    return None


def find_credits_by_keywords(keywords, movements: list = None) -> list:
    """Miroir CREDIT de `find_debits_by_keywords` : tous les mouvements au CREDIT dont le
    libelle contient TOUS les mots-cles (normalises). Sert au rapprochement des encaissements
    (depots cheque 'ENC CHEQ', virements Aramex, virements clients).
    `movements` permet de passer une liste deja recuperee (evite un 2e appel service)."""
    kws = [_norm_op(k) for k in keywords]
    src = movements if movements is not None else fetch_latest_movements()
    return [m for m in src if m["credit"] and all(k in _norm_op(m["operation"]) for k in kws)]


# --- Extraction de numeros dans le libelle (repris de outil-facturation-erpnext) ---
import re as _re

_CHEQUE_RE = _re.compile(r"\b\d{7}\b")   # n° de cheque = 7 chiffres
_BON_RE = _re.compile(r"\b\d{8}\b")      # n° de bon de remise = 8 chiffres


def find_cheque_number(op: str):
    """Premier n° de cheque (7 chiffres) trouve dans un libelle, ou None."""
    m = _CHEQUE_RE.search(op or "")
    return m.group(0) if m else None


def find_bon_number(op: str):
    """Premier n° de bon de remise (8 chiffres) trouve dans un libelle (credit 'ENC CHEQ'), ou None."""
    m = _BON_RE.search(op or "")
    return m.group(0) if m else None


_EFFET_RE = _re.compile(r"\d{10,}")      # n° de traite/effet = suite longue de chiffres (>=10)


def find_effet_number(op: str):
    """N° de traite (effet) dans un libelle 'ENCAISSEMENT EFFET <numero>', ou None.
    Ex. 'ENCAISSEMENT EFFET 011289089405' -> '011289089405'."""
    m = _EFFET_RE.search(op or "")
    return m.group(0) if m else None


# Mots-cles de libelle bancaire cote CREDIT (cf. outil-facturation-erpnext + libelles reels) :
#   depot de cheques encaisses     -> 'ENC CHEQ'
#   encaissement de traite (effet) -> 'ENCAISSEMENT EFFET <n° traite>'
ENC_CHEQUE_KEYWORDS = ("ENC", "CHEQ")
ENC_EFFET_KEYWORDS = ("ENCAISSEMENT", "EFFET")


def list_remises_cheques() -> list:
    """Liste des remises de cheques disponibles sur tej-bank-service (route reelle verifiee) :
        GET /banque/remises-cheques
    Reponse : [{"numero": "90028032", "filename": "remise_90028032.pdf",
                "size_bytes": 1547485, "modified": "2026-07-21T13:27:42"}, ...]
    Le `numero` (8 chiffres) EST le n° de bon de remise, egal au n° extrait du libelle bancaire
    'ENC CHEQ' cote CREDIT. Le detail des cheques est dans le PDF (cf. `fetch_remise_cheque_pdf`)."""
    r = requests.get(_base_url() + "/banque/remises-cheques", headers=_headers(), timeout=60)
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else data.get("remises", [])
    return [
        {
            "numero": str(it.get("numero") or "").strip(),
            "filename": it.get("filename"),
            "modified": _to_date(it.get("modified")) if it.get("modified") else None,
        }
        for it in items
    ]


def fetch_remise_cheque_pdf(numero: str) -> bytes:
    """Telecharge le bordereau PDF d'une remise de cheques (route reelle verifiee) :
        GET /banque/remises-cheques/{numero}/pdf
    Le PDF (bordereau bancaire, arabe/RTL) liste les cheques deposes : n° cheque, banque,
    emetteur, montant. Extraction fiable via OpenAI (cf. ai/remise_extract). Retourne les octets."""
    h = {k: v for k, v in _headers().items() if k != "Accept"}
    r = requests.get(_base_url() + f"/banque/remises-cheques/{numero}/pdf", headers=h, timeout=90)
    r.raise_for_status()
    return r.content


# NOTE traites : tej-bank-service n'expose AUCUNE route de remise de traites (seulement les
# remises de CHEQUES ci-dessus). Le flux traite reste a sourcer (endpoint service a ajouter,
# ou autre source) — hors perimetre tant que la route n'existe pas.
