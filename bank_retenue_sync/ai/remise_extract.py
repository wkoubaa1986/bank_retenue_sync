"""Extraction du bordereau de remise de cheques (tej-bank-service) via pypdf + OpenAI.

Le PDF (bordereau bancaire tunisien, arabe/RTL) liste les cheques deposes sur une remise :
n° de cheque, banque tiree, emetteur, montant ; + n° de remise, date, total.

Principe (regle utilisateur "pypdf avant OpenAI si c'est un PDF") : on extrait d'abord le
TEXTE avec pypdf, puis on le soumet a OpenAI en mode JSON (pas de vision : le texte suffit,
appel peu couteux, une remise est immuable -> extraite une seule fois).

Reutilise les helpers de ai/invoice_extract (client OpenAI + pdf_to_text).
"""
from __future__ import annotations

import json

import frappe

from bank_retenue_sync.ai.invoice_extract import _get_client_model_temp, pdf_to_text


def _to_float(v):
    """Nombre a point decimal. OpenAI renvoie deja des nombres 'plats' (1191.95), mais on
    tolere le format TN '1.191,950' (point=milliers, virgule=decimale) en repli."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if "," in s:  # format TN : point = milliers, virgule = decimale
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


_SYSTEM = (
    "Tu extrais les donnees d'un BORDEREAU DE REMISE DE CHEQUES d'une banque tunisienne "
    "(texte OCR/pypdf souvent bruite, arabe et francais melanges, chiffres parfois eclates). "
    "Reponds STRICTEMENT en JSON (aucun texte hors JSON) avec exactement ces cles : "
    "numero_remise (str : le numero du bordereau de remise), "
    "date_remise (YYYY-MM-DD : date de la remise), "
    "total (number : montant total de la remise), "
    "cheques (array d'objets, un par cheque depose, chacun avec : "
    "numero_cheque (str), banque (str : banque tiree, ex 'WIB','BNA','UIB'), "
    "emetteur (str : nom du tireur/emetteur), montant (number)). "
    "IMPORTANT : renvoie tous les montants comme des NOMBRES a point decimal SANS separateur "
    "de milliers (ex : 1191.95, pas '1.191,950'). Si une valeur est absente, mets null. "
    "N'invente jamais un cheque : n'inclus que ceux reellement listes."
)


def extract_remise(pdf_bytes: bytes, numero_hint: str = None) -> dict:
    """Bordereau de remise PDF -> dict :
        {numero_remise, date_remise, total, cheques: [{numero_cheque, banque, emetteur, montant}],
         _model, _total_matches (bool : somme des cheques == total)}.
    Fait UN appel OpenAI sur le texte pypdf. Leve si le PDF n'a aucun texte extractible."""
    client, model, temperature = _get_client_model_temp()
    text = pdf_to_text(pdf_bytes)
    if not text.strip():
        frappe.throw("Bordereau de remise sans texte extractible (scan image pur) : vision requise.")

    user = "Texte du bordereau de remise :\n\n" + text[:20000]
    if numero_hint:
        user += f"\n\nIndice : le numero de remise attendu est {numero_hint}."

    params = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    try:
        res = client.chat.completions.create(**params)
    except Exception as e:
        if "temperature" in str(e).lower():
            params.pop("temperature", None)
            res = client.chat.completions.create(**params)
        else:
            raise

    data = json.loads(res.choices[0].message.content)
    data["numero_remise"] = str(data.get("numero_remise") or numero_hint or "").strip()
    data["total"] = _to_float(data.get("total"))
    cheques = []
    for c in data.get("cheques") or []:
        cheques.append({
            "numero_cheque": str(c.get("numero_cheque") or "").strip(),
            "banque": str(c.get("banque") or "").strip(),
            "emetteur": str(c.get("emetteur") or "").strip(),
            "montant": _to_float(c.get("montant")),
        })
    data["cheques"] = cheques
    somme = sum(c["montant"] for c in cheques if c["montant"] is not None)
    data["_total_matches"] = data["total"] is not None and abs(somme - data["total"]) < 0.01
    data["_model"] = model
    return data
