"""Collecte des bordereaux de remise (DocType `BRS Bordereau Remise`), depuis la caisse ou la liste.

Le bordereau est seulement RANGE : banque, numero, date, nature, scan. L'identification bancaire s'en
servira ensuite (une remise BIAT se reconnait a son numero de bordereau, cf. `bank/biat.py`).
"""
from __future__ import annotations

import re
import unicodedata

import frappe

DOCTYPE = "BRS Bordereau Remise"
BANQUES = ("BIAT", "Zitouna")
NATURES = ("Chèques", "Traites", "Espèces")


# ---------------------------------------------------------------------------
# Pur
# ---------------------------------------------------------------------------

def numero_propre(numero) -> str:
    """Le numero du bordereau, chiffres seulement (« 7 473 685 » -> « 7473685 »). Leve s'il n'en a pas."""
    chiffres = "".join(c for c in str(numero or "") if c.isdigit())
    if not 4 <= len(chiffres) <= 14:
        raise ValueError("Le numéro du bordereau est une suite de 4 à 14 chiffres.")
    return chiffres


def nom_du_bordereau(banque: str, numero: str) -> str:
    if banque not in BANQUES:
        raise ValueError("Banque inconnue : choisissez BIAT ou Zitouna.")
    return f"BR-{banque}-{numero_propre(numero)}"


def lire_nom_fichier(nom: str) -> dict:
    """Ce que dit le nom d'un scan range a la facon de l'entreprise — « Ordre de versement Chèques
    Bq BIAT-7473685-du 09-11-2026.pdf » -> {banque, nature, numero, date_remise (YYYY-MM-DD)}. Une cle
    n'apparait que si elle est lue ; un nom quelconque rend {}. Le suffixe que Frappe colle au nom
    (« …-du 20-10-20267c37c5.pdf ») ne gene pas la lecture de la date."""
    brut = unicodedata.normalize("NFKD", str(nom or "")).encode("ascii", "ignore").decode()
    bas = brut.lower()
    out = {}
    if "biat" in bas:
        out["banque"] = "BIAT"
    elif "zitouna" in bas or "ezzitouna" in bas:
        out["banque"] = "Zitouna"
    for mot, nature in (("cheque", "Chèques"), ("traite", "Traites"), ("espece", "Espèces")):
        if mot in bas:
            out["nature"] = nature
            break
    date = re.search(r"du\s*(\d{2})[-./](\d{2})[-./](\d{4})", bas)
    if date and 1 <= int(date.group(2)) <= 12 and 1 <= int(date.group(1)) <= 31:
        out["date_remise"] = f"{date.group(3)}-{date.group(2)}-{date.group(1)}"
    # Le numero : le groupe de chiffres qui precede « -du », sinon le premier groupe long hors de la date.
    avant = re.search(r"(\d{4,14})\s*-\s*du\b", bas)
    if avant:
        out["numero"] = avant.group(1)
    else:
        sans_date = bas[:date.start()] if date else bas
        libre = re.findall(r"(?<!\d)\d{5,14}(?!\d)", sans_date)
        if libre:
            out["numero"] = libre[-1]
    return out


# ---------------------------------------------------------------------------
# Avec le site
# ---------------------------------------------------------------------------

@frappe.whitelist()
def lire_nom(nom: str) -> dict:
    """Preremplit le dialogue de collecte d'apres le nom du fichier choisi."""
    frappe.has_permission(DOCTYPE, "create", throw=True)
    return lire_nom_fichier(nom)


@frappe.whitelist()
def ajouter(banque: str, numero: str, date_remise: str, fichier: str, nature: str = "Chèques",
            montant_total=None, nombre=None, note: str = None) -> dict:
    """Range un bordereau. `fichier` : l'adresse du fichier deja televerse (champ Attach du dialogue).
    -> {name, deja} ; `deja` vrai si ce bordereau etait deja collecte (rien n'est alors modifie)."""
    frappe.has_permission(DOCTYPE, "create", throw=True)
    try:
        nom = nom_du_bordereau(banque, numero)
    except ValueError as e:
        frappe.throw(str(e))
    if nature not in NATURES:
        frappe.throw("Nature inconnue : Chèques, Traites ou Espèces.")
    if not fichier or not str(fichier).startswith(("/files/", "/private/files/")):
        frappe.throw("Joignez le scan ou la photo du bordereau.")
    if frappe.db.exists(DOCTYPE, nom):
        return {"name": nom, "deja": True}
    doc = frappe.get_doc({"doctype": DOCTYPE, "banque": banque, "numero": numero, "date_remise": date_remise,
                          "nature": nature, "fichier": fichier, "note": note,
                          "montant_total": frappe.utils.flt(montant_total) or None,
                          "nombre": frappe.utils.cint(nombre) or None}).insert()
    # Le fichier televerse par le dialogue n'est rattache a rien : il devient la piece jointe du bordereau,
    # donc lisible par qui peut lire le bordereau.
    for f in frappe.get_all("File", filters={"file_url": fichier, "attached_to_name": ["is", "not set"],
                                             "owner": frappe.session.user}, pluck="name"):
        frappe.db.set_value("File", f, {"attached_to_doctype": DOCTYPE, "attached_to_name": doc.name,
                                        "attached_to_field": "fichier"}, update_modified=False)
    return {"name": doc.name, "deja": False}


@frappe.whitelist()
def recents(limite: int = 12) -> list:
    """Les derniers bordereaux collectes, pour le dialogue de la caisse."""
    frappe.has_permission(DOCTYPE, "read", throw=True)
    return frappe.get_list(DOCTYPE, fields=["name", "banque", "nature", "numero", "date_remise", "montant_total",
                                            "fichier", "remis_par", "creation"],
                           order_by="date_remise desc, creation desc", limit_page_length=min(frappe.utils.cint(limite) or 12, 50))
