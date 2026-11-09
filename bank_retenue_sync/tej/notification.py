"""BILAN DU PASSAGE TEJ DE NUIT, EN MESSAGE RAVEN (09/11/2026).

JUSQU'ICI IL FALLAIT OUVRIR LA PAGE. Le passage de 04:00 demande un nouvel export des certificats
recus, les rapproche et range les PDF ; pour savoir s'il a tourne, combien de certificats sont
arrives et lesquels appellent une ecriture, il fallait aller lire « Derniere synchronisation » et
les tuiles de « Retenue a la source — Ventes ». Depuis que le portail n'accepte que les IP
tunisiennes (22/10/2026), une question de plus : le relais a-t-il tenu ? Ce module repond aux trois
dans un message Raven envoye a la fin du passage, par un bot dedie, a chaque destinataire regle.

CE QUE LE MESSAGE DIT, ET D'OU CA VIENT :
- le passage : `out["ingestion"]["statut"]` rendu par `orchestrator.run_certificats_ras`
  (« synchronise » = export renouvele et lu, « export inchange » = le portail n'a rien de neuf),
  ou l'echec si la tache a leve (le planificateur l'a deja journalise dans Error Log « brs scheduler ») ;
- les nouveaux certificats : `crees` / `revus` de `certificats.synchroniser` (= BRS Sync Run) ;
- ce qui appelle une action : les memes compteurs que les tuiles de la page (`api.certificats._kpis`
  sur l'annee en cours), pour que le message et l'ecran disent la meme chose ;
- le relais : `/health` du service (`tej_proxy`), masque, tel que `app/scrapers/proxy.py` l'expose.

RIEN ICI NE PEUT FAIRE ECHOUER LE PASSAGE : la notification est un a-cote, chaque etape est
enveloppee, et un echec d'envoi finit dans Error Log, jamais dans la tache.

`composer()` est PURE (aucun appel Frappe) : c'est elle que les tests couvrent.
"""
from __future__ import annotations

from datetime import datetime

BOT = "Retenue à la source Bot"
PAGE = "/app/retenue-source-vente"


def _dt(montant) -> str:
    """Un montant en dinars, trois decimales, separateur de milliers : « 2 348,688 DT »."""
    try:
        valeur = float(montant or 0)
    except (TypeError, ValueError):
        valeur = 0.0
    entier, dec = f"{valeur:,.3f}".split(".")
    return f"{entier.replace(',', ' ')},{dec} DT"


def composer(bilan: dict) -> str:
    """Le texte du message (markdown Raven) a partir d'un bilan deja calcule. PURE.

    bilan : {quand (str), passage ("synchronise" | "export inchange" | "echec" | "manuel"),
             erreur (str|None), crees, revus, kpis {sans_piece, retenue_sans_piece, ambigus,
             non_identifies, ecarts, anomalies, total}, relais (dict|None : {serveur, joignable,
             detail, type, session, tirages} ou {erreur}), url (str)}.
    """
    k = bilan.get("kpis") or {}
    passage = bilan.get("passage")
    if passage == "synchronise":
        etat = "✅ terminé — export du portail renouvelé et lu"
    elif passage == "export inchange":
        etat = "✅ terminé — le portail n'a rien de nouveau (export inchangé)"
    elif passage == "manuel":
        etat = "ℹ️ bilan demandé à la main (aucun passage lancé)"
    else:
        etat = "❌ échec — voir Error Log « brs scheduler »" + (f" : {bilan['erreur']}" if bilan.get("erreur") else "")

    lignes = [f"**Retenue à la source — passage TEJ du {bilan.get('quand', '')}**", f"Passage : {etat}"]
    if passage in ("synchronise", "export inchange"):
        lignes.append(f"Nouveaux certificats : **{int(bilan.get('crees') or 0)}** (revus : {int(bilan.get('revus') or 0)})")
    a_trancher = int(k.get("ambigus") or 0) + int(k.get("non_identifies") or 0)
    sans = int(k.get("sans_piece") or 0)
    lignes.append(
        f"Sans écriture : **{sans}** ({_dt(k.get('retenue_sans_piece'))})"
        + (" → à régulariser" if sans else "")
    )
    lignes.append(
        f"À trancher : {a_trancher} · Écarts de montant : {int(k.get('ecarts') or 0)} · "
        f"Anomalies : {int(k.get('anomalies') or 0)} · Certificats de l'année : {int(k.get('total') or 0)}"
    )
    relais = bilan.get("relais")
    if relais is None:
        lignes.append("Relais TEJ : non configuré (le portail doit accepter l'IP du serveur)")
    elif relais.get("erreur"):
        lignes.append(f"Relais TEJ : ⚠️ service injoignable ({relais['erreur']})")
    else:
        if relais.get("joignable"):
            etat_relais = "✅ joignable"
            if relais.get("type") == "session":
                etat_relais += f", session {relais.get('session')}, {int(relais.get('tirages') or 1)} tirage"
                etat_relais += "s" if int(relais.get("tirages") or 1) > 1 else ""
        else:
            etat_relais = "❌ aucun relais ne répond — " + str(relais.get("detail") or "")
        lignes.append(f"Relais TEJ : {relais.get('serveur') or '?'} — {etat_relais}")
    if bilan.get("url"):
        lignes.append(f"[Ouvrir la page]({bilan['url']})")
    return "\n\n".join(lignes)


def relais_depuis_health(health: dict) -> dict | None:
    """Le relais tel que /health le decrit -> le petit dict que `composer` attend. PURE."""
    tp = (health or {}).get("tej_proxy") or {}
    if not tp.get("configure"):
        return None
    actif = next((r for r in tp.get("relais") or [] if r.get("joignable")), None)
    base = actif or {"serveur": tp.get("serveur"), "detail": tp.get("detail")}
    return {"serveur": base.get("serveur"), "joignable": bool(tp.get("joignable")), "detail": base.get("detail"),
            "type": base.get("type"), "session": base.get("session"), "tirages": base.get("tirages")}


# ---------------------------------------------------------------------------------------------
# Ce qui touche Frappe : lecture des compteurs, du service, envoi Raven. Jamais fatal.
# ---------------------------------------------------------------------------------------------

def _kpis_annee() -> dict:
    import frappe
    from frappe.utils import getdate, nowdate

    from bank_retenue_sync.api import certificats as api

    debut = f"{getdate(nowdate()).year}-01-01"
    lignes = frappe.db.get_all(api.DOCTYPE, filters={"hors_perimetre": 0, "date_paiement": [">=", debut]},
                               fields=api.CHAMPS, limit_page_length=0)
    return api._kpis(lignes)


def _relais() -> dict | None:
    import frappe

    from bank_retenue_sync.client import service_client

    try:
        return relais_depuis_health(service_client.health())
    except Exception as e:                      # service eteint, URL fausse, jeton refuse : on le dit, on ne leve pas
        return {"erreur": str(e)[:160].replace("\n", " ") or type(e).__name__}


def bilan_passage(out: dict | None, erreur: str | None = None, passage: str | None = None) -> dict:
    """Le bilan complet a partir du retour de `run_certificats_ras` (None si la tache a leve)."""
    import frappe
    from frappe.utils import now_datetime

    ingestion = (out or {}).get("ingestion") or {}
    if passage is None:
        if out is None:
            passage = "echec"
        elif (out.get("statut") or "ok") != "ok":
            passage, erreur = "echec", erreur or out.get("detail")
        else:
            passage = ingestion.get("statut") or "synchronise"
    return {
        "quand": now_datetime().strftime("%d/%m/%Y %H:%M"),
        "passage": passage,
        "erreur": erreur,
        "crees": ingestion.get("crees", 0),
        "revus": ingestion.get("revus", 0),
        "kpis": _kpis_annee(),
        "relais": _relais(),
        "url": frappe.utils.get_url(PAGE),
    }


def _bot():
    """Le bot Raven qui parle, cree a la premiere utilisation (Raven cree son Raven User en on_update)."""
    import frappe

    if not frappe.db.exists("Raven Bot", BOT):
        frappe.get_doc({"doctype": "Raven Bot", "bot_name": BOT,
                        "description": "Bilan des passages TEJ (retenue à la source) — bank_retenue_sync"}
                       ).insert(ignore_permissions=True)
    return frappe.get_doc("Raven Bot", BOT)


def destinataires() -> list:
    import frappe

    brut = frappe.db.get_single_value("Bank Retenue Sync Settings", "notif_tej_destinataires") or ""
    return [u.strip() for u in brut.replace(",", "\n").splitlines() if u.strip()]


def envoyer(texte: str, utilisateurs: list | None = None) -> dict:
    """Envoie le texte en message direct a chaque destinataire. -> {envoyes, erreurs}."""
    import frappe

    utilisateurs = utilisateurs if utilisateurs is not None else destinataires()
    res = {"envoyes": [], "erreurs": []}
    if not utilisateurs:
        return res
    bot = _bot()
    for user in utilisateurs:
        try:
            if not frappe.db.exists("Raven User", {"user": user}):
                raise ValueError(f"{user} n'est pas un utilisateur Raven")
            bot.send_direct_message(user, text=texte, markdown=True)
            res["envoyes"].append(user)
        except Exception as e:
            res["erreurs"].append(f"{user} : {str(e)[:120]}")
    return res


def notifier_passage(out: dict | None, erreur: str | None = None) -> dict | None:
    """A la fin du passage de nuit : si le reglage est actif, composer et envoyer. Jamais fatal."""
    import frappe

    try:
        if not frappe.db.get_single_value("Bank Retenue Sync Settings", "notif_tej_raven"):
            return None
        bilan = bilan_passage(out, erreur)
        res = envoyer(composer(bilan))
        if res["erreurs"]:
            frappe.log_error(title="brs tej : notification Raven", message="\n".join(res["erreurs"]))
        frappe.db.commit()
        return res
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title="brs tej : notification Raven", message=frappe.get_traceback())
        return None


def envoyer_maintenant(utilisateurs: str | None = None) -> dict:
    """Depuis la console ou `bench execute` : le bilan de l'instant (aucun passage lance), envoye
    aux destinataires regles, ou a `utilisateurs` (emails separes par des virgules) pour l'essayer.
    `bench --site <site> execute bank_retenue_sync.tej.notification.envoyer_maintenant`"""
    import frappe

    liste = [u.strip() for u in utilisateurs.split(",")] if utilisateurs else None
    texte = composer(bilan_passage(None, passage="manuel"))
    res = envoyer(texte, liste)
    frappe.db.commit()
    return {"texte": texte, **res}
