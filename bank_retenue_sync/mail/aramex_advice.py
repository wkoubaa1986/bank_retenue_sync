"""Parseur du Payment Advice Aramex -> identification des virements.

Le fichier joint ".XLS" d'Aramex n'est PAS un vrai Excel : c'est un texte
tab-separe (TSV) avec l'entete :
    Document Number | Reference | Payment Date | Currency | Invoice Amount | Withholding Tax | Description

Particularites de format observees sur les emails reels :
  - separateur decimal = VIRGULE : "25,000" = 25.0 TND, "1576,000" = 1576.0
    (ce N'EST PAS un separateur de milliers) ;
  - montant negatif = signe moins EN FIN : "2,380-" = -2.380 (reversal) ;
  - date au format JJ.MM.AAAA.

Le net verse sur le compte bancaire = somme(Invoice Amount) - somme(Withholding Tax)
a la Payment Date : c'est ce couple (montant, date) qu'on identifie ensuite face
au releve bancaire. Module pur (bytes/str -> donnees), testable hors Frappe.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime


def parse_amount(raw: str) -> float:
    """'25,000' -> 25.0 ; '1576,000' -> 1576.0 ; '2,380-' -> -2.380 ; '' -> 0.0"""
    s = (raw or "").strip()
    if not s:
        return 0.0
    neg = s.endswith("-")
    s = s.rstrip("-").strip().replace(" ", "").replace(",", ".")
    try:
        val = float(s)
    except ValueError:
        return 0.0
    return -val if neg else val


def parse_date(raw: str):
    """JJ.MM.AAAA (ou variantes) -> datetime.date, sinon None."""
    s = (raw or "").strip()
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class AdviceLine:
    document_number: str
    reference: str
    payment_date: object          # datetime.date | None
    currency: str
    invoice_amount: float
    withholding_tax: float
    description: str

    @property
    def net(self) -> float:
        return round(self.invoice_amount - self.withholding_tax, 3)


@dataclass
class PaymentAdvice:
    lines: list
    currency: str
    payment_date: object          # datetime.date | None (date du virement)
    total_invoice: float
    total_withholding: float

    @property
    def net_total(self) -> float:
        """Montant net effectivement vire en banque."""
        return round(self.total_invoice - self.total_withholding, 3)

    @property
    def count(self) -> int:
        return len(self.lines)


_COLS = (
    ("doc", "document number", 0),
    ("ref", "reference", 1),
    ("date", "payment date", 2),
    ("cur", "currency", 3),
    ("amt", "invoice amount", 4),
    ("wht", "withholding tax", 5),
    ("desc", "description", 6),
)


def _to_text(content) -> str:
    """Decode le fichier joint en texte, robuste aux variantes d'encodage
    observees (ASCII/UTF-8 pour certains Payment Advice, UTF-16 pour d'autres)."""
    if isinstance(content, str):
        return content.replace("\x00", "")
    b = content or b""
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):                 # BOM UTF-16
        return b.decode("utf-16", errors="replace").replace("\x00", "")
    if b.count(b"\x00") > len(b) // 4:                      # UTF-16 sans BOM (beaucoup de NUL)
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                return b.decode(enc).replace("\x00", "")
            except Exception:
                pass
    # UTF-8 par defaut, puis filet de securite : retirer d'eventuels NUL residuels
    return b.decode("utf-8", errors="replace").replace("\x00", "")


def parse_advice(content) -> PaymentAdvice:
    """content : bytes ou str du fichier joint Aramex. Retourne un PaymentAdvice."""
    text = _to_text(content)
    rows = [r for r in csv.reader(io.StringIO(text), delimiter="\t") if any((c or "").strip() for c in r)]
    if not rows:
        return PaymentAdvice([], "", None, 0.0, 0.0)

    header = [c.strip().lower() for c in rows[0]]
    has_header = "document number" in header
    start = 1 if has_header else 0
    idx = {key: (header.index(name) if has_header and name in header else default)
           for key, name, default in _COLS}

    lines = []
    for r in rows[start:]:
        def g(i):
            return r[i].strip() if i < len(r) else ""
        lines.append(AdviceLine(
            document_number=g(idx["doc"]),
            reference=g(idx["ref"]),
            payment_date=parse_date(g(idx["date"])),
            currency=g(idx["cur"]),
            invoice_amount=parse_amount(g(idx["amt"])),
            withholding_tax=parse_amount(g(idx["wht"])),
            description=g(idx["desc"]),
        ))

    total_inv = round(sum(l.invoice_amount for l in lines), 3)
    total_wht = round(sum(l.withholding_tax for l in lines), 3)
    currency = next((l.currency for l in lines if l.currency), "")
    pdate = next((l.payment_date for l in lines if l.payment_date), None)
    return PaymentAdvice(lines, currency, pdate, total_inv, total_wht)
