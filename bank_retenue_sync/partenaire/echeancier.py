"""L'echeancier du partenaire : trois versements, et ce qu'un ajustement en absorbe.

Fonctions pures — ni frappe, ni base, ni reseau. C'est ici que vivent les regles d'arrondi et
d'absorption, et c'est pour cela qu'elles sont isolees : ce sont elles qui, fausses, produisent
un solde consolide faux pendant des mois sans que rien ne le signale.
"""
from __future__ import annotations

import calendar

PRECISION = 3


def _arr(v) -> float:
    return round(float(v or 0), PRECISION)


def _dernier_jour(annee: int, mois: int) -> str:
    return "%04d-%02d-%02d" % (annee, mois, calendar.monthrange(annee, mois)[1])


def _suivant(annee: int, mois: int) -> tuple[int, int]:
    return (annee + 1, 1) if mois == 12 else (annee, mois + 1)


def brut(total: float, annee: int, mois: int) -> list:
    """Decoupe `total` en trois versements, aux fins des mois M, M+1 et M+2.

    ⚠️ LES CENTIMES VONT SUR LE DERNIER VERSEMENT, jamais repartis. Trois tiers arrondis
    independamment ne resomment pas au total ; l'ecart est minuscule et il se cumule mois apres
    mois dans le consolide, ou il devient une dette fantome que personne ne sait expliquer.
    """
    part = _arr(_arr(total) / 3)
    reste = _arr(_arr(total) - 2 * part)

    a1, m1 = annee, mois
    a2, m2 = _suivant(a1, m1)
    a3, m3 = _suivant(a2, m2)
    return [
        {"date": _dernier_jour(a1, m1), "montant": part, "note": "M (fin du mois)"},
        {"date": _dernier_jour(a2, m2), "montant": part, "note": "M+1"},
        {"date": _dernier_jour(a3, m3), "montant": reste, "note": "M+2"},
    ]


def ajuster(echeancier: list, ajustement: float) -> tuple[list, float]:
    """Deduit `ajustement` de l'echeancier, en commencant par la premiere echeance.

    Rend (echeancier ajuste, report). Le report est ce que l'ajustement n'a pas pu absorber
    faute d'echeances : il descend au consolide au lieu de disparaitre.
    """
    reste = _arr(ajustement)
    ajuste = []
    for e in echeancier or []:
        montant = _arr(e.get("montant"))
        if reste <= 0:
            ajuste.append({**e, "montant": montant, "deduit": 0.0})
        elif reste >= montant:
            reste = _arr(reste - montant)
            ajuste.append({**e, "montant": 0.0, "deduit": montant,
                           "note": "%s — absorbée" % e.get("note", "")})
        else:
            ajuste.append({**e, "montant": _arr(montant - reste), "deduit": reste,
                           "note": "%s — partiellement absorbée" % e.get("note", "")})
            reste = 0.0
    return ajuste, _arr(reste) if reste > 0 else 0.0


def solde_net(benefice_aqua: float, benefice_partenaire: float) -> float:
    """Ce que le bilan d'activite laisse en faveur d'Aqua : son benefice moins celui du partenaire."""
    return _arr(_arr(benefice_aqua) - _arr(benefice_partenaire))


def ajustement(benefice_aqua: float, benefice_partenaire: float, charges: float = 0.0) -> float:
    """Ce qui vient EN DEDUCTION de l'echeancier : solde net du bilan + charges libres.

    ⚠️ L'ECHEANCIER NE SE CALCULE PAS SUR CE MONTANT, IL S'EN TROUVE REDUIT. Le partenaire doit
    ses COMMANDES du mois, etalees en trois versements ; le bilan d'activite et les charges
    portees par Aqua viennent s'imputer dessus. Une premiere version faisait l'inverse — elle
    etalait le solde du bilan et ignorait les commandes — et sortait un echeancier sans rapport
    avec celui que le partenaire recoit : 692 DT au lieu de 9 183 DT sur juin.
    """
    return _arr(solde_net(benefice_aqua, benefice_partenaire) + _arr(charges))


def consolider(historique: list) -> list:
    """Le solde consolide inter-mois. Fonction pure.

    `historique` : [{mois, ajustement, report, echeances: [{date, montant, deduit, note,
    statut, paye, reste}]}] — trie ou non, on trie ici.

    ⚠️ UNE ECHEANCE ABSORBEE N'EXISTE PAS AU CONSOLIDE. L'ajustement du mois l'a deja effacee ;
    la reporter reviendrait a reclamer deux fois la meme somme.

    ⚠️ ET LE REPORT SE DEDUIT DES ECHEANCES LES PLUS ANCIENNES. Ce que l'ajustement d'un mois
    n'a pas pu absorber faute d'echeances descend ici et s'impute par ordre de date : c'est la
    dette la plus vieille qui s'eteint la premiere, pas la plus commode.
    """
    from collections import defaultdict

    totaux = defaultdict(float)
    details = defaultdict(list)
    payes = defaultdict(float)
    restes = defaultdict(float)
    statuts = defaultdict(set)

    for mois in sorted(historique or [], key=lambda m: m.get("mois") or ""):
        cle = mois.get("mois") or ""
        ajustement = _arr(mois.get("ajustement"))
        mention = "  [ajust. %s = %.3f]" % (cle, ajustement) if ajustement else ""

        for e in mois.get("echeances") or []:
            if (e.get("statut") or "non_payé") == "absorbé":
                continue
            montant = _arr(e.get("montant"))
            if montant <= 0:
                continue
            date = e.get("date")
            deduit = _arr(e.get("deduit"))
            totaux[date] = _arr(totaux[date] + montant)
            if deduit:
                details[date].append("%s %s : %.3f − %.3f%s = +%.3f"
                                     % (cle, e.get("note") or "", montant + deduit, deduit,
                                        mention, montant))
            else:
                details[date].append("%s %s : +%.3f" % (cle, e.get("note") or "", montant))
            payes[date] = _arr(payes[date] + _arr(e.get("paye")))
            restes[date] = _arr(restes[date] + _arr(e.get("reste") if e.get("reste") is not None
                                                    else montant))
            statuts[date].add(e.get("statut") or "non_payé")

        report = _arr(mois.get("report"))
        for date in sorted(totaux):
            if report <= 0:
                break
            if totaux[date] >= report:
                totaux[date] = _arr(totaux[date] - report)
                restes[date] = max(0.0, _arr(restes[date] - report))
                details[date].append("↓ Report %s : −%.3f" % (cle, report))
                report = 0.0
            else:
                absorbe = totaux[date]
                report = _arr(report - absorbe)
                totaux[date], restes[date] = 0.0, 0.0
                details[date].append("↓ Report %s : −%.3f (partiel)" % (cle, absorbe))

    out = []
    for date in sorted(totaux):
        if totaux[date] <= 0.001:
            continue
        paye, reste = _arr(payes[date]), _arr(restes[date])
        out.append({"date": date, "montant": totaux[date],
                    "paye": paye or None, "reste": reste or None,
                    "statut": _statut(statuts[date], paye, reste),
                    "detail": "  |  ".join(details[date])})
    return out


def imputer(lignes: list, versements: list) -> tuple[list, float]:
    """Impute des reglements sur des echeances consolidees. Fonction pure.

    Rend (lignes mises a jour, excedent). Chaque ligne recoit `reglements`, la liste des pieces
    qui l'ont eteinte — sans quoi un « payé » a l'ecran ne se rattache a rien de verifiable.

    ⚠️ DE LA PLUS ANCIENNE A LA PLUS RECENTE, JAMAIS AUTREMENT. C'est la dette la plus vieille
    qui s'eteint la premiere : imputer sur l'echeance la plus commode ferait vieillir
    indefiniment un impaye tout en affichant des echeances recentes soldees.

    ⚠️ ET L'AVANCE DEJA PORTEE SUR UNE LIGNE COMPTE. L'amorce inscrit le trop-percu de juin sur
    l'echeance du 31/07 ; l'ignorer reclamerait deux fois les 28,210.
    """
    disponible = _arr(sum(_arr(v.get("montant")) for v in (versements or [])))
    restants = list(versements or [])
    out = []

    for ligne in sorted(lignes or [], key=lambda l: l.get("date") or ""):
        montant = _arr(ligne.get("montant"))
        deja = _arr(ligne.get("paye"))
        reste = max(0.0, _arr(montant - deja))
        pris = min(disponible, reste)
        pris = _arr(pris) if pris > 0 else 0.0

        pieces = []
        besoin = pris
        while besoin > 0.0005 and restants:
            v = restants[0]
            dispo = _arr(v.get("montant"))
            if dispo <= besoin + 0.0005:
                pieces.append({**v, "impute": dispo})
                besoin = _arr(besoin - dispo)
                restants.pop(0)
            else:
                pieces.append({**v, "impute": besoin})
                restants[0] = {**v, "montant": _arr(dispo - besoin)}
                besoin = 0.0

        disponible = _arr(disponible - pris)
        paye = _arr(deja + pris)
        solde = max(0.0, _arr(montant - paye))
        out.append({**ligne, "paye": paye or None, "reste": solde or None,
                    "reglements": pieces,
                    "statut": "payé" if solde <= 0.001 else ("partiel" if paye else "non_payé")})

    return out, _arr(disponible)


def _statut(statuts: set, paye: float, reste: float) -> str:
    if not statuts or statuts == {"absorbé"}:
        return "absorbé"
    if reste <= 0.001:
        return "payé"
    if paye > 0.001:
        return "partiel"
    return "non_payé"
