"""Paiements de la CARTE TECHNOLOGIQUE, lus chez tej-bank-service et comptabilises.

CE QUE LE SERVICE DONNE — et qui rend l'email inutile
------------------------------------------------------
`GET /banque/cartes/export/latest` rend un XLSX du releve de carte :

    Date | Operation | Detail operation | Statut | Reference | Montant
    18/07/2026 | Achat internet | FACEBK P32RUWR7X2 IE | Transaction approuvée | 927555 | 629.202

La colonne **Reference** est EXACTEMENT le numero que portent les ecritures saisies a la main
(« Facture Facebook 262299 ») : la cle d'idempotence existe donc deja, et les saisies historiques
sont reconnues sans rien reprendre.

⚠️ LE STATUT EST LA REGLE LA PLUS IMPORTANTE
---------------------------------------------
Le releve contient deux natures de lignes :
  - `Transaction approuvée` : le paiement a eu lieu, il y a une reference, il se comptabilise ;
  - `Solde insuffisant`     : la carte a REFUSE le paiement. Aucune reference, aucun argent n'a
    bouge. Sur l'export du 21/07, **7 lignes sur 17 sont des refus** — les comptabiliser aurait
    ajoute 3 447 DT de charges qui n'existent pas.
Le montant seul ne les distingue pas : un refus de 314,755 ressemble trait pour trait a un
paiement. Seul le statut tranche, et l'absence de reference le confirme.
"""
from __future__ import annotations

import io

import frappe
import openpyxl
import requests
from frappe.utils import flt, getdate

from bank_retenue_sync.bank.movements import _base_url, _col, _headers, _num, _to_date

COMPTE_CARTE = "Carte technologique - A&S"
COMPTE_CHARGE = "Frais de Marketing - A&S"

STATUT_OK = "approuv"          # « Transaction approuvée », insensible aux accents/casse
PREFIXE_REFERENCE = "Facture Facebook"


def fetch_latest_cartes() -> list:
    """Dernier export de carte, normalise : [{date, operation, detail, statut, reference, montant}]."""
    r = requests.get(_base_url() + "/banque/cartes/export/latest", headers=_headers(), timeout=60)
    r.raise_for_status()
    return parse_cartes_xlsx(r.content)


def parse_cartes_xlsx(content: bytes) -> list:
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    out = []
    for raw in rows[1:]:
        d = dict(zip(header, raw))
        jour = _to_date(_col(d, "Date"))
        montant = flt(_num(_col(d, "Montant")), 3)
        if not jour or not montant:
            continue
        out.append({
            "date": jour,
            "operation": str(_col(d, "Operation") or "").strip(),
            "detail": str(_col(d, "Detail operation", "Detail") or "").strip(),
            "statut": str(_col(d, "Statut") or "").strip(),
            "reference": str(_col(d, "Reference") or "").strip(),
            "montant": montant,
        })
    return out


def est_approuve(ligne: dict) -> bool:
    """Un paiement REEL : statut approuve ET reference presente.

    Les deux conditions, pas une seule — un refus n'a pas de reference, mais on ne se fie pas a
    cette absence pour conclure : c'est le statut qui porte l'information, la reference qui la
    confirme. Et sans reference il n'y aurait de toute facon aucune cle d'idempotence.
    """
    return (STATUT_OK in (ligne.get("statut") or "").lower()
            and bool((ligne.get("reference") or "").strip()))


def reference_ecriture(ligne: dict) -> str:
    """Numero de reference de l'ecriture — identique aux saisies manuelles historiques."""
    return "%s %s" % (PREFIXE_REFERENCE, (ligne.get("reference") or "").strip())


def derniere_ecriture(compte: str = COMPTE_CARTE):
    """Date de la DERNIERE ecriture portee au compte de la carte. -> date | None

    C'est le point de depart demande : on ne rejoue pas l'historique, on reprend la ou la
    comptabilite s'est arretee. Les lignes anterieures restent visibles au rapport de suivi,
    simplement elles ne sont plus candidates a la creation.
    """
    d = frappe.db.sql(
        """select max(gl.posting_date) from `tabGL Entry` gl
           where gl.account = %s and gl.is_cancelled = 0 and gl.credit > 0""", compte)[0][0]
    return getdate(d) if d else None


# Sentinelle : `depuis` omis = on reprend a la derniere ecriture ; `depuis=None` = AUCUNE borne.
# Confondre les deux rendait le rattrapage impossible a demander — c'est justement le cas ou l'on
# veut relire tout l'historique en s'appuyant sur la seule idempotence par numero.
_AUTO = object()


def a_comptabiliser(lignes: list = None, depuis=_AUTO) -> list:
    """Paiements approuves, posterieurs au dernier comptabilise, et pas deja saisis.

    L'idempotence tient au `cheque_no` : meme si `depuis` laisse passer une ligne ancienne, une
    ecriture existante la fait ecarter. La date n'est donc qu'un raccourci, jamais la seule
    garantie — c'est ce qui permet de la relacher sans risque pour un rattrapage.
    """
    lignes = lignes if lignes is not None else fetch_latest_cartes()
    depuis = derniere_ecriture() if depuis is _AUTO else depuis
    out = []
    for l in lignes:
        if not est_approuve(l):
            continue
        if depuis and getdate(l["date"]) < getdate(depuis):
            continue
        if frappe.db.exists("Journal Entry", {"cheque_no": reference_ecriture(l),
                                              "docstatus": ["<", 2]}):
            continue
        out.append(l)
    return sorted(out, key=lambda l: getdate(l["date"]))


def _ecritures_par_reference(references: list) -> dict:
    """{reference -> ecriture} pour les references donnees. Une seule par reference : c'est la
    definition meme de l'idempotence du flux, et le rapport de controle s'appuie dessus."""
    if not references:
        return {}
    rows = frappe.db.get_all(
        "Journal Entry", filters={"cheque_no": ["in", references], "docstatus": ["<", 2]},
        fields=["name", "cheque_no", "docstatus", "posting_date"], limit_page_length=0)
    return {r.cheque_no: r for r in rows}


# Les trois etats d'une ligne du releve, et leur sens comptable :
ETAT_COMPTABILISEE = "comptabilisee"      # paiement approuve, ecriture presente -> livres a jour
ETAT_A_COMPTABILISER = "a_comptabiliser"  # paiement approuve, AUCUNE ecriture -> livres en retard
ETAT_REFUSEE = "refusee"                  # refus de la carte : aucun argent n'a bouge, rien a saisir


def etat_controle(lignes: list = None, refresh: bool = False) -> dict:
    """Confronte le releve de CARTE aux ecritures : « mes livres sont-ils a jour ? ».

    C'est le seul controle possible sur ce compte. La carte est une tresorerie parallele : ses
    depenses ne paraissent JAMAIS au releve bancaire, donc le rapprochement bancaire ne peut rien
    en dire. Restent deux preuves, et elles doivent concorder toutes les deux :
      1. ligne par ligne — chaque paiement approuve du releve porte-t-il son ecriture ?
      2. en masse — le solde comptable egale-t-il le solde REEL lu chez le service ?

    Le premier controle explique le second : tant qu'un paiement manque, l'ecart de solde EST ce
    paiement, pas un frais. C'est pour cela que `a_jour` exige les deux.
    """
    if refresh:
        try:
            refresh_cartes()
        except Exception:
            pass          # non fatal : le dernier export vaut mieux qu'un controle qui echoue
    lignes = lignes if lignes is not None else fetch_latest_cartes()
    ecritures = _ecritures_par_reference(
        [reference_ecriture(l) for l in lignes if est_approuve(l)])

    detail, restants = [], []
    for l in sorted(lignes, key=lambda x: getdate(x["date"]), reverse=True):
        approuve = est_approuve(l)
        reference = reference_ecriture(l) if approuve else ""
        je = ecritures.get(reference) if approuve else None
        if not approuve:
            etat = ETAT_REFUSEE
        elif je:
            etat = ETAT_COMPTABILISEE
        else:
            etat = ETAT_A_COMPTABILISER
            restants.append(l)
        detail.append({
            "date": str(l["date"]), "operation": l.get("operation"), "detail": l.get("detail"),
            "statut": l.get("statut"), "montant": flt(l["montant"], 3),
            "reference": reference, "etat": etat,
            "journal_entry": je.name if je else None,
            "brouillon": bool(je and je.docstatus == 0),
        })

    comptable = solde_comptable()
    try:
        reel = solde_carte()
    except Exception as e:
        # Sans le solde du service il reste le controle ligne a ligne : degrade, pas perdu.
        reel = {"erreur": str(e)[:160]}
    ecart = (round(comptable - flt(reel.get("solde"), 3), 3)
             if reel.get("lu_le") else None)
    reste = round(sum(flt(l["montant"], 3) for l in restants), 3)
    return {
        "lignes": detail,
        "resume": {
            "total": len(detail),
            "comptabilisees": sum(1 for d in detail if d["etat"] == ETAT_COMPTABILISEE),
            "a_comptabiliser": len(restants),
            "refusees": sum(1 for d in detail if d["etat"] == ETAT_REFUSEE),
            "montant_a_comptabiliser": reste,
            "brouillons": sum(1 for d in detail if d["brouillon"]),
        },
        "solde_comptable": comptable,
        "solde_reel": flt(reel.get("solde"), 3) if reel.get("lu_le") else None,
        "ecart": ecart,
        "lu_le": reel.get("lu_le"),
        "plafond_restant": flt(reel.get("plafond_restant"), 3) if reel.get("lu_le") else None,
        "seuil_recharge": seuil_recharge(),
        "service": reel.get("erreur"),
        # A JOUR = les deux preuves concordent. Un ecart inconnu (service muet) ne vaut pas
        # concordance : on ne declare pas des livres a jour sur un controle qu'on n'a pas pu faire.
        "a_jour": not restants and ecart is not None and abs(ecart) < 0.005,
    }


@frappe.whitelist()
def controle_releve(refresh=0) -> dict:
    """Le controle, expose au bureau. Lecture seule : rien n'est cree ici.

    Ne leve jamais : le rapport l'appelle a chaque ouverture, et un service bancaire injoignable
    ne doit pas se traduire par une fenetre d'erreur devant un tableau parfaitement lisible.
    """
    frappe.only_for(["System Manager", "Accounts Manager", "Accounts User"])
    try:
        return etat_controle(refresh=bool(frappe.utils.cint(refresh)))
    except Exception as e:
        return {"erreur": str(e)[:200]}


def build_journal_entry(ligne: dict, insert: bool = True):
    """`Dr Frais de Marketing / Cr Carte technologique`, a la DATE D'OPERATION de la carte.

    Deux lignes, comme les saisies manuelles : la carte est deja approvisionnee, il n'y a ni TVA
    ni timbre sur un achat de publicite en ligne facture hors taxes par l'Irlande.
    """
    from bank_retenue_sync.expenses import journal

    montant = flt(ligne["montant"], 3)
    company = journal._company_of(COMPTE_CHARGE)
    cc = frappe.db.get_value("Company", company, "cost_center")
    lignes = [
        {"account": COMPTE_CHARGE, "debit": montant, "cost_center": cc},
        {"account": COMPTE_CARTE, "credit": montant},
    ]
    reference = reference_ecriture(ligne)
    remark = "%s\n%s — %s" % (reference, ligne.get("detail") or "", ligne.get("operation") or "")
    je = journal.build_journal_entry(
        company, ligne["date"], lignes, remark=remark,
        cheque_no=reference, cheque_date=ligne["date"])
    if not insert:
        return je                      # essai a blanc : rien n'est ecrit
    je.insert(ignore_permissions=True)
    # Meme reglage que tous les autres flux : le paiement est un FAIT constate au releve de
    # carte, pas une hypothese — s'il est coche, l'ecriture est validee.
    if journal._auto_submit_enabled():
        je.submit()
    return je


def solde_carte() -> dict:
    """Solde REEL de la carte et plafonds annuels, lus chez tej-bank-service.

    -> {solde, numero, masque, lu_le, plafond_paiement_restant, plafond_paiement_consomme}

    ⚠️ Ces routes (`/banque/cartes/solde`, `/banque/cartes/solde/latest`) NE FIGURENT PAS dans
    l'`openapi.json` du service — elles repondent pourtant en 200. On ne peut donc pas les
    decouvrir par le schema : c'est un point a signaler cote TEJ.

    Le solde comptable, lui, se DEDUIT (recharges − depenses). Avoir les deux permet de les
    confronter : c'est ce que fait le rapport de suivi, et c'est ainsi qu'on a vu qu'il manquait
    deux paiements de juillet.
    """
    r = requests.get(_base_url() + "/banque/cartes/solde/latest", headers=_headers(), timeout=30)
    r.raise_for_status()
    d = r.json() or {}
    paiement = ((d.get("plafonds") or {}).get("paiement") or {})
    return {
        "solde": flt(d.get("solde_carte_valeur"), 3),
        "numero": d.get("numero"), "masque": d.get("masque"),
        "lu_le": d.get("lu_le"), "devise": d.get("devise"),
        "plafond_annuel": flt(paiement.get("annuel_valeur"), 3),
        "plafond_consomme": flt(paiement.get("consomme_valeur"), 3),
        "plafond_restant": flt(paiement.get("restant_valeur"), 3),
    }


SEUIL_RECHARGE_DEFAUT = 700.0
# Une recharge n'est pas un appoint : on ne vire pas 555 DT pour repasser de justesse au-dessus du
# seuil, on remet une provision de travail sur la carte. Sinon la carte replonge sous le seuil au
# premier debit Facebook (600 DT en moyenne) et le virement recommence toutes les semaines.
MONTANT_RECHARGE_DEFAUT = 1500.0
SUJET_ALERTE = "Carte technologique à recharger"

# Le delta entre solde reel et solde comptable, une fois TOUTES les operations reconnues, ne peut
# etre qu'un frais preleve par la banque sur la carte. Pas de compte dedie : une carte bancaire a
# sa place dans les frais bancaires, qui portent deja les commissions du compte.
COMPTE_FRAIS = "Frais bancaire - A&S"
PREFIXE_FRAIS = "Frais carte technologique"


def solde_comptable(compte: str = COMPTE_CARTE) -> float:
    return flt(frappe.db.sql(
        """select sum(debit) - sum(credit) from `tabGL Entry`
           where account = %s and is_cancelled = 0""", compte)[0][0], 3)


def ecart_carte() -> dict:
    """{comptable, reel, ecart, lu_le} — positif = les livres portent PLUS que la carte."""
    reel = solde_carte()
    comptable = solde_comptable()
    return {"comptable": comptable, "reel": flt(reel.get("solde"), 3),
            "ecart": round(comptable - flt(reel.get("solde"), 3), 3),
            "lu_le": reel.get("lu_le")}


def sync_frais_carte(insert: bool = True) -> dict:
    """Aligne le solde comptable sur le solde REEL, par une ecriture de frais.

    ⚠️ UNIQUEMENT QUAND TOUTES LES OPERATIONS SONT RECONNUES. Tant qu'un paiement du releve de
    carte n'est pas comptabilise, le delta n'est PAS un frais : c'est ce paiement. Comptabiliser
    l'ecart a ce moment-la creerait une charge fictive ET masquerait l'operation manquante — deux
    erreurs pour le prix d'une. C'est le controle le plus important de cette fonction.

    Le sens suit le signe : les livres portent plus que la carte -> une charge manque
    (`Dr Frais bancaire / Cr Carte`) ; l'inverse -> un avoir.

    Une seule regularisation par date de lecture du solde : sans cette borne, deux passages le
    meme jour sur un solde relu entre-temps en creeraient deux.
    """
    from bank_retenue_sync.expenses import journal

    restants = a_comptabiliser()
    if restants:
        return {"statut": "operations non comptabilisees", "reste": len(restants),
                "montant": round(sum(flt(l["montant"], 3) for l in restants), 3)}

    etat = ecart_carte()
    ecart = etat["ecart"]
    if abs(ecart) < 0.005:
        return {"statut": "aligne", **etat}

    jour = getdate(str(etat.get("lu_le") or "")[:10]) if etat.get("lu_le") else getdate()
    reference = "%s %s" % (PREFIXE_FRAIS, jour)
    if frappe.db.exists("Journal Entry", {"cheque_no": reference, "docstatus": ["<", 2]}):
        return {"statut": "deja regularise", "reference": reference, **etat}

    montant = abs(ecart)
    company = journal._company_of(COMPTE_FRAIS)
    cc = frappe.db.get_value("Company", company, "cost_center")
    lignes = ([{"account": COMPTE_FRAIS, "debit": montant, "cost_center": cc},
               {"account": COMPTE_CARTE, "credit": montant}]
              if ecart > 0 else
              [{"account": COMPTE_CARTE, "debit": montant},
               {"account": COMPTE_FRAIS, "credit": montant, "cost_center": cc}])
    remark = ("%s\nAlignement du solde comptable (%s) sur le solde reel de la carte (%s), "
              "toutes operations comptabilisees." % (reference, etat["comptable"], etat["reel"]))
    je = journal.build_journal_entry(company, jour, lignes, remark=remark,
                                     cheque_no=reference, cheque_date=jour)
    if not insert:
        return {"statut": "a creer", "montant": montant, "je": "(dry-run)", **etat}
    je.insert(ignore_permissions=True)
    if journal._auto_submit_enabled():
        je.submit()
    return {"statut": "regularise", "montant": montant, "je": je.name, **etat}


def seuil_recharge() -> float:
    """Seuil sous lequel la carte doit etre rechargee. 0 desactive l'alerte."""
    try:
        v = frappe.db.get_single_value("Bank Retenue Sync Settings", "seuil_recharge_carte")
        return flt(v, 3) if v is not None else SEUIL_RECHARGE_DEFAUT
    except Exception:
        return SEUIL_RECHARGE_DEFAUT


def montant_a_payer() -> float:
    """Total des virements deja en attente, repris du tableau « Paiements a faire ».

    Import tardif volontaire : le rapport importe ce module pour la ligne de recharge, l'inverse
    creerait un cycle a l'import. Non bloquant — une alerte doit partir meme si le calcul echoue.
    """
    try:
        from bank_retenue_sync.bank_retenue_sync.report.paiements_a_faire import (
            paiements_a_faire as rapport)

        return flt(sum(l["montant"] for l in rapport._virements_a_faire(frappe._dict())), 3)
    except Exception:
        return 0.0


def montant_recharge() -> float:
    """Montant d'une recharge de carte. Reglable, 1500 DT par defaut.

    Un reglage a 0 retombe sur le defaut : c'est le SEUIL qui desactive la proposition, pas le
    montant. Une recharge de 0 DT n'a pas de sens, et l'afficher au tableau des paiements a faire
    ferait une ligne a virer... zero.
    """
    try:
        v = frappe.db.get_single_value("Bank Retenue Sync Settings", "montant_recharge_carte")
        return flt(v, 3) if v else MONTANT_RECHARGE_DEFAUT
    except Exception:
        return MONTANT_RECHARGE_DEFAUT


def solde_disponible_banque() -> float:
    """Solde du compte Zitouna au dernier releve archive. None si aucun releve.

    C'est le solde LU AU PORTAIL (capture d'ecran), pas le solde comptable ERPNext : un virement
    se fait avec l'argent que la banque a, pas avec celui que les livres croient avoir. L'ecart
    entre les deux est precisement ce que le reste de l'app s'emploie a reduire — s'en servir ici
    reviendrait a promettre un virement sur une provision qui n'existe pas encore.
    """
    from bank_retenue_sync.bank import solde as S

    try:
        d = S.dernier_solde()
    except Exception:
        return None
    return flt(d.solde_banque, 3) if d and d.get("solde_banque") is not None else None


def recharge_a_faire(solde=None) -> dict:
    """Le virement de recharge a proposer. -> {statut, montant, solde_carte, seuil, cible, ...}

    Deux bornes, dans cet ordre :
      1. le DECLENCHEUR est le seuil (700 DT) : sous ce solde, la carte doit etre rechargee ;
      2. le MONTANT est une recharge type (1500 DT), PAS le manque pour repasser le seuil.
    Un appoint de 555 DT laisserait la carte replonger sous le seuil des le premier debit — la
    recharge servirait alors a peine une campagne, et le virement serait a refaire chaque semaine.

    Le montant est ensuite plafonne par le SOLDE DISPONIBLE en banque : proposer un virement que
    le compte ne peut pas honorer, ce n'est pas une consigne, c'est un rejet a venir. Quand le
    solde bancaire est inconnu (aucune capture archivee), on propose la recharge pleine en le
    disant — c'est a l'utilisateur d'arbitrer, pas au tableau de se taire.
    """
    seuil = seuil_recharge()
    if not seuil:
        return {"statut": "desactive", "montant": 0.0, "seuil": 0.0}
    if solde is None:
        solde = solde_carte().get("solde")
    solde = flt(solde, 3)
    base = {"solde_carte": solde, "seuil": seuil, "cible": montant_recharge()}
    if solde >= seuil:
        return {"statut": "au dessus du seuil", "montant": 0.0, "plafonne": False, **base}

    dispo = solde_disponible_banque()
    if dispo is None:
        return {"statut": "a virer", "montant": base["cible"], "disponible": None,
                "plafonne": False, **base}
    montant = flt(min(base["cible"], max(dispo, 0.0)), 3)
    return {
        "statut": "a virer" if montant > 0 else "solde bancaire insuffisant",
        "montant": montant, "disponible": dispo, "plafonne": montant < base["cible"], **base,
    }


def _destinataires() -> list:
    """Qui recoit l'alerte : les gestionnaires comptables actifs.

    Le role plutot qu'une liste d'adresses en dur — un depart ou une arrivee ne doit pas se
    traduire par une alerte qui tombe dans le vide.
    """
    users = frappe.db.sql_list(
        """select distinct r.parent from `tabHas Role` r
           join `tabUser` u on u.name = r.parent
           where r.role in ('Accounts Manager', 'System Manager')
             and u.enabled = 1 and u.name not in ('Administrator', 'Guest')""")
    return users or ["Administrator"]


def alerte_recharge(solde: float = None, seuil: float = None, force: bool = False) -> dict:
    """Pose une alerte de recharge si le solde REEL passe sous le seuil.

    UNE SEULE ALERTE PAR JOUR, et c'est essentiel : la tache tourne quotidiennement et le solde
    reste bas jusqu'a la recharge. Un rappel repete a chaque passage cesserait d'etre lu — c'est
    le mecanisme meme de l'alerte qu'on detruirait.

    Le solde compare est celui de la CARTE (lu chez le service), pas le solde comptable : ce
    dernier peut etre fausse par un paiement pas encore saisi, et c'est justement quand la saisie
    est en retard que l'alerte compte le plus.
    """
    seuil = seuil_recharge() if seuil is None else flt(seuil, 3)
    if not seuil:
        return {"statut": "desactive"}
    if solde is None:
        solde = solde_carte().get("solde")
    solde = flt(solde, 3)
    if solde >= seuil:
        return {"statut": "au dessus du seuil", "solde": solde, "seuil": seuil}

    if not force and _deja_alerte_aujourdhui():
        return {"statut": "deja alerte aujourd'hui", "solde": solde, "seuil": seuil}

    recharge = recharge_a_faire(solde=solde)
    message = ("Le solde de la carte technologique est de <b>%s DT</b>, sous le seuil de %s DT.<br>"
               "Virement de recharge a faire depuis le compte Zitouna : <b>%s DT</b>.<br>"
               "Sans lui, les campagnes seront refusees — le releve de carte porte deja des "
               "« Solde insuffisant »." % (solde, seuil, recharge["montant"]))
    if recharge.get("plafonne"):
        # Dire pourquoi le montant n'est pas celui attendu, sinon l'alerte parait fausse.
        message += ("<br>Montant <b>plafonne par le solde disponible en banque</b> (%s DT) : la "
                    "recharge type est de %s DT." % (recharge.get("disponible"),
                                                     recharge.get("cible")))
    # Ce qui reste a payer par ailleurs conditionne la recharge : recharger la carte alors que
    # d'autres virements attendent, c'est arbitrer une tresorerie, pas executer une consigne.
    autres = montant_a_payer()
    if autres:
        message += ("<br><br>Pour memoire, <b>%s DT</b> de virements sont deja en attente "
                    "(tableau « Paiements a faire »)." % autres)
    sujet = "%s — solde %s DT" % (SUJET_ALERTE, solde)
    destinataires = _destinataires()
    for user in destinataires:
        _poser_notification(user, sujet, message)
    return {"statut": "alerte posee", "solde": solde, "seuil": seuil,
            "destinataires": destinataires}


# Les deux effets de bord sont isoles dans leurs propres fonctions : les tests les remplacent
# sans toucher a `frappe.get_doc` ni a `frappe.db.exists`. Patcher ces deux-la globalement avait
# casse 73 tests d'autres modules — le runner de Frappe les appelle lui aussi entre les tests.
def _deja_alerte_aujourdhui() -> bool:
    return bool(frappe.db.exists("Notification Log", {
        "subject": ["like", "%" + SUJET_ALERTE + "%"],
        "creation": [">=", frappe.utils.nowdate()]}))


def _poser_notification(user: str, sujet: str, message: str):
    frappe.get_doc({
        "doctype": "Notification Log", "for_user": user, "type": "Alert",
        "subject": sujet, "email_content": message,
    }).insert(ignore_permissions=True)


def refresh_cartes(timeout: int = 600) -> dict:
    """Declenche un nouvel export de carte chez le service, puis attend qu'il aboutisse.

    Non fatal par contrat : si le service est injoignable, l'appelant retombe sur le dernier
    export disponible. Un releve de carte d'hier vaut mieux qu'une tache qui echoue.
    """
    from bank_retenue_sync.bank import movements

    job = movements.start_job("/jobs/banque/cartes/export")
    return movements.wait_job(job, timeout=timeout)


def process_cartes(insert: bool = True, depuis=_AUTO, lignes: list = None) -> list:
    """Cree une ecriture par paiement approuve non encore comptabilise. -> [{ref, status, je}]"""
    out = []
    for l in a_comptabiliser(lignes=lignes, depuis=depuis):
        reference = reference_ecriture(l)
        try:
            je = build_journal_entry(l, insert=insert)
            out.append({"flux": "carte_techno", "ref": reference, "status": "created",
                        "je": je.name if insert and je else "(dry-run)",
                        "montant": l["montant"], "date": str(l["date"])})
        except Exception as e:
            out.append({"flux": "carte_techno", "ref": reference, "status": "error",
                        "error": str(e)[:160]})
    return out
