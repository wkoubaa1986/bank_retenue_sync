"""Recherche des emails sources.

Deux points d'entree, aucune ecriture (ce module ne fait que TROUVER + classifier ;
la generation de depense et le rapprochement bancaire se brancheront dessus) :

  - `find_in_communications()` : sur les emails deja importes dans ERPNext (Communication).
  - `scan_imap()` : directement en IMAP, LECTURE SEULE (SELECT readonly + BODY.PEEK),
    sans rien importer ni marquer comme lu -> aucune interference avec une autre instance.

`scan()` est l'entree whitelisted pratique (console / bouton) qui renvoie un resume.
"""
from __future__ import annotations

import email as email_lib
import imaplib
from email.header import decode_header
from email.utils import parsedate_to_datetime

import frappe

from bank_retenue_sync.mail.classifier import classify, extract_email, extract_period
from bank_retenue_sync.mail.sources import SOURCES


def _decode(v):
    if not v:
        return ""
    out = ""
    for txt, enc in decode_header(v):
        out += txt.decode(enc or "utf-8", errors="replace") if isinstance(txt, bytes) else txt
    return out.replace("\n", " ").strip()


def _match(sender, subject, when=None, categories=None):
    src = classify(sender, subject)
    if src is None:
        return None
    if categories and src.key not in categories:
        return None
    return {
        "source": src.key,
        "label": src.label,
        "role": src.role,
        "monthly": src.monthly,
        "sender": extract_email(sender),
        "subject": (subject or "").strip(),
        "period": extract_period(subject, when),
    }


# --- 1) Sur les Communications deja importees ------------------------------

def find_in_communications(since=None, categories=None, limit=500):
    """Classifie les emails recus deja presents dans ERPNext (DocType Communication)."""
    filters = {"communication_medium": "Email", "sent_or_received": "Received"}
    if since:
        filters["creation"] = (">=", since)
    rows = frappe.get_all(
        "Communication",
        filters=filters,
        fields=["name", "sender", "subject", "creation"],
        order_by="creation desc",
        limit=limit,
    )
    out = []
    for r in rows:
        m = _match(r.sender, r.subject, r.creation, categories)
        if m:
            m["communication"] = r.name
            out.append(m)
    return out


# --- 2) Directement en IMAP, LECTURE SEULE ---------------------------------

def _get_email_account(email_account=None):
    if email_account:
        return frappe.get_doc("Email Account", email_account)
    name = (
        frappe.db.get_value("Email Account", {"use_imap": 1, "default_incoming": 1})
        or frappe.db.get_value("Email Account", {"use_imap": 1})
    )
    if not name:
        frappe.throw("Aucun compte Email IMAP configure.")
    return frappe.get_doc("Email Account", name)


def _all_sender_patterns(categories=None):
    pats = set()
    for s in SOURCES:
        if categories and s.key not in categories:
            continue
        pats.update(s.senders)
    return sorted(pats)


def _search_candidate_uids(M, categories=None):
    """Pre-filtre serveur par expediteur (IMAP SEARCH FROM) pour ne pas parcourir
    toute la boite. Retourne un set d'UID (bytes)."""
    uids = set()
    for pat in _all_sender_patterns(categories):
        typ, data = M.search(None, "FROM", pat)
        if typ == "OK" and data and data[0]:
            uids.update(data[0].split())
    return uids


def _attachment_meta(M, uid):
    typ, md = M.fetch(uid, "(BODY.PEEK[])")
    raw = b"".join(p[1] for p in md if isinstance(p, tuple) and p[1])
    msg = email_lib.message_from_bytes(raw)
    atts = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fname = part.get_filename()
        disp = str(part.get("Content-Disposition") or "")
        if fname or "attachment" in disp.lower():
            try:
                size = len(part.get_payload(decode=True) or b"")
            except Exception:
                size = -1
            atts.append({
                "filename": _decode(fname) or "(sans nom)",
                "content_type": part.get_content_type(),
                "size": size,
            })
    return atts


def scan_imap(email_account=None, folder="INBOX", limit=200, categories=None, with_attachments=True):
    """Scan IMAP lecture seule. Renvoie la liste des emails sources trouves (recents
    d'abord), sans rien importer ni modifier cote serveur."""
    acc = _get_email_account(email_account)
    pwd = acc.get_password("password", raise_exception=True)
    login_id = acc.login_id if getattr(acc, "login_id_is_different", 0) else acc.email_id

    M = imaplib.IMAP4_SSL(acc.email_server or "imap.gmail.com", int(acc.incoming_port or 993), timeout=30)
    try:
        M.login(login_id, pwd)
        M.select(folder, readonly=True)   # readonly => aucune ecriture serveur possible

        uids = sorted(_search_candidate_uids(M, categories), key=lambda b: int(b), reverse=True)
        results = []
        for uid in uids:
            if len(results) >= limit:
                break
            typ, md = M.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw = b"".join(p[1] for p in md if isinstance(p, tuple) and p[1])
            msg = email_lib.message_from_bytes(raw)
            when = None
            try:
                when = parsedate_to_datetime(msg.get("Date"))
            except Exception:
                pass
            m = _match(_decode(msg.get("From")), _decode(msg.get("Subject")), when, categories)
            if not m:
                continue
            m["uid"] = uid.decode()
            m["date"] = _decode(msg.get("Date"))
            if with_attachments:
                m["attachments"] = _attachment_meta(M, uid)
            results.append(m)
        return results
    finally:
        try:
            M.logout()
        except Exception:
            pass


@frappe.whitelist()
def scan(source=None, limit=100, email_account=None):
    """Entree pratique (console/desk) : scan IMAP lecture seule + resume par source.

    Ex. console :
        from bank_retenue_sync.mail.finder import scan
        scan()                         # toutes les sources
        scan(source="total_invoice")  # une seule
    """
    frappe.only_for("System Manager")
    categories = [source] if source else None
    matches = scan_imap(email_account=email_account, categories=categories, limit=int(limit))
    by_source = {}
    for m in matches:
        by_source[m["source"]] = by_source.get(m["source"], 0) + 1
    return {"total": len(matches), "by_source": by_source, "matches": matches}
