"""Activer le robot BIAT depuis ERPNext : validation du navigateur par code SMS.

Le portail MyBIAT ne laisse entrer un navigateur qu'il ne connait pas qu'apres un code envoye par SMS. Le
service (`tej-bank-service`) a un job pour cela — `/jobs/biat/enrolement`, qui se connecte et ATTEND le code
depose par `/biat/code-sms` (une centaine de secondes). Ces trois fonctions le pilotent depuis la page
d'identification, pour que l'activation ne demande ni terminal ni acces au serveur :

    demarrer()          -> le robot se connecte ; le portail envoie le SMS s'il en faut un
    deposer_code(code)  -> le code recu est remis au robot (il n'est ni garde ni journalise ici)
    etat(job)           -> ou en est le job ; a la reussite, le releve automatique est active

Reserve aux System Managers. Le code n'est jamais devine ni redemande tout seul : chaque SMS vient d'un geste
de la personne, qui a le telephone en main.
"""
from __future__ import annotations

import re

import frappe

_CODE = re.compile(r"^\d{4,8}$")


def code_propre(code) -> str:
    """Le code SMS, chiffres seuls (« 549 865 » -> « 549865 »). Leve s'il n'a pas 4 a 8 chiffres. Pur."""
    chiffres = "".join(c for c in str(code or "") if c.isdigit())
    if not _CODE.match(chiffres):
        raise ValueError("Le code reçu par SMS est une suite de 4 à 8 chiffres.")
    return chiffres


def lire_job(job: dict) -> dict:
    """Ce que l'ecran a besoin de savoir d'un job d'enrolement. Pur.
    -> {statut: en_cours | reussi | echec, etape, erreur, code_saisi, comptes}"""
    job = job or {}
    statut = {"succeeded": "reussi", "failed": "echec", "cancelled": "echec"}.get(job.get("status"), "en_cours")
    details = ((job.get("result") or {}).get("details")) or {}
    from bank_retenue_sync.bank import biat

    erreur = (job.get("error") or "")[:400] if statut == "echec" else ""
    return {"statut": statut, "etape": ((job.get("progress") or {}).get("step")) or "", "erreur": erreur,
            # Compte verrouille par la banque, ou connexion refusee : ce n'est PAS a recommencer (cf. `motif_suspension`).
            "verrouille": biat.compte_verrouille(erreur), "refus": biat.connexion_refusee(erreur),
            "code_saisi": bool(details.get("code_saisi")), "comptes": len(details.get("comptes") or [])}


def _garde():
    frappe.only_for("System Manager")


@frappe.whitelist()
def demarrer() -> dict:
    """Lance la connexion du robot au portail. Si son navigateur n'est pas reconnu, le portail envoie un SMS."""
    _garde()
    from bank_retenue_sync.bank import movements as mv

    try:
        return {"job": mv.start_job("/jobs/biat/enrolement", {})}
    except Exception as e:
        frappe.throw("Le service bancaire n’a pas pu démarrer le robot BIAT : " + frappe.utils.escape_html(str(e)[:200])
                     + "<br>Le robot BIAT est-il bien déployé sur le service ?")


@frappe.whitelist()
def deposer_code(code) -> dict:
    """Remet au robot le code recu par SMS."""
    _garde()
    import requests

    from bank_retenue_sync.bank import movements as mv

    try:
        code = code_propre(code)
    except ValueError as e:
        frappe.throw(str(e))
    r = requests.post(mv._base_url() + "/biat/code-sms", headers=mv._headers(), json={"code": code}, timeout=30)
    if r.status_code >= 400:
        frappe.throw("Le service a refusé le code : " + frappe.utils.escape_html(r.text[:200]))
    return {"depose": True}


@frappe.whitelist()
def etat(job) -> dict:
    """Ou en est l'enrolement. A la reussite : le releve automatique est active et un premier releve part."""
    _garde()
    import requests

    from bank_retenue_sync.bank import movements as mv

    r = requests.get(mv._base_url() + f"/jobs/{job}", headers=mv._headers(), timeout=30)
    r.raise_for_status()
    out = lire_job(r.json())
    if out["statut"] == "echec":
        from bank_retenue_sync.bank import biat

        # Un code envoye et non saisi, un compte verrouille : le releve automatique doit s'arreter la aussi, et
        # le bandeau le dire — pas seulement quand l'echec vient d'un passage planifie.
        if biat.motif_suspension(out["erreur"]) and frappe.cache.get_value("brs_biat_echec_note:" + str(job)) is None:
            frappe.cache.set_value("brs_biat_echec_note:" + str(job), 1, expires_in_sec=3600)
            biat._noter_releve(False, out["erreur"])
            frappe.db.commit()
    if out["statut"] == "reussi":
        deja = frappe.utils.cint(frappe.db.get_single_value("Bank Retenue Sync Settings", "biat_robot_actif"))
        frappe.db.set_single_value("Bank Retenue Sync Settings", "biat_robot_actif", 1)
        from bank_retenue_sync.bank import biat

        biat.lever_suspension()                        # le navigateur vient d'etre reconnu : le releve reprend
        frappe.db.commit()
        out["active"] = True
        if not deja or frappe.cache.get_value("brs_biat_premier_releve:" + str(job)) is None:
            # Un premier releve tout de suite, en arriere-plan (une fois par enrolement, meme si l'ecran
            # redemande l'etat plusieurs fois).
            frappe.cache.set_value("brs_biat_premier_releve:" + str(job), 1, expires_in_sec=3600)
            frappe.enqueue("bank_retenue_sync.api.biat_robot.premier_releve", queue="long", timeout=1800,
                           job_id="brs-biat-releve", deduplicate=True)
    return out


def premier_releve():
    """Le releve qui suit l'activation : mouvements, solde, encaissements, classement."""
    from bank_retenue_sync.bank import biat

    try:
        biat.passage(refresh=True)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title="BIAT : premier relevé après l'activation du robot", message=frappe.get_traceback())
