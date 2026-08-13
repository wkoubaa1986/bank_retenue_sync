"""Rattachement d'un libelle de virement bancaire a un Customer, via OpenAI.

DERNIER RECOURS uniquement : `encaissement.party.identify` n'appelle ce resolveur que si l'alias
appris et le score fuzzy n'ont pas tranche. Sur une fenetre d'export typique cela represente
quelques appels, pas un par virement — et le nombre decroit a mesure que les alias s'accumulent.

Trois garde-fous, dans cet ordre :
  1. CHOIX FERME : le modele ne recoit qu'une liste courte de clients et ne peut repondre que par
     un nom de cette liste, ou null. Toute reponse hors liste est REJETEE (anti-hallucination) —
     meme principe que `ai/remise_extract`.
  2. SEUIL DE CONFIANCE : en dessous de `MIN_CONFIDENCE`, on prefere ne rien apparier.
  3. AUCUN CALCUL : le modele identifie un client, il ne choisit jamais quelles dettes solder ni
     quels montants. L'arithmetique au millime reste au subset-sum deterministe (`allocation.py`).
"""
from __future__ import annotations

import json

import frappe

from bank_retenue_sync.ai.invoice_extract import _get_client_model_temp

MIN_CONFIDENCE = 0.7

_SYSTEM = (
    "Tu rattaches un LIBELLE DE VIREMENT BANCAIRE tunisien au client correspondant, choisi dans "
    "une liste fermee. Le libelle est bruite : prefixes bancaires ('VIR TN AUTRE BQ'), TRONCATURE "
    "a une longueur fixe (le nom peut etre coupe en plein milieu), lettres parfois eclatees "
    "('S O C O B A T' = 'SOCOBAT'), formes juridiques presentes d'un cote et pas de l'autre "
    "(STE/SARL/SA), personnes physiques avec prenom et nom inverses. "
    "Reponds STRICTEMENT en JSON (aucun texte hors JSON) avec exactement ces cles : "
    "client (string : un nom RECOPIE A L'IDENTIQUE depuis la liste fournie, ou null si aucun ne "
    "correspond), confiance (number entre 0 et 1), raison (string : une phrase courte). "
    "REGLES ABSOLUES : n'invente jamais un nom absent de la liste ; en cas de doute entre deux "
    "clients, reponds null plutot que de choisir au hasard ; une simple ressemblance de secteur "
    "d'activite ne suffit pas, il faut une correspondance de NOM."
)


def choose_customer(operation: str, candidates: list) -> dict:
    """Libelle bancaire + liste fermee de clients -> {'customer', 'confiance', 'raison', '_model'}.
    `customer` vaut None si le modele n'a pas tranche, si sa confiance est trop faible, ou s'il a
    repondu un nom absent de `candidates`."""
    if not operation or not candidates:
        return {"customer": None, "confiance": 0.0, "raison": "aucun candidat", "_model": None}

    client, model, temperature = _get_client_model_temp()
    listing = "\n".join(f"- {c}" for c in candidates)
    user = (
        f"Libelle du virement recu :\n{operation}\n\n"
        f"Clients possibles (recopie exactement l'un de ces noms, ou null) :\n{listing}"
    )
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
        # Certains modeles recents refusent un `temperature` explicite : meme repli que remise_extract.
        if "temperature" in str(e).lower():
            params.pop("temperature", None)
            res = client.chat.completions.create(**params)
        else:
            raise

    data = json.loads(res.choices[0].message.content)
    name = (data.get("client") or "").strip() or None
    try:
        confiance = float(data.get("confiance") or 0.0)
    except (TypeError, ValueError):
        confiance = 0.0
    raison = str(data.get("raison") or "").strip()

    if name and name not in candidates:
        # Le modele a produit un nom qui n'existe pas : on le jette plutot que de le "rapprocher"
        # du plus ressemblant, ce qui reintroduirait exactement le risque qu'on veut eviter.
        return {"customer": None, "confiance": confiance,
                "raison": f"reponse hors liste ({name[:60]})", "_model": model}
    if confiance < MIN_CONFIDENCE:
        return {"customer": None, "confiance": confiance,
                "raison": f"confiance insuffisante ({confiance}) : {raison}"[:160], "_model": model}
    return {"customer": name, "confiance": confiance, "raison": raison, "_model": model}


def resolver(diagnostics: list = None):
    """Fabrique le callable `ai_resolver(operation, candidates) -> nom|None` attendu par
    `party.identify`. Un echec IA (cle absente, quota, reseau) n'est JAMAIS fatal : il retombe sur
    'client non identifie', ce qui produit un diagnostic au lieu d'un rapprochement faux."""
    def _resolve(operation, candidates):
        try:
            out = choose_customer(operation, candidates)
        except Exception as e:
            if diagnostics is not None:
                diagnostics.append({"type": "virement", "operation": operation,
                                    "reason": f"resolution IA indisponible: {str(e)[:100]}"})
            return None
        if not out["customer"] and diagnostics is not None and out.get("raison"):
            diagnostics.append({"type": "virement", "operation": operation,
                                "reason": f"IA sans reponse: {out['raison'][:120]}"})
        return out["customer"]
    return _resolve
