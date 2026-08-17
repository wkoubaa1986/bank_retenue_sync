"""Ventiler une facture de vente en bases et TVA, PAR TAUX. Fonctions pures.

⚠️ ON NE RETROUVE PAS UNE BASE EN DIVISANT LA TVA PAR SON TAUX. C'est ce que faisait l'outil
d'origine (`get_invoices_pdf.get_tva` : `tva / 0.19`), et cela tient exactement tant que la
facture ne porte qu'un taux et aucune ligne exoneree. Des qu'une facture melange 19 % et 7 %, ou
porte une ligne hors champ, la division rend une base plausible et fausse — plausible etant le
pire des deux, puisque rien ne la signale.

La base exacte est ailleurs : ERPNext ecrit dans chaque ligne de taxe un `item_wise_tax_detail`,
dictionnaire `{code_article: [taux, montant_de_taxe]}`. Croise avec le `net_amount` des lignes,
il donne la base par taux sans aucune division. La division ne sert plus que de repli, et quand
elle sert on le DIT (`source: "division"`).

⚠️ ET L'ECART NE SE LISSE PAS. La somme des bases doit retomber sur le total HT de la facture ;
la somme des TVA sur le total des taxes. Quand ce n'est pas le cas, la fonction rend l'ecart au
lieu de l'absorber : un recapitulatif qui equilibre toujours ne prouve rien.
"""
from __future__ import annotations

import json

PRECISION = 3


def _arr(v) -> float:
    try:
        return round(float(v or 0), PRECISION)
    except (TypeError, ValueError):
        return 0.0


def _taux(v) -> float:
    """19.0 -> 19.0 ; « 19 » -> 19.0 ; None -> 0.0. Le taux sert de CLE, il doit etre stable."""
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _detail_par_article(brut) -> dict:
    """Lit `item_wise_tax_detail` -> {code_article: (taux, montant)}. {} si illisible.

    Le champ est un texte JSON dans la base, mais arrive parfois deja decode selon le chemin de
    lecture. Les deux formes sont acceptees ; toute autre est traitee comme absente, ce qui
    bascule proprement sur le repli par division.
    """
    if not brut:
        return {}
    donnees = brut
    if isinstance(brut, (str, bytes)):
        try:
            donnees = json.loads(brut)
        except (ValueError, TypeError):
            return {}
    if not isinstance(donnees, dict):
        return {}
    out = {}
    for code, valeur in donnees.items():
        if isinstance(valeur, (list, tuple)) and len(valeur) >= 2:
            out[code] = (_taux(valeur[0]), _arr(valeur[1]))
    return out


def ventiler(lignes: list, taxes: list, net_total=None, total_taxes=None) -> dict:
    """Repartit une facture par taux de TVA.

    `lignes` : [{"item_code", "net_amount"}]  — les lignes d'article
    `taxes`  : [{"rate", "tax_amount", "item_wise_tax_detail"}] — la table des taxes

    Rend :
      taux            [{taux, base, tva}] trie par taux decroissant
      base_exoneree   HT des lignes qu'aucune taxe ne touche
      total_base      somme des bases + base exoneree
      total_tva       somme des TVA
      ecart_base      total HT de la facture - total_base   (0 attendu)
      ecart_tva       total des taxes de la facture - total_tva (0 attendu)
      source          « detail » (exact) ou « division » (repli)
    """
    net_par_article = {}
    for ligne in lignes or []:
        code = (ligne.get("item_code") or "").strip()
        net_par_article[code] = _arr(net_par_article.get(code, 0)) + _arr(ligne.get("net_amount"))

    bases, tvas = {}, {}
    articles_taxes = set()
    autres = 0.0
    autres_libelles = []
    source = "detail"

    def _autre(taxe, montant):
        """⚠️ LE TIMBRE FISCAL N'EST PAS DE LA TVA, ET CE N'EST PAS UN ECART. Taxe a montant
        fixe, sans taux : elle entre dans le total des taxes de la facture mais ne se rattache a
        aucune base. Sans ce panier, chaque mois affichait un ecart de TVA egal au nombre de
        factures du mois — et une alerte qui crie tous les mois n'alerte plus de rien."""
        nonlocal autres
        if not montant:
            return
        autres = _arr(autres + montant)
        libelle = (taxe.get("description") or taxe.get("account_head") or "").strip()
        if libelle and libelle not in autres_libelles:
            autres_libelles.append(libelle)

    for taxe in taxes or []:
        montant = _arr(taxe.get("tax_amount"))
        # ⚠️ LE TAUX SE LIT DANS LE DETAIL, PAS SUR LA LIGNE DE TAXE. Quand le taux vient d'un
        # modele de taxe d'article, `rate` vaut 0 sur la ligne alors que chaque article y porte
        # son taux reel. Trier sur `rate` renvoyait donc TOUTE la TVA dans les « autres taxes »,
        # et la ventilation par taux sortait vide — sur des factures parfaitement normales.
        detail = _detail_par_article(taxe.get("item_wise_tax_detail"))
        taxes_par_article = {code: (t, part) for code, (t, part) in detail.items()
                             if t > 0 and part}
        if taxes_par_article:
            for code, (taux, part) in taxes_par_article.items():
                bases[taux] = _arr(bases.get(taux, 0) + net_par_article.get(code, 0))
                tvas[taux] = _arr(tvas.get(taux, 0) + part)
                articles_taxes.add(code)
            continue
        if detail:
            # Detail present mais aucun article taxe : c'est une taxe forfaitaire.
            _autre(taxe, montant)
            continue
        # Pas de detail du tout : le taux de la ligne est la derniere chance.
        taux = _taux(taxe.get("rate"))
        if taux <= 0:
            _autre(taxe, montant)
            continue
        if not montant:
            continue
        source = "division"
        bases[taux] = _arr(bases.get(taux, 0) + montant * 100.0 / taux)
        tvas[taux] = _arr(tvas.get(taux, 0) + montant)

    base_exoneree = _arr(sum(net for code, net in net_par_article.items()
                             if code not in articles_taxes)) if source == "detail" else 0.0

    total_base = _arr(sum(bases.values()) + base_exoneree)
    total_tva = _arr(sum(tvas.values()))
    net_facture = _arr(net_total) if net_total is not None else total_base
    taxes_facture = _arr(total_taxes) if total_taxes is not None else _arr(total_tva + autres)

    return {
        "taux": [{"taux": t, "base": bases[t], "tva": tvas.get(t, 0.0)}
                 for t in sorted(bases, reverse=True)],
        "base_exoneree": base_exoneree,
        "autres_taxes": autres,
        "autres_taxes_libelles": autres_libelles,
        "total_base": total_base,
        "total_tva": total_tva,
        "ecart_base": _arr(net_facture - total_base),
        "ecart_tva": _arr(taxes_facture - total_tva - autres),
        "source": source,
    }


def cumuler(ventilations: list) -> dict:
    """Additionne les ventilations de plusieurs factures, taux par taux.

    Sert au pied du recapitulatif : c'est ce total-la qui part chez le comptable, et c'est donc
    lui qui doit porter les ecarts cumules plutot que de les diluer facture par facture.
    """
    bases, tvas = {}, {}
    exoneree = base = tva = autres = ecart_base = ecart_tva = 0.0
    libelles, divisions = [], 0

    for v in ventilations or []:
        for ligne in v.get("taux") or []:
            t = _taux(ligne.get("taux"))
            bases[t] = _arr(bases.get(t, 0) + _arr(ligne.get("base")))
            tvas[t] = _arr(tvas.get(t, 0) + _arr(ligne.get("tva")))
        exoneree = _arr(exoneree + _arr(v.get("base_exoneree")))
        base = _arr(base + _arr(v.get("total_base")))
        tva = _arr(tva + _arr(v.get("total_tva")))
        autres = _arr(autres + _arr(v.get("autres_taxes")))
        ecart_base = _arr(ecart_base + _arr(v.get("ecart_base")))
        ecart_tva = _arr(ecart_tva + _arr(v.get("ecart_tva")))
        for l in v.get("autres_taxes_libelles") or []:
            if l not in libelles:
                libelles.append(l)
        if v.get("source") == "division":
            divisions += 1

    return {
        "taux": [{"taux": t, "base": bases[t], "tva": tvas.get(t, 0.0)}
                 for t in sorted(bases, reverse=True)],
        "base_exoneree": exoneree,
        "autres_taxes": autres,
        "autres_taxes_libelles": libelles,
        "total_base": base,
        "total_tva": tva,
        "ecart_base": ecart_base,
        "ecart_tva": ecart_tva,
        "factures_par_division": divisions,
    }
