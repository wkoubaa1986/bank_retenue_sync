"""L'image de la caisse especes sur le mois, deleguee au rapport qui existe deja.

⚠️ ON NE RECALCULE PAS LA CAISSE. `customization_app.api.get_caisse_dashboard` appelle le Server
Script `get caisse situation api` et rend les quatre flux (entrees, achats, depenses,
versements) ; la page « Caisse Espèces » s'en sert deja. Refaire ce calcul ici produirait deux
soldes de caisse dans le meme bench, et le jour ou ils divergeraient, personne ne saurait lequel
croire. On lit le meme rapport et on ne fait qu'additionner.

⚠️ ET LE SOLDE INITIAL DU REGLAGE N'EST PAS UN SOLDE DE DEBUT DE MOIS. C'est le solde a la date
d'ORIGINE de la caisse (`date_debut` du reglage, 01/01/2025 aujourd'hui). L'appliquer tel quel a
un mois isole donnait un « solde de fin de mois » qui ne voulait rien dire et ne retombait jamais
sur celui de la page Caisse Espèces. L'ouverture du mois se CUMULE donc depuis l'origine :
  ouverture(M) = solde d'origine + mouvements(origine → veille du 1er de M)
  cloture(M)   = ouverture(M) + mouvements(M)
Au dernier mois couvert, la cloture retombe exactement sur le solde affiche par la page.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_days, flt

from bank_retenue_sync.facturation import periode

PRECISION = 3


def _appeler(nom: str, *args, **kwargs):
    """Appelle une methode de customization_app, ou rend None si elle n'est pas joignable.

    La dependance est reelle mais elle ne doit pas etre fatale : un bench sans customization_app
    doit afficher « caisse indisponible » et servir les quatre autres onglets.

    ⚠️ L'ECHEC EST JOURNALISE, PAS AVALE. Un `except` muet ici a deja fait passer une erreur
    d'import pour une app absente, et l'onglet annoncait tranquillement que le rapport de caisse
    n'existait pas sur un bench ou il etait parfaitement installe.
    """
    try:
        methode = frappe.get_attr("customization_app.api." + nom)
    except Exception:
        return None
    try:
        return methode(*args, **kwargs)
    except Exception:
        frappe.log_error(title="Caisse : appel %s en echec" % nom, message=frappe.get_traceback())
        return None


def _total(lignes) -> float:
    """Toutes les lignes du rapport portent leur montant sous la meme cle, `montant`.

    ⚠️ PAS DE RECHERCHE FLOUE DE COLONNE ICI. Une premiere version essayait `montant`, puis
    `especes`, puis `amount` : le jour ou aucune ne correspond, elle rend zero au lieu d'echouer,
    et un total faux a l'ecran ne se voit pas. Si la cle change, autant que ce soit visible.
    """
    return flt(sum(flt(l.get("montant")) for l in (lignes or [])), PRECISION)


def _reference(versement: dict) -> str:
    """La cle d'exclusion d'un versement : son numero d'ecriture.

    ⚠️ C'EST `journal_entry_number`, ET RIEN D'AUTRE. La page Caisse Espèces coche ses versements
    sur cette cle (`caisse_especes.js`), et le reglage la stocke sous le nom `versement_ref`.
    Une premiere version cherchait `reference` : le filtre ne matchait jamais, les versements
    exclus restaient comptes, et le solde s'en trouvait fausse sans le moindre signal.
    """
    return str(versement.get("journal_entry_number") or "").strip()


def _flux(debut: str, fin: str, exclus: set) -> dict | None:
    """Les quatre flux de la caisse entre deux dates, et le mouvement net qui en resulte."""
    donnees = _appeler("get_caisse_dashboard", debut, fin)
    if donnees is None:
        return None

    entrees = donnees.get("entrees_detail") or []
    achats = donnees.get("sorties_achat") or []
    depenses = donnees.get("sorties_dep") or []
    tous_versements = donnees.get("versements") or []
    versements = [v for v in tous_versements if _reference(v) not in exclus]
    ecartes = [v for v in tous_versements if _reference(v) in exclus]

    t_entrees, t_achats = _total(entrees), _total(achats)
    t_depenses, t_versements = _total(depenses), _total(versements)

    return {
        "periode": {"debut": debut, "fin": fin},
        "entrees": entrees, "achats": achats, "depenses": depenses,
        "versements": versements, "versements_ecartes": ecartes,
        "totaux": {
            "entrees": t_entrees, "achats": t_achats, "depenses": t_depenses,
            "versements": t_versements,
            "mouvement": flt(t_entrees - t_achats - t_depenses - t_versements, PRECISION),
        },
    }


def situation(mois: str) -> dict:
    """La caisse sur le mois : ouverture cumulee, flux du mois, cloture. -> dict."""
    mois = periode.normaliser(mois)
    debut, fin = periode.bornes(mois)
    indisponible = {
        "mois": mois, "libelle": periode.libelle(mois),
        "periode": {"debut": debut, "fin": fin}, "disponible": False,
        "message": "Le rapport de caisse (customization_app) n'est pas disponible sur ce bench.",
    }

    config = _appeler("get_caisse_config")
    if config is None:
        return indisponible
    exclus = {str(v.get("versement_ref") or "").strip()
              for v in (config.get("excluded_versements") or [])}
    exclus.discard("")
    origine = config.get("date_debut")
    solde_origine = flt(config.get("solde_initial"), PRECISION)

    du_mois = _flux(debut, fin, exclus)
    if du_mois is None:
        return indisponible

    anterieur = None
    if origine and str(origine) < debut:
        anterieur = _flux(str(origine), add_days(debut, -1), exclus)
    ouverture = flt(solde_origine + (anterieur["totaux"]["mouvement"] if anterieur else 0.0),
                    PRECISION)

    t = du_mois["totaux"]
    return {
        "mois": mois,
        "libelle": periode.libelle(mois),
        "periode": {"debut": debut, "fin": fin},
        "disponible": True,
        "origine": {
            "date": str(origine) if origine else None,
            "solde": solde_origine,
            "cumul_anterieur": anterieur["totaux"]["mouvement"] if anterieur else 0.0,
            "veille": add_days(debut, -1) if anterieur else None,
        },
        "totaux": {
            "ouverture": ouverture,
            "entrees": t["entrees"],
            "achats": t["achats"],
            "depenses": t["depenses"],
            "versements": t["versements"],
            "mouvement": t["mouvement"],
            "cloture": flt(ouverture + t["mouvement"], PRECISION),
        },
        "entrees": du_mois["entrees"],
        "achats": du_mois["achats"],
        "depenses": du_mois["depenses"],
        "versements": du_mois["versements"],
        # Seuls les versements ECARTES DU MOIS sont signales. Lister les exclusions du reglage
        # entier faisait apparaitre cinq references dont aucune ne concernait le mois affiche.
        "versements_ecartes": du_mois["versements_ecartes"],
    }


def evolution(mois: str) -> dict:
    """Le detail de la caisse du 1er janvier a la fin du mois, MOUVEMENT PAR MOUVEMENT.

    ⚠️ UN SOLDE DE CAISSE NE SE CONTROLE PAS SUR UN TOTAL. Ce qu'on cherche, c'est le point ou
    il decroche : le jour ou une sortie ne colle pas, ou le solde passe sous zero. Un cumul
    mensuel le cache — il faut la suite des mouvements, chacun avec son solde apres passage.

    Chaque ligne porte de quoi remonter a la piece : la date, la nature, le tiers ou le libelle,
    la reference du document. Sans ca, un ecart repere ne mene nulle part.
    """
    mois = periode.normaliser(mois)
    annee, _numero = periode.eclater(mois)
    _, fin = periode.bornes(mois)
    debut_annee = "%04d-01-01" % annee

    config = _appeler("get_caisse_config")
    if config is None:
        return {"disponible": False,
                "message": "Le rapport de caisse (customization_app) n'est pas disponible."}
    exclus = {str(v.get("versement_ref") or "").strip()
              for v in (config.get("excluded_versements") or [])}
    exclus.discard("")
    origine = config.get("date_debut")
    solde_origine = flt(config.get("solde_initial"), PRECISION)

    # Ouverture de l'annee : le solde d'origine, plus tout ce qui a bouge avant le 1er janvier.
    ouverture = solde_origine
    if origine and str(origine) < debut_annee:
        anterieur = _flux(str(origine), add_days(debut_annee, -1), exclus)
        if anterieur:
            ouverture = flt(solde_origine + anterieur["totaux"]["mouvement"], PRECISION)

    flux = _flux(debut_annee, fin, exclus)
    if flux is None:
        return {"disponible": False, "message": "Le rapport de caisse n'a rien rendu."}

    points = []
    for l in flux["entrees"]:
        points.append({"date": l.get("date"), "nature": "Entrée vente",
                       "libelle": l.get("client") or "", "piece": l.get("invoice_number") or "",
                       "entree": flt(l.get("montant"), PRECISION), "sortie": 0.0})
    for l in flux["achats"]:
        points.append({"date": l.get("date"), "nature": "Achat",
                       "libelle": l.get("supplier") or "", "piece": l.get("invoice_number") or "",
                       "entree": 0.0, "sortie": flt(l.get("montant"), PRECISION)})
    for l in flux["depenses"]:
        points.append({"date": l.get("date"), "nature": "Dépense",
                       "libelle": " ".join(str(l.get("description") or "").split()),
                       "piece": l.get("journal_entry_number") or "",
                       "entree": 0.0, "sortie": flt(l.get("montant"), PRECISION)})
    for l in flux["versements"]:
        points.append({"date": l.get("date"), "nature": "Versement en banque",
                       "libelle": " ".join(str(l.get("description") or "").split()),
                       "piece": l.get("journal_entry_number") or "",
                       "entree": 0.0, "sortie": flt(l.get("montant"), PRECISION)})

    # ⚠️ L'ORDRE DECIDE DU SOLDE. Deux mouvements du meme jour se suivent par nature — entrees
    # d'abord, versement en dernier — parce que c'est l'ordre reel : on encaisse, on paie, puis
    # on porte le reste a la banque. L'inverse ferait passer le solde sous zero pour rien.
    rang = {"Entrée vente": 0, "Achat": 1, "Dépense": 2, "Versement en banque": 3}
    points.sort(key=lambda p: (str(p["date"]), rang.get(p["nature"], 9)))

    courant = ouverture
    for p in points:
        courant = flt(courant + p["entree"] - p["sortie"], PRECISION)
        p["solde"] = courant

    t = flux["totaux"]
    return {
        "disponible": True,
        "annee": annee,
        "periode": {"debut": debut_annee, "fin": fin},
        "ouverture": ouverture,
        "points": points,
        "cloture": courant,
        "cumul": {"entrees": t["entrees"], "achats": t["achats"],
                  "depenses": t["depenses"], "versements": t["versements"]},
    }
