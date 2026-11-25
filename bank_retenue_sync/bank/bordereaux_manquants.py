"""Bordereaux de remise MANQUANTS : la relance de ceux qui tiennent la caisse (demande du 25/11/2026).

POURQUOI. La BIAT ne dit pas quels cheques composent une remise : son releve porte seulement « remise
n° 7473936 : 3 000 DT ». Le seul papier qui le dit est le bordereau, que la personne qui depose doit prendre
en photo (Caisse → Bordereaux de remise). Sans lui, quand plusieurs cheques ont le meme montant, rien ne
peut etre encaisse tout seul (25/11/2026 : trois cheques de 3 000 DT du meme client, remise « orpheline »).
Le 25/11/2026, sur cinq remises BIAT, deux bordereaux seulement avaient ete photographies. Sa demande :
« l'alerte de bordereaux manquants pour Salma et Nejib » — ceux qui ont le role `ROLE`.

CE QUI MANQUE, d'apres ce que la banque a deja dit :
  - une REMISE DE CHEQUES creditee (regle `enc_cheque`) restee orpheline (aucun paiement ne porte
    « BR:<n°> ») et dont le bordereau n'est pas collecte : la photo permet de l'encaisser tout de suite ;
  - une REMISE DE TRAITES dont la BIAT a preleve la commission (regle `com_effet`) : la remise est faite, son
    bordereau doit l'etre aussi (le montant des traites n'arrive qu'a l'echeance, et c'est lui qui le dira) ;
  - un TROU dans les numeros : les bordereaux BIAT se suivent (7473932, 7473933, 7473934, puis 7473936) ; un
    numero saute (7473935) est une remise faite mais pas encore creditee, ou un bordereau annule. Au-dela de
    `TROU_MAX` numeros d'ecart, ce n'est plus un trou mais un autre carnet (7473685 puis 7473932).

Rien de tout cela ne s'invente : une personne peut ECARTER une ligne (bordereau annule, perdu…), avec son
motif ; elle ne revient plus. Zitouna n'est pas concernee : elle publie le detail de ses remises.
"""
from __future__ import annotations

import datetime
import json

BANQUE = "BIAT"
ROLE = "Relance bordereaux"
#: Les mouvements regardes : le compte BIAT est ouvert depuis le 02/11/2026.
DEPUIS = "2026-11-01"
#: Ecart maximal entre deux numeros connus pour parler d'un trou (au-dela : un autre carnet).
TROU_MAX = 3
#: Une commission de remise d'effets est couverte par un bordereau de traites collecte dans ces jours-la, si
#: son numero n'est pas celui de la commission (le n° du libelle n'est peut-etre pas celui imprime).
FENETRE_TRAITES = 10
#: Lignes ecartees a la main : {cle: {motif, note, par, le}}, valeur globale en base.
CLE_ECARTES = "brs_bordereaux_ecartes"
MOTIFS = ("Annulé ou abîmé (pas de remise)", "Bordereau perdu", "Autre")

GENRE_REMISE, GENRE_TRAITES, GENRE_TROU = "remise_cheques", "remise_traites", "trou"


# ---------------------------------------------------------------------------
# Pur
# ---------------------------------------------------------------------------

def cle(genre: str, numero: str, date=None) -> str:
    """La cle stable d'une ligne : « BIAT|<genre>|<n°> » (la date quand la commission n'a pas de numero)."""
    return f"{BANQUE}|{genre}|{numero or ('du ' + str(date or ''))}"


def _chiffres(numero) -> str:
    return "".join(c for c in str(numero or "") if c.isdigit())


def _jour(date) -> str:
    """« 25/11 »."""
    texte = str(date or "")[:10]
    return f"{texte[8:10]}/{texte[5:7]}" if len(texte) == 10 else texte


def _dt(montant) -> str:
    """« 3 000,000 DT »."""
    entier, _, millimes = f"{float(montant or 0):,.3f}".replace(",", " ").partition(".")
    return f"{entier},{millimes} DT"


def _date(valeur):
    if isinstance(valeur, datetime.date):
        return valeur
    try:
        return datetime.date.fromisoformat(str(valeur)[:10])
    except ValueError:
        return None


def trous(numeros, trou_max: int = TROU_MAX) -> list:
    """Les numeros sautes entre deux numeros connus voisins (au plus `trou_max` d'affilee). Pur.
    -> [(numero manquant, numero connu juste apres)], dans l'ordre."""
    connus = {}
    for n in numeros or ():
        chiffres = _chiffres(n)
        if chiffres:
            connus.setdefault(int(chiffres), chiffres)
    valeurs = sorted(connus)
    out = []
    for a, b in zip(valeurs, valeurs[1:]):
        if 1 < b - a <= trou_max + 1:
            largeur = len(connus[a])
            out.extend((str(n).zfill(largeur), connus[b]) for n in range(a + 1, b))
    return out


def manquants(remises: list, commissions_effets: list, bordereaux: list, ecartes: dict) -> list:
    """Les bordereaux a photographier. Pur.

    `remises` : [{numero, date, montant, statut}] — remises de cheques creditees par la BIAT (toutes, meme
      encaissees : leurs numeros servent a reperer les trous) ;
    `commissions_effets` : [{numero, date, montant}] — commissions de remise d'effets ;
    `bordereaux` : [{numero, nature, date_remise}] — bordereaux BIAT deja collectes ;
    `ecartes` : {cle: …} — lignes ecartees a la main.
    -> [{cle, genre, numero, date, montant, titre, detail}], de la plus ancienne a la plus recente."""
    ecartes = ecartes or {}
    collectes = {_chiffres(b.get("numero")) for b in bordereaux or ()} - {""}
    out = []

    for r in remises or ():
        numero = _chiffres(r.get("numero"))
        if not numero or numero in collectes or r.get("statut") != "Orphelin":
            continue
        out.append({"genre": GENRE_REMISE, "numero": numero, "date": str(r.get("date") or "")[:10],
                    "montant": float(r.get("montant") or 0),
                    "titre": f"Remise de chèques BIAT n° {numero}",
                    "detail": f"{_dt(r.get('montant'))} encaissés par la BIAT le {_jour(r.get('date'))}, "
                              "sans bordereau : on ne sait pas quels chèques."})

    # Les bordereaux de traites collectes sans numero commun avec une commission couvrent la commission la plus
    # proche dans le temps (le n° du libelle n'est peut-etre pas celui imprime sur le papier).
    traites_libres = [b for b in bordereaux or () if b.get("nature") == "Traites"
                      and _chiffres(b.get("numero")) not in {_chiffres(c.get("numero")) for c in commissions_effets or ()}]
    for c in sorted(commissions_effets or (), key=lambda x: str(x.get("date") or "")):
        numero = _chiffres(c.get("numero"))
        if numero and numero in collectes:
            continue
        jour = _date(c.get("date"))
        proche = next((b for b in traites_libres if jour and _date(b.get("date_remise"))
                       and -1 <= (jour - _date(b.get("date_remise"))).days <= FENETRE_TRAITES), None)
        if proche:
            traites_libres.remove(proche)
            continue
        out.append({"genre": GENRE_TRAITES, "numero": numero, "date": str(c.get("date") or "")[:10],
                    "montant": None,
                    "titre": "Remise de traites BIAT" + (f" n° {numero}" if numero else ""),
                    "detail": f"la BIAT a pris sa commission le {_jour(c.get('date'))} : la remise est faite, "
                              "son bordereau n’est pas enregistré."})

    # Les trous : la serie des bordereaux de cheques (numeros credites + collectes + ecartes comme trous).
    serie = [r.get("numero") for r in remises or ()] \
        + [b.get("numero") for b in bordereaux or () if b.get("nature") == "Chèques"]
    dates = {_chiffres(r.get("numero")): str(r.get("date") or "")[:10] for r in remises or ()}
    for numero, suivant in trous(serie):
        if numero in collectes:
            continue                      # collecte sous une autre nature (lu « Traites ») : il n'est pas perdu
        out.append({"genre": GENRE_TROU, "numero": numero, "date": dates.get(suivant, ""), "montant": None,
                    "titre": f"Bordereau BIAT n° {numero}",
                    "detail": f"ni photo, ni encaissement de la BIAT, alors que le n° {suivant} est connu. "
                              "Remise faite : prenez-le en photo ; bordereau annulé ou abîmé : dites-le."})

    for ligne in out:
        ligne["cle"] = cle(ligne["genre"], ligne["numero"], ligne["date"])
    return sorted((ligne for ligne in out if ligne["cle"] not in ecartes),
                  key=lambda x: (x["date"] or "9999", x["numero"]))


def attendu(ligne: dict) -> dict:
    """Ce que la photo devrait montrer, pour completer une lecture incomplete de l'IA. Pur."""
    return {"banque": BANQUE, "numero": ligne.get("numero") or None,
            "nature": "Traites" if ligne.get("genre") == GENRE_TRAITES else "Chèques"}


# ---------------------------------------------------------------------------
# Avec le site
# ---------------------------------------------------------------------------

def ecartes() -> dict:
    import frappe

    try:
        # Lu dans la table, pas par `get_global` (sa copie en cache survit a une transaction annulee).
        brut = frappe.db.sql("select defvalue from `tabDefaultValue` where parent = '__global' and defkey = %s limit 1",
                             CLE_ECARTES)
        return json.loads((brut[0][0] if brut else None) or "{}")
    except Exception:
        return {}


def ecarter(cle_ligne: str, motif: str, note: str = None):
    import frappe

    if motif not in MOTIFS:
        frappe.throw("Motif inconnu.")
    if motif == "Autre" and not (note or "").strip():
        frappe.throw("Dites en quelques mots pourquoi il n’y a pas de bordereau.")
    tout = ecartes()
    tout[cle_ligne] = {"motif": motif, "note": (note or "").strip() or None, "par": frappe.session.user,
                       "le": frappe.utils.now()}
    frappe.db.set_global(CLE_ECARTES, json.dumps(tout, ensure_ascii=False))


def liste() -> list:
    """Les bordereaux BIAT a photographier, d'apres le registre et les bordereaux collectes."""
    import frappe

    from bank_retenue_sync.bank import biat, registry

    lignes = frappe.get_all(registry.DOCTYPE, filters={"banque": BANQUE, "date": [">=", DEPUIS]}, limit_page_length=0,
                            fields=["date", "operation", "sens", "debit", "credit", "statut", "ignore_manuel"])
    remises, commissions = [], []
    for ligne in lignes:
        vu = biat.detecter(ligne.operation, ligne.sens)
        if vu["regle"] == "enc_cheque" and vu["numero"]:
            # Une remise ecartee a la main dans l'identification bancaire reste un numero connu, sans relance.
            remises.append({"numero": vu["numero"], "date": ligne.date, "montant": ligne.credit,
                            "statut": None if ligne.ignore_manuel else ligne.statut})
        elif vu["regle"] == "com_effet" and not ligne.ignore_manuel:
            commissions.append({"numero": vu["numero"], "date": ligne.date, "montant": ligne.debit})
    bordereaux = frappe.get_all("BRS Bordereau Remise", filters={"banque": BANQUE}, limit_page_length=0,
                                fields=["numero", "nature", "date_remise"])
    out = manquants(remises, commissions, bordereaux, ecartes())
    for ligne in out:
        ligne["attendu"] = attendu(ligne)
    return out
