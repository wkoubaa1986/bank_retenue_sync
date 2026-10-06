"""Extraction du contenu de la facture Total depuis le ZIP joint.

Chaque ZIP TotalEnergies contient deux fichiers :
  - <ref>.PDF   : la facture (a comptabiliser / attacher a l'ecriture) ;
  - <ref>.XLSX  : les memes donnees en structure (utilisable pour extraire
    les montants sans OCR/IA, ou pour recouper l'extraction).

Module pur : bytes du ZIP -> fichiers. Testable hors Frappe.
"""
from __future__ import annotations

import re

import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime


def extract_invoice_files(zip_bytes: bytes) -> dict:
    """Retourne {'pdf': (nom, bytes) | None, 'xlsx': (nom, bytes) | None,
    'others': [(nom, bytes), ...]}. Leve zipfile.BadZipFile si le ZIP est invalide."""
    out = {"pdf": None, "xlsx": None, "others": []}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            low = info.filename.lower()
            data = zf.read(info)
            if low.endswith(".pdf") and out["pdf"] is None:
                out["pdf"] = (info.filename, data)
            elif low.endswith((".xlsx", ".xls")) and out["xlsx"] is None:
                out["xlsx"] = (info.filename, data)
            else:
                out["others"].append((info.filename, data))
    return out


# --- Parsing du XLSX Total -> totaux facture -------------------------------
#
# Le XLSX contient une ligne par transaction, mais les totaux facture
# (Total_HT / Total_TVA / Total_TTC, Numero/Date facture) sont REPETES sur
# chaque ligne. On lit donc l'entete puis la 1re ligne de donnees.

def _num(v) -> float:
    """Cellule openpyxl -> float. Gere int/float natifs et texte a virgule ('49,89')."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _to_date(v):
    """Cellule date -> datetime.date. Gere datetime natif et texte 'JJ/MM/AAAA'."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class TotalInvoice:
    invoice_no: str
    invoice_date: object          # datetime.date | None
    currency: str
    total_ht: float
    total_tva: float
    total_ttc: float
    client_name: str = ""
    client_no: str = ""
    lines_count: int = 0

    @property
    def period(self):
        d = self.invoice_date
        return f"{d.year:04d}-{d.month:02d}" if d else None

    @property
    def balanced(self) -> bool:
        return abs((self.total_ht + self.total_tva) - self.total_ttc) < 0.01


def parse_invoice_xlsx(xlsx_bytes: bytes) -> TotalInvoice:
    """Parse le XLSX de la facture Total et renvoie les totaux facture."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = [str(c).strip() if c is not None else "" for c in next(rows)]
    idx = {name: i for i, name in enumerate(header)}

    def cell(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    first = None
    count = 0
    for row in rows:
        if not any(c is not None and str(c).strip() for c in row):
            continue
        if first is None:
            first = row
        count += 1

    if first is None:
        return TotalInvoice("", None, "", 0.0, 0.0, 0.0)

    return TotalInvoice(
        invoice_no=str(cell(first, "Numéro_Facture") or "").strip(),
        invoice_date=_to_date(cell(first, "Date_facture")),
        currency=str(cell(first, "Devise_Facture") or "").strip(),
        total_ht=_num(cell(first, "Total_HT")),
        total_tva=_num(cell(first, "Total_TVA")),
        total_ttc=_num(cell(first, "Total_TTC")),
        client_name=str(cell(first, "Nom_Client") or "").strip(),
        client_no=str(cell(first, "Numéro_Client") or "").strip(),
        lines_count=count,
    )


# --------------------------------------------------------------------------- facture PDF (09/2026)
# Depuis septembre 2026, « TotalEnergies Marketing Tunisie » envoie la facture mensuelle des cartes
# prepayees en PDF (« Invoice_10022026.pdf »), plus en ZIP + XLSX : le flux ZIP l'ignorait sans rien
# dire (facture de septembre jamais saisie, constate le 06/10/2026). Lecture DETERMINISTE du texte :
# numero (FP…), date du document, ligne « Total general » (HT, TVA, TTC — timbre compris dans le HT).
# Les « factures de recharge » (numero PM…, « Operations de credit ») ne sont pas des depenses : la
# recharge est passee depuis la banque (« Recharge Carte Total FT… »). -> None.

_NB = r"(-?[\d,]+\.\d+|-?\d+)"


def _nombre_pdf(v: str) -> float:
    return float(v.replace(",", ""))


def parse_invoice_pdf_text(texte: str):
    """Texte (PyMuPDF) d'une facture PDF Total -> TotalInvoice, ou None si ce n'est pas une facture
    mensuelle de consommation (facture de recharge, document inconnu). Pur."""
    t = texte or ""
    numero = re.search(r"Num[ée]ro\s+Facture\s+([A-Z]{2}\d{2}/\d+)", t)
    if not numero or numero.group(1).upper().startswith("PM") or re.search(r"Op[ée]rations\s+de\s+cr[ée]dit", t):
        return None
    emise = re.search(r"Date\s+du\s+Document\s+(\d{2})/(\d{2})/(\d{4})", t)
    total = re.search(r"Total\s+g[ée]n[ée]ral\s+(?:TND\s+)?" + _NB + r"\s+" + _NB + r"\s+" + _NB, t)
    if not emise or not total:
        return None
    jour, mois, an = (int(x) for x in emise.groups())
    client = re.search(r"Num[ée]ro\s+Client\s+(\d+)", t)
    return TotalInvoice(
        invoice_no=numero.group(1), invoice_date=date(an, mois, jour), currency="TND",
        total_ht=_nombre_pdf(total.group(1)), total_tva=_nombre_pdf(total.group(2)),
        total_ttc=_nombre_pdf(total.group(3)), client_no=client.group(1) if client else "")


def parse_invoice_pdf(pdf_bytes: bytes):
    """PDF de la facture Total -> TotalInvoice | None (voir `parse_invoice_pdf_text`)."""
    import pymupdf

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        return parse_invoice_pdf_text("\n".join(page.get_text() for page in doc))

