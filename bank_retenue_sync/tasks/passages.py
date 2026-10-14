"""Journal des passages de verification bancaire (14/10/2026).

Constat prod du 14/10/2026 : le passage de 15h importe deux virements emis (Aramex, 710,410 et
11,700) a 15:01, puis s'arrete sans rien dire — ni reglement des dettes, ni ecriture de frais, ni
Error Log. Le resultat d'un passage n'etait conserve nulle part : `run_verification_bancaire`
rend un dict que `_safe` jetait, et chaque etape avale ses propres erreurs dans ce dict.

Chaque passage laisse donc une fiche `BRS Sync Run` (kind « verification_bancaire ») :
  - ouverte « Running » des le debut, et COMMITEE (un passage tue en plein vol laisse sa trace) ;
  - fermee « Success » / « Partial » (une etape en erreur) / « Failed » (exception), avec le
    resume etape par etape (`payload`) et les erreurs (`error_log`) ;
  - un passage encore « Running » au-dela de DELAI_INTERROMPU_MIN est declare INTERROMPU au tick
    suivant (worker redemarre, job tue).
Tout passage non « Success » alerte les System Managers (notification Desk, comme la panne de
synchro), une fois par passage.
"""

from __future__ import annotations

import json

import frappe

KIND = "verification_bancaire"
DOCTYPE = "BRS Sync Run"
DELAI_INTERROMPU_MIN = 45

#: les etapes du passage, dans l'ordre, avec leur libelle (cles de `run_verification_bancaire`)
ETAPES = (
    ("refresh", "Export bancaire"), ("solde", "Capture du solde"), ("encaissements", "Encaissements"),
    ("especes", "Versements d'espèces"), ("declarations", "Déclaration fiscale"), ("cnss", "CNSS"),
    ("ordres", "Ordres de paiement"), ("depenses", "Dépenses récurrentes"), ("contrats", "Contrats"),
    ("impayes", "Chèques impayés"), ("reglements", "Règlement des dettes (Aramex, honoraires)"),
    ("reclassification", "Reclassification"), ("carte", "Carte technologique"),
    ("ecritures", "Écriture mensuelle de frais"), ("reclassification_frais", "Reclassification (frais)"),
)
#: compteurs de l'import (pas des etapes) : repris dans la ligne « Export bancaire »
COMPTEURS = ("crees", "revus")
#: un diagnostic d'export qui n'est pas « ok » mais signale une panne
MOTS_PANNE = ("illisible", "erreur", "échec", "echec", "error", "timeout", "refus")


# ─── fonctions pures ─────────────────────────────────────────────────────────

def _erreurs_de(valeur) -> list:
    """Les erreurs portees par le resultat d'une etape : {"erreur": …} ou des elements de liste en
    statut « error ». PUR."""
    if isinstance(valeur, dict):
        if valeur.get("erreur"):
            return [str(valeur["erreur"])[:300]]
        if str(valeur.get("status") or "").lower() in ("error", "erreur"):
            return [str(valeur.get("error") or valeur.get("raison") or valeur)[:300]]
        return []
    if isinstance(valeur, list):
        out = []
        for v in valeur:
            for e in _erreurs_de(v):
                quoi = (v.get("je") or v.get("flux") or "") if isinstance(v, dict) else ""
                out.append(("%s : %s" % (quoi, e)) if quoi else e)
        return out
    return []


def _detail_de(cle, valeur, out=None) -> str:
    """Une ligne lisible pour une etape reussie. PUR."""
    if cle == "refresh":
        out = out or {}
        return "%s — %s nouveau(x), %s revu(s)" % (valeur or "—", out.get("crees", 0), out.get("revus", 0))
    if valeur is None:
        return "non lancée"
    if cle == "reglements" and isinstance(valeur, list):
        regles = [v for v in valeur if isinstance(v, dict) and v.get("status") == "regle"]
        attente = [v for v in valeur if isinstance(v, dict) and v.get("status") in ("en attente", "ambigu")]
        return "%d réglée(s)%s" % (len(regles), ", %d en attente" % len(attente) if attente else "")
    if isinstance(valeur, list):
        return "%d élément(s)" % len(valeur)
    if isinstance(valeur, dict):
        for k in ("crees", "created", "nouveaux", "revus", "importes"):
            if k in valeur:
                return "%s : %s" % (k, valeur[k])
        return "ok"
    return str(valeur)[:80]


def resume(out: dict) -> dict:
    """{"statut": "Success" | "Partial", "etapes": [{cle, libelle, ok, detail}], "erreurs": [...]}. PUR."""
    out = out or {}
    etapes, erreurs = [], []
    connues = {c for c, _l in ETAPES} | set(COMPTEURS)
    for cle, libelle in ETAPES + tuple((k, k) for k in out if k not in connues):
        if cle not in out:
            continue
        errs = _erreurs_de(out[cle])
        if cle == "refresh" and isinstance(out[cle], str) and any(m in out[cle].lower() for m in MOTS_PANNE):
            errs = [out[cle][:300]]
        erreurs += ["%s — %s" % (libelle, e) for e in errs]
        etapes.append({"cle": cle, "libelle": libelle, "ok": not errs,
                       "detail": "; ".join(errs)[:300] if errs else _detail_de(cle, out[cle], out)})
    return {"statut": "Partial" if erreurs else "Success", "etapes": etapes, "erreurs": erreurs}


def interrompus(passages: list, maintenant, delai_min: int = DELAI_INTERROMPU_MIN) -> list:
    """Les passages restes « Running » plus de `delai_min` minutes. PUR (`passages` : dicts avec name,
    status, started_at)."""
    from datetime import timedelta

    limite = maintenant - timedelta(minutes=delai_min)
    return [p["name"] for p in passages if p.get("status") == "Running" and p.get("started_at") and p["started_at"] < limite]


# ─── base ─────────────────────────────────────────────────────────────────────

def ouvrir(declencheur: str = "planifié") -> str:
    """La fiche du passage, « Running », commitee tout de suite."""
    doc = frappe.get_doc({"doctype": DOCTYPE, "kind": KIND, "status": "Running",
                          "started_at": frappe.utils.now_datetime(),
                          "payload": json.dumps({"declencheur": declencheur}, ensure_ascii=False)})
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


def fermer(nom: str, out: dict | None = None, exception: Exception | None = None) -> dict:
    """Ferme la fiche : statut, fin, resume ; alerte si le passage n'est pas un succes."""
    if exception is not None:
        res = {"statut": "Failed", "etapes": [], "erreurs": [str(exception)[:500]]}
    else:
        res = resume(out or {})
    try:
        ancien = json.loads(frappe.db.get_value(DOCTYPE, nom, "payload") or "{}")
    except Exception:
        ancien = {}
    frappe.db.set_value(DOCTYPE, nom, {
        "status": res["statut"], "finished_at": frappe.utils.now_datetime(),
        "payload": json.dumps(dict(ancien, etapes=res["etapes"]), ensure_ascii=False, default=str)[:60000],
        "error_log": "\n".join(res["erreurs"])[:10000] or None,
    }, update_modified=False)
    frappe.db.commit()
    if res["statut"] != "Success":
        alerter(nom, res["statut"], res["erreurs"])
    return res


def surveiller() -> list:
    """Au tick : un passage reste « Running » trop longtemps a ete tue en plein vol — on le dit."""
    lignes = frappe.get_all(DOCTYPE, filters={"kind": KIND, "status": "Running"},
                            fields=["name", "status", "started_at"])
    noms = interrompus(lignes, frappe.utils.now_datetime())
    for nom in noms:
        message = ("Interrompu : le passage n'a jamais rendu de résultat (worker redémarré, job tué, "
                   "ou blocage). Les étapes non atteintes seront rejouées au passage suivant.")
        frappe.db.set_value(DOCTYPE, nom, {"status": "Failed", "error_log": message}, update_modified=False)
        alerter(nom, "Interrompu", [message])
    if noms:
        frappe.db.commit()
    return noms


def alerter(nom: str, statut: str, erreurs: list):
    """Notification Desk aux System Managers (+ e-mail selon leurs réglages de notification)."""
    try:
        from bank_retenue_sync.orchestrator import _system_managers

        debut = frappe.db.get_value(DOCTYPE, nom, "started_at")
        sujet = "Vérification bancaire %s : %s" % (frappe.utils.format_datetime(debut, "dd/MM HH:mm") if debut else "",
                                                   {"Partial": "étape en erreur", "Failed": "échec"}.get(statut, statut))
        contenu = "<br>".join(frappe.utils.escape_html(e) for e in erreurs[:10]) + \
            "<br><br>Détail : page Identification bancaire › Dernière vérification (%s)." % nom
        for user in _system_managers():
            frappe.get_doc({"doctype": "Notification Log", "for_user": user, "type": "Alert",
                            "subject": sujet, "email_content": contenu,
                            "document_type": DOCTYPE, "document_name": nom}).insert(ignore_permissions=True)
        frappe.log_error("%s\n%s" % (sujet, "\n".join(erreurs)), "brs verification")
    except Exception as e:
        frappe.log_error("alerte verification bancaire echouee\n%s" % e, "brs verification")


def derniers(limite: int = 10) -> list:
    """Les derniers passages, pour la page : statut, debut, fin, declencheur, etapes, erreurs."""
    out = []
    for r in frappe.get_all(DOCTYPE, filters={"kind": KIND}, fields=["name", "status", "started_at", "finished_at",
                                                                     "payload", "error_log"],
                            order_by="started_at desc", limit_page_length=int(limite or 10)):
        try:
            p = json.loads(r.payload or "{}")
        except Exception:
            p = {}
        out.append({"name": r.name, "status": r.status, "started_at": r.started_at, "finished_at": r.finished_at,
                    "declencheur": p.get("declencheur"), "etapes": p.get("etapes") or [],
                    "erreurs": [e for e in (r.error_log or "").split("\n") if e]})
    return out
