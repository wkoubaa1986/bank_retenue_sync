"""Rapprochement d'un certificat TEJ avec le systeme : quel client, quelle ecriture, quelle facture.

DEUX QUESTIONS, DEUX NATURES
----------------------------
Identifier le CLIENT est une question de langage : le portail donne un matricule et une raison
sociale, ERPNext a 4 689 fiches saisies a la main. Identifier la PIECE est une question de
comptabilite : montants, dates, references. Les melanger produirait un rapprochement qui se
trompe pour de mauvaises raisons — d'ou deux etages nettement separes, comme dans les flux
d'encaissement (`encaissement/party.py` puis `encaissement/allocation.py`).

L'ORDRE DES ETAGES EST L'ESSENTIEL
----------------------------------
Client : matricule fiscal (EXACT, gratuit, 34 certificats 2026 sur 44) -> alias appris ->
raison sociale approchee -> IA en dernier recours. Chaque etage n'est consulte que si le
precedent n'a pas tranche. Le matricule d'abord, parce qu'il ne se trompe pas : c'est une cle,
pas une ressemblance.

Piece : ecriture RAS au montant exact -> au montant approchant -> reglement net -> assiette.
Le reglement net merite une explication : un client qui retient 1 % paie le solde, soit
`TTC - retenue`. Ce reglement-la, LUI, existe en banque et porte deja sa facture. Le retrouver
donne la facture du certificat sans rien deviner — 17 cas sur 34 en 2026, et une seule facture
a chaque fois.

L'ECART DE 0,010 DT N'EST PAS UNE ERREUR
----------------------------------------
Nos ecritures calculent 1 % du TTC timbre fiscal compris ; le client declare 1 % hors timbre.
1 DT de timbre => 0,010 DT d'ecart, systematique. On le RECONNAIT (l'appariement l'admet) mais on
ne le corrige jamais d'office : la comptabilite est deja passee, c'est a l'utilisateur d'arbitrer.
"""
from __future__ import annotations

import json

import frappe
from frappe.utils import add_days, flt, getdate

from bank_retenue_sync.encaissement import party
from bank_retenue_sync.encaissement.allocation import tolerance
from bank_retenue_sync.tej import certificats as C
from bank_retenue_sync.tej import matricule as MF

DOCTYPE = "Retenue Certificate"

# Fenetre entre la date de paiement declaree au portail et la date de la piece comptable. Large :
# le client declare quand il paie, nous comptabilisons quand nous constatons, et les deux dates
# s'ecartent de plusieurs semaines sans que rien ne soit anormal.
FENETRE_JOURS = 45
FENETRE_REGLEMENT = 75

# Timbre fiscal : 1 DT sur la facture, donc 0,010 DT sur une retenue de 1 %. C'est l'ecart le plus
# frequent entre notre saisie et la declaration du client.
TIMBRE = 1.0

# ⚠️ MARGE PROPRE A L'IDENTIFICATION DU CLIENT, beaucoup plus serree que `tolerance()`.
# `tolerance()` plancher a 1 DT : sur une retenue de 3,710 DT, elle accepterait tout ce qui se
# trouve entre 2,71 et 4,71 — assez large pour designer le mauvais client. Quand c'est le MONTANT
# qui doit nommer un tiers, il doit coller au millime, timbre fiscal compris (0,010 a 0,012).
MARGE_IDENTIFICATION = 0.02


def mode_ras() -> str:
    """Le mode de paiement qui porte la retenue de vente.

    Lu dans les reglages, avec repli sur la constante de `customization_app` : la chaine ne doit
    exister qu'a UN endroit, sinon renommer le mode casserait le rapprochement en silence.
    """
    try:
        v = frappe.db.get_single_value("Bank Retenue Sync Settings", "ras_mode_of_payment")
        if v:
            return v
    except Exception:
        pass
    try:
        from customization_app.retenue_source import RAS_MODE

        return RAS_MODE
    except Exception:
        return "Retenue a la source vente"


# ------------------------------------------------------------------ contexte

class Contexte:
    """Tous les index de rapprochement, charges UNE fois par passage.

    Entierement injectable : les tests en construisent un a la main et tournent sans base, comme
    `LinkContext` du moteur de classification bancaire.
    """

    def __init__(self, index_mf=None, clients=None, alias=None, pes_ras=None, encaissements=None,
                 refs=None, factures=None, prises=None, factures_prises=None, index_num=None):
        self.index_mf = index_mf or {}            # {matricule normalise: [customer]}
        self.index_num = index_num or {}          # {numero du contribuable: [customer]} — vivier
        self.clients = clients or []              # [customer_name] candidats au fuzzy
        self.alias = alias or {}                  # {libelle normalise: customer}
        self.pes_ras = pes_ras or {}              # {customer: [{name, posting_date, paid_amount}]}
        self.encaissements = encaissements or {}  # {customer: [{name, posting_date, paid_amount}]}
        self.refs = refs or {}                    # {payment_entry: [{doctype, name, montant}]}
        self.factures = factures or {}            # {customer: [{name, posting_date, grand_total}]}
        # ⚠️ {piece: certificat qui la porte}, et non un simple ensemble : au second passage, un
        # certificat retrouvait « sa » propre ecriture marquee comme prise — par lui-meme — et se
        # declarait sans piece. Une piece prise PAR CE certificat lui reste disponible.
        self.prises = dict(prises or {})
        self.factures_prises = dict(factures_prises or {})

    def piece_libre(self, piece: str, cert_name: str) -> bool:
        detenteur = self.prises.get(piece)
        return detenteur is None or detenteur == cert_name

    def facture_libre(self, facture: str, cert_name: str) -> bool:
        detenteur = self.factures_prises.get(facture)
        return detenteur is None or detenteur == cert_name

    def reserver(self, cert_name: str, piece: str = None, facture: str = None):
        if piece:
            self.prises[piece] = cert_name
        if facture:
            self.factures_prises[facture] = cert_name


def charger_contexte(annee_min: int = None) -> Contexte:
    """Construit le contexte depuis la base. Une seule requete par index."""
    annee_min = C.ANNEE_MINIMALE if annee_min is None else annee_min
    depuis = "%s-01-01" % (annee_min - 1)         # un an de marge : les pieces precedent le portail

    clients = frappe.db.sql(
        "select name, customer_name, tax_id from `tabCustomer` where disabled = 0", as_dict=1)
    paires = [(c.tax_id, c.name) for c in clients if c.tax_id]
    index_mf = MF.index(paires)
    index_num = MF.index_par_numero(paires)

    mode = mode_ras()
    pes = frappe.db.sql("""select name, party, posting_date, paid_amount, mode_of_payment
                           from `tabPayment Entry`
                           where docstatus = 1 and payment_type = 'Receive'
                             and party_type = 'Customer' and posting_date >= %s""",
                        depuis, as_dict=1)
    pes_ras, encaissements = {}, {}
    for p in pes:
        cible = pes_ras if p.mode_of_payment == mode else encaissements
        cible.setdefault(p.party, []).append(
            {"name": p.name, "posting_date": p.posting_date, "paid_amount": flt(p.paid_amount, 3)})

    refs = {}
    for r in frappe.db.sql("""select per.parent, per.reference_doctype, per.reference_name,
                                     per.allocated_amount
                              from `tabPayment Entry Reference` per
                              join `tabPayment Entry` pe on pe.name = per.parent
                              where pe.docstatus = 1 and pe.posting_date >= %s""",
                           depuis, as_dict=1):
        refs.setdefault(r.parent, []).append({"doctype": r.reference_doctype,
                                              "name": r.reference_name,
                                              "montant": flt(r.allocated_amount, 3)})

    factures = {}
    for f in frappe.db.sql("""select name, customer, posting_date, grand_total, outstanding_amount
                              from `tabSales Invoice`
                              where docstatus = 1 and posting_date >= %s""", depuis, as_dict=1):
        factures.setdefault(f.customer, []).append(
            {"name": f.name, "posting_date": f.posting_date,
             "grand_total": flt(f.grand_total, 3), "outstanding": flt(f.outstanding_amount, 3)})

    # Une ecriture ne peut justifier qu'UN certificat : sans cette exclusion, deux retenues du
    # meme montant se rapprocheraient toutes deux de la meme piece.
    prises = {r.payment_entry: r.name for r in frappe.db.get_all(
        DOCTYPE, filters={"payment_entry": ["is", "set"]},
        fields=["name", "payment_entry"], limit_page_length=0)}
    factures_prises = {r.sales_invoice: r.name for r in frappe.db.get_all(
        DOCTYPE, filters={"sales_invoice": ["is", "set"]},
        fields=["name", "sales_invoice"], limit_page_length=0)}
    return Contexte(index_mf=index_mf, index_num=index_num, clients=[c.name for c in clients],
                    alias=alias_appris(), pes_ras=pes_ras, encaissements=encaissements, refs=refs,
                    factures=factures, prises=prises, factures_prises=factures_prises)


def alias_appris(rows=None) -> dict:
    """{raison sociale normalisee: customer} appris des certificats tranches A LA MAIN.

    C'est la boucle d'apprentissage du flux : corriger un client aujourd'hui rapproche
    automatiquement les certificats suivants du meme declarant, sans fuzzy ni IA. On ne retient
    que les libelles sans ambiguite — un meme declarant renvoye vers deux clients differents
    n'apprend rien, il signale un doublon de fiche.
    """
    if rows is None:
        rows = frappe.db.get_all(DOCTYPE, filters={"match_status": "Manually Matched",
                                                   "customer": ["is", "set"]},
                                 fields=["declarant", "customer"], limit_page_length=0)
    par_libelle = {}
    for r in rows:
        libelle = party.normalize(r.get("declarant") or "")
        if libelle:
            par_libelle.setdefault(libelle, set()).add(r.get("customer"))
    return {k: list(v)[0] for k, v in par_libelle.items() if len(v) == 1}


# ------------------------------------------------------------------ client

def identifier_client(cert: dict, ctx: Contexte, ai_resolver=None) -> dict:
    """-> {customer, method, score, raison, candidats}. `customer` a None si non tranche."""
    cle = MF.normaliser(cert.get("declarant_matricule"))
    homonymes = ctx.index_mf.get(cle or "", [])
    if len(homonymes) == 1:
        return {"customer": homonymes[0], "method": "matricule", "score": 1.0,
                "raison": "matricule fiscal %s" % cle, "candidats": []}

    # LA LETTRE CLE MANQUE SOUVENT COTE ERPNEXT, et elle manque en silence : « 1398789 / A / M /
    # 000 » se lit « 1398789A » alors que le portail declare « 1398789L » — le A est le code de
    # categorie, la lettre cle a saute a la saisie. La cle exacte ne peut alors RIEN trouver, et le
    # certificat retombe sur la raison sociale, que le portail tronque et coupe en morceaux
    # (« BUSINESS HOTEL MANAG EMENT TUNIS BHM TUNI ») : deux clients se sont retrouves a egalite et
    # le certificat est reste non identifie.
    #
    # Le NUMERO du contribuable, lui, concorde. Il ne vaut pas cle — des chiffres saisis de travers
    # peuvent tomber sur un autre contribuable — mais quand il ne designe qu'UNE fiche, c'est une
    # preuve bien plus forte qu'une ressemblance de nom. On tranche donc, en marquant la decision
    # pour revue humaine (`METHODES_A_REVOIR`) : visible, verifiable, et jamais silencieuse.
    numero = MF.chiffres(cert.get("declarant_matricule"))
    memes_chiffres = [c for c in ctx.index_num.get(numero or "", []) if c not in homonymes]
    if not homonymes and len(memes_chiffres) == 1:
        # ⚠️ ON DEMANDE QUAND MEME SON AVIS AU NOM, sur ce seul candidat. Deux preuves
        # independantes qui concordent (le numero et la raison sociale) valent mieux qu'une, et
        # cela evite de coller un « a verifier » PERMANENT sur des lignes deja justes — le drapeau
        # n'est jamais efface par personne, il ne doit donc se lever que quand il apprend quelque
        # chose. Le vivier restreint change tout : c'est en le comparant aux 4 689 clients que
        # « BUSINESS HOTEL MANAG EMENT TUNIS BHM TUNI » se retrouvait a egalite avec une ecole.
        # Jamais d'IA ici : payer pour confirmer une identite numerique n'a pas de sens.
        confirme = party.identify(cert.get("declarant") or "", memes_chiffres, aliases=ctx.alias)
        corrobore = bool(confirme.get("customer"))
        return {"customer": memes_chiffres[0], "method": "matricule_numero",
                "score": 0.95 if corrobore else 0.85, "revue": not corrobore,
                "raison": "numero de contribuable %s%s ; la lettre cle du portail (%s) ne figure "
                          "pas cote ERPNext (tax_id a corriger)"
                          % (numero, " et raison sociale concordante" if corrobore
                             else " ; raison sociale non concordante, a verifier",
                             cert.get("declarant_matricule")),
                "candidats": []}

    # Plusieurs fiches pour un meme matricule : on ne tranche pas ici, mais on RESTREINT le vivier
    # aux homonymes. La raison sociale les departage presque toujours, et l'ambiguite qui subsiste
    # signale un doublon de fiche client a fusionner. Meme geste, plus faible, pour les fiches qui
    # partagent le numero : elles restent un bien meilleur vivier que les 4 689 clients.
    candidats = homonymes or memes_chiffres or ctx.clients
    res = party.identify(cert.get("declarant") or "", candidats,
                         aliases=ctx.alias, ai_resolver=ai_resolver)
    if res.get("customer"):
        return {"customer": res["customer"], "method": res["method"],
                "score": res.get("score") or 0.0,
                "raison": "raison sociale (%s)" % res["method"], "candidats": []}

    # Dernier etage, apres le nom et apres l'IA : le montant de la retenue deja comptabilisee.
    # Il ne nomme pas le client par ressemblance mais par imputation — quand il tranche, il a
    # raison ; quand il ne tranche pas, il se tait.
    depart = departager_par_ecriture(cert, ctx, candidats)
    if depart["customer"]:
        return {"customer": depart["customer"], "method": "ecriture", "score": 1.0,
                "raison": "seul client portant une ecriture de retenue de ce montant a cette date "
                          "(%s)" % ", ".join(depart["pieces"]),
                "candidats": []}

    raison = res.get("raison") or "client non identifie"
    if homonymes:
        raison = "%s clients partagent le matricule %s" % (len(homonymes), cle)
    elif len(memes_chiffres) > 1:
        raison = "%s clients partagent le numero de contribuable %s" % (len(memes_chiffres), numero)
    if depart["porteurs"]:
        raison += " ; %s d'entre eux portent une ecriture du bon montant" % len(depart["porteurs"])
    return {"customer": None, "method": "aucun", "score": res.get("score") or 0.0,
            "raison": raison,
            "candidats": (homonymes or memes_chiffres or depart["porteurs"])[:10]}


# ------------------------------------------------------------------ piece

def ecart_timbre(ecart: float) -> bool:
    """L'ecart s'explique-t-il par le timbre fiscal ?

    Notre ecriture retient 1 % du TTC timbre compris, le client declare 1 % hors timbre : 1 DT de
    timbre fait donc 0,010 DT d'ecart, arrondi a 0,011 selon le sens des arrondis de chaque cote.
    Le reconnaitre evite de presenter comme suspects les 13 certificats les plus normaux du lot —
    mais ne les corrige pas pour autant : la comptabilite est deja passee.
    """
    attendu = round(TIMBRE / 100.0, 3)
    return abs(abs(flt(ecart, 3)) - attendu) <= 0.002


def _dans_fenetre(jour, reference, jours) -> bool:
    if not jour or not reference:
        return False
    return getdate(add_days(reference, -jours)) <= getdate(jour) <= getdate(add_days(reference,
                                                                                     jours))


def apparier_piece(cert: dict, ctx: Contexte, fenetre: int = FENETRE_JOURS) -> dict:
    """Trouve l'ecriture RAS qui porte cette retenue. -> {payment_entry, mode, ecart, ...}."""
    customer = cert.get("customer")
    retenue = flt(cert.get("montant_retenue"), 3)
    jour = cert.get("date_paiement")
    libres = [p for p in ctx.pes_ras.get(customer, [])
              if ctx.piece_libre(p["name"], cert.get("name"))]
    if not libres:
        return {"payment_entry": None, "mode": "aucune",
                "raison": "aucune ecriture de retenue disponible pour ce client", "candidats": []}

    exactes = [p for p in libres if abs(p["paid_amount"] - retenue) < 0.005]
    dans = [p for p in exactes if _dans_fenetre(p["posting_date"], jour, fenetre)]
    if len(dans) == 1:
        return _piece(dans[0], "montant_exact", 0.0, "montant exact dans la fenetre")
    if len(dans) > 1:
        return _ambigu(dans, "%s ecritures au meme montant dans la fenetre" % len(dans))
    # ⚠️ L'EXACTITUDE DU MONTANT NE BAT PAS LA DATE A SEPT MOIS DE DISTANCE. Cas reel SPH Khamsa :
    # deux ecritures de 24,400 pile, de septembre et d'octobre 2025, contre une de 24,410 le
    # 13/04/2026 — trois jours apres la declaration au portail, et dont l'ecart EST le timbre
    # fiscal. Les deux exactes rendaient le certificat « ambigu » et l'analyse s'arretait la, sans
    # jamais regarder la seule ecriture qui tombait au bon moment.
    # Un ecart de timbre DANS la fenetre est une meilleure preuve qu'un montant exact hors d'elle :
    # le premier s'explique (1 DT de timbre = 0,010 de retenue), le second n'est qu'une repetition
    # de montant, frequente chez un client qui facture toujours la meme prestation.
    # Le mode reste « montant_approchant », celui qu'elle aurait eu plus bas : seul son RANG change.
    # Lui donner un mode a elle aurait fait sauter le drapeau de revue qui accompagne tout ecart, et
    # change le comportement d'un chemin qui, lui, allait tres bien.
    timbre = [p for p in libres
              if _dans_fenetre(p["posting_date"], jour, fenetre)
              and ecart_timbre(p["paid_amount"] - retenue)]
    if len(timbre) == 1:
        ecart = round(timbre[0]["paid_amount"] - retenue, 3)
        return _piece(timbre[0], "montant_approchant", ecart,
                      "montant approchant (ecart %s) — explique par le timbre fiscal de %s DT, "
                      "dans la fenetre de %s jours" % (ecart, TIMBRE, fenetre))

    if len(exactes) == 1:
        return _piece(exactes[0], "montant_exact_hors_fenetre", 0.0,
                      "montant exact, hors fenetre de %s jours" % fenetre)
    if len(exactes) > 1:
        return _ambigu(exactes, "%s ecritures au meme montant, toutes hors fenetre" % len(exactes))

    marge = tolerance(retenue)
    proches = [p for p in libres
               if abs(p["paid_amount"] - retenue) <= marge
               and _dans_fenetre(p["posting_date"], jour, fenetre)]
    def _approchant(p):
        ecart = round(p["paid_amount"] - retenue, 3)
        raison = "montant approchant (ecart %s)" % ecart
        if ecart_timbre(ecart):
            raison += " — explique par le timbre fiscal de %s DT" % TIMBRE
        return _piece(p, "montant_approchant", ecart, raison)

    if len(proches) == 1:
        return _approchant(proches[0])
    if len(proches) > 1:
        # ⚠️ « APPROCHANT » COUVRE DEUX CHOSES TRES DIFFERENTES. `tolerance()` plancher a 1 DT :
        # sur une retenue de 53,760 elle admet aussi bien 53,770 (le timbre fiscal, ecart explique)
        # que 54,760 (1 DT d'ecart, inexplique). Les mettre sur le meme rang rendait « ambigu » les
        # quatre certificats STE TAURUS, alors qu'une seule ecriture tombe au millime a chaque fois.
        serres = [p for p in proches
                  if abs(p["paid_amount"] - retenue) <= MARGE_IDENTIFICATION]
        if len(serres) == 1:
            return _approchant(serres[0])
        return _ambigu(proches, "%s ecritures approchantes" % len(proches))

    # ⚠️ PAS DE RATTRAPAGE « seule ecriture du client ». Essaye en vrai, il a rapproche un
    # certificat de 110,260 DT d'une ecriture de 15,840 DT — un ecart de 94 DT — au seul motif
    # qu'elle etait la seule dans la fenetre. Pire : il volait ainsi la piece au certificat de
    # 15,837 DT auquel elle appartenait. Une piece dont le montant ne dit rien n'est pas une
    # preuve ; on la SIGNALE comme candidate, on ne la retient pas.
    fenetres = [p for p in libres if _dans_fenetre(p["posting_date"], jour, fenetre)]
    if fenetres:
        ecarts = ", ".join("%s (ecart %s)" % (p["name"], round(p["paid_amount"] - retenue, 3))
                           for p in fenetres[:3])
        return {"payment_entry": None, "mode": "ecart_important", "ecart": None,
                "raison": "aucune ecriture au bon montant ; candidates : %s" % ecarts,
                "candidats": [p["name"] for p in fenetres[:10]]}
    return {"payment_entry": None, "mode": "aucune",
            "raison": "aucune ecriture de retenue dans la fenetre", "candidats": []}


def pieces_ras_de_la_facture(facture: str, customer: str, ctx: Contexte,
                             cert_name: str = None) -> list:
    """Les ecritures de retenue IMPUTEES a cette facture. Sans aucune condition de date.

    Sert aux deux sens : a l'appariement (`apparier_par_facture`) et au garde-fou de la
    regularisation (`paiements.verifier`) — la meme question posee par les deux bouts doit avoir
    une seule reponse, donc un seul code.
    """
    if not facture or not customer:
        return []
    return [p for p in ctx.pes_ras.get(customer, [])
            if (cert_name is None or ctx.piece_libre(p["name"], cert_name))
            and any(r["doctype"] == "Sales Invoice" and r["name"] == facture and r.get("montant")
                    for r in ctx.refs.get(p["name"], []))]


def apparier_par_facture(cert: dict, ctx: Contexte, facture: str = None) -> dict:
    """L'ecriture de retenue ALLOUEE A LA FACTURE identifiee. La preuve par l'imputation.

    ⚠️ CET ETAGE EXISTE PARCE QUE LA DATE MENT. Le client declare au portail le jour ou il paie ;
    nous comptabilisons la retenue le jour ou nous encaissons. CLIMA FILTRI PRO : ecriture du
    25/02, certificat du 03/06 — 98 jours, trois fois la fenetre. Le certificat ressortait donc
    « sans ecriture » alors que l'ecriture etait la, imputee a la facture meme. Et la
    regularisation proposee ensuite aurait comptabilise la retenue une SECONDE fois : c'est le
    seul chemin de cette app qui pouvait creer une double comptabilisation.

    L'imputation a la facture est une preuve plus forte que la proximite des dates : elle designe
    la creance, pas un voisinage. La date ne sert donc plus de filtre ici — seulement le montant.
    """
    facture = facture or cert.get("sales_invoice")
    retenue = flt(cert.get("montant_retenue"), 3)
    candidats = pieces_ras_de_la_facture(facture, cert.get("customer"), ctx, cert.get("name"))
    if not candidats:
        return {"payment_entry": None, "mode": "aucune", "candidats": [],
                "raison": "aucune ecriture de retenue imputee a la facture %s" % facture}
    if len(candidats) > 1:
        return _ambigu(candidats, "%s ecritures de retenue sur la facture %s"
                       % (len(candidats), facture))
    piece = candidats[0]
    ecart = round(piece["paid_amount"] - retenue, 3)
    if abs(ecart) > tolerance(retenue):
        # Une retenue imputee a la bonne facture mais d'un tout autre montant n'est pas la notre :
        # ce peut etre une seconde retenue du meme client sur la meme facture.
        return {"payment_entry": None, "mode": "ecart_important", "ecart": ecart,
                "candidats": [piece["name"]],
                "raison": "l'ecriture %s imputee a la facture vaut %s (ecart %s)"
                          % (piece["name"], piece["paid_amount"], ecart)}
    raison = "ecriture de retenue imputee a la facture %s (%s DT du %s)" % (
        facture, piece["paid_amount"], piece["posting_date"])
    if ecart and ecart_timbre(ecart):
        raison += " — ecart %s explique par le timbre fiscal" % ecart
    return _piece(piece, "impute_a_la_facture", ecart, raison)


def departager_par_ecriture(cert: dict, ctx: Contexte, candidats: list,
                            fenetre: int = FENETRE_JOURS) -> dict:
    """LA COMPTABILITE DEPARTAGE CE QUE LE LIBELLE NE DEPARTAGE PAS.

    Deux fiches pour un meme matricule (« STE TAURUS » et « Sté TAURUS - 1 »), ou deux raisons
    sociales aussi proches (« Technolab » et « Sté TEC ») : le nom ne tranche pas, mais une seule
    des fiches porte une ecriture de retenue du bon montant a la bonne date. Cet etage rend au
    rapprochement les 4 certificats TAURUS et le certificat Technolab, sans IA et sans arbitrage.

    ⚠️ Il ne tranche QUE si un seul client porte une telle ecriture — deux porteurs, c'est
    l'ambiguite meme, et elle doit remonter.
    """
    retenue = flt(cert.get("montant_retenue"), 3)
    jour = cert.get("date_paiement")
    porteurs = {}
    for client in candidats or []:
        for p in ctx.pes_ras.get(client, []):
            if (ctx.piece_libre(p["name"], cert.get("name"))
                    and abs(p["paid_amount"] - retenue) <= MARGE_IDENTIFICATION
                    and _dans_fenetre(p["posting_date"], jour, fenetre)):
                porteurs.setdefault(client, []).append(p["name"])
    if len(porteurs) != 1:
        return {"customer": None, "pieces": [], "porteurs": sorted(porteurs)}
    client, pieces = next(iter(porteurs.items()))
    return {"customer": client, "pieces": pieces, "porteurs": [client]}


def _piece(pe: dict, mode: str, ecart: float, raison: str) -> dict:
    return {"payment_entry": pe["name"], "mode": mode, "ecart": ecart, "raison": raison,
            "candidats": []}


def _ambigu(candidats: list, raison: str) -> dict:
    return {"payment_entry": None, "mode": "ambigu", "ecart": None, "raison": raison,
            "candidats": [c["name"] for c in candidats[:10]]}


# ------------------------------------------------------------------ facture

def facture_de_piece(pe_name: str, ctx: Contexte) -> dict:
    """La facture (ou la commande) que l'ecriture solde. Les references a 0,000 sont ignorees.

    Une Payment Entry generee depuis une commande garde une ligne « Sales Order » a zero : la
    prendre pour la piece rattacherait le certificat a une commande alors que la facture existe.
    """
    lignes = [r for r in ctx.refs.get(pe_name, []) if r.get("montant")]
    factures = [r["name"] for r in lignes if r["doctype"] == "Sales Invoice"]
    if len(factures) == 1:
        return {"sales_invoice": factures[0], "sales_order": None, "raison": "facture de l'ecriture"}
    if len(factures) > 1:
        return {"sales_invoice": None, "sales_order": None,
                "raison": "%s factures sur la meme ecriture" % len(factures),
                "candidats": factures}
    commandes = [r["name"] for r in lignes if r["doctype"] == "Sales Order"]
    if len(commandes) == 1:
        return {"sales_invoice": None, "sales_order": commandes[0],
                "raison": "commande de l'ecriture, facture a venir"}
    return {"sales_invoice": None, "sales_order": None, "raison": "ecriture sans piece allouee"}


def facture_par_reglement_net(cert: dict, ctx: Contexte,
                              fenetre: int = FENETRE_REGLEMENT) -> dict:
    """Retrouve la facture par le REGLEMENT NET (TTC - retenue), timbre compris.

    Le client qui retient paie le solde ; ce reglement-la existe en banque et porte deja sa
    facture. C'est le seul chemin qui donne la facture d'un certificat sans piece de retenue.
    """
    customer = cert.get("customer")
    net = round(flt(cert.get("total_brut"), 3) - flt(cert.get("montant_retenue"), 3), 3)
    if not customer or net <= 0:
        return {"sales_invoice": None, "raison": "assiette inexploitable", "candidats": []}
    # Le timbre de 1 DT est sur la facture mais hors assiette declaree : le reglement reel depasse
    # donc le net calcule d'environ 1 DT. La tolerance doit l'admettre, sinon aucun cas ne passe.
    marge = max(tolerance(net), TIMBRE + 0.05)
    jour = cert.get("date_paiement")
    proches = [p for p in ctx.encaissements.get(customer, [])
               if abs(p["paid_amount"] - net) <= marge and _dans_fenetre(p["posting_date"], jour,
                                                                        fenetre)]
    if len(proches) != 1:
        return {"sales_invoice": None, "candidats": [p["name"] for p in proches[:10]],
                "raison": ("%s reglements nets possibles" % len(proches)) if proches
                else "aucun reglement net de %s DT autour du %s" % (net, jour)}
    factures = [r["name"] for r in ctx.refs.get(proches[0]["name"], [])
                if r["doctype"] == "Sales Invoice" and r.get("montant")]
    if len(factures) != 1:
        # ⚠️ PAS UN ECHEC : UNE PISTE. Le client paie souvent son solde en une fois pour plusieurs
        # factures, parfois partiellement — BHM : 367,29 net (371,00 - 3,71) impute a 271,29 sur une
        # facture de 466 et 96,00 sur une autre. Aucune facture ne vaut donc l'assiette, et la regle
        # de l'assiette ne trouvera rien : c'est CE reglement qui nomme les creances, et lui seul.
        # Rendre les factures sous une cle distincte : `candidats` porte des ECRITURES dans les
        # autres branches, et melanger les deux ferait proposer une piece a la place d'une facture.
        return {"sales_invoice": None, "candidats": factures[:10],
                "factures_du_reglement": factures[:10], "reglement": proches[0]["name"],
                "raison": "le reglement net %s (%s DT) porte %s factures"
                          % (proches[0]["name"], proches[0]["paid_amount"], len(factures))}
    return {"sales_invoice": factures[0], "reglement": proches[0]["name"], "candidats": [],
            "raison": "reglement net %s (%s DT) du %s" % (proches[0]["name"],
                                                          proches[0]["paid_amount"],
                                                          proches[0]["posting_date"])}


def facture_par_assiette(cert: dict, ctx: Contexte, fenetre: int = FENETRE_REGLEMENT) -> dict:
    """Facture dont le TTC egale l'assiette du certificat — timbre fiscal compris ou non.

    ⚠️ LA REGLE DU TIMBRE, verifiee sur les donnees reelles : notre facture porte 1 DT de timbre
    fiscal que l'assiette declaree au portail n'inclut pas. 1 154,000 au certificat contre
    1 155,000 en facture ; 1 244,504 contre 1 245,505 ; 11 026,000 contre 11 027,000. Chercher
    l'egalite stricte ne trouvait donc RIEN sur ces trois cas, alors que la facture etait la, a la
    date exacte du certificat. Les deux cibles sont donc testees.

    Quand plusieurs factures conviennent, la date departage — et si elle ne departage pas, on ne
    tranche pas : la liste part en candidats.
    """
    customer = cert.get("customer")
    assiette = flt(cert.get("total_brut"), 3)
    cibles = (assiette, round(assiette + TIMBRE, 3))
    candidates = [f for f in ctx.factures.get(customer, [])
                  if any(abs(f["grand_total"] - cible) < 0.05 for cible in cibles)
                  and ctx.facture_libre(f["name"], cert.get("name"))]

    def _retenue(f, precision):
        avec_timbre = abs(f["grand_total"] - cibles[1]) < 0.05
        return {"sales_invoice": f["name"], "candidats": [],
                "raison": "facture au TTC de %s%s%s" % (
                    f["grand_total"], " (assiette + timbre)" if avec_timbre else "", precision)}

    if len(candidates) == 1:
        return _retenue(candidates[0], "")
    if len(candidates) > 1:
        proches = [f for f in candidates if _dans_fenetre(f["posting_date"],
                                                          cert.get("date_paiement"), fenetre)]
        if len(proches) == 1:
            return _retenue(proches[0], ", seule dans la fenetre de dates")
        return {"sales_invoice": None, "candidats": [f["name"] for f in candidates[:10]],
                "raison": "%s factures au TTC de %s" % (len(candidates), assiette)}
    return {"sales_invoice": None, "candidats": [],
            "raison": "aucune facture au TTC de %s (ni %s)" % cibles}


# ------------------------------------------------------------------ decision

# Modes de piece retenus au PREMIER passage. L'ordre des passages compte autant que l'ordre des
# etages : traite chronologiquement, un certificat au montant approchant s'appropriait l'ecriture
# d'un autre certificat qui, lui, tombait au millime. On sert d'abord les preuves exactes.
MODES_FORTS = ("montant_exact", "montant_exact_hors_fenetre")

# Identification juste mais indirecte : le montant d'une ecriture ne nomme pas un tiers, il le
# designe par imputation. Elle rapproche — et elle se relit. (L'identification par le numero de
# contribuable demande sa revue au cas par cas, via la cle `revue` : voir `identifier_client`.)
METHODES_A_REVOIR = ("ecriture",)


def rapprocher_un(cert: dict, ctx: Contexte, ai_resolver=None, modes=None) -> dict:
    """Decide de tout pour UN certificat. Rend les champs a poser, jamais None.

    `modes` limite les appariements acceptes (premier passage). Aucun silence : meme un
    certificat ecarte porte le motif de son ecartement.
    """
    if cert.get("hors_perimetre"):
        return {"match_status": "Ignore", "match_method": "aucun",
                "match_raison": "hors perimetre : certificat anterieur a %s" % C.ANNEE_MINIMALE}
    etat = cert.get("etat_depot") or C.normaliser_etat(cert.get("etat_source"))
    if etat == C.ETAT_ANNULE:
        return {"match_status": "Ignore", "match_method": "aucun",
                "match_raison": "certificat annule au portail : aucune retenue a comptabiliser"}
    if cert.get("anomalie"):
        return {"match_status": "Ignore", "match_method": "aucun",
                "match_raison": "anomalie : %s" % (cert.get("anomalie_raison") or "")}
    if etat not in (C.ETAT_RECUE, C.ETAT_RECTIFIE):
        return {"match_status": "Ignore", "match_method": "aucun",
                "match_raison": "etat non exploitable (%s)" % (cert.get("etat_source") or etat)}

    client = identifier_client(cert, ctx, ai_resolver=ai_resolver)
    if not client["customer"]:
        return {"match_status": "Ambiguous" if client["candidats"] else "Unmatched",
                "customer": None, "match_method": "aucun", "match_score": client["score"],
                "match_raison": client["raison"],
                "match_candidates": json.dumps(client["candidats"], ensure_ascii=False)
                if client["candidats"] else None}

    enrichi = dict(cert, customer=client["customer"])
    piece = apparier_piece(enrichi, ctx)
    out = {"customer": client["customer"], "match_method": client["method"],
           "match_score": client["score"], "payment_entry": piece.get("payment_entry"),
           "ecart_piece": piece.get("ecart") or 0.0}
    # Pose des l'identification, donc valable sur TOUTES les issues qui suivent — y compris « sans
    # piece », ou le champ serait autrement remis a zero avec le reste de la decision.
    if client.get("revue") or client["method"] in METHODES_A_REVOIR:
        out["revue_requise"] = 1

    if piece.get("payment_entry") and modes and piece.get("mode") not in modes:
        # Preuve trop faible pour ce passage : on laisse la piece aux certificats exacts.
        return {**out, "payment_entry": None, "match_status": "Reporte",
                "match_raison": "%s ; %s (reporte au second passage)" % (client["raison"],
                                                                        piece["raison"])}

    if piece.get("payment_entry"):
        facture = facture_de_piece(piece["payment_entry"], ctx)
        out.update({"sales_invoice": facture.get("sales_invoice"),
                    "sales_order": facture.get("sales_order"),
                    "match_status": "Auto Matched",
                    "match_raison": "%s ; %s ; %s" % (client["raison"], piece["raison"],
                                                      facture["raison"])})
        # Un ecart de montant se voit, il ne se corrige pas tout seul : la piece est deja
        # comptabilisee, et c'est a l'utilisateur de decider s'il l'ajuste.
        if piece.get("mode") == "montant_approchant":
            out["revue_requise"] = 1
        return out

    if piece.get("mode") in ("ambigu", "ecart_important"):
        out.update({"match_status": "Ambiguous",
                    "match_raison": "%s ; %s" % (client["raison"], piece["raison"]),
                    "match_candidates": json.dumps(piece["candidats"], ensure_ascii=False),
                    "revue_requise": 1})
        return out

    # Sans ecriture : on cherche quand meme LA FACTURE, car c'est elle qui rendra la creation du
    # paiement possible et sure. Le reglement net d'abord (il porte la facture), l'assiette ensuite.
    net = facture_par_reglement_net(enrichi, ctx)
    facture = net if net.get("sales_invoice") else facture_par_assiette(enrichi, ctx)

    # Le reglement net a nomme ses factures sans pouvoir choisir : cette piste vaut infiniment mieux
    # que le « aucune facture au TTC de X » de la regle de l'assiette, qui ne peut RIEN trouver quand
    # le client a paye plusieurs factures d'un coup. Sans cette reprise, l'information etait
    # decouverte puis jetee, et l'utilisateur restait devant un cul-de-sac.
    if not facture.get("sales_invoice") and net.get("factures_du_reglement"):
        facture = {**facture, "candidats": net["factures_du_reglement"],
                   "raison": "%s ; %s" % (facture.get("raison") or "", net["raison"])}

    # La facture connue, on redemande l'ecriture — mais par l'IMPUTATION cette fois, sans condition
    # de date. Jamais au premier passage : les appariements au millime servent d'abord, sinon un
    # certificat passe par la facture prendrait l'ecriture d'un certificat qui, lui, tombe juste.
    if facture.get("sales_invoice") and not modes:
        par_facture = apparier_par_facture(enrichi, ctx, facture["sales_invoice"])
        if par_facture.get("payment_entry"):
            out.update({"payment_entry": par_facture["payment_entry"],
                        "ecart_piece": par_facture.get("ecart") or 0.0,
                        "sales_invoice": facture["sales_invoice"],
                        "match_status": "Auto Matched",
                        "match_raison": "%s ; %s ; %s" % (client["raison"], facture["raison"],
                                                          par_facture["raison"]),
                        "revue_requise": 1 if (par_facture.get("ecart") or client.get("revue")
                                               or client["method"] in METHODES_A_REVOIR) else 0})
            return out

    out.update({
        "match_status": "Sans piece",
        "sales_invoice": facture.get("sales_invoice"),
        "match_raison": "%s ; aucune ecriture de retenue ; %s" % (client["raison"],
                                                                  facture["raison"]),
        "match_candidates": json.dumps(facture.get("candidats") or [], ensure_ascii=False)
        if facture.get("candidats") else None,
    })
    return out


# Champs remis a zero avant chaque decision : sans cela, un rapprochement devenu faux (piece
# annulee, client corrige) laisserait derriere lui des valeurs d'un passage precedent, et le
# tableau montrerait une facture qui ne correspond plus a rien.
CHAMPS_DECISION = ("customer", "payment_entry", "sales_invoice", "sales_order", "match_status",
                   "match_method", "match_score", "match_raison", "match_candidates",
                   "ecart_piece", "revue_requise")

CHAMPS_CERTIFICAT = ("name", "reference", "declarant", "declarant_matricule", "date_paiement",
                     "etat_depot", "etat_source", "total_brut", "total_ht", "total_tva",
                     "montant_retenue", "anomalie", "anomalie_raison", "hors_perimetre",
                     "match_status", "customer", "payment_entry", "revue_requise")


def rapparier_manuel(cert: dict, ctx: Contexte = None) -> dict:
    """Retrouve la piece d'un certificat dont le client vient d'etre pose A LA MAIN.

    Le client et le statut « Manually Matched » sont conserves — c'est l'arbitrage humain, il fait
    autorite. Seules la piece et la facture sont completees : l'utilisateur vient de donner
    l'information qui manquait, il doit en voir l'effet immediatement plutot qu'au cron du
    lendemain.
    """
    ctx = ctx or charger_contexte()
    piece = apparier_piece(cert, ctx)
    valeurs = {"ecart_piece": flt(piece.get("ecart") or 0, 3)}
    if piece.get("payment_entry"):
        facture = facture_de_piece(piece["payment_entry"], ctx)
        valeurs.update({"payment_entry": piece["payment_entry"],
                        "sales_invoice": facture.get("sales_invoice"),
                        "sales_order": facture.get("sales_order"),
                        "match_raison": "client pose a la main ; %s ; %s" % (piece["raison"],
                                                                             facture["raison"])})
    else:
        net = facture_par_reglement_net(cert, ctx)
        facture = net if net.get("sales_invoice") else facture_par_assiette(cert, ctx)
        # Meme etage que dans `rapprocher_un` : le client vient d'etre pose, la facture suit, et
        # c'est l'imputation — pas la date — qui retrouve l'ecriture.
        par_facture = (apparier_par_facture(cert, ctx, facture["sales_invoice"])
                       if facture.get("sales_invoice") else {})
        valeurs.update({"payment_entry": par_facture.get("payment_entry"),
                        "ecart_piece": flt(par_facture.get("ecart") or 0, 3),
                        "sales_invoice": facture.get("sales_invoice"),
                        "match_raison": "client pose a la main ; %s ; %s%s" % (
                            piece["raison"], facture["raison"],
                            " ; " + par_facture["raison"] if par_facture else "")})
    frappe.db.set_value(DOCTYPE, cert["name"], valeurs, update_modified=False)
    return valeurs


def certificats_a_rapprocher(annee_min: int = None, tout: bool = False) -> list:
    """Certificats candidats. Les arbitrages humains sont exclus : la machine ne repasse pas
    derriere l'utilisateur."""
    filtres = {"match_status": ["!=", "Manually Matched"]}
    if not tout:
        filtres["hors_perimetre"] = 0
    return frappe.db.get_all(DOCTYPE, filters=filtres, fields=list(CHAMPS_CERTIFICAT),
                             order_by="date_paiement", limit_page_length=0)


def rapprocher(certificats: list = None, ctx: Contexte = None, insert: bool = True,
               use_ai: bool = False, annee_min: int = None) -> dict:
    """Rapproche les certificats du perimetre. -> compteurs + detail par statut.

    `insert=0` ne touche a rien : c'est l'essai a blanc, et il rend exactement ce que la
    validation ecrirait.
    """
    certificats = certificats_a_rapprocher(annee_min) if certificats is None else certificats
    ctx = ctx or charger_contexte(annee_min)
    diagnostics = []
    resolver = None
    if use_ai:
        from bank_retenue_sync.ai import party_match

        resolver = party_match.resolver(diagnostics)

    res = {"traites": 0, "auto": 0, "sans_piece": 0, "ambigus": 0, "non_identifies": 0,
           "ignores": 0, "ecarts": 0, "tours": 0, "detail": [], "diagnostics": diagnostics}

    # PREMIER PASSAGE : seuls les appariements au millime, et on reserve leurs pieces.
    # Sans lui, un certificat approchant traite plus tot prend l'ecriture d'un certificat exact
    # traite plus tard — constate en reel sur ABC MED, ou un certificat de 110,260 DT avait
    # capture l'ecriture de 15,840 DT appartenant au certificat de 15,837 DT.
    for cert in certificats:
        decision = rapprocher_un(cert, ctx, ai_resolver=None, modes=MODES_FORTS)
        if decision.get("match_status") == "Auto Matched" and decision.get("payment_entry"):
            ctx.reserver(cert["name"], piece=decision["payment_entry"],
                         facture=decision.get("sales_invoice"))

    # PUIS ON FAIT CONVERGER. Chaque appariement retire une piece du vivier et peut donc lever
    # l'ambiguite d'un autre certificat — mais seulement s'il est traite APRES lui. Repeter
    # jusqu'a stabilite rend le resultat independant de l'ordre : sans cela, deux executions
    # successives ne rendaient pas la meme chose (24 puis 27 rapprochements sur les memes donnees).
    decisions, precedent = {}, None
    for tour in range(3):
        decisions = {}
        for cert in certificats:
            d = rapprocher_un(cert, ctx, ai_resolver=resolver if tour == 0 else None)
            decisions[cert["name"]] = d
            if d.get("match_status") == "Auto Matched" and d.get("payment_entry"):
                ctx.reserver(cert["name"], piece=d["payment_entry"],
                             facture=d.get("sales_invoice"))
        courant = {k: v.get("payment_entry") for k, v in decisions.items()}
        res["tours"] = tour + 1
        if courant == precedent:
            break
        precedent = courant

    for cert in certificats:
        decision = decisions[cert["name"]]
        valeurs = {k: decision.get(k) for k in CHAMPS_DECISION}
        # Les colonnes numeriques de Frappe refusent NULL : un chemin de decision qui ne parle pas
        # d'ecart ni de score doit ecrire 0, pas rien.
        valeurs["revue_requise"] = int(decision.get("revue_requise") or 0)
        valeurs["ecart_piece"] = flt(decision.get("ecart_piece") or 0, 3)
        valeurs["match_score"] = flt(decision.get("match_score") or 0, 3)
        res["traites"] += 1
        statut = decision.get("match_status")
        if statut == "Auto Matched":
            res["auto"] += 1
            if flt(decision.get("ecart_piece"), 3):
                res["ecarts"] += 1
        elif statut == "Sans piece":
            res["sans_piece"] += 1
        elif statut == "Ambiguous":
            res["ambigus"] += 1
        elif statut == "Ignore":
            res["ignores"] += 1
        else:
            res["non_identifies"] += 1
        res["detail"].append({"reference": cert.get("reference"), "declarant": cert.get("declarant"),
                              "montant": flt(cert.get("montant_retenue"), 3), **valeurs})
        if insert:
            frappe.db.set_value(DOCTYPE, cert["name"], valeurs, update_modified=False)
    return res
