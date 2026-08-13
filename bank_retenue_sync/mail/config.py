"""Sources email : configuration lue depuis les Settings, avec repli sur des defauts.

POURQUOI CE MODULE
------------------
Les expediteurs et sujets etaient ecrits DEUX fois, et les deux copies pouvaient diverger sans
que rien ne le signale :
  - `mail/sources.py` les declarait proprement... mais personne ne le lisait ;
  - `orchestrator.py` les reecrivait en dur (`fetch_messages(sender="totalenergies.com", ...)`),
    et c'est cette copie-la qui tournait reellement.

Cette table est desormais la source unique de verite. Les defauts ci-dessous reproduisent
EXACTEMENT ce que l'orchestrateur faisait en dur — changer une valeur ici change le comportement,
ne rien toucher le laisse identique.
"""
from __future__ import annotations

import frappe

SETTINGS = "Bank Retenue Sync Settings"
TABLE_FIELD = "sources_email"

ROLE_DEPENSE = "Depense mensuelle"
ROLE_BANQUE = "Identification bancaire"
ROLE_VERIF = "Verification"

# Valeurs reprises telles quelles de orchestrator.py : toute divergence serait un changement de
# comportement sur des flux valides en production.
DEFAULTS = (
    {"cle": "total_invoice", "libelle": "TotalEnergies - facture mensuelle",
     "role": ROLE_DEPENSE, "expediteurs": "totalenergies.com", "sujet": "facture",
     "limite": 4, "extension": ".zip", "motif_nom_piece_jointe": "",
     "notes": "Autres expediteurs possibles a ajouter si besoin : total.tn, applicam.com."},

    {"cle": "aramex_invoice", "libelle": "Aramex - facture mensuelle (E-INV)",
     "role": ROLE_DEPENSE, "expediteurs": "e.aramex.com", "sujet": "E-INV",
     "limite": 3, "extension": ".pdf", "motif_nom_piece_jointe": ""},

    {"cle": "aramex_payment_advice", "libelle": "Aramex - avis de paiement (virement recu)",
     "role": ROLE_BANQUE, "expediteurs": "e.aramex.com", "sujet": "",
     "limite": 20, "extension": ".xls", "motif_nom_piece_jointe": "",
     "notes": "SUJET VOLONTAIREMENT VIDE : le format du sujet varie, on cherche par expediteur "
              "seul et on retient les pieces jointes Excel. Les factures E-INV, elles, sont des "
              "PDF, donc les deux flux ne se melangent pas."},

    {"cle": "comptable_honoraire", "libelle": "Comptable - note d'honoraire",
     "role": ROLE_DEPENSE, "expediteurs": "belghithayman@gmail.com", "sujet": "",
     "limite": 6, "extension": ".pdf", "motif_nom_piece_jointe": "honoraire"},

    {"cle": "comptable_declaration", "libelle": "Comptable - declaration mensuelle (verification)",
     "role": ROLE_VERIF, "expediteurs": "belghithayman@gmail.com", "sujet": "",
     "limite": 25, "extension": ".pdf", "motif_nom_piece_jointe": "decl.ste",
     "notes": "Sert de PIECE JUSTIFICATIVE au prelevement detecte en banque : sans ce PDF, "
              "aucune ecriture de declaration n'est creee."},

    {"cle": "comptable_cnss", "libelle": "Comptable - CNSS trimestriel (verification)",
     "role": ROLE_VERIF, "expediteurs": "belghithayman@gmail.com", "sujet": "CNSS",
     "limite": 15, "extension": ".pdf", "motif_nom_piece_jointe": ""},
)

_DEFAUTS_PAR_CLE = {d["cle"]: d for d in DEFAULTS}


def _normalise(row: dict) -> dict:
    out = {"actif": 1, "sujet": "", "limite": 20, "extension": "",
           "motif_nom_piece_jointe": "", "notes": "", "role": ROLE_DEPENSE}
    out.update({k: v for k, v in row.items() if v is not None})
    out["expediteurs_liste"] = [s.strip() for s in str(out.get("expediteurs") or "").split(",")
                                if s.strip()]
    return out


def load_sources() -> dict:
    """{cle -> source}. Lit la table des Settings ; repli sur les defauts si l'app n'est pas
    installee ou la table vide (le moteur reste utilisable et testable sans base)."""
    rows = []
    try:
        if frappe.db and frappe.db.exists("DocType", SETTINGS):
            doc = frappe.get_cached_doc(SETTINGS)
            rows = [r.as_dict() for r in (doc.get(TABLE_FIELD) or [])]
    except Exception:
        rows = []
    if not rows:
        rows = list(DEFAULTS)
    return {r["cle"]: _normalise(r) for r in rows if r.get("cle")}


def get_source(cle: str) -> dict:
    """Une source par sa cle. Repli sur le defaut si elle a ete supprimee de la table : un flux
    ne doit jamais s'arreter parce qu'une ligne de configuration manque."""
    src = load_sources().get(cle)
    if src:
        return src
    if cle in _DEFAUTS_PAR_CLE:
        frappe.logger("brs").warning("source email '%s' absente de la configuration : defaut utilise", cle)
        return _normalise(dict(_DEFAUTS_PAR_CLE[cle]))
    raise KeyError("source email inconnue : %s" % cle)


def fetch(cle: str, limit: int = None, since=None) -> list:
    """Messages de la source `cle`, selon sa configuration.

    Une recherche IMAP par expediteur (le serveur n'accepte qu'une valeur FROM a la fois), puis
    fusion en dedupliquant sur l'uid. Une source inactive ne rend rien.
    """
    from bank_retenue_sync.mail.mailbox import fetch_messages

    src = get_source(cle)
    if not src.get("actif"):
        return []
    n = limit or src.get("limite") or 20
    out, vus = [], set()
    for expediteur in src["expediteurs_liste"]:
        for msg in fetch_messages(sender=expediteur, subject=(src.get("sujet") or None),
                                  since=since, limit=n):
            if msg["uid"] in vus:
                continue
            vus.add(msg["uid"])
            out.append(msg)
    return out


def attachment_of(cle: str, msg: dict):
    """Piece jointe attendue par la source (extension et/ou fragment de nom). -> (nom, bytes)|None."""
    from bank_retenue_sync.mail.mailbox import attachment

    src = get_source(cle)
    return attachment(msg, ext=(src.get("extension") or None),
                      contains=(src.get("motif_nom_piece_jointe") or None))


def seed_defaults(overwrite: bool = False) -> int:
    """Amorce la table des Settings. Les lignes deja presentes (par `cle`) sont conservees."""
    doc = frappe.get_single(SETTINGS)
    existantes = {r.cle for r in (doc.get(TABLE_FIELD) or []) if r.cle}
    n = 0
    for row in DEFAULTS:
        if row["cle"] in existantes and not overwrite:
            continue
        doc.append(TABLE_FIELD, dict(row))
        n += 1
    if n:
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return n


def message_date(msg: dict):
    """Date de RECEPTION d'un message, depuis son en-tete Date. None si illisible.

    Sert a dater les ecritures de facture fournisseur : la convention retenue est le dernier jour
    du mois precedant cette date (cf. journal.fin_mois_precedent).
    """
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(msg.get("date") or "").date()
    except Exception:
        return None
