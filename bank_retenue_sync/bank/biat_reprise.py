"""Reprise en production de ce qui a ete fait pour la BIAT sur le site de developpement (16/11/2026).

Demande : « fais le patch pour les actions et les identifications de cheques que j'ai faites en dev, pour les
reproduire en prod ». Sur le dev, l'utilisateur a :
  - range au registre les 7 premiers mouvements du compte BIAT (ouvert le 02/11/2026) ;
  - encaisse la remise 7473685 (3 cheques, par son bordereau lu par l'IA) ;
  - encaisse « sans bordereau » les remises 7473932 et 7473933 (un cheque chacune, designe a l'ecran).

EN DEUX TEMPS, ET CE N'EST PAS UN CHOIX :
  1. `amorcer()` — au `migrate` (patch) : les mouvements entrent au registre. Rien de comptable.
  2. `rejouer()` — au premier PASSAGE de verification qui suit (`biat.passage`) : les trois remises sont
     encaissees avec les memes cheques. ⚠️ Pas au `migrate` : ERPNext n'y execute AUCUN server script
     (`server_script_utils` : `if frappe.flags.in_migrate: return`), et un Encaissement Paiement soumis sans
     son script « Traitement des encaissement » resterait une coquille — soumis, mais les cheques encore en
     portefeuille.

RIEN N'EST FORCE. Les cheques sont retrouves en production par leur NUMERO et leur MONTANT (pas par le nom du
paiement, qui peut differer), parmi ceux encore en portefeuille ; l'encaissement passe par les memes controles
que le bouton « Encaisser sans bordereau » (`biat.encaisser_remise_manuelle`). Une remise deja encaissee, ou
dont un cheque manque, est laissee telle quelle, et dite. La reprise se rejoue a chaque passage tant qu'une
remise reste a faire, puis se tait (`CLE_FAIT`).
"""
from __future__ import annotations

import frappe

BANQUE = "BIAT"
CLE_FAIT = "brs_biat_reprise_2026_11"

#: Les mouvements du compte BIAT du 02/11 au 16/11/2026, tels que le robot les a exportes. Les memes lignes
#: reviendront par le robot : la cle du registre (date, reference, montants) les reconnait, aucun doublon.
MOUVEMENTS = (
    {"date": "2026-11-02", "date_valeur": "2026-11-03", "operation": "Versement TND",
     "reference": "TT26306750847374", "debit": 0.0, "credit": 500.0},
    {"date": "2026-11-12", "date_valeur": "2026-11-10", "operation": "Debit Divers Commission CHQ A Enc/Bordereau : 7473685",
     "reference": "FT26316900881801", "debit": 2.678, "credit": 0.0},
    {"date": "2026-11-12", "date_valeur": "2026-11-13", "operation": "Credit Divers Chq Telec A Enc Cre Global/borde : 7473685",
     "reference": "FT26316013070285", "debit": 0.0, "credit": 3010.127},
    {"date": "2026-11-16", "date_valeur": "2026-11-12", "operation": "Debit Divers Commission CHQ A Enc/Bordereau : 7473932",
     "reference": "FT26320001348690", "debit": 0.893, "credit": 0.0},
    {"date": "2026-11-16", "date_valeur": "2026-11-12", "operation": "Debit Divers Commission CHQ A Enc/Bordereau : 7473933",
     "reference": "FT26320022058630", "debit": 0.893, "credit": 0.0},
    {"date": "2026-11-16", "date_valeur": "2026-11-17", "operation": "Credit Divers Chq Telec A Enc Cre Global/borde : 7473932",
     "reference": "FT26320835306852", "debit": 0.0, "credit": 581.0},
    {"date": "2026-11-16", "date_valeur": "2026-11-17", "operation": "Credit Divers Chq Telec A Enc Cre Global/borde : 7473933",
     "reference": "FT26320835306933", "debit": 0.0, "credit": 1523.125},
)

#: Ce que l'utilisateur a designe sur le dev : {n° de bordereau: [(n° du cheque, montant du paiement)]}.
REMISES = {
    "7473685": (("4001767", 270.0), ("4001066", 1045.0), ("8965167", 1694.88)),
    "7473932": (("0000088", 581.0),),
    "7473933": (("0001259", 1523.125),),
}


def _chiffres(numero) -> str:
    return "".join(c for c in str(numero or "") if c.isdigit()).lstrip("0")


def trouver_cheques(designes, portefeuille: list) -> dict:
    """Les paiements du portefeuille qui sont les cheques designes. Pur.
    `designes` : [(n° du cheque, montant)] ; `portefeuille` : [{name, numero, montant}].
    -> {"paiements": [noms, dans l'ordre], "manquants": [raisons]}. Un cheque est reconnu a son numero (zeros de
    tete ignores) ET a son montant, s'il est SEUL dans ce cas : jamais d'a-peu-pres pour une ecriture comptable."""
    paiements, manquants, pris = [], [], set()
    for numero, montant in designes:
        candidats = [p for p in portefeuille or []
                     if p.get("name") not in pris and _chiffres(p.get("numero")) == _chiffres(numero)
                     and abs(float(p.get("montant") or 0) - float(montant)) <= 0.0005]
        if len(candidats) == 1:
            paiements.append(candidats[0]["name"])
            pris.add(candidats[0]["name"])
        elif not candidats:
            manquants.append(f"chèque n° {numero} ({float(montant):.3f}) : absent du portefeuille")
        else:
            manquants.append(f"chèque n° {numero} ({float(montant):.3f}) : {len(candidats)} paiements possibles")
    return {"paiements": paiements, "manquants": manquants}


def amorcer() -> dict:
    """Range les mouvements au registre (idempotent) et les classe, sans rien comptabiliser."""
    from bank_retenue_sync.bank import biat, registry

    out = registry.upsert_movements([dict(m) for m in MOUVEMENTS], origine="manuel", banque=BANQUE)
    out.pop("cles", None)
    biat.classer_registre(persist=True, _frais=False)
    return out


def fait() -> bool:
    return bool(frappe.db.sql("select 1 from `tabDefaultValue` where parent = '__global' and defkey = %s and defvalue = 'fait' limit 1",
                              CLE_FAIT))


def rejouer() -> dict:
    """Encaisse les remises designees sur le dev. Appele par `biat.passage` ; ne leve jamais.
    -> {} si la reprise est deja faite, sinon {encaissees: [...], deja: [...], en_attente: {n°: raison}}"""
    from bank_retenue_sync.bank import biat, registry
    from bank_retenue_sync.encaissement import pending

    if fait():
        return {}
    out = {"encaissees": [], "deja": [], "en_attente": {}}
    try:
        if not frappe.db.has_column("Encaissement Paiement", "custom_banque_depot") or not biat._script_sait_la_banque():
            return dict(out, en_attente={"toutes": "le script « Traitement des encaissement » n’est pas encore à jour"})
        for numero, designes in REMISES.items():
            if frappe.db.sql("select name from `tabPayment Entry` where docstatus = 1 and reference_no regexp %s limit 1",
                             ("BR:[[:space:]]*" + numero + "([^0-9]|$)",)):
                out["deja"].append(numero)
                continue
            cle = None
            for ligne in frappe.db.get_all(registry.DOCTYPE, filters={"banque": BANQUE, "sens": "Credit"},
                                           fields=["name", "operation"], limit_page_length=0):
                vu = biat.detecter(ligne.operation, "Credit")
                if vu["regle"] == "enc_cheque" and vu["numero"] == numero:
                    cle = ligne.name
                    break
            if not cle:
                out["en_attente"][numero] = "la ligne bancaire de cette remise n’est pas au registre"
                continue
            portefeuille = [{"name": pe["name"], "numero": pe.get("numero"), "montant": pe.get("paid_amount")}
                            for pe in pending._pending_pes(pending.CHEQUE_ACCOUNT, pending.already_encaissed_refs())]
            trouves = trouver_cheques(designes, portefeuille)
            if trouves["manquants"]:
                out["en_attente"][numero] = " ; ".join(trouves["manquants"])
                continue
            try:
                e = biat.encaisser_remise_manuelle(
                    cle, trouves["paiements"],
                    par="reprise des désignations validées sur le site de développement le 16/11/2026")
                out["encaissees"].append({"bordereau": numero, "encaissement": e["encaissement"], "ecart": e["ecart"]})
            except Exception as e:
                frappe.db.rollback()
                out["en_attente"][numero] = frappe.utils.strip_html(str(e))[:200]
        if not out["en_attente"]:
            frappe.db.set_global(CLE_FAIT, "fait")
        frappe.db.commit()
        if out["encaissees"] or out["en_attente"]:
            _prevenir(out)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title="BIAT : reprise des encaissements du dev", message=frappe.get_traceback())
    return out


def _prevenir(out: dict):
    """Une notification aux responsables : la reprise se fait sans personne devant l'ecran."""
    try:
        from bank_retenue_sync.orchestrator import _system_managers

        lignes = [f"Remise {e['bordereau']} encaissée : {e['encaissement']}" for e in out["encaissees"]]
        lignes += [f"Remise {n} déjà encaissée" for n in out["deja"]]
        lignes += [f"Remise {n} NON encaissée — {raison}" for n, raison in out["en_attente"].items()]
        sujet = "BIAT : reprise des encaissements faits sur le site de développement" + (
            " — à regarder" if out["en_attente"] else " — terminée")
        for user in _system_managers():
            frappe.get_doc({"doctype": "Notification Log", "for_user": user, "type": "Alert", "subject": sujet,
                            "email_content": "<br>".join(frappe.utils.escape_html(l) for l in lignes)}).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.log_error(title="BIAT : notification de la reprise", message=frappe.get_traceback())
