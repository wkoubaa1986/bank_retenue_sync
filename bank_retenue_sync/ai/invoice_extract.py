"""Extraction des donnees d'une facture PDF via OpenAI.

Config lue depuis le Single DocType "AI Settings" (fourni par woocommerce_fusion) :
  - openai_api_key
  - open_ai_model    (defaut gpt-4o-mini)      <- note: fieldname avec underscore
  - open_ai_temperature (defaut 0.2)
Repli cle : frappe.conf.openai_api_key.

Le PDF est converti en texte (pypdf) puis soumis a OpenAI en mode JSON. Module
volontairement autonome (ne couple pas woocommerce_fusion), il lit juste le DocType.
"""
from __future__ import annotations

import io
import json

import frappe

_SETTINGS_FIELDS = {
    "key": ("openai_api_key",),
    "model": ("open_ai_model", "openai_model"),
    "temperature": ("open_ai_temperature", "openai_temperature"),
}


def _ai_settings_name():
    for dt in ("AI Settings", "AI settings"):
        if frappe.db.exists("DocType", dt):
            return dt
    return None


def _read_setting(*fieldnames):
    dt = _ai_settings_name()
    if not dt:
        return None
    doc = frappe.get_cached_doc(dt)
    for fn in fieldnames:
        val = getattr(doc, fn, None)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _get_client_model_temp():
    api_key = _read_setting(*_SETTINGS_FIELDS["key"]) or frappe.conf.get("openai_api_key")
    if not api_key:
        frappe.throw("Cle OpenAI absente (AI Settings.openai_api_key ou site_config).")
    model = _read_setting(*_SETTINGS_FIELDS["model"]) or "gpt-4o-mini"
    try:
        temperature = float(_read_setting(*_SETTINGS_FIELDS["temperature"]) or 0.2)
    except (TypeError, ValueError):
        temperature = 0.2
    from openai import OpenAI
    return OpenAI(api_key=api_key), model, temperature


def pdf_pages_text(pdf_bytes: bytes):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [(page.extract_text() or "") for page in reader.pages]


def pdf_to_text(pdf_bytes: bytes) -> str:
    return "\n".join(pdf_pages_text(pdf_bytes))


_SYSTEM_PROMPT = (
    "Tu es un extracteur de donnees de factures (contexte tunisien). On te fournit le TEXTE "
    "brut d'une facture. Reponds STRICTEMENT en JSON (aucun texte hors JSON) avec exactement "
    "ces cles : invoice_no (str), invoice_date (YYYY-MM-DD), currency (str, ex 'TND'), "
    "total_ht (number, montant taxable/HT), total_tva (number, TVA), "
    "stamp_duty (number, droit de timbre / 'Timbre', ou null), "
    "total_ttc (number, montant TTC a payer), "
    "vat_rate (number en %, ou null), supplier_name (str), "
    # Le matricule fiscal du FOURNISSEUR : c'est le beneficiaire du certificat de retenue a la
    # source, et le portail TEJ ne connait pas d'autre facon de le designer. Il n'est renseigne
    # que sur 17 fiches fournisseur sur 42, alors qu'il figure sur toutes les factures.
    "supplier_tax_id (str, matricule fiscal du FOURNISSEUR — souvent note 'MF', 'M.F.', "
    "'Matricule Fiscal', 'Identifiant Unique' ou 'RNE' ; format tunisien : 7 chiffres puis une "
    "lettre cle, puis des codes de categorie, ex '1646863/M/A/M/000' ou '1298092B' — recopie-le "
    "TEL QUEL, sans reformater ; ne le confonds pas avec le matricule de l'ACHETEUR, du numero "
    "de registre de commerce ou du numero de telephone ; null si absent). "
    "Les montants sont des nombres a point decimal, sans separateur de milliers. "
    "Les totaux se trouvent souvent en FIN de document (Montant Taxable, TVA, Timbre, Montant TTC). "
    "Si une valeur est absente ou incertaine, mets null. "
    "Regle de coherence : total_ht + total_tva + (stamp_duty ou 0) == total_ttc."
)


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def extract_invoice(pdf_bytes: bytes, extra_hint: str = None) -> dict:
    """PDF -> dict {invoice_no, invoice_date, currency, total_ht, total_tva,
    total_ttc, vat_rate, supplier_name, _balanced, _model}. Fait UN appel OpenAI."""
    client, model, temperature = _get_client_model_temp()
    pages = pdf_pages_text(pdf_bytes)
    text = "\n".join(pages)
    if not text.strip():
        frappe.throw("PDF sans texte extractible (probable scan image) : extraction vision requise.")

    # Les totaux sont sur la DERNIERE page -> on l'inclut explicitement en plus du
    # texte complet, pour qu'ils soient toujours dans le prompt (facture longue).
    user = "Texte de la facture :\n\n" + text[:40000]
    if len(pages) > 1:
        user += "\n\n----- DERNIERE PAGE (contient les totaux) -----\n" + (pages[-1] or "")[:6000]
    if extra_hint:
        user += "\n\nIndice contextuel : " + extra_hint

    params = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": temperature,
    }
    try:
        res = client.chat.completions.create(**params)
    except Exception as e:
        # certains modeles n'acceptent que la temperature par defaut -> retry sans
        if "temperature" in str(e).lower():
            params.pop("temperature", None)
            res = client.chat.completions.create(**params)
        else:
            raise

    data = json.loads(res.choices[0].message.content)

    # normalisation des montants (timbre inclus)
    for k in ("total_ht", "total_tva", "stamp_duty", "total_ttc", "vat_rate"):
        data[k] = _to_float(data.get(k))
    ht, tva, ttc = data.get("total_ht"), data.get("total_tva"), data.get("total_ttc")
    stamp = data.get("stamp_duty") or 0.0
    data["_balanced"] = (
        ht is not None and tva is not None and ttc is not None
        and abs((ht + tva + stamp) - ttc) < 0.01
    )
    data["_model"] = model
    return data


_HONORAIRE_SYSTEM = (
    "Tu extrais les donnees d'une NOTE D'HONORAIRE (facture d'un cabinet comptable, contexte "
    "tunisien). Reponds STRICTEMENT en JSON (aucun texte hors JSON) avec ces cles : "
    "invoice_date (YYYY-MM-DD, date de la note), total_ht (number), total_tva (number), "
    "timbre_fiscal (number, droit de timbre), total_ttc (number, Total Facture TTC), "
    "retenue_source (number : R/S retenue a la source, souvent 3%), "
    "net_a_payer (number : NET A PAYER apres retenue). "
    "Montants a point decimal, sans separateur de milliers. Valeur absente -> 0. "
    "Coherence : total_ht + total_tva + timbre_fiscal == total_ttc, et total_ttc - retenue_source == net_a_payer."
)


def extract_honoraire(pdf_bytes: bytes) -> dict:
    """Note d'honoraire PDF -> dict {invoice_date, total_ht, total_tva, timbre_fiscal,
    total_ttc, retenue_source, net_a_payer, _balanced, _model}. Fait UN appel OpenAI."""
    client, model, temperature = _get_client_model_temp()
    text = pdf_to_text(pdf_bytes)
    if not text.strip():
        frappe.throw("Note d'honoraire sans texte extractible : vision requise.")
    res = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _HONORAIRE_SYSTEM},
            {"role": "user", "content": "Texte de la note d'honoraire :\n\n" + text[:15000]},
        ],
    )
    data = json.loads(res.choices[0].message.content)
    for k in ("total_ht", "total_tva", "timbre_fiscal", "total_ttc", "retenue_source", "net_a_payer"):
        data[k] = _to_float(data.get(k))
    ttc = data.get("total_ttc")
    net, rs = data.get("net_a_payer"), data.get("retenue_source")
    data["_balanced"] = (ttc is not None and net is not None and rs is not None
                         and abs((net + rs) - ttc) < 0.01)
    data["_model"] = model
    return data


# ------------------------------------------------------------------ scans (sans couche texte)

_SCAN_SYSTEM_PROMPT = _SYSTEM_PROMPT + (
    " Le document est un SCAN : lis les montants tels qu'ils sont IMPRIMES. "
    "⚠️ Format tunisien : la virgule est le separateur DECIMAL et les montants ont TROIS "
    "decimales — « 1 087,021 » vaut 1087.021, pas 1087021 ; « 923,700 » vaut 923.7. "
    "L'espace ou le point separe les milliers. Verifie ta lecture avec la regle de coherence : "
    "si total_ht + total_tva + timbre ne tombe pas sur total_ttc, tu as mal lu une virgule."
)


def extract_invoice_scan(pdf_bytes: bytes, extra_hint: str = None) -> dict:
    """Extraction d'une facture SCANNEE, sans couche texte. -> meme dict que `extract_invoice`.

    ⚠️ POURQUOI UNE SECONDE VOIE. `extract_invoice` lit le texte du PDF : parfait pour les factures
    recues par email, inutilisable sur un scan, qui n'est qu'une image. Or les factures fournisseurs
    locales arrivent en papier — sur 222 pieces jointes de factures d'achat, la premiere essayee
    rendait « PDF sans texte extractible ».

    Le PDF part donc TEL QUEL au modele (API Responses, `input_file`), qui le rasterise de son cote.
    Aucune dependance nouvelle : ni poppler, ni PyMuPDF — dont l'installation aurait demande de
    reconstruire l'image Docker de production.

    ⚠️ ET LE FORMAT TUNISIEN EST UN PIEGE A LUI SEUL. Sans consigne explicite, le modele a lu
    « 923,688 » comme 922688 et rendu un TTC de 1098.99 pour une facture a 1087,021. Trois decimales
    et virgule decimale : c'est dit dans le prompt, et la regle de coherence sert de garde-fou.
    """
    import base64

    client, model, _temperature = _get_client_model_temp()
    if not hasattr(client, "responses"):
        frappe.throw("Le SDK OpenAI installe ne sait pas lire un PDF (API Responses absente).")

    consigne = "Facture fournisseur scannee." + (" %s" % extra_hint if extra_hint else "")
    b64 = base64.b64encode(pdf_bytes).decode()
    res = client.responses.create(
        model=model,
        instructions=_SCAN_SYSTEM_PROMPT,
        input=[{"role": "user", "content": [
            {"type": "input_file", "filename": "facture.pdf",
             "file_data": "data:application/pdf;base64,%s" % b64},
            {"type": "input_text", "text": consigne}]}])

    texte = (res.output_text or "").strip()
    if texte.startswith("```"):
        # Le modele encadre parfois son JSON d'un bloc de code : on le retire sans etre malin.
        texte = texte.strip("`")
        texte = texte.split("\n", 1)[1] if texte.lower().startswith("json") else texte
        texte = texte.rsplit("```", 1)[0]
    data = json.loads(texte)

    for k in ("total_ht", "total_tva", "stamp_duty", "total_ttc", "vat_rate"):
        data[k] = _to_float(data.get(k))
    ht, tva, ttc = data.get("total_ht"), data.get("total_tva"), data.get("total_ttc")
    stamp = data.get("stamp_duty") or 0.0
    data["_balanced"] = (ht is not None and tva is not None and ttc is not None
                         and abs((ht + tva + stamp) - ttc) < 0.01)
    data["_model"] = model
    data["_scan"] = True
    return data


def extract_invoice_image(image_bytes: bytes, mimetype: str = "image/jpeg",
                          extra_hint: str = None) -> dict:
    """Extraction d'une facture PHOTOGRAPHIEE (caisse : photo de telephone). -> meme dict.

    Meme voie que `extract_invoice_scan` (API Responses, prompt scan tunisien), mais l'image
    part en `input_image` — un JPEG n'est pas un PDF, `input_file` le refuserait.
    """
    import base64

    client, model, _temperature = _get_client_model_temp()
    if not hasattr(client, "responses"):
        frappe.throw("Le SDK OpenAI installe ne sait pas lire une image (API Responses absente).")

    consigne = "Facture fournisseur photographiee." + (" %s" % extra_hint if extra_hint else "")
    b64 = base64.b64encode(image_bytes).decode()
    res = client.responses.create(
        model=model,
        instructions=_SCAN_SYSTEM_PROMPT,
        input=[{"role": "user", "content": [
            {"type": "input_image",
             "image_url": "data:%s;base64,%s" % (mimetype or "image/jpeg", b64)},
            {"type": "input_text", "text": consigne}]}])

    texte = (res.output_text or "").strip()
    if texte.startswith("```"):
        texte = texte.strip("`")
        texte = texte.split("\n", 1)[1] if texte.lower().startswith("json") else texte
        texte = texte.rsplit("```", 1)[0]
    data = json.loads(texte)

    for k in ("total_ht", "total_tva", "stamp_duty", "total_ttc", "vat_rate"):
        data[k] = _to_float(data.get(k))
    ht, tva, ttc = data.get("total_ht"), data.get("total_tva"), data.get("total_ttc")
    stamp = data.get("stamp_duty") or 0.0
    data["_balanced"] = (ht is not None and tva is not None and ttc is not None
                         and abs((ht + tva + stamp) - ttc) < 0.01)
    data["_model"] = model
    data["_scan"] = True
    return data


def extract_invoice_any(pdf_bytes: bytes, extra_hint: str = None) -> dict:
    """Le texte d'abord, le scan ensuite. C'est l'entree a utiliser quand on ignore la nature du PDF.

    L'ordre n'est pas indifferent : lire le texte quand il existe est plus fidele (aucune
    reconnaissance de caracteres) et moins couteux qu'envoyer une image au modele.
    """
    try:
        if pdf_to_text(pdf_bytes).strip():
            return extract_invoice(pdf_bytes, extra_hint=extra_hint)
    except Exception:
        pass
    return extract_invoice_scan(pdf_bytes, extra_hint=extra_hint)
