"""Classification des mouvements bancaires : identifie / orphelin / ignore / a verifier.

C'est la reponse a « qu'est-ce qui est rapproche, et qu'est-ce qui reste ». Le module ne cree ni ne
modifie aucun document : il constate. Les creations sont le role de `expenses/engine.py` et des
flux d'encaissement.

PRINCIPE : AUCUN SILENCE
------------------------
Tout mouvement ressort avec un statut ET une raison. Un libelle qu'aucune regle ne reconnait devient
`a_verifier` avec « libelle inconnu » — jamais un mouvement absent du rapport. C'est ce qui rend
l'inventaire exhaustif, et donc utilisable comme reste-a-faire.

LES PREUVES DE RAPPROCHEMENT SONT HETEROGENES (et c'est l'existant)
-------------------------------------------------------------------
Selon le flux, « ce mouvement a un document en face » se prouve differemment :
  - cheques/traites/aramex/virements -> cle bancaire presente dans un `Encaissement Paiement`
    (`pending.consumed_bank_keys`) ;
  - virements saisis a la main        -> reference dans le `reference_no` d'une Payment Entry
    (`pending.bank_refs_already_booked`) ;
  - depenses (declaration, CNSS...)   -> `cheque_no` d'un Journal Entry, ou reference bancaire
    citee dans son libelle (`expenses.lookup`) ;
  - versements d'especes              -> ecriture caisse -> banque du meme montant
    (`especes.find_journal_entries`).
On ne les unifie pas — on les interroge toutes, via un contexte charge une seule fois.

UNE REFERENCE `LD…` EST UN NUMERO DE CONTRAT, PAS UN NUMERO D'OPERATION
-----------------------------------------------------------------------
Toutes les echeances d'un meme leasing portent la MEME reference sur le releve. Chercher les
ecritures qui la citent en ramene donc des dizaines (31 pour LD2227700127, depuis 2024), et le
mouvement ressortait « a verifier : plusieurs ecritures citent cette reference ». C'etait une
FAUSSE ambiguite systematique : 20 groupes sur 23, soit 224 817 DT, etaient parfaitement
comptabilises.

La bonne unite de rapprochement n'est pas la ligne mais le GROUPE (reference, date) : la banque
eclate une echeance en plusieurs debits du meme jour (principal, profit, takaful, TVA, timbre),
dont la SOMME egale le decaissement porte par l'ecriture. Verifie au centime sur
ACC-JV-2026-00473 : 324,838 + 281,190 + 136,245 + 1 + 115,145 = 858,418.
Cf. `apparier_echeances`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.bank import registry, rules as R

STATUT_IDENTIFIE = "Identifie"
# La piece existe mais elle est en BROUILLON : identifiee, PAS encore au grand livre. Statut
# distinct (demande utilisateur 2026-08-19) pour que la page montre ce qui attend une soumission.
STATUT_IDENTIFIE_BROUILLON = "Identifie non comptabilise"
STATUT_ORPHELIN = "Orphelin"
STATUT_IGNORE = "Ignore"
STATUT_A_VERIFIER = "A verifier"

# Raison posee quand la reference est citee par plusieurs ecritures. Nommee parce que
# `_resoudre_echeance` doit RECONNAITRE ce verdict pour le remplacer : sur une reference de
# contrat, la multiplicite n'est pas une ambiguite, c'est la normale.
RAISON_REFS_MULTIPLES = "plusieurs ecritures citent cette reference bancaire"

# Ecart de dates admis entre le prelevement et sa comptabilisation (cas reel : ecriture du 18/07
# pour un prelevement du 20/07).
FENETRE_ECHEANCE = 4


@dataclass
class Classification:
    cle: str
    date: object = None
    operation: str = ""
    reference: str = ""
    debit: float = 0.0
    credit: float = 0.0
    categorie: str = ""
    sous_categorie: str = ""
    regle: str = ""
    action: str = ""
    statut: str = STATUT_A_VERIFIER
    document_type: str = None
    document_name: str = None
    raison: str = ""
    numero: str = None
    groupe: str = None
    # 0 = aucun ecart mesure (pas de piece rattachee, ou montant inconnu). Les colonnes Currency
    # sont NOT NULL cote base, et 0 ne declenche aucune alerte : la valeur neutre convient.
    montant_document: float = 0.0
    ecart: float = 0.0


@dataclass
class LinkContext:
    """Tous les index de rapprochement, charges UNE fois puis passes au classifieur.

    Entierement injectable : les tests construisent un contexte a la main et tournent sans base.
    """
    consumed: dict = field(default_factory=lambda: {"cheque": set(), "traite": set(),
                                                    "aramex": set(), "virement": set()})
    booked: dict = field(default_factory=dict)
    je_par_reference: dict = field(default_factory=dict)
    cheque_no_index: dict = field(default_factory=dict)
    # Payment Entry soumises touchant le compte bancaire, dans les deux sens. Sans elles, un
    # reglement fournisseur (Payment Entry `Pay`, 154 sur Zitouna) ressort « orphelin » alors
    # qu'il est parfaitement saisi.
    pe_bancaires: list = field(default_factory=list)
    # Ecritures de journal touchant la banque, avec le CUMUL de leurs lignes bancaires (une
    # echeance de pret en porte deux). Vide = index non charge : `_resoudre_echeance` ne conclut
    # alors RIEN et retombe sur la resolution par reference.
    ecritures_bancaires: list = field(default_factory=list)
    # {(reference, date) -> appariement} calcule par `apparier_echeances`.
    echeances: dict = field(default_factory=dict)
    # TOUTES les pieces ERPNext touchant le compte bancaire (Journal Entry ET Payment Entry),
    # cumulees par piece. Sert au dernier recours symetrique `apparier_restants`, et c'est le
    # meme inventaire que celui du rapprochement inverse (cf. bank/ecarts.py).
    pieces: list = field(default_factory=list)
    je_finder: object = None          # especes.find_journal_entries(date, montant) -> list
    # {cle bancaire -> montant impute} : sert a mesurer l'ecart de paiement quand le mouvement est
    # rattache a un Encaissement Paiement, dont le total groupe plusieurs operations bancaires.
    montants_par_cle: dict = field(default_factory=dict)
    # Etat des Encaissement Paiement par cle bancaire (pending.etat_encaissements_par_cle) :
    # docs (flux -> cle -> nom), etats (nom -> docstatus + ecarts bloquants), montants_brouillon.
    # Demande utilisateur 2026-08-18 : le mouvement identifie doit dire PAR QUEL encaissement,
    # mentionner s'il est encore en brouillon, et signaler ses ecarts a resoudre.
    encaissements: dict = field(default_factory=dict)
    # Noms des Journal Entry encore en BROUILLON : une piece identifiee qui figure ici n'est pas
    # au grand livre -> statut « Identifie non comptabilise » (post-controle de classify_one).
    je_brouillons: set = field(default_factory=set)
    # {(flux, cle) -> somme des credits du RELEVE partageant cette cle}. Un bordereau peut etre
    # credite en PLUSIEURS fois (bon 90028275 : 67 + 135) : l'ecart d'un mouvement isole doit se
    # mesurer sur le GROUPE (somme banque vs somme pieces), pas ligne contre total — sinon la
    # page affichait -68 et -136 la ou l'ecart reel du bordereau est -1.
    banque_par_cle: dict = field(default_factory=dict)


def build_context(movements: list, date_from=None, date_to=None) -> LinkContext:
    """Charge les index depuis la base. Un seul passage par run."""
    from bank_retenue_sync.encaissement import especes, pending
    from bank_retenue_sync.expenses import lookup

    refs = [m.get("reference") for m in (movements or []) if m.get("reference")]
    # Les NUMEROS extraits du libelle (n° de cheque) rejoignent les references bancaires dans le
    # meme index : certains reglements ne sont pas des Payment Entry mais de simples ecritures de
    # journal, qui portent le numero dans leur remarque — cas reels « Achat quincaillerie …
    # Chq N° 4000969 bq Zitouna » et « Vidange voiture CHERY Chq N° 4001008-Bq Zitouna ».
    # Sans eux, ces reglements restaient introuvables des deux cotes.
    banque_par_cle = {}
    for m in (movements or []):
        rule = R.find_rule(m)
        numero = R.extract_numero(rule, m) if rule else None
        if numero:
            refs.append(numero)
        if rule is not None and rule.flux:
            cle = _cle_bancaire(rule, m, numero)
            if cle:
                k = (rule.flux, cle)
                banque_par_cle[k] = round(
                    banque_par_cle.get(k, 0.0)
                    + (flt(m.get("credit"), 3) or flt(m.get("debit"), 3)), 3)
    dates = [m["date"] for m in (movements or []) if m.get("date")]
    date_from = date_from or (min(dates) if dates else None)
    date_to = date_to or (max(dates) if dates else None)
    # MARGE AMONT sur les index de PIECES : les operations du bord de fenetre ont leurs pieces
    # datees AVANT le premier mouvement du registre (salaires du 31/03 debites le 01/04,
    # honoraire du 25/03 vire le 02/04). Sans elle, chaque debut de registre fabrique des
    # orphelins artificiels — constate en reel au passage du registre a « depuis le 01/04 ».
    from datetime import timedelta
    pieces_from = (getdate(date_from) - timedelta(days=45)) if date_from else None

    cheque_index = lookup.cheque_no_index(pieces_from, date_to)
    return LinkContext(
        consumed=pending.consumed_bank_keys(),
        booked=pending.bank_refs_already_booked(refs),
        je_par_reference=_sans_ecritures_de_frais(
            lookup.journal_entries_by_bank_reference(refs, pieces_from, date_to), cheque_index),
        cheque_no_index=cheque_index,
        pe_bancaires=lookup.payment_entries_bancaires(pieces_from, date_to),
        ecritures_bancaires=lookup.ecritures_bancaires_cumulees(pieces_from, date_to),
        pieces=lookup.pieces_bancaires(pieces_from, date_to),
        je_finder=especes.find_journal_entries,
        montants_par_cle=pending.montants_par_cle_bancaire(),
        encaissements=pending.etat_encaissements_par_cle(),
        je_brouillons=set(frappe.get_all("Journal Entry", filters={"docstatus": 0},
                                         pluck="name")),
        banque_par_cle=banque_par_cle,
    )


def _sans_ecritures_de_frais(index: dict, cheque_no_index: dict) -> dict:
    """Retire les ecritures MENSUELLES de frais de l'index par reference bancaire.

    FAUSSE AMBIGUITE SYSTEMATIQUE, de la meme famille que celle des references de contrat `LD…`.
    L'ecriture mensuelle de frais liste dans sa remarque les references de toutes les commissions
    du mois (« Réf. banque CHG…, FT2618214DY4, … »). Or une commission porte LA MEME REFERENCE
    que l'operation qui l'a generee. Chercher qui cite `FT2618214DY4` ramenait donc deux
    ecritures — le salaire et le cumul de frais — et le mouvement ressortait « plusieurs
    ecritures citent cette reference » alors qu'il etait parfaitement comptabilise.
    Cas reel : les 4 salaires de juillet et la facture Aramex, 6 305 DT declares non rapproches.

    Une ecriture de frais n'identifie JAMAIS une operation par sa reference : le lien d'un frais
    a son cumul passe par `_frais_du_mois` (index des `cheque_no`), pas par ici.
    """
    from bank_retenue_sync.expenses import fees

    frais = {nom for cle, nom in (cheque_no_index or {}).items()
             if str(cle).startswith(fees.PREFIXE_MENSUEL)}
    if not frais:
        return index
    out = {}
    for ref, noms in (index or {}).items():
        restants = [n for n in noms if n not in frais]
        if restants:
            out[ref] = restants
    return out


# ------------------------------------------------------------------ resolution par flux

def _cle_bancaire(rule, m, numero):
    """Cle qu'un `Encaissement Paiement` porterait pour ce mouvement, selon le flux.

    Conventions verifiees sur les saisies historiques (cf. pending.consumed_bank_keys) :
    le flux cheque indexe le n° de BON de remise, les autres la reference bancaire.
    """
    if rule.flux == "cheque":
        return numero
    if rule.flux in ("traite", "aramex", "virement"):
        return (m.get("reference") or "").strip()
    return None


def _pieces_de(booked, ref):
    """Les pieces {name, party} derriere une reference de `booked` — [] si l'appelant
    a fourni un simple set (anciens tests / appels)."""
    if isinstance(booked, dict):
        return booked.get(ref) or []
    return []


def _mesurer_ecart(ctx: Classification, m: dict, montant_document):
    """Ecart entre ce que la BANQUE a verse et ce qui a ete COMPTABILISE pour la meme operation.

    Convention de signe : positif = la banque a credite plus que la piece ; negatif = la piece
    porte plus que ce que la banque a verse (cas d'un montant de cheque mal saisi).

    On ne juge pas ici : la valeur est posee, c'est la page qui la signale au-dela du seuil. Un
    ecart d'environ 1 DT sur un VIREMENT est un frais preleve a la source ; sur un CHEQUE, rien
    n'est preleve sur le montant (la commission est debitee en ligne distincte), donc le meme
    ecart y traduit une erreur de saisie.
    """
    if montant_document is None:
        return
    banque = round(flt(m.get("credit"), 3) or flt(m.get("debit"), 3), 3)
    ctx.montant_document = round(flt(montant_document), 3)
    ctx.ecart = round(banque - ctx.montant_document, 3)


def _resoudre_flux(rule, m, numero, ctx: Classification, context: LinkContext):
    """Statut d'un mouvement pris en charge par un flux d'encaissement deja code."""
    cle = _cle_bancaire(rule, m, numero)
    if cle and cle in (context.consumed.get(rule.flux) or set()):
        ctx.statut = STATUT_IDENTIFIE
        ctx.document_type = "Encaissement Paiement"
        ctx.raison = ""
        enc = context.encaissements or {}
        nom = ((enc.get("docs") or {}).get(rule.flux) or {}).get(cle)
        ctx.document_name = nom
        montant = (context.montants_par_cle or {}).get(cle)
        etat = (enc.get("etats") or {}).get(nom) if nom else None
        if etat and etat.get("docstatus") == 0:
            # BROUILLON : rien n'est encore poste sur le compte bancaire — le montant vient des
            # lignes du brouillon lui-meme, et l'utilisateur doit savoir qu'une soumission reste
            # a faire (voire des ecarts a resoudre avant elle).
            ctx.statut = STATUT_IDENTIFIE_BROUILLON
            if montant is None:
                montant = ((enc.get("montants_brouillon") or {})
                           .get(rule.flux) or {}).get(cle)
            n_ecarts = etat.get("ecarts_bloquants") or 0
            if n_ecarts:
                ctx.raison = ("encaissement en BROUILLON avec {0} écart(s) à résoudre — "
                              "soumission bloquée".format(n_ecarts))
            else:
                ctx.raison = "encaissement en brouillon — à soumettre"
        # Plusieurs credits peuvent partager la cle (bordereau credite en deux fois) : l'ecart
        # se mesure alors somme banque vs somme pieces — le meme pour chaque ligne du groupe.
        banque_groupe = (context.banque_par_cle or {}).get((rule.flux, cle))
        if montant is not None and banque_groupe is not None:
            ctx.montant_document = round(flt(montant), 3)
            ctx.ecart = round(flt(banque_groupe) - ctx.montant_document, 3)
        else:
            _mesurer_ecart(ctx, m, montant)
        return

    ref = (m.get("reference") or "").strip()
    if ref and ref in context.booked:
        ctx.statut = STATUT_IDENTIFIE
        ctx.document_type = "Payment Entry"
        # La piece et son client S'AFFICHENT : « une PE existe » sans dire laquelle
        # obligeait a fouiller la base a la main (constat utilisateur 28/08/2026).
        pieces = _pieces_de(context.booked, ref)
        if pieces:
            ctx.document_name = pieces[0]["name"]
        qui = ", ".join(filter(None, (p.get("party") for p in pieces))) or ""
        ctx.raison = ("saisi hors du flux automatique%s : l'argent est en banque, "
                      "mais le compte d'attente d'origine peut rester ouvert"
                      % ((" — " + qui) if qui else ""))
        if len(pieces) > 1:
            ctx.raison += " (%d pièces portent cette référence)" % len(pieces)
        return

    if rule.flux == "especes" and context.je_finder:
        montant = round(m.get("credit") or 0.0, 3)
        candidats = context.je_finder(m.get("date"), montant) or []
        if len(candidats) == 1:
            brouillon = candidats[0].get("docstatus") == 0
            ctx.statut = STATUT_IDENTIFIE_BROUILLON if brouillon else STATUT_IDENTIFIE
            ctx.document_type = "Journal Entry"
            ctx.document_name = candidats[0]["name"]
            if brouillon:
                ctx.raison = "écriture en brouillon — à soumettre"
            return
        if len(candidats) > 1:
            # Deux depots du meme montant dans la fenetre : le montant ne tranche pas, mais une
            # ANNOTATION le peut — si UNE seule candidate cite la reference bancaire du
            # mouvement (« Ref : TT... », posee par especes.annotate ou a la main), c'est elle.
            # Cas reel : les deux transferts de 20 000 du 06/05 (recette / compte associe).
            from bank_retenue_sync.encaissement import especes as _esp
            citants = [c for c in candidats if _esp._reference_presente(c, ref)]
            if len(citants) == 1:
                ctx.statut = STATUT_IDENTIFIE_BROUILLON if citants[0].get("docstatus") == 0 \
                    else STATUT_IDENTIFIE
                ctx.document_type = "Journal Entry"
                ctx.document_name = citants[0]["name"]
                ctx.raison = "" if citants[0].get("docstatus") != 0 \
                    else "écriture en brouillon — à soumettre"
                return
            ctx.statut = STATUT_A_VERIFIER
            ctx.raison = "plusieurs ecritures caisse -> banque du meme montant : non tranche"
            return
        ctx.statut = STATUT_ORPHELIN
        ctx.raison = "versement d'especes sans ecriture de caisse correspondante"
        return

    if rule.flux in ("declaration", "cnss"):
        _resoudre_journal(rule, m, ctx, context,
                          absent="prelevement detecte, ecriture non encore creee")
        return

    ctx.statut = STATUT_ORPHELIN
    ctx.raison = "%s non rapproche%s" % (rule.label.lower(),
                                         "" if numero else " (aucun n° dans le libelle)")


def _resoudre_journal(rule, m, ctx: Classification, context: LinkContext, absent: str = None):
    """Statut d'un mouvement cense correspondre a une piece comptable.

    On interroge TROIS sources avant de conclure a l'orphelin — une seule ne suffit pas :
      1. une ecriture de journal citant la reference bancaire ;
      2. une reference deja portee par une Payment Entry (`pending.bank_refs_already_booked`) ;
      3. une Payment Entry appariee par reference, montant ou montant approchant.
    La 3e est indispensable : les reglements fournisseurs sont des Payment Entry, pas des
    ecritures de journal, et leurs references sont saisies a la main donc parfois fautives.
    """
    from bank_retenue_sync.expenses import lookup

    ref = (m.get("reference") or "").strip().upper()
    noms = context.je_par_reference.get(ref) or []
    if noms:
        ctx.statut = STATUT_IDENTIFIE
        ctx.document_type = "Journal Entry"
        ctx.document_name = noms[0]
        if len(noms) > 1:
            ctx.statut = STATUT_A_VERIFIER
            ctx.raison = "%s : %s" % (RAISON_REFS_MULTIPLES, ", ".join(noms[:4]))
        return

    pe, mode, ecart = lookup.apparier_payment_entry(m, context.pe_bancaires or [])
    if pe:
        ctx.document_type = "Payment Entry"
        ctx.document_name = pe["name"]
        # La piece est connue : on pose son montant et l'ecart, quel que soit le mode
        # d'appariement. Un appariement par reference EXACTE peut lui aussi cacher un ecart.
        _mesurer_ecart(ctx, m, pe.get("paid_amount"))
        if mode == "approchant":
            # Ecart entre la piece et le releve : identifie, mais le montant merite un oeil.
            ctx.statut = STATUT_A_VERIFIER
            ctx.raison = ("apparie a %s (%s) par le montant, avec un ecart de %s : "
                          "reference bancaire non concordante" % (pe["name"], pe.get("party"), ecart))
        else:
            ctx.statut = STATUT_IDENTIFIE
            ctx.raison = ("apparie par le montant : la reference bancaire saisie ne correspond pas"
                          if mode == "montant" else "")
        return

    if ref and ref in (context.booked or {}):
        ctx.statut = STATUT_IDENTIFIE
        ctx.document_type = "Payment Entry"
        pieces = _pieces_de(context.booked, ref)
        if pieces:
            ctx.document_name = pieces[0]["name"]
        qui = ", ".join(filter(None, (p.get("party") for p in pieces))) or ""
        ctx.raison = "reference deja portee par une Payment Entry" + \
            ((" — " + qui) if qui else "")
        return

    ctx.statut = STATUT_ORPHELIN
    ctx.raison = absent or "regle applicable : l'ecriture peut etre creee"


# ------------------------------------------------- rapprochement par groupe (echeances)

def _jour(m: dict) -> str:
    d = m.get("date")
    return d.strftime("%d-%m-%Y") if hasattr(d, "strftime") else str(d)


def cle_echeance(m: dict) -> tuple:
    """Unite de rapprochement d'une echeance : (reference de contrat, jour du prelevement)."""
    return ((m.get("reference") or "").strip().upper(), _jour(m))


def _tolerance_echeance(montant: float) -> float:
    """Meme convention que `allocation.tolerance` : plancher 1 DT, 0,5 %, plafond 10 DT.

    Le plancher n'est pas theorique — le leasing FIAT LD2227700127 est saisi chaque mois a
    1 196,957 alors que la banque preleve 1 197,957 : le droit de timbre de 1 DT est omis.
    L'ecriture doit etre RETROUVEE malgre cet ecart, et l'ecart signale, pas absorbe.
    """
    return min(max(1.0, 0.005 * abs(montant)), 10.0)


def apparier_echeances(movements: list, context: LinkContext, rules=None) -> dict:
    """{(reference, jour) -> {voucher, montant, ecart, nb, total}} pour les groupes d'echeance.

    Deux passes, de la plus sure a la plus permissive — l'ordre compte, car le montant seul
    confondrait deux operations voisines :
      1. l'ecriture CITE la reference du contrat (index `je_par_reference`) : le lien est
         explicite, on admet alors l'ecart de tolerance ;
      2. aucune ecriture ne la cite : on n'accepte qu'un montant EXACT (5 millimes), dans la
         fenetre de dates. C'est le cas des acomptes de financement, dont les ecritures portent
         un libelle metier (« Avance Cenntro Logistar 100 ») sans reference bancaire.

    Une ecriture n'est consommee QU'UNE FOIS : sans cela, le blocage de provision et l'acompte
    du meme montant se rattacheraient tous deux a la meme piece.
    """
    ecritures = list(context.ecritures_bancaires or [])
    if not ecritures:
        return {}

    groupes: dict = {}
    for m in (movements or []):
        rule = R.find_rule(m, rules)
        if not rule or rule.groupe != "echeance":
            continue
        g = groupes.setdefault(cle_echeance(m), {"total": 0.0, "nb": 0, "date": m.get("date")})
        g["total"] = round(g["total"] + flt(m.get("debit"), 3) + flt(m.get("credit"), 3), 3)
        g["nb"] += 1

    consommees: set = set()
    out: dict = {}
    # Ordre deterministe (date puis reference) : le resultat ne doit pas dependre de l'ordre
    # d'arrivee des mouvements dans l'export.
    for cle in sorted(groupes, key=lambda k: (str(groupes[k]["date"]), k[0])):
        g = groupes[cle]
        ref, total = cle[0], g["total"]
        jour = getdate(g["date"]) if g.get("date") else None
        if not jour or not total:
            continue
        cites = set((context.je_par_reference or {}).get(ref) or [])
        proches = [e for e in ecritures if e["voucher_no"] not in consommees
                   and abs((getdate(e["posting_date"]) - jour).days) <= FENETRE_ECHEANCE]

        candidats = [e for e in proches if e["voucher_no"] in cites
                     and abs(e["montant"] - total) <= _tolerance_echeance(total)]
        if not candidats:
            candidats = [e for e in proches if abs(e["montant"] - total) < 0.005]

        # Les groupes SANS piece figurent aussi au resultat : `_resoudre_echeance` a besoin de
        # leur taille pour distinguer une echeance manquante d'un composant isole.
        best = None
        if candidats:
            # Le meilleur : ecart minimal, puis date la plus proche, puis nom — jamais le hasard.
            best = min(candidats, key=lambda e: (round(abs(e["montant"] - total), 3),
                                                 abs((getdate(e["posting_date"]) - jour).days),
                                                 e["voucher_no"]))
            consommees.add(best["voucher_no"])
        out[cle] = {"voucher": best["voucher_no"] if best else None,
                    "montant": best["montant"] if best else 0.0,
                    "ecart": round(total - best["montant"], 3) if best else 0.0,
                    "nb": g["nb"], "total": total}
    return out


def _resoudre_echeance(rule, m, ctx: Classification, context: LinkContext):
    """Statut d'une ligne appartenant a un groupe d'echeance."""
    cle = cle_echeance(m)
    ap = (context.echeances or {}).get(cle)

    # Un composant SEUL dans son groupe n'est pas une echeance : c'est une TVA sur commission
    # bancaire (references 'CHG…'). L'annoncer « echeance non comptabilisee » serait faux, et lui
    # poser une cle de groupe le ferait passer pour le fragment d'une operation qui n'existe pas.
    if rule.composant and (ap is None or ap["nb"] == 1):
        # TVA ou timbre sur commission bancaire : ils entrent dans le cumul MENSUEL des frais.
        je = _frais_du_mois(m, context)
        if je:
            ctx.statut = STATUT_IDENTIFIE
            ctx.document_type = "Journal Entry"
            ctx.document_name = je
            ctx.raison = ""
            return
        _resoudre_journal(rule, m, ctx, context)
        return

    ctx.groupe = "echeance-%s-%s" % cle
    if ap and ap["voucher"]:
        ctx.document_type = "Journal Entry"
        ctx.document_name = ap["voucher"]
        # L'ecart porte sur le GROUPE, pas sur la ligne. Le reporter sur chacune des N lignes le
        # multiplierait par N dans toute somme : il reste dans la raison, et les colonnes
        # numeriques ne sont renseignees que sur un groupe d'une seule ligne.
        if ap["nb"] == 1:
            _mesurer_ecart(ctx, m, ap["montant"])
        if abs(ap["ecart"]) < 0.005:
            ctx.statut = STATUT_IDENTIFIE
            ctx.raison = ""
        else:
            ctx.statut = STATUT_A_VERIFIER
            ctx.raison = ("echeance de %s lignes, total banque %s : l'ecriture %s n'en porte "
                          "que %s, ecart de %s" % (ap["nb"], ap["total"], ap["voucher"],
                                                   ap["montant"], ap["ecart"]))
            # L'ecart porte sur le GROUPE : on le passe explicitement, les colonnes de la ligne
            # restant vides pour ne pas le compter N fois.
            _absorber_ecart(ctx, m, context, ecart=ap["ecart"], montant=ap["total"])
        return

    _resoudre_journal(rule, m, ctx, context)
    # La multiplicite des ecritures n'a AUCUN sens sur une reference de contrat : toutes les
    # echeances la citent. Si le groupe n'a trouve aucune piece alors que l'index etait charge,
    # c'est que l'echeance n'est pas comptabilisee — on le dit, au lieu de laisser croire a un
    # doute d'appariement.
    if context.ecritures_bancaires and ctx.raison.startswith(RAISON_REFS_MULTIPLES):
        ctx.statut = STATUT_ORPHELIN
        ctx.document_type = None
        ctx.document_name = None
        ctx.raison = ("echeance non comptabilisee : aucune ecriture du %s ne solde le "
                      "prelevement de %s (reference de contrat %s)"
                      % (cle[1], m.get("debit") or m.get("credit"), cle[0]))


def _frais_du_mois(m: dict, context: LinkContext):
    """Nom de l'ecriture MENSUELLE de frais couvrant ce mouvement, si elle existe.

    Un frais bancaire n'est jamais comptabilise a l'unite : il rejoint le cumul de son mois
    (cf. expenses/fees.py). Tant que ce cumul n'existe pas, le mouvement reste « a verifier » ;
    des qu'il existe, le mouvement EST comptabilise — au meme titre qu'une ligne rattachee a une
    piece — et doit ressortir « identifie ». Sans cela, 146 lignes deja couvertes gonflaient
    indefiniment le compteur du reste-a-faire.

    Vaut aussi pour la TVA sur commission (references `CHG…`), qui entre dans le meme cumul.
    """
    from bank_retenue_sync.expenses import fees

    jour = m.get("date")
    if not jour:
        return None
    return (context.cheque_no_index or {}).get(fees.cle_mensuelle(fees.periode_de(jour)))


def _absorber_ecart(c: Classification, m: dict, context: LinkContext, ecart: float = None,
                    montant: float = None) -> bool:
    """Un ecart RESIDUEL sur une piece deja trouvee est repris par le cumul mensuel.

    Il n'y a plus rien a rapprocher : le lien est etabli, c'est le montant qui cloche de quelques
    dinars. Le laisser « a verifier » indefiniment bloquait 20 lignes pour 4 DT — les 4 echeances
    du leasing FIAT, dont l'ecriture omet le droit de timbre de 1 DT chaque mois.

    Meme convention que `_frais_du_mois`, dont c'est le prolongement : le mouvement devient
    « identifie » des lors que l'ecriture mensuelle du mois EXISTE, celle-la meme qui porte les
    commissions, leur TVA, les deltas de paiement et desormais ces residus (cf.
    expenses/residus.py). Au-dela de la tolerance on ne touche a rien : ce n'est plus un residu
    mais une erreur, et elle doit rester visible.
    """
    from bank_retenue_sync.bank import ecarts as E

    ecart = c.ecart if ecart is None else ecart
    montant = (c.debit or c.credit) if montant is None else montant
    if not ecart or abs(flt(ecart, 3)) > E.tolerance(montant):
        return False
    je = _frais_du_mois(m, context)
    if not je:
        return False
    c.statut = STATUT_IDENTIFIE
    c.raison = ("ecart de %s repris dans le cumul mensuel des frais et ecarts (%s)"
                % (round(flt(ecart, 3), 3), je))
    return True


def _journal_citant(cle: str, context: LinkContext):
    """Ecriture de journal citant cette cle, avec le montant qu'elle sort de la banque.

    `cle` est soit un n° de cheque extrait du libelle, soit la REFERENCE BANCAIRE du mouvement.
    La seconde est indispensable pour les flux « sans automatisation prevue » : un paiement
    Orange ou une recharge saisis a la main citent la reference `FT…` dans leur remarque, et
    rien d'autre ne les relie au releve.

    -> {voucher_no, montant} | None. On exige UNE seule ecriture : une cle citee par deux
    pieces n'identifie plus rien. Le montant vient de `ecritures_bancaires` (cumul des lignes
    bancaires de l'ecriture), ce qui permet de mesurer l'ecart avec le releve au lieu de
    l'ignorer — cas reel : cheque 4001008, 231,821 preleves pour 232,136 comptabilises.
    """
    if not cle:
        return None
    noms = (context.je_par_reference or {}).get(str(cle).strip().upper()) or []
    if len(noms) != 1:
        return None
    montants = {e["voucher_no"]: e["montant"] for e in (context.ecritures_bancaires or [])}
    return {"voucher_no": noms[0], "montant": montants.get(noms[0])}


def classify_one(m: dict, context: LinkContext, rules=None) -> Classification:
    debit = round(m.get("debit") or 0.0, 3)
    credit = round(m.get("credit") or 0.0, 3)
    c = Classification(
        cle=registry.movement_key(m), date=m.get("date"), operation=m.get("operation") or "",
        reference=(m.get("reference") or "").strip(), debit=debit, credit=credit)

    rule = R.find_rule(m, rules)
    if rule is None:
        c.statut = STATUT_A_VERIFIER
        c.raison = "libelle inconnu : aucune regle ne le reconnait (a ajouter dans bank/rules.py)"
        return c

    c.categorie = rule.categorie
    c.sous_categorie = rule.sous_categorie
    c.regle = rule.key
    c.action = rule.action
    c.numero = R.extract_numero(rule, m)

    # Le regroupement prime sur l'action : une echeance ne se rapproche jamais ligne a ligne,
    # quelle que soit l'automatisation prevue pour la ligne prise isolement.
    if rule.groupe == "echeance":
        _resoudre_echeance(rule, m, c, context)
    elif rule.action == R.ACTION_IGNORE:
        c.statut = STATUT_IGNORE
        c.raison = rule.label
    elif rule.action == R.ACTION_AGREGAT:
        # Ces mouvements ne se comptabilisent JAMAIS a l'unite : le statut se decide au niveau du
        # groupe (cf. expenses/fees.py). Ici on se contente de poser la cle de groupe.
        jour = m.get("date")
        c.groupe = "frais-%s" % (jour.strftime("%d-%m-%Y") if hasattr(jour, "strftime") else jour)
        je = _frais_du_mois(m, context)
        if je:
            c.statut = STATUT_IDENTIFIE
            c.document_type = "Journal Entry"
            c.document_name = je
            c.raison = ""
        else:
            c.statut = STATUT_A_VERIFIER
            c.raison = "frais bancaires : traites par agregat journalier, jamais a l'unite"
    elif rule.action == R.ACTION_FLUX:
        _resoudre_flux(rule, m, c.numero, c, context)
    elif rule.action == R.ACTION_JOURNAL:
        _resoudre_journal(rule, m, c, context)
    else:
        # Informatif : aucune ecriture n'est proposee, mais si une piece existe deja (un reglement
        # fournisseur, un cheque emis), autant le dire. Sans cela, 16 reglements de cheque
        # parfaitement saisis restaient « a verifier ».
        from bank_retenue_sync.expenses import lookup

        # `c.numero` porte le n° de cheque extrait du libelle : c'est la preuve la plus forte
        # pour un reglement fournisseur, et elle etait jusqu'ici inutilisee.
        pe, mode, ecart = lookup.apparier_payment_entry(
            m, context.pe_bancaires or [], numero=c.numero)
        if pe and mode != "approchant":
            c.statut = STATUT_IDENTIFIE
            c.document_type = "Payment Entry"
            c.document_name = pe["name"]
            _mesurer_ecart(c, m, pe.get("paid_amount"))
            # Le numero tranche l'identite ; l'ecart de montant, lui, ne doit pas disparaitre
            # avec elle — il vaut de quelques millimes a plus d'un dinar sur les cas reels.
            c.raison = ("appariee par le n° de cheque, avec un ecart de %s sur le montant" % c.ecart
                        if mode == "numero" and abs(c.ecart) >= 0.005 else "")
        else:
            # Dernier recours : une ECRITURE DE JOURNAL citant le n° de cheque. Tous les
            # reglements ne passent pas par une Payment Entry — certains sont saisis en direct,
            # avec le numero dans la remarque.
            je = (_journal_citant(c.numero, context)
                  or _journal_citant(c.reference, context))
            if je:
                c.statut = STATUT_IDENTIFIE
                c.document_type = "Journal Entry"
                c.document_name = je["voucher_no"]
                _mesurer_ecart(c, m, je["montant"])
                c.raison = ("citee par une ecriture de journal, avec un ecart de %s sur le montant"
                            % c.ecart if abs(c.ecart) >= 0.005 else "")
            else:
                c.statut = STATUT_A_VERIFIER
                c.raison = "%s : categorise, aucune automatisation prevue" % rule.label

    # Point unique pour toutes les resolutions ligne a ligne : une piece trouvee dont le montant
    # differe de quelques dinars n'est pas un rapprochement en attente, c'est un residu.
    if c.statut == STATUT_A_VERIFIER and c.document_name and not c.groupe:
        _absorber_ecart(c, m, context)

    # Post-controle transversal : identifie par une ecriture de journal encore en BROUILLON
    # -> « Identifie non comptabilise ». Toutes les voies (reference, numero de cheque,
    # citation) passent ici, inutile de le repeter dans chaque branche.
    if (c.statut == STATUT_IDENTIFIE and c.document_type == "Journal Entry"
            and c.document_name in (context.je_brouillons or set())):
        c.statut = STATUT_IDENTIFIE_BROUILLON
        c.raison = ((c.raison + " — ") if c.raison else "") + "écriture en brouillon, à soumettre"

    return c


def apparier_restants(classifications: list, context: LinkContext) -> int:
    """Dernier recours SYMETRIQUE : apparier ce qui reste, des deux cotes, par montant et date.

    Beaucoup d'ecritures manuelles ne citent AUCUNE reference bancaire — « DÉPENSE DIVERS : ACHAT
    CLAVIERS SANS FIL » face a « REGLEMENT CB 0405MYTEK INFOR ». Aucune des passes precedentes ne
    peut les relier : il n'y a pas de cle commune, seulement un montant et une date. Sur le
    registre reel, 13 pieces etaient dans ce cas.

    TROIS GARDE-FOUS, car un appariement par montant est le plus fragile de tous :
      1. la piece ne doit citer AUCUNE cle du releve — si elle en cite une, elle appartient au
         mouvement correspondant, meme quand ce mouvement a ete rapproche autrement (cas des
         ecritures GROUPEES : une seule piece couvre trois paiements Orange et cite leurs trois
         references) ;
      2. le candidat doit etre unique DES DEUX COTES (cf. `ecarts.apparier_par_montant`) : les
         deux transferts d'especes de 20 000 DT du meme jour restent non tranches ;
      3. les lignes traitees par GROUPE (frais journaliers, echeances) sont exclues : elles ne se
         comptabilisent jamais a l'unite, donc aucune piece isolee ne peut leur correspondre.

    Le statut devient `Identifie`, mais la raison dit explicitement d'ou vient le lien — c'est la
    meme convention que l'appariement par montant d'une Payment Entry (`mode == "montant"`).
    """
    from bank_retenue_sync.bank import ecarts

    pieces = list(context.pieces or [])
    if not pieces:
        return 0

    cites = {c.document_name for c in classifications if c.document_name}
    cles = set()
    for c in classifications:
        for v in (c.reference, c.numero):
            v = str(v or "").strip().upper()
            if len(v) >= ecarts.MIN_CLE_LEN:
                cles.add(v)
    dispo = [p for p in pieces
             if p["voucher_no"] not in cites and not ecarts.piece_cite(p, cles)]
    if not dispo:
        return 0

    par_cle = {c.cle: c for c in classifications}
    restants = [{"cle": c.cle, "date": c.date, "montant": c.debit or c.credit,
                 "sens": "Debit" if c.debit else "Credit"}
                for c in classifications
                if c.statut in (STATUT_ORPHELIN, STATUT_A_VERIFIER)
                and not c.document_name and not c.groupe]

    def _rattacher(cle, piece):
        c = par_cle[cle]
        c.document_type = piece["voucher_type"]
        c.document_name = piece["voucher_no"]
        _mesurer_ecart(c, {"date": c.date, "debit": c.debit, "credit": c.credit},
                       piece["montant"])
        return c

    paires = ecarts.apparier_par_montant(restants, dispo)
    for cle, piece in paires.items():
        c = _rattacher(cle, piece)
        c.statut = STATUT_IDENTIFIE
        c.raison = ("apparie par le montant et la date : la piece ne cite aucune reference "
                    "bancaire (%s)" % (piece.get("texte") or "")[:60])

    # SECONDE PASSE, A LA TOLERANCE : la piece existe mais porte un montant FAUX.
    # Cas reel : la recharge Total du 20/07, prelevee 703,500 et comptabilisee 703,000. Sans
    # elle, le prelevement ressortait « non comptabilise » et la piece « sans mouvement » — deux
    # verdicts faux pour une seule erreur de saisie, et l'ecart de 0,500 restait invisible.
    # Le statut reste « a verifier » : le lien est probable, le montant ne l'est pas. C'est la
    # meme convention que le mode « approchant » de `lookup.apparier_payment_entry`.
    pris = {p["voucher_no"] for p in paires.values()}
    restants2 = [r for r in restants if r["cle"] not in paires]
    dispo2 = [p for p in dispo if p["voucher_no"] not in pris]
    proches = ecarts.apparier_par_montant(restants2, dispo2, marge=None)
    for cle, piece in proches.items():
        c = _rattacher(cle, piece)
        c.statut = STATUT_A_VERIFIER
        c.raison = ("apparie a %s par le montant approchant, avec un ecart de %s : la piece ne "
                    "cite aucune reference bancaire" % (piece["voucher_no"], c.ecart))
        # Meme regle que pour les resolutions ligne a ligne : si le mois porte deja son cumul,
        # le residu y est repris et le mouvement n'a plus a rester en attente.
        _absorber_ecart(c, {"date": c.date, "debit": c.debit, "credit": c.credit}, context)
    return len(paires) + len(proches)


def classify(movements: list, context: LinkContext = None, rules=None) -> list:
    context = context if context is not None else build_context(movements)
    # Passe de groupe AVANT la passe ligne a ligne : chaque ligne d'une echeance a besoin du
    # verdict de son groupe. Un contexte deja renseigne (tests, rejeu) n'est pas recalcule.
    if not context.echeances:
        context.echeances = apparier_echeances(movements, context, rules)
    classifications = [classify_one(m, context, rules) for m in (movements or [])]
    # APRES la passe ligne a ligne, et seulement sur ce qu'elle n'a pas su rapprocher : l'ordre
    # importe, l'appariement par montant ne doit jamais primer sur une cle bancaire.
    apparier_restants(classifications, context)
    return classifications


def summarize(classifications: list) -> dict:
    """KPI par sens et par statut : nombre et montant. Alimente les tuiles de la page."""
    out = {}
    for c in classifications:
        sens = "debit" if c.debit else "credit"
        bucket = out.setdefault(sens, {})
        s = bucket.setdefault(c.statut, {"nb": 0, "montant": 0.0})
        s["nb"] += 1
        s["montant"] = round(s["montant"] + (c.debit or c.credit), 3)
    return out


def run(date_from=None, date_to=None, movements=None, persist: bool = True,
        snapshot_run: str = None) -> dict:
    """Classe les mouvements du registre et y ecrit le resultat.

    Source par defaut : le REGISTRE (pas le service). L'app tourne donc sans tej-bank-service
    joignable — indispensable ici, ou le microservice vit dans un autre devcontainer.
    """
    movements = movements if movements is not None else registry.registry_as_movements(
        date_from=date_from, date_to=date_to)
    context = build_context(movements, date_from, date_to)
    classifications = classify(movements, context)
    out = {"mouvements": len(movements), "kpi": summarize(classifications)}
    if persist:
        out.update(registry.apply_classifications(classifications, snapshot_run=snapshot_run))
        frappe.db.commit()
    return out
