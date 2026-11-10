"""L'estimation de la déclaration mensuelle (le formulaire papier du comptable), à partir d'ERPNext.

Le comptable rend chaque mois un « projet de déclaration » manuscrit (DECL.STE AQUA 09 2026.pdf,
reçu le 09/10/2026) : retenues à la source, TFP / FOPROLOS, TVA collectée par taux, TVA
récupérable, reliquat, ventes en suspension, droit de timbre, TCL, récapitulatif. On ne l'attend
plus pour savoir ce qu'il va contenir : on l'estime depuis la base, ligne par ligne, et on DIT
pour chaque ligne d'où vient le chiffre.

Trois natures de ligne, affichées comme telles :
  · EXACTE : la base la porte telle quelle (TVA collectée par taux, ventes en suspension, nombre
    de factures, retenue 1 % sur les achats). Septembre 2026 : TVA 19 % identique au dinar près.
  · APPROCHÉE : la base la porte, mais pas découpée comme le comptable (TVA récupérable : il
    isole les immobilisations, nous non ; TCL : il ajoute les retardataires).
  · HYPOTHÈSE : ERPNext ne la connaît pas (masse salariale brute, IRPP, CSS, loyer, honoraires,
    report de TVA). On part de la dernière déclaration connue, et l'écran laisse corriger.

⚠️ TOUT ICI EST PUR : ni frappe, ni base. `estimer` reçoit des nombres déjà lus et rend le
formulaire ; les lectures vivent dans `api.cloture.get_declaration`.
"""
from __future__ import annotations

PRECISION = 3

TAUX = {
    "tfp": 2.0,          # taxe de formation professionnelle, « autres »
    "foprolos": 1.0,
    "css": 1.0,          # contribution sociale de solidarité (sur le formulaire : ligne C.S.S 1 %)
    "loyer": 10.0,
    "honoraires": 3.0,
    "marche": 1.0,       # marchés, personnes morales : 1 % du TTC
    "tcl": 0.2,
    "timbre": 1.0,       # 1,000 DT par facture
}

# Les déclarations rendues par le comptable, relevées à la main sur ses projets. Elles servent de
# point de départ aux hypothèses ET de colonne « déclaré » pour vérifier l'estimation du même mois.
DECLARATIONS_CONNUES = {
    "2026-09": {
        "source": "DECL.STE AQUA 09 2026.pdf (projet reçu le 09/10/2026)",
        "irpp_base": 6226.512, "irpp": 838.242, "css": 28.221,
        "loyer_base": 3055.555, "loyer": 305.555,
        "honoraires_base": 238.0, "honoraires": 7.140,
        "marche_base": 11754.250, "marche": 117.540,
        "retenues_total": 1296.698,
        "salaires_bruts": 6674.863, "tfp": 133.497, "foprolos": 66.748,
        "tva_collectee": [{"taux": 7.0, "base": 9604.828, "tva": 672.337},
                          {"taux": 19.0, "base": 65724.049, "tva": 12487.594}],
        "tva_collectee_total": 13159.931,
        "tva_immobilisations": 537.013, "tva_achats_locaux": 3096.121, "tva_recuperable": 3633.134,
        "report": 0.0, "tva_a_payer": 9526.797,
        "ventes_suspension": 11345.133,
        "timbre_nb": 305, "timbre": 305.0,
        "tcl_base": 99833.941, "tcl": 199.667,
        "total": 11528.407,
    },
}


def _arr(v) -> float:
    return round(float(v or 0), PRECISION)


def derniere_declaration_connue(mois: str | None = None) -> tuple[str | None, dict | None]:
    """La déclaration connue la plus récente jusqu'à `mois` inclus (ou la dernière de toutes).
    Le mois lui-même d'abord : sur un mois déclaré, l'estimation se confronte à ses propres
    hypothèses, et l'écart ne mesure que ce que la base sait."""
    cles = sorted(m for m in DECLARATIONS_CONNUES if not mois or m <= mois)
    if not cles:
        return None, None
    return cles[-1], DECLARATIONS_CONNUES[cles[-1]]


def hypotheses_par_defaut(mois: str | None = None) -> dict:
    """Ce qu'ERPNext ne sait pas, repris de la dernière déclaration connue. Chaque clé est
    corrigeable à l'écran ; `origine` dit d'où viennent les valeurs."""
    origine, d = derniere_declaration_connue(mois)
    if not d:
        return {"origine": None, "salaires_bruts": 0.0, "taux_irpp": 0.0, "taux_css": 0.0,
                "loyer_base": 0.0, "honoraires_base": 0.0, "report": 0.0}
    brut = d["salaires_bruts"] or 1.0
    return {
        "origine": origine,
        "salaires_bruts": d["salaires_bruts"],
        # IRPP et CSS dépendent du barème de chaque salarié : on reprend le POIDS observé sur la
        # masse brute, pas le barème.
        "taux_irpp": round(100.0 * d["irpp"] / brut, 3),
        "taux_css": round(100.0 * d["css"] / brut, 3),
        "loyer_base": d["loyer_base"],
        "honoraires_base": d["honoraires_base"],
        "report": 0.0,
    }


def fusionner_hypotheses(defauts: dict, corrections: dict | None) -> dict:
    """Les hypothèses de l'écran par-dessus les défauts ; une valeur illisible garde le défaut."""
    out = dict(defauts)
    for k, v in (corrections or {}).items():
        if k not in out or k == "origine":
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def _ligne(cle, libelle, base, taux, montant, source, note="") -> dict:
    return {"cle": cle, "libelle": libelle, "base": _arr(base), "taux": taux,
            "montant": _arr(montant), "source": source, "note": note}


def estimer(donnees: dict, hyp: dict) -> dict:
    """Le formulaire estimé. Fonction pure.

    `donnees` (lu par l'API) :
      ventes   : {taux: [{taux, base, tva}], base_exoneree, nombre, total_base, total_tva}
      charges  : {tva_achats, tva_depenses, marche_retenue (le 1 % effectivement retenu sur les
                  achats du mois, lu au compte de retenue), honoraires_retenue (le 3 % retenu sur
                  la note du comptable, None si aucune note n'a été payée dans le mois)}
    `hyp` : voir `hypotheses_par_defaut`.

    Les retenues se lisent au compte « Retenue a la source achat » : c'est ce qui a été retenu,
    donc ce que le comptable déclare — la base se retrouve en divisant par le taux, comme sur
    son formulaire (septembre 2026 : 117,540 → 11 754,250, au dinar près).
    """
    v, c = donnees.get("ventes") or {}, donnees.get("charges") or {}
    brut = _arr(hyp.get("salaires_bruts"))
    honoraires_lus = c.get("honoraires_retenue")

    # --- retenues à la source -------------------------------------------------------------
    irpp = _arr(brut * _arr(hyp.get("taux_irpp")) / 100.0)
    css = _arr(brut * _arr(hyp.get("taux_css")) / 100.0)
    loyer_base = _arr(hyp.get("loyer_base"))
    honoraires_base = _arr(_arr(honoraires_lus) * 100.0 / TAUX["honoraires"]) if honoraires_lus is not None \
        else _arr(hyp.get("honoraires_base"))
    marche_retenue = _arr(c.get("marche_retenue"))
    marche_base = _arr(marche_retenue * 100.0 / TAUX["marche"])
    retenues = [
        _ligne("irpp", "Ret. à la source (salaires)", brut, None, irpp, "hypothese",
               "poids de l'IRPP sur la masse brute, repris de la dernière déclaration"),
        _ligne("css", "C.S.S", brut, TAUX["css"], css, "hypothese",
               "poids observé sur la dernière déclaration"),
        _ligne("loyer", "Loyer", loyer_base, TAUX["loyer"], loyer_base * TAUX["loyer"] / 100, "hypothese",
               "loyer mensuel déclaré, payé avec la déclaration (compte Taxe Loyer)"),
        _ligne("honoraires", "Honoraire", honoraires_base, TAUX["honoraires"],
               honoraires_base * TAUX["honoraires"] / 100,
               "exacte" if honoraires_lus is not None else "hypothese",
               "note d'honoraires du comptable payée dans le mois" if honoraires_lus is not None
               else "aucune note d'honoraires payée dans le mois : dernière déclaration reprise"),
        _ligne("marche", "Marché P.M", marche_base, TAUX["marche"], marche_retenue, "exacte",
               "le 1 % retenu sur les achats et dépenses du mois (compte de retenue d'achat)"),
    ]
    retenues_total = _arr(sum(l["montant"] for l in retenues))

    # --- TFP / FOPROLOS --------------------------------------------------------------------
    tfp = _arr(brut * TAUX["tfp"] / 100)
    foprolos = _arr(brut * TAUX["foprolos"] / 100)

    # --- TVA collectée -----------------------------------------------------------------------
    collectee = [{"taux": float(t.get("taux") or 0), "base": _arr(t.get("base")), "tva": _arr(t.get("tva"))}
                 for t in sorted(v.get("taux") or [], key=lambda t: float(t.get("taux") or 0))]
    collectee_base = _arr(sum(t["base"] for t in collectee))
    collectee_tva = _arr(sum(t["tva"] for t in collectee))

    # --- TVA récupérable ---------------------------------------------------------------------
    tva_achats, tva_depenses = _arr(c.get("tva_achats")), _arr(c.get("tva_depenses"))
    recuperable = _arr(tva_achats + tva_depenses)
    report = _arr(hyp.get("report"))
    solde = _arr(collectee_tva - recuperable - report)
    tva_a_payer = solde if solde > 0 else 0.0
    credit = _arr(-solde) if solde < 0 else 0.0

    # --- autres ------------------------------------------------------------------------------
    suspension = _arr(v.get("base_exoneree"))
    nb_factures = int(v.get("nombre") or 0)
    timbre = _arr(nb_factures * TAUX["timbre"])
    tcl_base = _arr(_arr(v.get("total_base")) + _arr(v.get("total_tva")))
    tcl = _arr(tcl_base * TAUX["tcl"] / 100)

    total = _arr(retenues_total + tfp + foprolos + tva_a_payer + timbre + tcl)
    return {
        "retenues": {"lignes": retenues, "total": retenues_total},
        "tfp": _ligne("tfp", "TFP — autres", brut, TAUX["tfp"], tfp, "hypothese", "sur la masse salariale brute"),
        "foprolos": _ligne("foprolos", "FOPROLOS", brut, TAUX["foprolos"], foprolos, "hypothese", "sur la masse salariale brute"),
        "tva": {
            "collectee": collectee, "collectee_base": collectee_base, "collectee_tva": collectee_tva,
            "source_collectee": "exacte",
            "recuperable": [
                _ligne("tva_achats", "S/achats (factures d'achat)", None, None, tva_achats, "approchee",
                       "le comptable isole les immobilisations, pas nous"),
                _ligne("tva_depenses", "S/dépenses (journal)", None, None, tva_depenses, "approchee", ""),
            ],
            "recuperable_total": recuperable,
            "report": report, "solde": solde, "a_payer": tva_a_payer, "credit": credit,
        },
        "suspension": _ligne("suspension", "Ventes en suspension de TVA", suspension, None, suspension, "exacte",
                             "base à 0 % des factures du mois"),
        "timbre": _ligne("timbre", "Droit de timbres", nb_factures, None, timbre, "exacte",
                         "%d facture(s) × 1,000 DT" % nb_factures),
        "tcl": _ligne("tcl", "T.C.L", tcl_base, TAUX["tcl"], tcl, "approchee",
                      "bases + TVA des factures du mois ; le comptable y ajoute les retardataires"),
        "recap": {"rs": retenues_total, "tfp": tfp, "foprolos": foprolos, "tva": tva_a_payer,
                  "timbre": timbre, "tcl": tcl, "total": total},
    }


def comparer(estimation: dict, declare: dict | None) -> dict:
    """Les écarts estimation − déclaré, clé par clé, quand on connaît la déclaration du mois."""
    if not declare:
        return {}
    e = estimation
    valeurs = {
        "irpp": e["retenues"]["lignes"][0]["montant"], "css": e["retenues"]["lignes"][1]["montant"],
        "loyer": e["retenues"]["lignes"][2]["montant"], "honoraires": e["retenues"]["lignes"][3]["montant"],
        "marche": e["retenues"]["lignes"][4]["montant"], "retenues_total": e["retenues"]["total"],
        "tfp": e["tfp"]["montant"], "foprolos": e["foprolos"]["montant"],
        "tva_collectee_total": e["tva"]["collectee_tva"], "tva_recuperable": e["tva"]["recuperable_total"],
        "tva_a_payer": e["tva"]["a_payer"], "ventes_suspension": e["suspension"]["montant"],
        "timbre": e["timbre"]["montant"], "tcl": e["tcl"]["montant"], "total": e["recap"]["total"],
    }
    return {k: {"estime": v, "declare": _arr(declare.get(k)), "ecart": _arr(v - _arr(declare.get(k)))}
            for k, v in valeurs.items() if k in declare}
