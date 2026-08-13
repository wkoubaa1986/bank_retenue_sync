"""Extraction des montants d'une DECLARATION FISCALE MENSUELLE tunisienne (DECL.pdf).

Le PDF a du texte extractible (formulaire arabe). On envoie le texte a OpenAI et on
recupere les composantes necessaires au mapping comptable de l'utilisateur :

  - total_a_payer      : total general du au fisc (=> Cr Banque)
  - rs_loyer_10        : retenue 10% sur montants payes aux personnes physiques residentes
                         (loyers) => compte "Taxe Loyer"
  - rs_honoraires_3    : retenue 3% sur honoraires => compte "Retenue a la source achat"
  - tva_solde          : solde TVA a payer (ajoute a "Taxe Loyer" s'il est > 0)
  - timbre_fiscal      : droit de timbre => compte "Timbre Fiscal"
  - tcl                : taxe sur les etablissements (T.C.L) => compte "T.C.L"

Le reste (salaires, solidarite, 1%, TFP, FOPROLOS...) est calcule par difference dans
le constructeur d'ecriture et impute a "Impot sur revenu + CNSS".
"""
from __future__ import annotations

import json

from bank_retenue_sync.ai.invoice_extract import _get_client_model_temp, pdf_to_text

_DECL_SYSTEM = (
    "Tu analyses une DECLARATION FISCALE MENSUELLE tunisienne (formulaire, texte arabe/francais). "
    "On veut les MONTANTS PAYES (colonne 'mbلغ الخصم' / montant du retenu). "
    "Reponds STRICTEMENT en JSON (aucun texte hors JSON) avec ces cles : "
    "period (YYYY-MM), currency (str, ex 'TND'), "
    "total_a_payer (number : total general a payer selon ce tir/declaration), "
    "rs_loyer_10 (number : retenue a la source 10% sur les montants payes aux personnes "
    "PHYSIQUES residentes -> loyers ; ligne 'المدفوعة للمقيمين والمستقرين ... أشخاص طبيعيون' au taux 10%), "
    "rs_achat_1pct (number : retenue a la source 1% sur les ACQUISITIONS de biens, "
    "materiels, equipements et services d'un montant >= 1000 dinars TVA comprise, versees aux "
    "societes soumises a l'IS ; ligne 'المبالغ المدفوعة بعنوان الاقتناءات من سلع ومعدات وتجهيزات "
    "وخدمات ... بنسبة 20%' au taux 1%), "
    "tva_solde (number : solde de TVA A PAYER, 0 si aucun), "
    "tva_credit (number : CREDIT / excedent de TVA reportable au mois suivant -> le montant "
    "precede de 'ف' (فائض) sur la ligne 'الأداء على القيمة المضافة' dans le tableau de synthese "
    "'خلاصة الأداءات' ; 0 s'il y a une TVA a payer au lieu d'un credit), "
    "timbre_fiscal (number : معلوم الطابع الجبائي / droit de timbre), "
    "tcl (number : المعلوم على المؤسسات ذات الصبغة الصناعية أو التجارية أو المهنية / taxe locale T.C.L). "
    "Montants a point decimal, sans separateur de milliers. Valeur absente -> 0. "
    "Sois precis sur les taux (10% loyers vs 3% honoraires vs 1%)."
)

_KEYS = ("total_a_payer", "rs_loyer_10", "rs_achat_1pct", "tva_solde", "tva_credit",
         "timbre_fiscal", "tcl")


def _num(v):
    if v in (None, ""):
        return 0.0
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def extract_declaration(pdf_bytes: bytes) -> dict:
    """DECL.pdf -> composantes fiscales (dict). Fait UN appel OpenAI (texte)."""
    client, model, temperature = _get_client_model_temp()
    text = pdf_to_text(pdf_bytes)
    if not text.strip():
        from frappe import throw
        throw("DECL sans texte extractible : vision requise (non disponible).")

    res = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _DECL_SYSTEM},
            {"role": "user", "content": "Texte de la declaration :\n\n" + text[:30000]},
        ],
    )
    data = json.loads(res.choices[0].message.content)
    out = {k: _num(data.get(k)) for k in _KEYS}
    out["period"] = data.get("period")
    out["currency"] = data.get("currency") or "TND"
    out["_model"] = model
    return out
