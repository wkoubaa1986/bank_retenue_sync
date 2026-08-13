"""Ecarts residuels sur des pieces DEJA rapprochees — le troisieme terme du cumul mensuel.

LE PROBLEME
-----------
Un mouvement dont la piece est trouvee mais dont le montant differe de quelques dinars restait
« a verifier » indefiniment. Il n'y a pourtant rien a rapprocher : le lien est etabli, c'est le
MONTANT qui cloche. Cas reels sur le registre :
  - le leasing FIAT `LD2227700127` : la banque preleve 1 197,957 et l'ecriture porte 1 196,957,
    tous les mois depuis mai — le droit de timbre de 1 DT est omis a chaque echeance ;
  - le virement JEGHAM du 13/07 : 4 548,082 preleves, 4 549,082 comptabilises ;
  - la recharge Total du 20/07 : 703,500 prelevees, 703,000 comptabilisees.
A quatre echeances de leasing, cela faisait 20 lignes bloquees pour 4 DT.

LA SOLUTION EST CELLE QUE L'UTILISATEUR APPLIQUE DEJA AUX FRAIS
---------------------------------------------------------------
Ces residus rejoignent le CUMUL MENSUEL, exactement comme les commissions et les deltas de
paiement (cf. `fees.cumul_mensuel`, qui les additionne deja). Le mouvement devient alors
« identifie », son residu est porte une fois par mois, et rien ne disparait.

TROIS REGLES QUI EVITENT LES FAUX RESIDUS
------------------------------------------
1. **Un residu par GROUPE, jamais par ligne.** Une echeance de leasing est eclatee en 5 debits
   dont la somme egale le decaissement de l'ecriture : l'ecart porte sur le groupe. Le reporter
   sur chaque ligne le multiplierait par 5.
2. **Les CREDITS sont exclus.** Leurs deltas sont deja calcules par `pertes.ecarts_du_releve` et
   entrent dans le meme cumul — les compter ici les doublerait.
3. **Au-dela de la tolerance, ce n'est plus un residu mais une erreur** : la ligne reste « a
   verifier », et aucun cumul ne vient la masquer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from frappe.utils import flt, getdate

from bank_retenue_sync.bank import ecarts as E


@dataclass
class Residu:
    date: object
    cle: str                      # cle du mouvement, ou cle du groupe d'echeance
    reference: str
    libelle: str
    banque: float                 # ce que la banque a preleve / verse
    document: float               # ce que la piece porte
    ecart: float                  # banque - document (positif = la banque a pris plus)
    document_name: str
    groupe: str = None
    sens: str = "Debit"
    # Montant a porter au CREDIT du compte bancaire pour recaler ERPNext sur la banque.
    # Le signe s'inverse selon le sens, et c'est le seul point delicat du module :
    #   debit  — la banque a preleve `banque`, la piece n'a credite que `document` : le compte
    #            ERPNext est trop haut de (banque − document), il faut le crediter d'autant ;
    #   credit — la banque a verse `banque`, la piece a debite `document` : le compte est trop
    #            haut de (document − banque).
    effet: float = 0.0


def _jour_du_groupe(jour: str):
    """La cle d'echeance porte le jour au format `JJ-MM-AAAA` (cf. `classify.cle_echeance`)."""
    try:
        return datetime.strptime(str(jour), "%d-%m-%Y").date()
    except ValueError:
        return getdate(jour)


def _tolere(ecart: float, montant: float) -> bool:
    """Un residu se comptabilise ; une erreur se corrige. La tolerance separe les deux."""
    return 0.005 <= abs(flt(ecart, 3)) <= E.tolerance(montant)


def residus_du_releve(movements: list, context=None, rules=None,
                      classifications: list = None) -> list:
    """Ecarts residuels des DEBITS rapproches. -> [Residu]

    `classifications` / `context` sont injectables : les tests tournent sans base, et un appelant
    qui vient de classer ne reclasse pas.
    """
    from bank_retenue_sync.bank import classify as C

    if context is None or classifications is None:
        context = context if context is not None else C.build_context(movements)
        classifications = C.classify(movements, context, rules)

    out = []
    # 1. les groupes d'echeance : UN residu par groupe (reference, jour), pas par ligne.
    for cle, ap in (context.echeances or {}).items():
        if not ap.get("voucher") or not _tolere(ap.get("ecart"), ap.get("total")):
            continue
        ecart = flt(ap.get("ecart"), 3)
        out.append(Residu(
            date=_jour_du_groupe(cle[1]),
            cle="echeance-%s-%s" % cle, reference=cle[0], libelle="echeance %s" % cle[0],
            banque=flt(ap.get("total"), 3), document=flt(ap.get("montant"), 3),
            ecart=ecart, document_name=ap.get("voucher"), groupe="echeance-%s-%s" % cle,
            sens="Debit", effet=ecart))

    # 2. les lignes hors groupe, DANS LES DEUX SENS.
    # ⚠️ Les credits ont longtemps ete exclus ici, au motif que `pertes.ecarts_du_releve` les
    # traitait. C'EST FAUX : `pertes` ne couvre que le flux dettes clients, et manquait 11 des
    # 14 ecarts de credit de juillet (Aramex −2,380 recurrent, remises de cheques, effets),
    # soit 8,174 DT jamais comptabilises. On les reprend donc ici, en ecartant ceux que
    # `pertes` compte deja — les additionner les doublerait.
    deja = _references_de_pertes(movements)
    for c in classifications or []:
        montant = flt(c.debit, 3) or flt(c.credit, 3)
        # ⚠️ La contrepartie se prouve par `document_type`, PAS par `document_name` : un
        # encaissement rapproche d'un `Encaissement Paiement` n'a pas de nom de document (le
        # flux indexe une cle bancaire, pas une piece unique), et son ecart est mesure contre
        # `pending.montants_par_cle_bancaire`. Exiger le nom ecartait TOUS les credits — donc
        # exactement les pertes de non paiement que ce module doit reprendre.
        if c.groupe or not (c.document_name or c.document_type) or not montant:
            continue
        if not _tolere(c.ecart, montant):
            continue
        if (c.reference or "").strip().upper() in deja:
            continue
        sens = "Debit" if c.debit else "Credit"
        ecart = flt(c.ecart, 3)
        out.append(Residu(
            date=c.date, cle=c.cle, reference=c.reference, libelle=c.operation,
            banque=montant, document=flt(c.montant_document, 3), ecart=ecart,
            document_name=c.document_name, sens=sens,
            effet=ecart if sens == "Debit" else round(-ecart, 3)))
    return out


def _references_de_pertes(movements: list) -> set:
    """References dont l'ecart est deja porte par `pertes.ecarts_du_releve`.

    Le module de pertes tourne sur les MEMES mouvements et alimente le MEME cumul mensuel :
    sans ce garde-fou, un delta de paiement serait compte deux fois dans l'ecriture.
    """
    from bank_retenue_sync.expenses import pertes

    try:
        return {str(getattr(e, "reference", "") or "").strip().upper()
                for e in (pertes.ecarts_du_releve(movements) or [])}
    except Exception:
        # Sans base (tests) ou si le flux echoue, mieux vaut ne rien exclure que tout perdre :
        # le doublon eventuel se verrait, une omission silencieuse non.
        return set()


def cumul_mensuel(residus: list, periode: str) -> float:
    """Somme algebrique des EFFETS du mois, a porter au credit du compte bancaire.

    Le signe compte : les residus se compensent (juillet porte +0,500 de recharge Total et
    −1,000 de virement JEGHAM). Les additionner en valeur absolue creerait une charge fictive.
    On somme `effet` et non `ecart` : sur un credit, les deux sont opposes.
    """
    from bank_retenue_sync.expenses.fees import periode_de

    return flt(sum(flt(r.effet, 3) for r in (residus or [])
                   if r.date and periode_de(r.date) == periode), 3)
