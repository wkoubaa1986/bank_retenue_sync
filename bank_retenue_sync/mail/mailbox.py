"""Acces IMAP reutilisable (LECTURE SEULE) pour l'orchestration.

Centralise la connexion + la recuperation des messages/pieces jointes, jusque-la
dupliquee dans les scripts. N'importe jamais rien (SELECT readonly, BODY.PEEK).
"""
from __future__ import annotations

import email as email_lib
import imaplib

from bank_retenue_sync.mail.finder import _decode, _get_email_account


def _connect(email_account=None):
    acc = _get_email_account(email_account)
    pwd = acc.get_password("password", raise_exception=True)
    login_id = acc.login_id if getattr(acc, "login_id_is_different", 0) else acc.email_id
    M = imaplib.IMAP4_SSL(acc.email_server or "imap.gmail.com", int(acc.incoming_port or 993), timeout=30)
    M.login(login_id, pwd)
    M.select("INBOX", readonly=True)
    return M


def fetch_messages(sender=None, subject=None, since=None, limit=30, email_account=None):
    """Retourne [{uid, subject, date, from, attachments:[(nom, bytes)]}] (recents d'abord).
    `since` au format IMAP '01-Jun-2026'."""
    M = _connect(email_account)
    try:
        # IMAP : une valeur multi-mots doit etre une chaine QUOTEE, sinon 'BAD Could not
        # parse command' (ex. SUBJECT "payment advice"). Les valeurs d'un seul mot passent aussi.
        def _q(v):
            return '"%s"' % str(v).replace('"', "")

        crit = []
        if sender:
            crit += ["FROM", _q(sender)]
        if subject:
            crit += ["SUBJECT", _q(subject)]
        if since:
            crit += ["SINCE", since]
        if not crit:
            crit = ["ALL"]
        typ, d = M.search(None, *crit)
        uids = sorted(d[0].split(), key=lambda b: int(b), reverse=True)[:limit] if (d and d[0]) else []
        out = []
        for uid in uids:
            typ, md = M.fetch(uid, "(BODY.PEEK[])")
            raw = b"".join(p[1] for p in md if isinstance(p, tuple) and p[1])
            msg = email_lib.message_from_bytes(raw)
            atts = []
            for part in msg.walk():
                fn = part.get_filename()
                if fn:
                    atts.append((_decode(fn), part.get_payload(decode=True) or b""))
            out.append({
                "uid": uid.decode(),
                "subject": _decode(msg.get("Subject")),
                "date": _decode(msg.get("Date")),
                "from": _decode(msg.get("From")),
                "attachments": atts,
            })
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def attachment(msg: dict, ext=None, contains=None):
    """Premiere piece jointe qui matche extension et/ou fragment de nom. -> (nom, bytes) | None."""
    for name, data in msg["attachments"]:
        low = name.lower()
        if ext and not low.endswith(ext):
            continue
        if contains and contains.lower() not in low.replace(" ", ""):
            continue
        return (name, data)
    return None
