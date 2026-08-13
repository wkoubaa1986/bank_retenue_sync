"""Rapprochement SYMETRIQUE : ce qui manque d'un cote comme de l'autre.

POURQUOI CE MODULE
------------------
`bank/classify.py` ne repond qu'a UNE moitie de la question : « ce mouvement bancaire a-t-il une
piece en face ? ». La moitie inverse — « cette ecriture ERPNext a-t-elle un mouvement bancaire en
face ? » — n'etait posee nulle part. Or les deux ecarts ne se corrigent pas de la meme facon :

  - un MOUVEMENT sans piece  -> il manque une ecriture (l'argent a bouge, la compta l'ignore) ;
  - une PIECE sans mouvement -> soit l'operation n'est pas passee en banque (ecriture a annuler ou
    a corriger), soit elle est passee mais le lien n'a pas ete etabli (rapprochement a faire).

C'est la reponse a l'ecart de solde : tant qu'on ne regarde que le releve, une ecriture inventee ou
saisie deux fois cote ERPNext reste invisible.

LE PIEGE : ERPNEXT ECLATE, LA BANQUE GROUPE
-------------------------------------------
Une remise de cheques est UN credit au releve mais une Payment Entry PAR CHEQUE dans ERPNext
(340 PE pour 158 799 DT sur la periode). Un rapprochement piece a piece declare donc des centaines
de faux « non rapprochees ». Le lien n'est pas le montant : c'est la CLE BANCAIRE (reference `FT…`,
n° de bon de remise, n° de cheque) que la piece cite dans son texte. Verifie sur donnees reelles :
361 pieces sans lien direct, dont 344 rattachees par leur cle. Il n'en reste que 17 a regarder.

L'APPARIEMENT PAR MONTANT EST UN DERNIER RECOURS, ET IL EXIGE L'UNICITE DES DEUX COTES
--------------------------------------------------------------------------------------
Beaucoup d'ecritures manuelles ne citent AUCUNE reference bancaire : « DÉPENSE DIVERS : ACHAT
CLAVIERS SANS FIL » face a « REGLEMENT CB 0405MYTEK INFOR ». Rien ne les relie que le montant et la
date. On les apparie alors, mais seulement si le candidat est unique DES DEUX COTES — sinon les
deux transferts d'especes de 20 000 DT du 06/05 se rattacheraient au hasard l'un a l'autre.
Cf. le piege inverse documente dans la memoire projet : une recherche par montant ne voit pas les
ecritures GROUPEES (3 paiements Orange dans une seule ecriture), d'ou l'ordre : cle d'abord,
montant ensuite, et jamais de conclusion « non comptabilise » sur le seul montant.
"""
from __future__ import annotations

import re
from collections import defaultdict

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.bank import registry, rules as R

# En deca, une cle se retrouve par hasard dans n'importe quel texte (meme seuil que
# `lookup.MIN_REF_LEN`, et meme raison).
MIN_CLE_LEN = 6

# Ecart de dates admis entre le passage en banque et la comptabilisation. Meme valeur que
# `classify.FENETRE_ECHEANCE` : les saisies devancent ou suivent le releve de un a trois jours.
FENETRE_JOURS = 4

# Une piece posterieure a cette marge par rapport au dernier mouvement importe ne prouve rien :
# le releve ne va pas encore jusque-la. Cas reel : deux remises de cheques des 08 et 10/08 pour un
# registre qui s'arrete le 10/08.
MARGE_FIN_REGISTRE = 2

# Les montants sont en millimes : « exact » veut dire ecart nul a 4 millimes pres, ce qui est
# strictement equivalent au `< 0.005` employe partout ailleurs dans l'app.
MARGE_EXACTE = 0.004

STATUT_PROBABLE = "Probable"
STATUT_ECART_MONTANT = "Ecart de montant"
STATUT_DOUBLON = "Doublon probable"
STATUT_SANS_TRACE = "Sans trace"
STATUT_TROP_RECENT = "Trop recent"
STATUT_HORS_REGISTRE = "Hors registre"

# Ordre d'affichage et de synthese : du plus explicable au plus inquietant.
STATUTS_ECART = (STATUT_PROBABLE, STATUT_ECART_MONTANT, STATUT_DOUBLON, STATUT_HORS_REGISTRE,
                 STATUT_TROP_RECENT, STATUT_SANS_TRACE)

_RUN_CHIFFRES = re.compile(r"\d{%d,}" % MIN_CLE_LEN)


def cles_du_mouvement(m: dict, rule=None) -> set:
    """Cles par lesquelles une piece ERPNext peut citer ce mouvement.

    La reference bancaire (`FT…`) et, quand la regle sait l'extraire, le NUMERO porte par le
    libelle : n° de cheque (« REGLEMENT CHEQUE 4000968 … »), n° de bon de remise, n° d'effet.
    Ce numero est souvent le SEUL lien : les reglements fournisseurs le portent dans leur
    `reference_no` sans jamais citer la reference bancaire.
    """
    cles = set()
    ref = (m.get("reference") or "").strip().upper()
    if len(ref) >= MIN_CLE_LEN:
        cles.add(ref)
    rule = rule if rule is not None else R.find_rule(m)
    numero = R.extract_numero(rule, m) if rule else None
    if numero and len(str(numero).strip()) >= MIN_CLE_LEN:
        cles.add(str(numero).strip().upper())
    return cles


def cles_du_releve(movements) -> set:
    """Toutes les cles bancaires connues du registre, en un seul ensemble."""
    cles = set()
    for m in movements or []:
        cles |= cles_du_mouvement(m)
    return cles


def piece_cite(piece: dict, cles: set) -> set:
    """Cles du releve citees par le texte de cette piece.

    Test de sous-chaine sur le texte NORMALISE en majuscules — la comparaison doit rester
    STRICTE sur les suites de chiffres : une reference fautive (« 400966 » pour un cheque
    4000966) ne doit pas etre rattrapee en silence, elle doit ressortir.
    """
    texte = str(piece.get("texte") or "").upper()
    if not texte:
        return set()
    return {c for c in cles if c in texte}


def apparier_par_montant(mouvements: list, pieces: list, fenetre: int = FENETRE_JOURS,
                         marge: float = MARGE_EXACTE) -> dict:
    """{cle du mouvement -> piece} pour les paires SANS reference commune.

    Dernier recours, et le plus fragile : on n'apparie que si le candidat est unique DES DEUX
    COTES. Deux mouvements du meme montant le meme jour, ou deux pieces du meme montant, ne sont
    jamais tranches — ils ressortent tels quels, a l'arbitrage humain.

    `marge=None` autorise l'ecart de `tolerance()` au lieu du montant exact. C'est le cas d'une
    piece saisie pour un montant FAUX : la recharge Total du 20/07, prelevee 703,500 et
    comptabilisee 703,000. Sans cette seconde passe, le prelevement ressort « non comptabilise »
    et la piece « sans mouvement bancaire » — deux verdicts faux pour une seule erreur de saisie,
    et l'ecart de 0,500 reste invisible. L'appelant marque alors la ligne « a verifier », jamais
    « identifie » : le lien est probable, le montant ne l'est pas.
    """
    candidats: dict = defaultdict(list)
    inverse: dict = defaultdict(list)
    for m in mouvements or []:
        jour = getdate(m["date"]) if m.get("date") else None
        if not jour:
            continue
        montant = flt(m.get("montant"), 3) or flt(m.get("debit"), 3) or flt(m.get("credit"), 3)
        seuil = marge if marge is not None else tolerance(montant)
        for p in pieces or []:
            if p.get("sens") != m.get("sens"):
                continue
            if abs(flt(p.get("montant"), 3) - montant) > seuil:
                continue
            if abs((getdate(p["posting_date"]) - jour).days) > fenetre:
                continue
            candidats[m["cle"]].append(p)
            inverse[p["voucher_no"]].append(m["cle"])
    return {cle: ps[0] for cle, ps in candidats.items()
            if len(ps) == 1 and len(inverse[ps[0]["voucher_no"]]) == 1}


def tolerance(montant: float) -> float:
    """Meme convention que `classify._tolerance_echeance` : plancher 1 DT, 0,5 %, plafond 10 DT."""
    return min(max(1.0, 0.005 * abs(flt(montant, 3))), 10.0)


def _candidats_montant(montant: float, jour, sens: str, autres: list, cle_date: str,
                       fenetre: int = FENETRE_JOURS, marge: float = 0.005) -> list:
    """Elements de `autres` du meme sens et du meme montant (a `marge` pres), a `fenetre` jours.

    `marge` elargie sert a retrouver les operations comptabilisees pour un montant FAUX : le
    cas reel est une recharge Total saisie 703,000 alors que la banque a preleve 703,500. Sans
    cette seconde passe, la piece ressort « aucune trace » et le prelevement « non comptabilise »
    — deux verdicts faux pour une seule erreur de saisie.
    """
    if not jour:
        return []
    jour = getdate(jour)
    return sorted(
        [a for a in autres
         if a.get("sens") == sens
         and abs(flt(a.get("montant"), 3) - flt(montant, 3)) <= marge
         and a.get(cle_date)
         and abs((getdate(a[cle_date]) - jour).days) <= fenetre],
        key=lambda a: (round(abs(flt(a.get("montant"), 3) - flt(montant, 3)), 3),
                       abs((getdate(a[cle_date]) - jour).days)))


def doublons_de_pieces(pieces: list) -> dict:
    """{voucher_no -> [jumelles]} parmi les pieces qu'AUCUN mouvement ne rapproche.

    Deux pieces de meme date, meme sens et meme montant, dont aucune n'a de contrepartie au
    releve, sont le signalement le plus direct d'une double comptabilisation. Cas reel : la
    recharge Total du 30/04 comptabilisee par `ACC-JV-2026-00304` le jour meme, puis a nouveau
    par `ACC-JV-2026-00466` le 03/07 lors d'un rattrapage — alors que le releve ne porte QU'UN
    debit de 603 ce jour-la.

    On n'affirme rien : deux reglements identiques le meme jour existent. Mais le fait qu'aucune
    des deux ne soit rapprochee est ce qui distingue le doublon du hasard, et c'est un verdict
    autrement plus actionnable que « anterieure au registre ».

    ⚠️ Date EXACTE, pas une fenetre : un ecart de quelques jours designe deux operations
    distinctes bien plus souvent qu'une saisie en double.
    """
    par_cle: dict = defaultdict(list)
    for p in pieces or []:
        par_cle[(str(p.get("posting_date")), p.get("sens"),
                 round(flt(p.get("montant"), 3), 3))].append(p)
    out: dict = {}
    for jumelles in par_cle.values():
        if len(jumelles) < 2:
            continue
        for p in jumelles:
            out[p["voucher_no"]] = [j["voucher_no"] for j in jumelles
                                    if j["voucher_no"] != p["voucher_no"]]
    return out


def _indices_hors_fenetre(montant: float, sens: str, jour, autres: list, cle_date: str,
                          etiquette) -> list:
    """Memes montant et sens, mais HORS de la fenetre de dates. Un indice, pas un appariement.

    Le decalage n'est pas toujours une erreur : les recharges de carte Total et les factures
    Aramex sont comptabilisees au DERNIER JOUR DU MOIS PRECEDENT (cf. `journal.fin_mois_precedent`),
    soit jusqu'a un mois avant le prelevement. Les apparier automatiquement sur cette distance
    creerait des faux positifs entre deux recharges identiques ; les taire laisserait croire que
    l'operation n'est nulle part. On les montre donc, sans conclure.
    """
    jour = getdate(jour) if jour else None
    out = []
    for a in autres or []:
        if a.get("sens") != sens or not a.get(cle_date):
            continue
        if abs(flt(a.get("montant"), 3) - flt(montant, 3)) >= 0.005:
            continue
        if jour and abs((getdate(a[cle_date]) - jour).days) <= FENETRE_JOURS:
            continue
        out.append("%s du %s" % (etiquette(a), a[cle_date]))
    return out[:3]


def rapprochement(date_from=None, date_to=None, compte: str = None) -> dict:
    """Inventaire des deux cotes, et de ce qui manque a chacun.

    Rend :
      `erpnext_sans_banque` : pieces ERPNext qu'aucun mouvement du registre ne cite ;
      `banque_sans_erpnext` : mouvements du registre sans piece, hors arbitrage humain ;
      `totaux`              : compteurs et montants, pour les tuiles de la page.
    """
    from bank_retenue_sync.encaissement.pending import BANK_ACCOUNT
    from bank_retenue_sync.expenses import lookup

    compte = compte or BANK_ACCOUNT
    champs = ["name as cle", "date", "operation", "reference", "debit", "credit", "montant",
              "sens", "statut", "categorie", "raison", "document_type", "document_name",
              "ignore_manuel"]
    # LE LIEN NE S'ARRETE PAS AU BORD DE LA PERIODE. Les cles et les documents se lisent sur TOUT
    # le registre : une piece du 02/06 rattachee a un mouvement du 30/05 est rapprochee, et la
    # declarer « sans mouvement » parce que le filtre commence au 01/06 serait faux. Filtrer les
    # cles avec la fenetre gonflait la liste de 13 a 23 lignes.
    tous = frappe.db.get_all(registry.DOCTYPE, limit_page_length=0, order_by="date asc",
                             fields=champs)
    movements = [m for m in tous if _dans(m.get("date"), date_from, date_to)]
    if not movements:
        return {"erpnext_sans_banque": [], "banque_sans_erpnext": [],
                "totaux": _totaux([], []), "periode": {"du": date_from, "au": date_to},
                "pieces": 0, "mouvements": 0}

    bornes = [m["date"] for m in movements if m.get("date")]
    debut, fin = min(bornes), max(bornes)
    # Bornes du REGISTRE, et non du filtre : « hors registre » veut dire que le releve ne couvre
    # pas cette date, pas que l'utilisateur a restreint son affichage.
    bornes_registre = [m["date"] for m in tous if m.get("date")]
    debut_registre, fin_registre = min(bornes_registre), max(bornes_registre)
    pieces = lookup.pieces_bancaires(date_from or debut, date_to or fin, compte=compte)

    cles = cles_du_releve(tous)
    cites = {m["document_name"] for m in tous if m.get("document_name")}

    # ---- cote ERPNext : la piece est-elle reliee au releve ?
    restants_pieces, liees = [], {}
    for p in pieces:
        if p["voucher_no"] in cites:
            liees[p["voucher_no"]] = "citee par le registre"
            continue
        touchees = piece_cite(p, cles)
        if touchees:
            liees[p["voucher_no"]] = "cle bancaire %s" % ", ".join(sorted(touchees)[:2])
            continue
        restants_pieces.append(p)

    # ---- cote banque : le mouvement a-t-il une piece ?
    restants_mvts = [m for m in movements
                     if not m.get("ignore_manuel")
                     and not m.get("document_name")
                     and m.get("statut") not in ("Identifie", "Ignore")]

    # Les mouvements deja rattaches ne peuvent plus servir de candidat : proposer un mouvement
    # identifie comme contrepartie d'une piece orpheline suggererait un doublon inexistant.
    dispo_mvts = [dict(m, montant=flt(m.get("montant"), 3)) for m in restants_mvts]

    jumelles = doublons_de_pieces(restants_pieces)
    for p in restants_pieces:
        exacts = _candidats_montant(p["montant"], p["posting_date"], p["sens"], dispo_mvts, "date")
        proches = exacts or _candidats_montant(p["montant"], p["posting_date"], p["sens"],
                                               dispo_mvts, "date",
                                               marge=tolerance(p["montant"]))
        p["candidats"] = [{"cle": c["cle"], "date": c["date"], "operation": c["operation"],
                           "reference": c["reference"], "montant": flt(c["montant"], 3),
                           "statut": c["statut"]} for c in proches[:5]]
        if exacts:
            p["statut_ecart"] = STATUT_PROBABLE
            p["motif"] = ("meme montant au releve le %s (%s) : l'operation est passee en banque, "
                          "mais la piece ne cite aucune reference"
                          % (exacts[0]["date"], (exacts[0]["operation"] or "")[:40]))
        elif proches:
            p["statut_ecart"] = STATUT_ECART_MONTANT
            p["motif"] = ("le releve porte %s le %s (%s) : ecart de %s sur le montant comptabilise"
                          % (flt(proches[0]["montant"], 3), proches[0]["date"],
                             (proches[0]["operation"] or "")[:30],
                             round(flt(proches[0]["montant"], 3) - p["montant"], 3)))
        elif p["voucher_no"] in jumelles:
            # Avant le verdict de periode : « anterieure au registre » est vrai mais inerte,
            # tandis qu'une piece en double se corrige tout de suite.
            p["statut_ecart"] = STATUT_DOUBLON
            p["motif"] = ("meme date, meme sens et meme montant que %s, et aucune des deux n'est "
                          "rapprochee : double comptabilisation probable"
                          % ", ".join(jumelles[p["voucher_no"]][:3]))
        elif getdate(p["posting_date"]) < getdate(debut_registre):
            # Ni erreur ni oubli : le registre ne remonte pas jusque-la. C'est la coupure de
            # debut de periode — la seule facon de la lever est d'importer un export anterieur.
            p["statut_ecart"] = STATUT_HORS_REGISTRE
            p["motif"] = ("anterieure au premier mouvement importe (%s) : le releve ne remonte "
                          "pas jusque-la" % debut_registre)
        elif getdate(p["posting_date"]) > getdate(fin_registre) - _jours(MARGE_FIN_REGISTRE):
            p["statut_ecart"] = STATUT_TROP_RECENT
            p["motif"] = ("posterieure au dernier mouvement importe (%s) : le releve ne va pas "
                          "encore jusque-la" % fin_registre)
        else:
            p["statut_ecart"] = STATUT_SANS_TRACE
            p["motif"] = "aucun mouvement bancaire de ce montant a cette date"
            p["indices"] = _indices_hors_fenetre(
                p["montant"], p["sens"], p["posting_date"], dispo_mvts, "date",
                lambda a: "%s %s" % ((a.get("operation") or "")[:30], a.get("reference") or ""))
            if p["indices"]:
                p["motif"] += " — mais le meme montant existe au releve : %s" % "; ".join(
                    p["indices"])

    for m in restants_mvts:
        exacts = _candidats_montant(m.get("montant"), m.get("date"), m.get("sens"),
                                    restants_pieces, "posting_date")
        proches = exacts or _candidats_montant(m.get("montant"), m.get("date"), m.get("sens"),
                                               restants_pieces, "posting_date",
                                               marge=tolerance(m.get("montant")))
        m["candidats"] = [{"voucher_type": c["voucher_type"], "voucher_no": c["voucher_no"],
                           "posting_date": c["posting_date"], "montant": c["montant"],
                           "texte": (c.get("texte") or "")[:80]} for c in proches[:5]]
        if exacts:
            m["statut_ecart"] = STATUT_PROBABLE
            m["motif"] = ("piece ERPNext du meme montant le %s (%s) : lien a etablir"
                          % (exacts[0]["posting_date"], exacts[0]["voucher_no"]))
        elif proches:
            m["statut_ecart"] = STATUT_ECART_MONTANT
            m["motif"] = ("%s du %s porte %s : ecart de %s avec le prelevement"
                          % (proches[0]["voucher_no"], proches[0]["posting_date"],
                             proches[0]["montant"],
                             round(flt(m.get("montant"), 3) - proches[0]["montant"], 3)))
        else:
            m["statut_ecart"] = STATUT_SANS_TRACE
            m["motif"] = m.get("raison") or "aucune piece ERPNext de ce montant a cette date"
            m["indices"] = _indices_hors_fenetre(
                m.get("montant"), m.get("sens"), m.get("date"), restants_pieces, "posting_date",
                lambda a: a["voucher_no"])
            if m["indices"]:
                m["motif"] += " — mais une piece du meme montant existe : %s" % "; ".join(
                    m["indices"])

    return {
        "periode": {"du": date_from or debut, "au": date_to or fin},
        "mouvements": len(movements), "pieces": len(pieces),
        "liees": len(liees),
        "erpnext_sans_banque": sorted(restants_pieces, key=lambda p: str(p["posting_date"])),
        "banque_sans_erpnext": sorted(restants_mvts, key=lambda m: str(m["date"])),
        "totaux": _totaux(restants_pieces, restants_mvts),
    }


def _groupes_de_rapprochement(movements: list, pieces: list) -> list:
    """Composantes connexes {mouvements, pieces} reliees par une cle bancaire ou un document.

    C'EST LA SEULE UNITE OU L'ECART A UN SENS. Ni la ligne ni la piece ne conviennent :
      - la banque GROUPE (une remise = un credit) la ou ERPNext ECLATE (une piece par cheque) ;
      - ERPNext GROUPE (une ecriture pour trois paiements Orange) la ou la banque ECLATE.
    Comparer 1 pour 1 donne des dizaines de milliers de dinars de faux ecarts dans les deux sens.
    Une composante, elle, s'equilibre exactement quand tout est juste — et son solde non nul est
    un ecart REEL, au millime.
    """
    parent: dict = {}

    def trouver(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def unir(a, b):
        ra, rb = trouver(a), trouver(b)
        if ra != rb:
            parent[ra] = rb

    par_cle: dict = defaultdict(list)
    for m in movements or []:
        noeud = ("M", m["cle"])
        trouver(noeud)
        # UNE REFERENCE `LD…` EST UN NUMERO DE CONTRAT : toutes les echeances du meme leasing la
        # portent. L'utiliser comme cle de graphe fusionnerait juin, juillet et aout en une seule
        # composante — un ecart unique de +3,000 au lieu de trois fois +1,000, un par mois, alors
        # que la correction, elle, est mensuelle. La ligne est donc indexee sous la cle de son
        # GROUPE ; son rattachement a l'ecriture passe par `document_name`, pose par `classify`.
        if str(m.get("groupe") or "").startswith("echeance-"):
            par_cle[m["groupe"]].append(noeud)
            continue
        for k in cles_du_mouvement(m):
            par_cle[k].append(noeud)

    noms_pieces = {p["voucher_no"]: ("P", p["voucher_no"]) for p in pieces or []}
    for p in pieces or []:
        trouver(noms_pieces[p["voucher_no"]])
    # Le document pose par la classification est un lien a part entiere : il vient d'un
    # appariement (montant, n° de cheque, groupe d'echeance) qu'aucune cle textuelle ne porte.
    for m in movements or []:
        cible = noms_pieces.get(m.get("document_name"))
        if cible:
            unir(("M", m["cle"]), cible)
    for p in pieces or []:
        for k in piece_cite(p, set(par_cle)):
            for noeud in par_cle[k]:
                unir(noms_pieces[p["voucher_no"]], noeud)

    index_m = {("M", m["cle"]): m for m in movements or []}
    index_p = {("P", p["voucher_no"]): p for p in pieces or []}
    groupes: dict = defaultdict(lambda: {"mouvements": [], "pieces": []})
    for noeud, m in index_m.items():
        groupes[trouver(noeud)]["mouvements"].append(m)
    for noeud, p in index_p.items():
        groupes[trouver(noeud)]["pieces"].append(p)
    return list(groupes.values())


def _signe(sens: str) -> float:
    """Convention unique : un CREDIT au releve fait entrer de l'argent."""
    return 1.0 if sens == "Credit" else -1.0


def explication_ecart(date_from=None, date_to=None, compte: str = None) -> dict:
    """D'ou vient l'ecart entre le solde bancaire et le solde ERPNext, sur une periode.

    L'identite est EXACTE par construction : chaque mouvement et chaque piece appartient a
    exactement une composante, donc la somme des soldes de composantes vaut l'ecart de flux de
    la periode. Rien ne peut se perdre en route — c'est ce qui distingue cette decomposition
    d'un inventaire de suspects.

        ecart de periode = flux ERPNext - flux banque = somme des soldes de composantes

    ⚠️ NE PAS calculer cet ecart a partir du champ `ecart` du registre : sur une ecriture
    GROUPEE, chacune des N lignes bancaires porte l'ecart du groupe entier, et la somme le
    compte N fois (cas reel : 3 x 51,856 pour les trois paiements Orange du 04/08, alors que
    l'ecriture ACC-JV-2026-00535 les couvre exactement).
    """
    from bank_retenue_sync.encaissement.pending import BANK_ACCOUNT
    from bank_retenue_sync.expenses import lookup

    compte = compte or BANK_ACCOUNT
    # Les composantes se construisent sur TOUT le registre : un lien ne s'arrete pas au bord de
    # la fenetre. Seuls les MONTANTS sont ensuite bornes a la periode.
    movements = frappe.db.get_all(
        registry.DOCTYPE, limit_page_length=0, order_by="date asc",
        fields=["name as cle", "date", "operation", "reference", "debit", "credit", "montant",
                "sens", "statut", "groupe", "document_name"])
    pieces = lookup.pieces_bancaires(compte=compte)

    # LES FRAIS SORTENT DU GRAPHE, ET CE N'EST PAS UN DETAIL.
    # L'ecriture de frais est MENSUELLE : elle est rattachee a des dizaines de commissions
    # etalees sur tout le mois, et chaque commission partage sa reference avec l'operation qui
    # l'a generee. Elle fait donc pont entre toutes les operations du mois — une seule
    # composante de 300 lignes, sans aucune valeur d'analyse. Les frais forment leur propre
    # bloc, exactement comme l'app les comptabilise (cf. expenses/fees.py).
    # Le critere n'est pas le groupe mais L'ECRITURE DE RATTACHEMENT : une TVA sur commission
    # entre dans le meme cumul mensuel sans porter de groupe « frais » (elle est declaree
    # composant d'echeance, cf. classify._resoudre_echeance). La rattacher par son document
    # evite qu'elle ressorte seule, comme un ecart qu'elle n'est pas.
    frais_vouchers = {m["document_name"] for m in movements
                      if (m.get("groupe") or "").startswith("frais-") and m.get("document_name")}

    def _est_frais(m):
        return ((m.get("groupe") or "").startswith("frais-")
                or m.get("document_name") in frais_vouchers)

    frais_mvts = [m for m in movements if _est_frais(m)]
    movements = [m for m in movements if not _est_frais(m)]
    pieces_frais = [p for p in pieces if p["voucher_no"] in frais_vouchers]
    pieces = [p for p in pieces if p["voucher_no"] not in frais_vouchers]

    def dans(jour):
        return _dans(jour, date_from, date_to)

    groupes = []
    for g in _groupes_de_rapprochement(movements, pieces):
        mvts = [m for m in g["mouvements"] if dans(m.get("date"))]
        pcs = [p for p in g["pieces"] if dans(p.get("posting_date"))]
        if not mvts and not pcs:
            continue
        banque = round(sum(_signe(m["sens"]) * flt(m["montant"], 3) for m in mvts), 3)
        erpnext = round(sum(_signe(p["sens"]) * flt(p["montant"], 3) for p in pcs), 3)
        solde = round(erpnext - banque, 3)
        if abs(solde) < 0.005:
            continue
        groupes.append({
            "solde": solde, "banque": banque, "erpnext": erpnext,
            "nature": _nature(mvts, pcs, g),
            "mouvements": [{"cle": m["cle"], "date": m["date"], "reference": m["reference"],
                            "operation": m["operation"], "sens": m["sens"],
                            "montant": flt(m["montant"], 3)} for m in mvts],
            "pieces": [{"voucher_type": p["voucher_type"], "voucher_no": p["voucher_no"],
                        "posting_date": p["posting_date"], "sens": p["sens"],
                        "montant": p["montant"], "texte": (p.get("texte") or "")[:80]}
                       for p in pcs],
        })
    # Bloc des frais : le releve les preleve a l'unite, la comptabilite les cumule par mois.
    # Son solde vaut donc les DELTAS DE PAIEMENT fondus dans l'ecriture mensuelle (cf. la regle
    # enoncee par l'utilisateur : commissions + TVA + delta des paiements). Ces memes deltas
    # ressortent, en sens inverse, sur les composantes des encaissements concernes : ils se
    # compensent, et c'est ce que la decomposition doit montrer plutot que masquer.
    fb = round(sum(_signe(m["sens"]) * flt(m["montant"], 3)
                   for m in frais_mvts if dans(m.get("date"))), 3)
    fe = round(sum(_signe(p["sens"]) * flt(p["montant"], 3)
                   for p in pieces_frais if dans(p.get("posting_date"))), 3)
    if abs(round(fe - fb, 3)) >= 0.005:
        groupes.append({
            "solde": round(fe - fb, 3), "banque": fb, "erpnext": fe,
            "nature": "frais bancaires : preleves a l'unite, comptabilises par cumul mensuel",
            "mouvements": [], "pieces": [
                {"voucher_type": p["voucher_type"], "voucher_no": p["voucher_no"],
                 "posting_date": p["posting_date"], "sens": p["sens"], "montant": p["montant"],
                 "texte": (p.get("texte") or "")[:80]}
                for p in pieces_frais if dans(p.get("posting_date"))],
        })
    groupes.sort(key=lambda g: -abs(g["solde"]))

    flux_banque = round(sum(_signe(m["sens"]) * flt(m["montant"], 3)
                            for m in movements + frais_mvts if dans(m.get("date"))), 3)
    flux_erpnext = round(sum(_signe(p["sens"]) * flt(p["montant"], 3)
                             for p in pieces + pieces_frais if dans(p.get("posting_date"))), 3)
    total = round(sum(g["solde"] for g in groupes), 3)
    return {
        "periode": {"du": date_from, "au": date_to},
        "flux_banque": flux_banque, "flux_erpnext": flux_erpnext,
        "ecart": round(flux_erpnext - flux_banque, 3),
        "groupes": groupes, "explique": total, "synthese": _synthese(groupes),
        # Doit valoir 0 : sinon la decomposition a perdu une ligne, et il faut le voir.
        "controle": round(round(flux_erpnext - flux_banque, 3) - total, 3),
    }


def _synthese(groupes: list) -> list:
    """L'ecart par NATURE. C'est le seul niveau ou l'on decide quoi faire.

    Un decalage de bord de periode (l'encaissement du 03/06 dont les pieces sont datees de mai)
    se resorbe tout seul au mois suivant ; un mouvement sans piece se comptabilise ; un ecart de
    montant se corrige. Les melanger dans un total unique ne dit rien de ce qu'il y a a faire.
    """
    par_nature: dict = {}
    for g in groupes:
        s = par_nature.setdefault(g["nature"], {"nature": g["nature"], "nb": 0, "solde": 0.0})
        s["nb"] += 1
        s["solde"] = round(s["solde"] + g["solde"], 3)
    return sorted(par_nature.values(), key=lambda s: -abs(s["solde"]))


def _nature(mvts: list, pcs: list, groupe: dict) -> str:
    """Ce que le desequilibre signifie, en une phrase — la seule chose qui se corrige."""
    if not pcs:
        return ("mouvement bancaire sans piece dans la periode"
                if not groupe["pieces"] else "piece rattachee hors periode")
    if not mvts:
        return ("piece sans mouvement bancaire dans la periode"
                if not groupe["mouvements"] else "mouvement rattache hors periode")
    return "montants differents entre le releve et la comptabilite"


# Verdicts dont la contrepartie EXISTE deja : le mouvement est au releve (donc dans le solde
# bancaire) ou anterieur au registre. Les resoudre ne deplace pas l'ecart d'un millime — seul le
# LIEN manque. Les inclure dans une projection les compterait deux fois.
VERDICTS_SANS_EFFET = (STATUT_PROBABLE, STATUT_ECART_MONTANT, STATUT_HORS_REGISTRE)


def ecart_ouverture() -> dict:
    """Point de depart du rapprochement, lu dans les Reglages. -> {date, montant}

    POURQUOI UN ECART D'OUVERTURE PARAMETRABLE
    ------------------------------------------
    Le registre ne remonte pas a l'origine du compte : tout ce qui precede le premier export est
    hors de portee, et se retrouve dans l'ecart sans qu'aucune action ne puisse l'y enlever. Le
    figer a une date choisie permet de repartir de zero — on ne pretend pas que l'ecart historique
    n'existe pas, on le declare ACQUIS pour ne plus suivre que ce qui se cree apres.

    C'est la meme convention qu'un solde d'ouverture comptable : la ligne du dessus est un
    constat, pas une mesure.
    """
    try:
        st = frappe.get_cached_doc("Bank Retenue Sync Settings")
        return {"date": st.get("ecart_ouverture_date"),
                "montant": flt(st.get("ecart_ouverture"), 3)}
    except Exception:
        return {"date": None, "montant": 0.0}


def ecart_net(brut: float) -> float:
    """Ecart affiche : le brut, moins l'ecart d'ouverture accepte."""
    if brut is None:
        return None
    return round(flt(brut, 3) - flt(ecart_ouverture().get("montant"), 3), 3)


def mesurer_ecart_a(date_reference) -> float:
    """Ecart (banque − ERPNext) a une date donnee, pour alimenter le reglage d'ouverture.

    Le solde bancaire n'est connu qu'a la date de capture : on le REMONTE en retirant le flux du
    registre posterieur a la date demandee. C'est une deduction, pas une mesure — mais c'est la
    seule possible tant que le portail ne rend pas d'historique de solde.
    """
    from bank_retenue_sync.bank import solde as S

    dernier = S.dernier_solde()
    if not dernier:
        return None
    flux = flt(frappe.db.sql(
        """select sum(credit) - sum(debit) from `tab%s`
           where `date` > %%s and `date` <= %%s""" % registry.DOCTYPE,
        (getdate(date_reference), getdate(dernier.date_solde)))[0][0], 3)
    banque = round(flt(dernier.solde_banque, 3) - flux, 3)
    return round(banque - S.solde_erpnext(date_reference), 3)


def effets_projection(rapport: dict):
    """Effet de chaque poste non rapproche sur l'ecart. -> (postes, effet_delai, effet_correction)

    Partie PURE de la projection : aucun acces base, donc testable telle quelle.

    LE CLASSEMENT SE FAIT PAR NATURE, PAS PAR COTE — et c'est essentiel.
    ----------------------------------------------------------------------
    Un DOUBLON cote ERPNext a presque toujours son pendant cote banque : la recharge Total du
    30/04 comptabilisee deux fois (−603) et celle du 22/06 jamais comptabilisee (+603) sont la
    MEME erreur, et elles s'annulent. Les ranger l'une en « cote ERPNext » et l'autre en « cote
    banque » faisait annoncer une degradation de 603 DT qui n'existe pas.

      `delai`      : la banque n'a pas encore passe l'operation. Se resorbe SEUL, sans decision.
      `correction` : il faut trancher — annuler un doublon, creer une ecriture manquante. Les
                     deux cotes s'y retrouvent, et leurs effets opposes se compensent a vue.
    """
    postes, effet_delai, effet_correction = [], 0.0, 0.0

    pieces = [p for p in (rapport.get("erpnext_sans_banque") or [])
              if p.get("statut_ecart") not in VERDICTS_SANS_EFFET]
    doublons = {}
    for p in pieces:
        if p.get("statut_ecart") == STATUT_DOUBLON:
            doublons.setdefault((str(p["posting_date"]), p["sens"],
                                 round(flt(p["montant"], 3), 3)), []).append(p)
    for verdict in STATUTS_ECART:
        lot = [p for p in pieces if p.get("statut_ecart") == verdict]
        if not lot:
            continue
        if verdict == STATUT_DOUBLON:
            # k pieces identiques, k−1 en trop : l'effet porte sur l'excedent, pas sur le lot.
            effet = round(sum(_signe(g[0]["sens"]) * flt(g[0]["montant"], 3) * (len(g) - 1)
                              for g in doublons.values()), 3)
            nb = sum(len(g) - 1 for g in doublons.values())
        else:
            effet = round(sum(_signe(p["sens"]) * flt(p["montant"], 3) for p in lot), 3)
            nb = len(lot)
        # Un doublon ne s'efface pas avec le temps : il se decide, comme une ecriture a creer.
        nature = "correction" if verdict == STATUT_DOUBLON else "delai"
        if nature == "delai":
            effet_delai = round(effet_delai + effet, 3)
        else:
            effet_correction = round(effet_correction + effet, 3)
        postes.append({"cote": "erpnext", "nature": nature, "verdict": verdict, "nb": nb,
                       "effet": effet, "lignes": [p["voucher_no"] for p in lot[:6]]})

    # Cote banque : comptabiliser un mouvement fait bouger ERPNext, donc l'ecart en sens INVERSE.
    mvts = [m for m in (rapport.get("banque_sans_erpnext") or [])
            if m.get("statut_ecart") not in VERDICTS_SANS_EFFET]
    if mvts:
        effet = round(-sum(_signe(m["sens"]) * flt(m["montant"], 3) for m in mvts), 3)
        effet_correction = round(effet_correction + effet, 3)
        postes.append({"cote": "banque", "nature": "correction", "verdict": "a comptabiliser",
                       "nb": len(mvts), "effet": effet,
                       "lignes": [m.get("reference") for m in mvts[:6]]})
    return postes, effet_delai, effet_correction


def projection_ecart(date_from=None, date_to=None, rapport: dict = None) -> dict:
    """Ce que deviendrait l'ecart une fois traite ce qui n'est rapproche d'aucun cote.

    POURQUOI CETTE PROJECTION EST BIEN DEFINIE
    ------------------------------------------
    Une piece sans mouvement se resout de DEUX facons opposees — la banque finit par passer
    l'operation, ou la piece etait fausse et on l'annule — et **les deux deplacent l'ecart du
    meme montant, dans le meme sens**. Exemple des 136 DT deposes le 08/08 : si la banque
    credite, `banque` monte de 136 ; si la piece est annulee, `ERPNext` baisse de 136. Dans les
    deux cas `banque − ERPNext` gagne +136. La projection ne prejuge donc pas de l'issue.

    CE QUI EST EXCLU, ET POURQUOI
    -----------------------------
    Les verdicts `Probable`, `Ecart de montant` et `Hors registre` : leur contrepartie existe
    deja (au releve, donc dans le solde bancaire). Il n'y manque que le LIEN — les projeter
    compterait deux fois de l'argent deja present des deux cotes.

    ⚠️ UN DOUBLON NE COMPTE QUE POUR k−1. Les deux pieces d'une paire portent le meme verdict,
    mais une seule est en trop : sommer les deux doublerait la correction.
    """
    from bank_retenue_sync.bank import solde as S

    r = rapport if rapport is not None else rapprochement(date_from=date_from, date_to=date_to)
    dernier = S.dernier_solde()
    banque = flt(getattr(dernier, "solde_banque", 0), 3) if dernier else None
    erpnext = S.solde_erpnext(dernier.date_solde) if dernier else None
    brut = round(banque - erpnext, 3) if dernier else None
    ouverture = ecart_ouverture()
    ecart = ecart_net(brut)

    postes, effet_delai, effet_correction = effets_projection(r)

    return {
        "date": getattr(dernier, "date_solde", None), "banque": banque, "erpnext": erpnext,
        "ecart": ecart, "ecart_brut": brut, "ouverture": ouverture, "postes": postes,
        "effet_delai": effet_delai, "effet_correction": effet_correction,
        # Deux projections : ce qui se resorbe SEUL (la banque rattrape son retard), puis ce qui
        # exige une decision. Les confondre ferait croire qu'un seul geste suffit ; les separer
        # par COTE, comme je l'avais fait d'abord, cassait les paires qui se compensent.
        "ecart_projete_delai": (round(ecart + effet_delai, 3) if ecart is not None else None),
        "ecart_projete": (round(ecart + effet_delai + effet_correction, 3)
                          if ecart is not None else None),
    }


def _dans(jour, date_from=None, date_to=None) -> bool:
    """Filtre de periode, borne incluse des deux cotes. Une date absente n'est jamais retenue."""
    if not jour:
        return False
    jour = getdate(jour)
    if date_from and jour < getdate(date_from):
        return False
    if date_to and jour > getdate(date_to):
        return False
    return True


def _jours(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def _totaux(pieces: list, mouvements: list) -> dict:
    """Compteurs par cote et par sens. Les montants restent en valeur absolue : c'est un
    reste-a-faire, pas un solde — les additionner en signe n'aurait aucun sens."""
    def bloc(rows):
        out = {"nb": len(rows), "montant": round(sum(flt(r.get("montant"), 3) for r in rows), 3)}
        for sens in ("Debit", "Credit"):
            lignes = [r for r in rows if r.get("sens") == sens]
            out[sens.lower()] = {"nb": len(lignes),
                                 "montant": round(sum(flt(r.get("montant"), 3) for r in lignes), 3)}
        for statut in STATUTS_ECART:
            lignes = [r for r in rows if r.get("statut_ecart") == statut]
            out[statut.lower().replace(" ", "_")] = {
                "nb": len(lignes),
                "montant": round(sum(flt(r.get("montant"), 3) for r in lignes), 3)}
        return out

    return {"erpnext_sans_banque": bloc(pieces), "banque_sans_erpnext": bloc(mouvements)}
