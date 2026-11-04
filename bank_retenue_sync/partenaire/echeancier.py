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

    ⚠️ UNE ECHEANCE NULLE N'ABSORBE RIEN. Un mois sans commande du partenaire a trois echeances
    a 0 : les dire « absorbées » faisait croire que l'ajustement y avait ete impute, alors qu'il
    descend en entier au consolide — octobre 2026 : trois lignes « absorbée » a 0,000 et
    425,070 de report (signale par l'utilisateur le 04/11/2026).
    """
    reste = _arr(ajustement)
    ajuste = []
    for e in echeancier or []:
        montant = _arr(e.get("montant"))
        if montant <= 0:
            ajuste.append({**e, "montant": 0.0, "deduit": 0.0,
                           "note": "%s — néant" % e.get("note", "")})
            continue
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

    ⚠️ LE REPORT NE S'IMPUTE PAS ICI, MAIS DANS `reporter`, APRES LES REGLEMENTS. Le deduire
    ici, avant les reglements, l'inscrivait sur l'echeance la plus ancienne meme payee : le
    report d'octobre 2026 (425,070 dus par Aqua World) aurait « réduit » l'echeance du 31/07,
    reglee depuis aout, et le rapport suivant aurait reecrit au partenaire un montant qu'il a deja
    paye. Le reste du par echeance etait le meme ; c'est ce qu'on affiche qui mentait.
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


def reporter(lignes: list, historique: list) -> tuple[list, float]:
    """Impute le report de chaque mois sur les echeances ENCORE OUVERTES. Fonction pure.

    Rend (lignes, report non impute). `lignes` sort d'`imputer` : les reglements y sont deja.

    ⚠️ LA PLUS ANCIENNE ECHEANCE OUVERTE, PAS LA PLUS ANCIENNE TOUT COURT. Le report est ce que
    l'ajustement d'un mois n'a pas pu absorber faute d'echeances — un mois sans commande, comme
    octobre 2026. C'est la dette la plus vieille qui s'eteint la premiere ; une echeance deja
    payee n'en est plus une. Il reduit le DU de l'echeance qu'il touche, comme l'absorption
    reduit celui de l'echeancier du mois, et le detail le dit.

    ⚠️ CE QUI DEPASSE TOUTES LES ECHEANCES OUVERTES N'EST PAS PERDU. Il revient a l'appelant,
    qui l'ajoute a l'excedent : c'est une avance du partenaire, pas une somme qui disparait.
    """
    out = [dict(l) for l in sorted(lignes or [], key=lambda l: l.get("date") or "")]
    non_impute = 0.0
    for mois in sorted(historique or [], key=lambda m: m.get("mois") or ""):
        report = _arr(mois.get("report"))
        if report <= 0:
            continue
        cle = mois.get("mois") or ""
        for l in out:
            if report <= 0.0005:
                break
            ouvert = _arr(l.get("reste"))
            if ouvert <= 0.001:
                continue
            pris = min(ouvert, report)
            l["montant"] = _arr(_arr(l.get("montant")) - pris)
            reste = _arr(ouvert - pris)
            l["reste"] = reste or None
            l["report"] = _arr(_arr(l.get("report")) + pris)
            l["detail"] = "  |  ".join(x for x in (l.get("detail"), "↓ Report %s : −%.3f%s" % (
                cle, pris, "" if pris >= report - 0.0005 else " (partiel)")) if x)
            paye = _arr(l.get("paye"))
            l["statut"] = (("payé" if paye > 0.001 else "absorbé") if reste <= 0.001
                           else ("partiel" if paye > 0.001 else "non_payé"))
            report = _arr(report - pris)
        non_impute = _arr(non_impute + max(0.0, report))
    # Une echeance entierement eteinte par un report, sans rien de paye, n'existe plus au
    # consolide — comme une echeance absorbee (cf. `consolider`).
    out = [l for l in out if _arr(l.get("montant")) > 0.001 or _arr(l.get("paye")) > 0.001]
    return out, non_impute


def etat(historique: list, versements: list) -> dict:
    """Le consolide et ses totaux. Fonction pure — `consolider`, `imputer`, puis `reporter`."""
    lignes, excedent = imputer(consolider(historique), versements)
    lignes, non_impute = reporter(lignes, historique)
    excedent = _arr(excedent + non_impute)
    return {"lignes": lignes, "excedent": excedent,
            "total": _arr(sum(_arr(l.get("montant")) for l in lignes)),
            "paye": _arr(sum(_arr(l.get("paye")) for l in lignes)),
            "reste": _arr(sum(_arr(l.get("reste")) for l in lignes))}


def changements(avant: list, apres: list) -> list:
    """Ce qu'une projection change au consolide, echeance par echeance. Fonction pure.

    Rend les seules echeances dont le du ou le reste bouge, avec l'avant et l'apres : c'est la
    reponse a « ou s'impute le report de ce mois ? » — en octobre 2026, sur l'echeance du 31/10,
    la plus ancienne encore ouverte.
    """
    a = {l.get("date"): l for l in avant or []}
    p = {l.get("date"): l for l in apres or []}
    out = []
    for date in sorted(set(a) | set(p)):
        la, lp = a.get(date) or {}, p.get(date) or {}
        ma, mp = _arr(la.get("montant")), _arr(lp.get("montant"))
        ra, rp = _arr(la.get("reste")), _arr(lp.get("reste"))
        if abs(ma - mp) > 0.0005 or abs(ra - rp) > 0.0005:
            out.append({"date": date, "nouvelle": date not in a, "montant_avant": ma,
                        "montant_apres": mp, "reste_avant": ra, "reste_apres": rp})
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
