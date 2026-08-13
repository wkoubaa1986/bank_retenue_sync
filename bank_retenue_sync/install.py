"""Injections dans l'espace de travail ERPNext, rejouees a chaque migration.

POURQUOI UN HOOK ET NON UNE FIXTURE
-----------------------------------
Le workspace `Accounting` appartient a ERPNext : toute modification directe est ECRASEE a la
prochaine mise a jour de l'app. On reinstalle donc le raccourci apres chaque `bench migrate`,
de facon idempotente — c'est le seul moyen de le voir survivre la ou l'utilisateur le demande,
dans l'onglet Comptabilite, plutot que de le reléguer dans un espace a part.

Meme raison pour ne rien supprimer : on ajoute si absent, on ne touche a rien d'autre.
"""
from __future__ import annotations

import json

import frappe

WORKSPACE = "Accounting"
DOCTYPE = "BRS Ordre de Paiement"
LABEL = "Paiements à faire"


REPORT = "Paiements a faire"
CARTE = "Trésorerie A&S"


def _ajouter_bloc(ws, type_bloc: str, nom: str, col: int = 3) -> bool:
    """Ajoute le bloc de MISE EN PAGE correspondant a un raccourci ou une carte.

    ⚠️ SANS CE BLOC, RIEN NE S'AFFICHE. Depuis Frappe v15 le workspace se rend a partir du champ
    `content` — une liste de blocs facon editeur — et non plus de ses tables enfant. Ajouter une
    ligne dans `shortcuts` ou `links` cree l'objet mais ne le POSE nulle part : il existe en base,
    invisible a l'ecran. C'est exactement ce qui s'est produit ici.

    `col` est la largeur en douziemes de la grille : 3 = un quart de ligne, comme les raccourcis
    existants de l'onglet Comptabilite.
    """
    cle = "shortcut_name" if type_bloc == "shortcut" else "card_name"
    blocs = json.loads(ws.content or "[]")
    if any(b.get("type") == type_bloc and (b.get("data") or {}).get(cle) == nom for b in blocs):
        return False
    blocs.append({"id": frappe.generate_hash(length=10), "type": type_bloc,
                  "data": {cle: nom, "col": col}})
    ws.content = json.dumps(blocs)
    return True


def after_migrate():
    _ajouter_raccourci_paiements()
    _ajouter_carte_tresorerie()


def _ajouter_carte_tresorerie():
    """Carte « Trésorerie A&S » dans l'onglet Comptabilite : le rapport et la liste des ordres.

    Un raccourci est un bouton ; une carte est une SECTION du sommaire, ou l'on cherche un
    rapport quand on ne se souvient plus de son nom. Les deux se completent, et c'est la carte
    qui rend le tableau de bord trouvable sans le connaitre.
    """
    if not frappe.db.exists("Workspace", WORKSPACE):
        return None
    ws = frappe.get_doc("Workspace", WORKSPACE)
    if any((l.label or "") == CARTE for l in (ws.links or [])):
        if _ajouter_bloc(ws, "card", CARTE, col=4):
            ws.flags.ignore_permissions = True
            ws.flags.ignore_links = True
            ws.save()
            frappe.db.commit()
        return None

    entrees = [
        {"type": "Card Break", "label": CARTE, "link_count": 2, "hidden": 0, "onboard": 0},
        {"type": "Link", "label": "Paiements à faire", "link_type": "Report",
         "link_to": REPORT, "is_query_report": 1, "dependencies": "BRS Ordre de Paiement",
         "hidden": 0, "onboard": 0},
        {"type": "Link", "label": "Ordres de paiement", "link_type": "DocType",
         "link_to": "BRS Ordre de Paiement", "hidden": 0, "onboard": 0},
    ]
    for e in entrees:
        if e["type"] == "Link" and not frappe.db.exists(
                "Report" if e.get("link_type") == "Report" else "DocType", e["link_to"]):
            return None
    for e in entrees:
        ws.append("links", e)
    _ajouter_bloc(ws, "card", CARTE, col=4)
    ws.flags.ignore_permissions = True
    ws.flags.ignore_links = True
    ws.save()
    frappe.db.commit()
    return CARTE


def _ajouter_raccourci_paiements():
    """Raccourci « Paiements à faire » dans l'onglet Comptabilite, filtre sur les ordres en attente.

    Le filtre fait tout le sens de la liste : un ordre passe a « Vire » des que le debit parait
    au releve (`ordres.confirmer_par_banque`), il disparait donc du raccourci de lui-meme. La
    liste ne montre que ce qui reste A FAIRE — c'est ce qui la rend consultable sans tri.
    """
    if not frappe.db.exists("Workspace", WORKSPACE) or not frappe.db.exists("DocType", DOCTYPE):
        return None
    ws = frappe.get_doc("Workspace", WORKSPACE)
    if any(s.link_to == DOCTYPE for s in (ws.shortcuts or [])):
        # La ligne existe : il peut malgre tout manquer son bloc de mise en page.
        if _ajouter_bloc(ws, "shortcut", LABEL):
            ws.flags.ignore_permissions = True
            ws.flags.ignore_links = True
            ws.save()
            frappe.db.commit()
        return None

    ws.append("shortcuts", {
        "type": "DocType", "link_to": DOCTYPE, "label": LABEL,
        "doc_view": "List", "color": "Orange",
        # Le filtre est stocke en JSON par Frappe : c'est lui qui borne la liste aux ordres
        # encore ouverts.
        "stats_filter": frappe.as_json({"statut": "En attente"}),
    })
    _ajouter_bloc(ws, "shortcut", LABEL)
    ws.flags.ignore_permissions = True
    ws.flags.ignore_links = True
    ws.save()
    frappe.db.commit()
    return LABEL
