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

# L'espace « Banque » appartient a cette app : c'est la que vivent deja l'identification
# bancaire, le registre des mouvements et la carte technologique. La cloture mensuelle s'y range
# naturellement — c'est le meme geste, le meme mois, et souvent la meme session de travail.
WORKSPACE_BANQUE = "Banque"
PAGE_CLOTURE = "facturation-mensuelle"
LABEL_CLOTURE = "Facturation mensuelle"
ENTETE_CLOTURE = "Clôture mensuelle"

PAGE_PARTENAIRE = "partenaire-economiq"
LABEL_PARTENAIRE = "Economiq Aqua Solution"


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


def _ajouter_entete(ws, texte: str) -> bool:
    """Un titre de section dans la mise en page du workspace, pose une seule fois.

    Sans lui, le raccourci de la cloture atterrirait sous le titre « Identification bancaire »
    et passerait pour un ecran du rapprochement — ce qu'il n'est pas.
    """
    blocs = json.loads(ws.content or "[]")
    if any(b.get("type") == "header" and texte in ((b.get("data") or {}).get("text") or "")
           for b in blocs):
        return False
    blocs.append({"id": frappe.generate_hash(length=10), "type": "header",
                  "data": {"text": '<span class="h4"><b>%s</b></span>' % texte, "col": 12}})
    ws.content = json.dumps(blocs)
    return True


def _ajouter_raccourci_partenaire():
    """Raccourci « Economiq Aqua Solution » dans l espace Banque.

    Le tableau de bord du partenaire etait un onglet de « Facturation mensuelle ». Il n y avait
    pas sa place : le dossier mensuel est une remise au comptable, ponctuelle et datee ; le compte
    du partenaire se suit toute l annee. Deux gestes differents, deux ecrans.
    """
    return _poser_raccourci(PAGE_PARTENAIRE, LABEL_PARTENAIRE, ENTETE_CLOTURE, "Purple")


def _poser_raccourci(page: str, label: str, entete: str, couleur: str):
    """Un raccourci de Page dans l espace Banque, sous son en-tete de section.

    ⚠️ UN RACCOURCI SANS SON BLOC N EXISTE PAS A L ECRAN. Depuis la v15 c est `content` qui rend
    le workspace : la ligne dans `shortcuts` cree l objet, le bloc le POSE. Les deux, ou rien.
    """
    if not frappe.db.exists("Workspace", WORKSPACE_BANQUE):
        return None
    if not frappe.db.exists("Page", page):
        return None

    ws = frappe.get_doc("Workspace", WORKSPACE_BANQUE)
    modifie = False
    if not any(s.link_to == page for s in (ws.shortcuts or [])):
        ws.append("shortcuts", {"type": "Page", "link_to": page, "label": label,
                                "color": couleur})
        modifie = True
    modifie = _ajouter_entete(ws, entete) or modifie
    modifie = _ajouter_bloc(ws, "shortcut", label, col=4) or modifie

    if modifie:
        ws.flags.ignore_permissions = True
        ws.flags.ignore_links = True
        ws.save()
        frappe.db.commit()
    return label


def _ajouter_raccourci_cloture():
    """Raccourci « Facturation mensuelle » dans l espace Banque."""
    return _poser_raccourci(PAGE_CLOTURE, LABEL_CLOTURE, ENTETE_CLOTURE, "Green")


def after_migrate():
    _ajouter_raccourci_paiements()
    _ajouter_carte_tresorerie()
    _ajouter_raccourci_cloture()
    _ajouter_raccourci_partenaire()


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
    """Raccourci « Paiements à faire » dans l'onglet Comptabilite -> le RAPPORT du meme nom.

    Demande utilisateur (19/08) : le raccourci ouvre le rapport « Paiements a faire » (les
    virements que l'outil prepare), pas la liste des ordres. L'ancienne cible (liste
    `BRS Ordre de Paiement` filtree « En attente ») est RETARGETEE en place si elle existe —
    et son bloc de mise en page est repose s'il manque (sans bloc, rien ne s'affiche).
    """
    if not frappe.db.exists("Workspace", WORKSPACE) or not frappe.db.exists("Report", REPORT):
        return None
    ws = frappe.get_doc("Workspace", WORKSPACE)
    modifie = False
    existant = next((s for s in (ws.shortcuts or []) if s.label == LABEL
                     or s.link_to in (DOCTYPE, REPORT)), None)
    if existant is None:
        ws.append("shortcuts", {"type": "Report", "link_to": REPORT, "label": LABEL,
                                "color": "Orange"})
        modifie = True
    elif existant.type != "Report" or existant.link_to != REPORT:
        existant.type = "Report"
        existant.link_to = REPORT
        existant.label = LABEL
        existant.doc_view = ""
        existant.stats_filter = None
        modifie = True
    modifie = _ajouter_bloc(ws, "shortcut", LABEL) or modifie
    if modifie:
        ws.flags.ignore_permissions = True
        ws.flags.ignore_links = True
        ws.save()
        frappe.db.commit()
    return LABEL
