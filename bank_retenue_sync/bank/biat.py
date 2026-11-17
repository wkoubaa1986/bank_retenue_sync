"""Mouvements de la BIAT : import au registre et RECONNAISSANCE des lignes.

Demande du 16/11/2026 : « faire pareil pour la BIAT », en commencant par les mouvements et les
cheques ; puis « le plus important maintenant : les transactions peuvent etre detectees ».

CE QUE CE MODULE FAIT — ET CE QU'IL NE FAIT PAS ENCORE
------------------------------------------------------
Il range les mouvements BIAT au registre (`BRS Bank Movement`, banque = « BIAT ») et dit de chaque
ligne ce qu'elle EST : remise de cheques (avec son numero de bordereau), commission de cette remise,
versement d'especes. Quand une piece d'ERPNext la porte deja sans ambiguite, il la designe.

Il ne cree AUCUNE ecriture et ne lance aucun encaissement. L'« Encaissement Paiement » de
customization_app deplace les cheques vers le compte Zitouna, ecrit en dur dans son server script :
tant qu'il ne sait pas viser la BIAT, une remise BIAT reste « a rapprocher » ici.

LES LIBELLES (compte ouvert le 02/11/2026 — trois natures de lignes vues au 16/11/2026)
------------------------------------------------------------------------------------
    Versement TND                                                    credit   reference TT…
    Credit Divers Chq Telec A Enc Cre Global/borde : 7473933         credit   reference FT…
    Debit Divers Commission CHQ A Enc/Bordereau : 7473932            debit    reference FT…

Le numero de BORDEREAU (7 chiffres) est la cle d'une remise : il relie le credit de la remise a sa
commission, et c'est lui qu'on cherche sur les Payment Entry (`BR:<numero>`, la convention deja
suivie pour Zitouna). Tout autre libelle sort « a verifier : libelle BIAT inconnu » — jamais un
mouvement passe sous silence.

D'OU VIENNENT LES MOUVEMENTS
----------------------------
De tej-bank-service (`POST /jobs/biat/mouvements/export`, `GET /biat/mouvements/export/latest`),
dont l'export reprend les colonnes de l'export Zitouna : le lecteur de `bank/movements.py` sert tel
quel. `importer_fichier` prend le meme fichier quand le service n'est pas joignable.
"""
from __future__ import annotations

import re

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

from bank_retenue_sync.bank import registry

BANQUE = "BIAT"
#: Fenetre de recherche d'une ecriture autour de la date d'un versement (jours).
FENETRE_VERSEMENT = 5
#: Tolerance sur le total d'une remise (frais preleves a la source, arrondis).
TOLERANCE_REMISE = 1.0

#: Reference de l'ecriture mensuelle des frais BIAT : « BIAT frais bancaires MM-YYYY ». Elle ne commence
#: PAS par « Frais bancaire » : le classement Zitouna indexe les ecritures dont la reference commence
#: ainsi (`fees.PREFIXE_MENSUEL`), et les frais des deux banques ne doivent jamais se croiser.
PREFIXE_FRAIS = "BIAT frais bancaires "
COMPTE_FRAIS = "Frais bancaire - A&S"

STATUT_IDENTIFIE = "Identifie"
STATUT_BROUILLON = "Identifie non comptabilise"
STATUT_ORPHELIN = "Orphelin"
STATUT_A_VERIFIER = "A verifier"

_BORDEREAU = r"BORDE\w*\s*:?\s*(\d{4,12})"
#: (cle de regle, sens, motif sur le libelle en majuscules sans accents, categorie, sous-categorie, libelle)
#: Les cles et categories sont celles des regles Zitouna de meme nature : filtres et rapports les
#: retrouvent sans rien apprendre de neuf.
REGLES = (
    ("enc_cheque", "Credit", re.compile(r"CHQ\s+TELEC\s+A\s+ENC.*?" + _BORDEREAU), "encaissement", "cheque",
     "Remise de chèques"),
    ("com_enc_cheque", "Debit", re.compile(r"COMMISSION\s+CHQ\s+A\s+ENC.*?" + _BORDEREAU), "frais_bancaires", "commission",
     "Commission de remise de chèques"),
    ("versement_especes", "Credit", re.compile(r"^VERSEMENT\b"), "caisse", "versement",
     "Versement d’espèces"),
    # TERMINAL DE PAIEMENT (cartes) — libelles vus le 17/11/2026 :
    #   « Reglement Affilie BIAT TPE : 0010010006 / Remise : 20261116-0811990019-1990010006 »   credit du brut
    #   « COM TVA REF REMISE AFF 0010010006 »                                                   commission, a part
    # La remise elle-meme se designe par les courriels de la banque (`bank/monetique.py`).
    ("remise_tpe", "Credit", re.compile(r"\bREGLEMENT\s+AFFILIE\b.*?\bTPE\b"), "encaissement", "carte",
     "Remise du terminal de paiement (cartes)"),
    ("com_tpe", "Debit", re.compile(r"\bCOM\w*\s+(?:TVA\s+)?REF\s+REMISE\s+AFF"), "frais_bancaires", "commission",
     "Commission du terminal de paiement (cartes)"),
    # Virement recu d'un client : meme mot entier que le flux Zitouna (`matching.is_virement_credit`), Aramex
    # exclu (son reversement a son propre circuit). Place APRES les regles precises : il ne prend que le reste.
    ("vir_recu", "Credit", re.compile(r"^(?!.*ARAMEX).*\bVIR(?:EMENT|T)?\b"), "encaissement", "virement",
     "Virement reçu"),
)


# ---------------------------------------------------------------------------
# Reconnaissance d'un libelle — pur
# ---------------------------------------------------------------------------

def _m(valeur) -> float:
    """Un montant au millime. Pas `frappe.utils.flt(v, 3)` : hors site (tests purs), il rend 0."""
    try:
        return round(float(valeur or 0), 3)
    except (TypeError, ValueError):
        return 0.0


def _norme(texte: str) -> str:
    import unicodedata

    sans = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode()
    return " ".join(sans.upper().split())


def detecter(operation: str, sens: str) -> dict:
    """Ce qu'est une ligne BIAT d'apres son libelle et son sens.
    -> {regle, categorie, sous_categorie, libelle, numero} ; `regle` vide si le libelle est inconnu."""
    texte = _norme(operation)
    for cle, sens_regle, motif, categorie, sous_categorie, libelle in REGLES:
        if sens != sens_regle:
            continue
        trouve = motif.search(texte)
        if trouve:
            return {"regle": cle, "categorie": categorie, "sous_categorie": sous_categorie, "libelle": libelle,
                    "numero": trouve.group(1) if trouve.groups() else ""}
    return {"regle": "", "categorie": "", "sous_categorie": "", "libelle": "", "numero": ""}


def classer_ligne(ligne: dict, contexte: dict) -> dict:
    """Le classement d'UNE ligne du registre. `contexte` : ce qu'ERPNext porte deja, charge une fois
    (`charger_contexte`) — {"paiements_par_bordereau": {numero: [{name, montant, docstatus, compte}]},
    "ecritures_par_reference": {reference: [{name, docstatus}]}, "ecritures_versement": [{name,
    date, montant, docstatus}], "remises": {numero: montant du credit}}. Pur."""
    sens = ligne.get("sens") or ("Debit" if _m(ligne.get("debit")) else "Credit")
    montant = _m(ligne.get("debit") or ligne.get("credit") or ligne.get("montant"))
    vu = detecter(ligne.get("operation"), sens)
    out = {"cle": ligne.get("cle") or ligne.get("name"), "categorie": vu["categorie"], "regle": vu["regle"],
           "groupe": None, "statut": STATUT_A_VERIFIER, "raison": "", "document_type": None,
           "document_name": None, "montant_document": 0.0, "ecart": 0.0}
    tpe = (contexte.get("tpe") or {}).get(out["cle"])
    if tpe:
        # REMISE DU TERMINAL DE PAIEMENT (cartes) : reconnue par le MONTANT de l'avis de credit que la banque
        # envoie par courriel, jamais par la date (« le releve ne veut pas dire date de valeur », 17/11/2026).
        # Reconnue, pas comptabilisee : le passage en comptabilite n'est pas encore decide.
        from bank_retenue_sync.bank import monetique

        out.update(categorie="encaissement", regle="remise_tpe", groupe="tpe:" + tpe["remises"][0],
                   statut=STATUT_BROUILLON, document_type=monetique.DOCTYPE,
                   document_name=tpe["remises"][0] if len(tpe["remises"]) == 1 else None,
                   montant_document=montant, raison=monetique.raison_credit(tpe))
        ecritures = tpe.get("ecritures") or []
        if ecritures and not tpe.get("refus"):
            # L'ecriture de la remise est passee (banque debitee, compte d'attente des cartes solde).
            brouillon = any(int(e.get("docstatus") or 0) == 0 for e in ecritures)
            out.update(statut=STATUT_BROUILLON if brouillon else STATUT_IDENTIFIE, document_type="Journal Entry",
                       document_name=ecritures[0]["name"] if len(ecritures) == 1 else None)
        return out
    if vu["regle"] == "remise_tpe":
        out.update(statut=STATUT_ORPHELIN, raison="remise du terminal de paiement (cartes) : l’avis de la banque n’est "
                   "pas encore arrivé par courriel, ou son montant diffère — à rapprocher au prochain passage")
        return out
    if vu["regle"] == "com_tpe":
        # COMMISSION DU TERMINAL : un frais bancaire comme un autre (ecriture mensuelle des frais BIAT), avec
        # son TAUX par rapport aux transactions de la remise (demande du 17/11/2026).
        from bank_retenue_sync.bank import monetique

        p = (contexte.get("prelevements_tpe") or {}).get(out["cle"]) or {}
        if p.get("name"):
            out["groupe"] = "tpe:" + p["name"]
        raison = p.get("raison") or monetique.raison_prelevement(montant, None)
        du_mois = contexte.get("frais_mensuels", {}).get(periode_de(ligne.get("date")))
        if du_mois:
            out.update(statut=STATUT_BROUILLON if int(du_mois.get("docstatus") or 0) == 0 else STATUT_IDENTIFIE,
                       document_type="Journal Entry", document_name=du_mois["name"], montant_document=montant,
                       raison=f"{raison} — dans l’écriture mensuelle des frais BIAT ({cle_frais(periode_de(ligne.get('date')))})")
        else:
            out.update(statut=STATUT_ORPHELIN, raison=raison + " — frais bancaires à comptabiliser")
        return out
    if not vu["regle"]:
        out["raison"] = "libellé BIAT inconnu : à classer (aucune règle ne le reconnaît)"
        return out
    numero = vu["numero"]
    if numero:
        out["groupe"] = "bordereau:" + numero

    if vu["regle"] == "vir_recu":
        # MEME LOGIQUE QUE ZITOUNA : le virement solde les dettes du client (`encaisser_virements`), et se
        # reconnait ensuite au paiement qui cite sa reference bancaire.
        reference = (ligne.get("reference") or "").upper()
        paiements = contexte.get("paiements_par_reference", {}).get(reference, [])
        encaissement = contexte.get("encaissements_par_reference", {}).get(reference)
        if paiements:
            total = _m(sum(_m(p["montant"]) for p in paiements))
            brouillon = any(int(p.get("docstatus") or 0) == 0 for p in paiements)
            out.update(statut=STATUT_BROUILLON if brouillon else STATUT_IDENTIFIE,
                       document_type="Encaissement Paiement" if encaissement else "Payment Entry",
                       document_name=encaissement or (paiements[0]["name"] if len(paiements) == 1 else None),
                       montant_document=total, ecart=_m(montant - total))
            clients = sorted({p.get("client") for p in paiements if p.get("client")})
            detail = "virement reçu" + (f" de {', '.join(clients)}" if clients else "") \
                + f" : {len(paiements)} paiement(s) citent sa référence"
            if encaissement:
                detail += f" (encaissement {encaissement})"
            if abs(out["ecart"]) > 0.0005:
                detail += f" — écart de {out['ecart']:.3f} avec le crédit de la banque"
            out["raison"] = detail
            return out
        brouillon = contexte.get("brouillons_par_reference", {}).get(reference)
        if brouillon:
            out.update(statut=STATUT_BROUILLON, document_type="Encaissement Paiement", document_name=brouillon,
                       raison=f"virement reçu : l’encaissement {brouillon} est en brouillon (à soumettre)")
            return out
        refus = contexte.get("refus_virements", {}).get(reference)
        out.update(statut=STATUT_A_VERIFIER if refus else STATUT_ORPHELIN,
                   raison="virement reçu : " + (f"pas encaissé — {refus}" if refus
                                                else "pas encore rapproché des dettes d’un client"))
        return out

    if vu["regle"] == "enc_cheque":
        paiements = contexte.get("paiements_par_bordereau", {}).get(numero, [])
        if not paiements:
            # Le bordereau papier est le seul document qui dit quels cheques composent une remise BIAT : on dit
            # s'il est deja collecte (`BRS Bordereau Remise`) ou s'il reste a recueillir a la caisse.
            collecte = contexte.get("bordereaux", {}).get(numero)
            lecture = contexte.get("lectures", {}).get(numero)
            suite = (f"bordereau collecté ({collecte}), " + (f"lu par l’IA — {lecture}" if lecture else "chèques à rapprocher")) \
                if collecte else "bordereau à collecter (caisse → Bordereaux de remise)"
            # Un bordereau collecte et bon est encaisse tout seul (`encaisser_remises`) : s'il ne l'est pas,
            # c'est qu'une chose l'empeche, et c'est elle qu'il faut lire ici.
            refus = contexte.get("refus", {}).get(numero)
            out.update(statut=STATUT_A_VERIFIER if (collecte and refus) else STATUT_ORPHELIN,
                       raison=f"remise de chèques, bordereau {numero} : aucun paiement ne porte « BR:{numero} » — {suite}"
                       + (f" ; pas encaissé : {refus}" if (collecte and refus) else ""))
            return out
        total = _m(sum(_m(p["montant"]) for p in paiements))
        brouillon = any(int(p.get("docstatus") or 0) == 0 for p in paiements)
        # La piece a ouvrir depuis la ligne : l'encaissement du bordereau quand il existe (il liste les cheques
        # et porte les ecarts), sinon le paiement s'il est seul.
        encaissement = contexte.get("encaissements", {}).get(numero)
        out.update(statut=STATUT_BROUILLON if brouillon else STATUT_IDENTIFIE,
                   document_type="Encaissement Paiement" if encaissement else "Payment Entry",
                   document_name=encaissement or (paiements[0]["name"] if len(paiements) == 1 else None),
                   montant_document=total, ecart=_m(montant - total))
        detail = f"remise de chèques, bordereau {numero} : {len(paiements)} paiement(s) portent « BR:{numero} »"
        if encaissement:
            detail += f" (encaissement {encaissement})"
        if 0.0005 < abs(out["ecart"]) <= SEUIL_ECART_ABSORBE:
            du_mois = contexte.get("frais_mensuels", {}).get(periode_de(ligne.get("date")))
            detail += f" — petit écart de {out['ecart']:.3f} avec le crédit de la banque" + (
                f", porté par l’écriture mensuelle {cle_frais(periode_de(ligne.get('date')))}" if du_mois
                else ", à porter dans l’écriture mensuelle des frais")
        if abs(out["ecart"]) > SEUIL_ECART_ABSORBE:
            detail += f" pour {total:.3f} — écart de {out['ecart']:.3f} avec le crédit de la banque"
        out["raison"] = detail
        return out

    if vu["regle"] == "com_enc_cheque":
        ecritures = contexte.get("ecritures_par_reference", {}).get((ligne.get("reference") or "").upper(), [])
        du_mois = contexte.get("frais_mensuels", {}).get(periode_de(ligne.get("date")))
        if du_mois:
            # Les frais ne se comptabilisent jamais a l'unite : la commission rejoint l'ecriture de son mois.
            out.update(statut=STATUT_BROUILLON if int(du_mois.get("docstatus") or 0) == 0 else STATUT_IDENTIFIE,
                       document_type="Journal Entry", document_name=du_mois["name"], montant_document=montant,
                       raison=f"commission de la remise {numero} : dans l’écriture mensuelle des frais BIAT "
                              f"({cle_frais(periode_de(ligne.get('date')))})")
            return out
        if ecritures:
            brouillon = any(int(e.get("docstatus") or 0) == 0 for e in ecritures)
            out.update(statut=STATUT_BROUILLON if brouillon else STATUT_IDENTIFIE, document_type="Journal Entry",
                       document_name=ecritures[0]["name"], montant_document=montant,
                       raison=f"commission de la remise {numero} : écriture qui cite la référence bancaire")
        else:
            remise = contexte.get("remises", {}).get(numero)
            suite = f" (remise de {remise:.3f})" if remise else ""
            out.update(statut=STATUT_ORPHELIN, raison=f"commission de la remise de chèques, bordereau {numero}{suite} : "
                       "frais bancaires à comptabiliser")
        return out

    # versement d'especes : l'ecriture qui cite la reference bancaire, sinon celle du meme montant a quelques jours
    reference = (ligne.get("reference") or "").upper()
    ecritures = contexte.get("ecritures_par_reference", {}).get(reference, [])
    comment = "écriture qui cite la référence bancaire"
    if not ecritures:
        jour = getdate(ligne.get("date")) if ligne.get("date") else None
        ecritures = [e for e in contexte.get("ecritures_versement", [])
                     if abs(_m(e["montant"]) - montant) < 0.0005 and jour
                     and abs((getdate(e["date"]) - jour).days) <= FENETRE_VERSEMENT]
        comment = "écriture du même montant sur le compte BIAT"
    if len(ecritures) == 1:
        e = ecritures[0]
        out.update(statut=STATUT_BROUILLON if int(e.get("docstatus") or 0) == 0 else STATUT_IDENTIFIE,
                   document_type="Journal Entry", document_name=e["name"], montant_document=montant,
                   raison=f"versement d’espèces : {comment}")
    elif ecritures:
        out.update(statut=STATUT_A_VERIFIER, raison=f"versement d’espèces : {len(ecritures)} écritures possibles "
                   "(" + ", ".join(e["name"] for e in ecritures[:4]) + ") — à trancher")
    else:
        out.update(statut=STATUT_ORPHELIN, raison="versement d’espèces : aucune écriture caisse → BIAT du même montant")
    return out


# ---------------------------------------------------------------------------
# Frais bancaires : une ecriture par mois, separee de celle de Zitouna — pur
# ---------------------------------------------------------------------------
# Decision du 16/11/2026 : « pour le moment tout passe en frais bancaire pour la BIAT ; separe les frais
# de la BIAT de ceux de Zitouna ; le mecanisme est le meme ». Donc, comme `expenses/fees.py` : UNE
# ecriture par mois, refaite en entier depuis le registre a chaque frais nouveau (jamais incrementee :
# une ligne vue deux fois ne double rien), datee du dernier frais du mois. Pas de part TVA ni timbre :
# la BIAT ne les donne pas sur des lignes a part, et tout va en « Frais bancaire ».

def periode_de(jour) -> str:
    """« YYYY-MM » d'une date (ou de son texte ISO)."""
    return str(jour or "")[:7]


def cle_frais(periode: str) -> str:
    """Reference de l'ecriture du mois : « BIAT frais bancaires MM-YYYY »."""
    annee, mois = periode.split("-")
    return "%s%s-%s" % (PREFIXE_FRAIS, mois, annee)


#: En deca de ce seuil, l'ecart d'une ligne identifiee est repute etre des frais ou un arrondi : il rejoint
#: l'ecriture mensuelle. Au-dela ce n'est plus un frais mais une erreur ou un impaye : il reste affiche, a
#: traiter. Meme seuil que la page (`api.mouvements.SEUIL_ECART_DEFAUT`, « écarts de paiement au-delà de 5 »).
SEUIL_ECART_ABSORBE = 5.0


def ecart_a_absorber(ligne: dict) -> float:
    """Le petit ecart d'une ligne IDENTIFIEE, vu du compte bancaire : banque − comptabilite. Pur.
    Positif : la banque a plus que la comptabilite (un cheque credite 0,247 de plus que le paiement saisi) ;
    negatif : elle a moins (frais retenus sur un virement). 0 si la ligne n'est pas identifiee, est ecartee a
    la main, est elle-meme un frais, ou si l'ecart depasse `SEUIL_ECART_ABSORBE`."""
    if ligne.get("statut") != STATUT_IDENTIFIE or int(ligne.get("ignore_manuel") or 0) \
            or ligne.get("categorie") == "frais_bancaires":
        return 0.0
    ecart = _m(ligne.get("ecart"))
    if abs(ecart) < 0.0005 or abs(ecart) > SEUIL_ECART_ABSORBE:
        return 0.0
    sens = ligne.get("sens") or ("Debit" if _m(ligne.get("debit")) else "Credit")
    return ecart if sens != "Debit" else _m(-ecart)


def cumul_frais(lignes: list, periode: str) -> dict:
    """Le cumul mensuel de la BIAT : ses frais, ET les petits ecarts de ses encaissements — une seule
    ecriture, comme pour Zitouna (« il y a un process qui absorbe les ecarts de paiement dans les frais »,
    16/11/2026). Pur.

      frais   : lignes de debit de categorie « frais_bancaires », hors lignes ecartees a la main ;
      ecarts  : somme des `ecart_a_absorber` du mois (banque − comptabilite) ;
      total   : frais − ecarts = ce qu'il faut porter au CREDIT du compte BIAT pour que la comptabilite
                retombe sur la banque. Negatif quand les ecarts favorables depassent les frais du mois.
    -> {periode, cle, frais, ecarts, nb_ecarts, total, nb, jour (derniere ligne), references}"""
    retenues = [ligne for ligne in lignes
                if (ligne.get("categorie") == "frais_bancaires" and not ligne.get("ignore_manuel")
                    and periode_de(ligne.get("date")) == periode and _m(ligne.get("debit")) > 0)]
    portes = [(ligne, ecart_a_absorber(ligne)) for ligne in lignes if periode_de(ligne.get("date")) == periode]
    portes = [(ligne, e) for ligne, e in portes if e]
    frais = _m(sum(_m(ligne.get("debit")) for ligne in retenues))
    ecarts = _m(sum(e for _l, e in portes))
    return {"periode": periode, "cle": cle_frais(periode), "nb": len(retenues), "frais": frais,
            "ecarts": ecarts, "nb_ecarts": len(portes), "total": _m(frais - ecarts),
            "jour": max((str(ligne.get("date")) for ligne in retenues + [l for l, _e in portes]), default=None),
            "references": sorted({(ligne.get("reference") or "").strip() for ligne in retenues} - {""})}


# ---------------------------------------------------------------------------
# Avec le site
# ---------------------------------------------------------------------------

#: Ecart tolere PAR CHEQUE entre le credit de la banque et les paiements d'ERPNext — meme politique que les
#: encaissements Zitouna (`encaissement.matching.TOLERANCE_PAIEMENT`, 1 DT par paiement).
ECART_PAR_PIECE = 1.0
COMPTE_CHEQUES = "Chèques - A&S"
#: Pourquoi un virement recu n'a pas ete encaisse, garde d'un passage a l'autre ({reference: raison}) : la
#: raison vient de l'identification du client (alias, ressemblance, IA), qu'on ne rejoue pas a chaque affichage.
#: ⚠️ EN BASE (valeur globale), pas dans le cache : la soumission d'un encaissement vide le cache du site, et
#: la raison posee juste avant disparaissait avant le classement (constate a l'essai du 16/11/2026).
CLE_REFUS_VIREMENTS = "brs_biat_refus_virements"


def _refus_virements() -> dict:
    import json

    try:
        # Lu dans la table, pas par `get_global` : sa copie en cache survit a une transaction annulee.
        brut = frappe.db.sql("select defvalue from `tabDefaultValue` where parent = '__global' and defkey = %s limit 1",
                             CLE_REFUS_VIREMENTS)
        return {str(k).upper(): v for k, v in json.loads((brut[0][0] if brut else None) or "{}").items()}
    except Exception:
        return {}


def _garder_refus_virements(refus: dict):
    import json

    frappe.db.set_global(CLE_REFUS_VIREMENTS, json.dumps(refus or {}, ensure_ascii=False))


def decider_encaissement(ligne: dict, numero: str, bordereau: dict, paiements: dict) -> dict:
    """Peut-on encaisser cette remise de cheques a partir de son bordereau collecte ? Pur.

    LE BORDEREAU PAPIER TIENT LIEU DE PORTAIL. Zitouna publie le detail de chaque remise, que le robot
    telecharge ; la BIAT ne publie rien : le seul document qui dit quels cheques composent la remise est le
    bordereau depose a la caisse (`BRS Bordereau Remise`), lu par l'IA et rapproche des paiements. Le n° du
    libelle bancaire est le n° imprime sur le papier — c'est le lien.

    `ligne` : {date, credit, reference} du registre ; `bordereau` : None ou {name, nature, lecture_statut,
    a_signaler, pieces: [{numero, montant, paiement, client}]} ; `paiements` : {nom: {montant, compte,
    docstatus, en_encaissement}} des paiements cites par le bordereau.
    -> {faire, raison, lignes: [lignes de « Liste Cheque »], total, ecart, ecarts: [diagnostics « Toléré »]}

    Encaisse seulement ce qui est SUR : bordereau lu sans rien a signaler, chaque cheque encore en
    portefeuille, et un credit bancaire qui tombe sur le total des paiements a `ECART_PAR_PIECE` pres par
    cheque. Tout le reste est refuse AVEC SA RAISON, qui s'affiche sur la ligne de l'identification."""
    non = lambda raison: {"faire": False, "raison": raison, "lignes": [], "total": 0.0, "ecart": 0.0, "ecarts": []}  # noqa: E731
    if not bordereau:
        return non("bordereau à collecter (caisse → Bordereaux de remise)")
    if bordereau.get("nature") != "Chèques":
        return non(f"le bordereau collecté ({bordereau.get('name')}) n’est pas une remise de chèques")
    if bordereau.get("lecture_statut") != "Lue":
        return non("le bordereau n’est pas encore lu par l’IA" if bordereau.get("lecture_statut") != "Échec"
                   else "l’IA n’a pas pu lire le bordereau : relisez-le ou saisissez ses chèques sur sa fiche")
    signale = [l for l in (bordereau.get("a_signaler") or "").split("\n") if l.strip()]
    if signale:
        return non("à signaler sur le bordereau — " + signale[0] + (f" (+{len(signale) - 1})" if len(signale) > 1 else ""))
    pieces = bordereau.get("pieces") or []
    if not pieces:
        return non("aucun chèque sur le bordereau collecté")
    vus, lignes, total = set(), [], 0.0
    for p in pieces:
        nom = p.get("paiement")
        pe = (paiements or {}).get(nom) if nom else None
        if not pe:
            return non(f"chèque n° {p.get('numero') or '?'} : pas de paiement dans ERPNext")
        if nom in vus:
            return non(f"le paiement {nom} est cité deux fois par le bordereau")
        vus.add(nom)
        if int(pe.get("docstatus") or 0) != 1 or pe.get("compte") != COMPTE_CHEQUES:
            return non(f"chèque n° {p.get('numero') or '?'} : le paiement {nom} n’est plus dans le portefeuille "
                       f"(« {pe.get('compte') or 'annulé'} »)")
        if pe.get("en_encaissement"):
            return non(f"chèque n° {p.get('numero') or '?'} : le paiement {nom} est déjà sur un autre encaissement")
        montant = _m(pe.get("montant"))
        total = _m(total + montant)
        lignes.append({"ref_paiement": nom, "n_cheque": p.get("numero"), "valeur": montant, "emmeteur": p.get("client"),
                       "bon_remise": numero, "date": ligne.get("date"), "statut": "Versé"})
    credit = _m(ligne.get("credit") or ligne.get("montant"))
    ecart = _m(credit - total)
    if abs(ecart) > ECART_PAR_PIECE * len(lignes) + 0.0005:
        return non(f"le crédit de la banque ({credit:.3f}) et les chèques du bordereau dans ERPNext ({total:.3f}) "
                   f"diffèrent de {abs(ecart):.3f}")
    # Les petits ecarts, cheque par cheque : traces (« Toléré »), jamais bloquants. On ne les detaille que si la
    # lecture du bordereau est PROUVEE par le credit de la banque (somme des montants lus = credit) ; sinon un
    # montant mal lu passerait pour un ecart de paiement, et seul l'ecart global de la ligne fait foi.
    ecarts = []
    lus = [p.get("montant") for p in pieces]
    if all(v is not None for v in lus) and abs(_m(sum(_m(v) for v in lus)) - credit) <= 0.0005:
        for p, l in zip(pieces, lignes):
            delta = _m(l["valeur"] - _m(p["montant"]))
            if abs(delta) > 0.0005:
                ecarts.append({"type": "ecart", "flux": "cheque", "sous_type": "Toléré", "bloquant": 0,
                               "reason": "ecart <= tolerance", "reference": ligne.get("reference") or "",
                               "credit": credit, "bon": numero, "suivi": p.get("numero") or "",
                               "client": p.get("client"), "ref_paiement": l["ref_paiement"],
                               "montant_piece": l["valeur"], "montant_advice": _m(p["montant"]), "ecart": delta})
    return {"faire": True, "raison": "", "lignes": lignes, "total": total, "ecart": ecart, "ecarts": ecarts}


def resume_solde(lignes: list, erpnext: float, lu: dict = None, mois_comptabilises=()) -> dict:
    """Le tableau de bord de la BIAT, a partir des lignes du registre. Pur.

    `lignes` : [{date, sens, debit, credit, statut, ignore_manuel, ecart, operation, raison}] ; `erpnext` :
    solde du compte BIAT en comptabilite ; `lu` : le dernier solde lu sur le portail par le robot
    ({solde, date, lu_le}), s'il existe.

    LE SOLDE DE LA BANQUE. Lu sur le portail quand le robot tourne ; sinon c'est le CUMUL du registre — le
    compte a ete ouvert le 02/11/2026 et le registre porte tous ses mouvements depuis l'ouverture (verifie
    le 16/11/2026 : cumul 5 609,788 = solde lu par le robot). Quand les deux existent, leur difference
    (`hors_registre`) dit qu'il manque des mouvements au registre : cela ne se corrige pas par une ecriture.

    L'ECART (banque − ERPNext) se decompose : ce qui est au releve mais pas encore en comptabilite
    (`en_attente` : lignes ni identifiees ni ignorees), les petits ecarts des lignes identifiees
    (`petits_ecarts`), les lignes ecartees a la main (`ignores`), le hors-registre, et ce qui reste
    (`inexplique`, qui doit valoir zero).
    `ecart_projete` : l'ecart une fois comptabilise ce qui est en attente.

    `mois_comptabilises` : les mois (« YYYY-MM ») dont l'ecriture mensuelle des frais est SOUMISE. Elle porte
    deja les petits ecarts de ses encaissements (`ecart_a_absorber`) : ils sont rendus a part (`absorbes`) et
    ne pesent plus sur l'ecart — ils sont en comptabilite."""
    credits = _m(sum(_m(l.get("credit")) for l in lignes))
    debits = _m(sum(_m(l.get("debit")) for l in lignes))
    cumul = _m(credits - debits)
    dates = [str(l.get("date")) for l in lignes if l.get("date")]
    attente, petits, ignores, absorbes = [], [], [], []
    for l in lignes:
        sens = l.get("sens") or ("Debit" if _m(l.get("debit")) else "Credit")
        signe = -1 if sens == "Debit" else 1
        montant = _m(l.get("debit") if sens == "Debit" else l.get("credit"))
        if l.get("statut") == STATUT_IDENTIFIE:
            if abs(_m(l.get("ecart"))) > 0.0005:
                piece = {"date": str(l.get("date") or ""), "operation": l.get("operation"),
                         "montant": montant, "effet": _m(signe * _m(l.get("ecart")))}
                if ecart_a_absorber(l) and periode_de(l.get("date")) in set(mois_comptabilises or ()):
                    absorbes.append(piece)
                else:
                    petits.append(piece)
        elif int(l.get("ignore_manuel") or 0):
            ignores.append(_m(signe * montant))        # ecartee a la main : la banque a bouge, pas la comptabilite
        else:
            attente.append({"date": str(l.get("date") or ""), "operation": l.get("operation"), "sens": sens,
                            "montant": montant, "effet": _m(signe * montant), "statut": l.get("statut") or "",
                            "raison": l.get("raison") or ""})
    banque = _m(lu["solde"]) if lu and lu.get("solde") is not None else cumul
    ecart = _m(banque - _m(erpnext))
    net_attente = _m(sum(a["effet"] for a in attente))
    total_petits = _m(sum(p["effet"] for p in petits))
    hors_registre = _m(banque - cumul)
    net_ignores = _m(sum(ignores))
    return {"solde_banque": banque, "source": "portail" if (lu and lu.get("solde") is not None) else "registre",
            "lu": lu or None, "cumul_registre": cumul, "mouvements": len(lignes),
            "premiere_operation": min(dates) if dates else None, "derniere_operation": max(dates) if dates else None,
            "erpnext": _m(erpnext), "ecart": ecart, "ecart_projete": _m(ecart - net_attente),
            "en_attente": {"nb": len(attente), "net": net_attente,
                           "credits": _m(sum(a["montant"] for a in attente if a["sens"] == "Credit")),
                           "debits": _m(sum(a["montant"] for a in attente if a["sens"] == "Debit")), "lignes": attente},
            "petits_ecarts": {"nb": len(petits), "total": total_petits, "lignes": petits},
            "absorbes": {"nb": len(absorbes), "total": _m(sum(a["effet"] for a in absorbes)), "lignes": absorbes},
            "ignores": {"nb": len(ignores), "net": net_ignores}, "hors_registre": hors_registre,
            "inexplique": _m(ecart - net_attente - total_petits - hors_registre - net_ignores) + 0.0}   # jamais « -0.0 »


def proposer_cheques(credit: float, cheques: list, maximum: int = 6) -> list:
    """Les cheques du portefeuille qui font, ensemble, le credit d'une remise. Pur.

    Pour une remise SANS BORDEREAU (depot fait avant la collecte des bordereaux, bordereau perdu) : on ne sait
    pas quels cheques elle contient, mais le credit de la banque le dit souvent a lui seul. `cheques` :
    [{name, montant}]. -> les `name` proposes, ou [] si rien n'est net :
      - UN cheque au montant exact, s'il est seul a ce montant ;
      - sinon LA combinaison de 2 a `maximum` cheques dont la somme tombe au millime sur le credit, si elle
        est unique (deux combinaisons possibles = on ne choisit pas a la place de la personne)."""
    from itertools import combinations

    credit = _m(credit)
    cheques = [c for c in cheques or [] if _m(c.get("montant")) > 0]
    exacts = [c for c in cheques if abs(_m(c["montant"]) - credit) <= 0.0005]
    if len(exacts) == 1:
        return [exacts[0]["name"]]
    if exacts or len(cheques) > 22:
        return []
    plus_petits = [c for c in cheques if _m(c["montant"]) < credit]
    for taille in range(2, min(maximum, len(plus_petits)) + 1):
        trouvees = [combi for combi in combinations(plus_petits, taille)
                    if abs(_m(sum(_m(c["montant"]) for c in combi)) - credit) <= 0.0005]
        if len(trouvees) == 1:
            return [c["name"] for c in trouvees[0]]
        if trouvees:
            return []
    return []


def resume_passage(out: dict) -> dict:
    """Ce que le JOURNAL des verifications retient du passage BIAT (`tasks/passages`). Pur.
    Une cle « erreur » fait passer la verification en « etape en erreur » : notification aux responsables,
    ligne rouge dans « Dernieres verifications ». C'est ainsi qu'une connexion refusee par le portail, un code
    SMS redemande ou un encaissement qui n'a pas pu etre soumis se SAVENT, au lieu de dormir dans un journal
    technique (demande du 16/11/2026 : « comment je sais, lors du cron, qu'une connexion a ete refusee ? »)."""
    out = out or {}
    erreurs = []
    if out.get("erreur"):
        erreurs.append("relevé du portail : " + str(out["erreur"]))
    if isinstance(out.get("solde"), dict) and out["solde"].get("erreur"):
        erreurs.append("lecture du solde : " + str(out["solde"]["erreur"]))
    for cle, nom in (("encaissements", "remises"), ("virements", "virements")):
        for e in (out.get(cle) or {}).get("erreurs") or []:
            erreurs.append(f"{nom} : " + str(e.get("erreur") or e))
    remises = len([e for e in (out.get("encaissements") or {}).get("encaisses") or [] if e.get("soumis")])
    virements = len([e for e in (out.get("virements") or {}).get("encaisses") or [] if e.get("soumis")])
    res = {"robot": bool(out.get("robot")), "nouveaux": out.get("crees"), "remises": remises, "virements": virements}
    cartes = out.get("cartes")
    if isinstance(cartes, dict):
        # Les courriels du terminal de paiement : combien de remises sont arrivees, et si la boite n'a pas pu
        # etre lue. Une boite injoignable est DITE, sans faire passer la verification en erreur (elle se relit
        # au passage suivant ; rien n'est perdu).
        res["cartes"] = len(cartes.get("nouvelles") or [])
        if cartes.get("erreur"):
            res["cartes_non_lues"] = str(cartes["erreur"])[:120]
        elif cartes.get("illisibles"):
            res["cartes_non_lues"] = "PDF non reconnu : " + ", ".join(cartes["illisibles"][:3])
    compta = out.get("cartes_compta") or {}
    if compta.get("ecritures"):
        res["cartes_ecritures"] = len(compta["ecritures"])
    for e in compta.get("erreurs") or []:
        erreurs.append("cartes : " + str(e.get("erreur") or e))
    if out.get("suspendu"):
        # L'alerte est partie au passage ou le portail a demande le code ; les suivants le rappellent dans le
        # journal sans alerter de nouveau (sept notifications par jour n'apprendraient rien de plus).
        res["suspendu"] = True
        res["motif"] = out.get("motif") or "code"
    if erreurs:
        res["erreur"] = " ; ".join(erreurs)[:300]
    return res


def compte() -> str:
    """Le compte comptable de la BIAT : celui du Bank Account de la societe, sinon « BIAT - A&S »."""
    return frappe.db.get_value("Bank Account", {"bank": BANQUE, "is_company_account": 1, "disabled": 0}, "account") \
        or "BIAT - A&S"


def charger_contexte(lignes: list) -> dict:
    """Ce qu'ERPNext porte deja pour ces lignes. Lecture seule."""
    numeros, references, remises = set(), set(), {}
    for ligne in lignes:
        sens = ligne.get("sens") or ("Debit" if flt(ligne.get("debit")) else "Credit")
        vu = detecter(ligne.get("operation"), sens)
        if vu["numero"]:
            numeros.add(vu["numero"])
            if vu["regle"] == "enc_cheque":
                remises[vu["numero"]] = flt(ligne.get("credit") or ligne.get("montant"), 3)
        if ligne.get("reference"):
            references.add(ligne["reference"].upper())

    paiements = {}
    for numero in numeros:
        # « BR:7473933 » dans le n° de reference d'un paiement, numero entier (pas « BR:74739331 »).
        lignes_pe = frappe.db.sql(
            """select name, paid_amount as montant, docstatus, paid_to as compte, reference_no
               from `tabPayment Entry` where docstatus < 2 and reference_no like %s""",
            ("%BR:" + numero + "%",), as_dict=True)
        exactes = [p for p in lignes_pe if re.search(r"BR:\s*" + re.escape(numero) + r"(?!\d)", p.reference_no or "")]
        if exactes:
            paiements[numero] = exactes

    le_compte = compte()
    par_reference = {}
    for reference in references:
        ecritures = frappe.db.sql(
            """select distinct je.name, je.docstatus from `tabJournal Entry` je
               join `tabJournal Entry Account` a on a.parent = je.name and a.account = %s
               where je.docstatus < 2 and (je.user_remark like %s or je.cheque_no like %s or je.remark like %s)""",
            (le_compte, f"%{reference}%", f"%{reference}%", f"%{reference}%"), as_dict=True)
        if ecritures:
            par_reference[reference] = ecritures
    versements = frappe.db.sql(
        """select je.name, je.posting_date as date, je.docstatus, a.debit_in_account_currency as montant
           from `tabJournal Entry` je join `tabJournal Entry Account` a on a.parent = je.name
           where je.docstatus < 2 and a.account = %s and a.debit_in_account_currency > 0
             and ifnull(je.cheque_no, '') not like 'TPE-%%'""",
        (le_compte,), as_dict=True)
    mensuelles = {}
    for e in frappe.db.get_all("Journal Entry", filters={"cheque_no": ["like", PREFIXE_FRAIS + "%"], "docstatus": ["<", 2]},
                               fields=["name", "cheque_no", "docstatus"]):
        mois, annee = e.cheque_no[len(PREFIXE_FRAIS):].split("-")[:2]
        mensuelles[f"{annee}-{mois}"] = {"name": e.name, "docstatus": e.docstatus}
    bordereaux, lectures = {}, {}
    if numeros and frappe.db.exists("DocType", "BRS Bordereau Remise"):
        lu = frappe.db.has_column("BRS Bordereau Remise", "lecture_resume")
        for b in frappe.db.get_all("BRS Bordereau Remise", filters={"banque": BANQUE, "numero": ["in", list(numeros)]},
                                   fields=["name", "numero"] + (["lecture_resume"] if lu else [])):
            bordereaux[b.numero] = b.name
            if b.get("lecture_resume"):
                lectures[b.numero] = b.lecture_resume
    # Virements recus : les paiements qui citent la reference bancaire (« <reference> - BIAT »), et
    # l'encaissement — soumis ou en brouillon — qui porte cette reference dans ses dettes client.
    paiements_ref, encaissements_ref, brouillons_ref = {}, {}, {}
    for ligne in lignes:
        if not ligne.get("reference") or detecter(ligne.get("operation"), ligne.get("sens") or "Credit")["regle"] != "vir_recu":
            continue
        reference = ligne["reference"].upper()
        trouves = frappe.db.sql(
            """select name, paid_amount as montant, docstatus, paid_to as compte, party as client
               from `tabPayment Entry` where docstatus < 2 and reference_no like %s""", (f"%{reference}%",), as_dict=True)
        if trouves:
            paiements_ref[reference] = trouves
        for nom, statut_doc in frappe.db.sql(
                """select distinct e.name, e.docstatus from `tabListe des Dettes client` c
                   join `tabEncaissement Paiement` e on e.name = c.parent
                   where c.parenttype = 'Encaissement Paiement' and c.`n_chèque` = %s and e.docstatus < 2""", (ligne["reference"],)):
            (encaissements_ref if int(statut_doc) == 1 else brouillons_ref)[reference] = nom
    refus_virements = _refus_virements()
    # L'encaissement soumis de chaque bordereau, pour l'ouvrir depuis la ligne.
    encaissements = {}
    if paiements and frappe.db.has_column("Encaissement Paiement", "custom_banque_depot"):
        for nom, bon in frappe.db.sql(
                """select c.parent, c.bon_remise from `tabListe Cheque` c
                   join `tabEncaissement Paiement` e on e.name = c.parent
                   where c.parenttype = 'Encaissement Paiement' and e.docstatus = 1
                     and ifnull(e.custom_banque_depot, '') = %s and c.bon_remise in %s""", (BANQUE, list(paiements))):
            encaissements[bon] = nom
    # Pourquoi une remise dont le bordereau est collecte n'est pas (encore) encaissee.
    refus = {}
    migre = bool(bordereaux) and frappe.db.has_column("Encaissement Paiement", "custom_banque_depot")
    for ligne in (lignes if migre else []):
        vu = detecter(ligne.get("operation"), ligne.get("sens") or ("Debit" if flt(ligne.get("debit")) else "Credit"))
        numero = vu["numero"]
        if vu["regle"] != "enc_cheque" or not numero or numero in paiements or numero not in bordereaux:
            continue
        brouillon = _encaissement_en_cours(numero)
        if brouillon:
            refus[numero] = f"l’encaissement {brouillon} est en brouillon (à soumettre)"
            continue
        b = _bordereau(numero)
        decision = decider_encaissement(ligne, numero, b, _etat_paiements([p.get("paiement") for p in (b or {}).get("pieces", [])]))
        if not decision["faire"]:
            refus[numero] = decision["raison"]
    tpe = _remises_tpe(lignes)
    return {"tpe": tpe, "prelevements_tpe": _prelevements_tpe(lignes, tpe),
            "paiements_par_bordereau": paiements, "ecritures_par_reference": par_reference,
            "ecritures_versement": versements, "remises": remises, "frais_mensuels": mensuelles,
            "bordereaux": bordereaux, "lectures": lectures, "refus": refus, "encaissements": encaissements,
            "paiements_par_reference": paiements_ref, "encaissements_par_reference": encaissements_ref,
            "brouillons_par_reference": brouillons_ref, "refus_virements": refus_virements}


def credit_de_terminal(operation: str) -> bool:
    """Le libelle dit-il « remise du terminal de paiement » ? Pur. Sert a ne pas prendre un tel credit pour
    le virement d'un client (`encaisser_virements` solderait ses dettes avec l'argent des cartes)."""
    from bank_retenue_sync.bank import monetique

    return bool(monetique.MOTIF_TPE.search(_norme(operation)))


def candidats_tpe(lignes: list) -> list:
    """Les credits qui PEUVENT etre une remise du terminal : ceux qu'aucune regle ne reconnait, et ceux dont
    le libelle le dit. Jamais une ligne rattachee a la main. Pur.
    -> [{cle, date, montant, bordereau?, jour_remise?, terminal?}] (ce que le libelle dit de la remise)"""
    from bank_retenue_sync.bank import monetique

    out = []
    for ligne in lignes:
        sens = ligne.get("sens") or ("Debit" if _m(ligne.get("debit")) else "Credit")
        if sens != "Credit" or ligne.get("lien_manuel"):
            continue
        if detecter(ligne.get("operation"), sens)["regle"] and not credit_de_terminal(ligne.get("operation")):
            continue
        candidat = {"cle": ligne.get("cle") or ligne.get("name"), "date": ligne.get("date"),
                    "montant": _m(ligne.get("credit") or ligne.get("montant"))}
        candidat.update({k: v for k, v in monetique.lire_libelle(ligne.get("operation")).items()
                         if k in ("bordereau", "jour_remise", "terminal")})
        out.append(candidat)
    return out


def prelevements_tpe(lignes: list) -> list:
    """Les debits de commission du terminal, avec la reference du terminal lue sur le libelle. Pur.
    -> [{cle, date, montant, terminal}]"""
    from bank_retenue_sync.bank import monetique

    out = []
    for ligne in lignes:
        sens = ligne.get("sens") or ("Debit" if _m(ligne.get("debit")) else "Credit")
        if sens != "Debit" or ligne.get("lien_manuel") or detecter(ligne.get("operation"), sens)["regle"] != "com_tpe":
            continue
        out.append({"cle": ligne.get("cle") or ligne.get("name"), "date": ligne.get("date"),
                    "montant": _m(ligne.get("debit") or ligne.get("montant")),
                    "terminal": monetique.lire_libelle(ligne.get("operation")).get("terminal") or ""})
    return out


def _remises_tpe(lignes: list) -> dict:
    """{cle de ligne: remise(s) du terminal qu'elle paie} — vide si rien n'est arrive par courriel, None si
    la lecture a echoue (les fiches gardent alors leur etat)."""
    try:
        from bank_retenue_sync.bank import monetique

        return monetique.affectations(candidats_tpe(lignes))
    except Exception:
        frappe.log_error(title="BIAT : remises du terminal non rapprochées", message=frappe.get_traceback())
        return None                                    # None : « pas su lire », a ne pas confondre avec « aucune »


def _prelevements_tpe(lignes: list, tpe) -> dict:
    """{cle de ligne: la remise dont la banque preleve la commission, et son taux}. Ne leve jamais."""
    try:
        from bank_retenue_sync.bank import monetique

        return monetique.prelevements(prelevements_tpe(lignes), tpe or {})
    except Exception:
        frappe.log_error(title="BIAT : commissions du terminal non rapprochées", message=frappe.get_traceback())
        return {}


def _comptabiliser_cartes() -> dict:
    """Cartes : paiements ramenes au compte d'attente (une fois), puis ecriture des remises creditees.
    Ne leve jamais : une erreur est rendue, et portee au journal des verifications."""
    try:
        from bank_retenue_sync.bank import monetique

        ramenes = monetique.rapatrier_paiements()
        out = monetique.comptabiliser()
        out["rapatries"] = ramenes["deplaces"]
        out["erreurs"] = ramenes["erreurs"] + out["erreurs"]
        return out
    except Exception as e:
        frappe.log_error(title="BIAT : comptabilisation des remises carte", message=frappe.get_traceback())
        return {"ecritures": [], "refus": {}, "rapatries": [], "erreurs": [{"erreur": str(e)[:200]}]}


def classer_registre(persist: bool = True, _frais: bool = True) -> dict:
    """Reclasse toutes les lignes BIAT du registre. Un arbitrage humain (ignore, lien manuel) n'est
    jamais ecrase : `apply_classifications` saute les lignes ignorees, et une ligne rattachee a la
    main garde son rattachement. -> {mouvements, kpi, maj?}"""
    lignes = frappe.db.get_all(
        registry.DOCTYPE, filters={"banque": BANQUE}, limit_page_length=0, order_by="date asc, creation asc",
        fields=["name as cle", "date", "operation", "reference", "debit", "credit", "montant", "sens",
                "lien_manuel", "ignore_manuel"])
    contexte = charger_contexte(lignes)
    classements = [classer_ligne(ligne, contexte) for ligne in lignes if not ligne.lien_manuel]
    kpi = {}
    par_cle = {c["cle"]: c for c in classements}
    for ligne in lignes:
        c = par_cle.get(ligne.cle)
        if not c:
            continue
        case = kpi.setdefault(ligne.sens or "Credit", {}).setdefault(c["statut"], {"nb": 0, "montant": 0.0})
        case["nb"] += 1
        case["montant"] = flt(case["montant"] + flt(ligne.montant), 3)
    out = {"mouvements": len(lignes), "kpi": kpi}
    if persist:
        out.update(registry.apply_classifications(classements))
        if contexte.get("tpe") is not None:
            from bank_retenue_sync.bank import monetique

            monetique.noter_credits(contexte["tpe"], lignes)   # chaque fiche de remise dit si elle est creditee
            monetique.noter_frais(contexte.get("prelevements_tpe"), lignes)
        if _frais:
            # Les frais reconnus rejoignent l'ecriture de leur mois ; on reclasse ensuite, une fois, pour que
            # chaque commission designe cette ecriture — et chaque remise carte creditee, la sienne.
            out["frais"] = sync_frais()
            out["cartes_compta"] = _comptabiliser_cartes()
            if any(f.get("statut") in ("cree", "remplacee", "soumise") for f in out["frais"]) \
                    or out["cartes_compta"].get("ecritures") or out["cartes_compta"].get("refus"):
                suite = classer_registre(persist=True, _frais=False)
                out.update(kpi=suite["kpi"], maj=suite.get("maj"))
    return out


def sync_frais(periode: str = None, insert: bool = True) -> list:
    """Refait l'ecriture mensuelle des frais BIAT de chaque mois qui porte des frais (ou de `periode`).

        Dr  Frais bancaire - A&S     total du mois
        Cr  BIAT - A&S               total du mois

    Memes regles que les frais Zitouna (`expenses/fees.sync_ecriture_mensuelle`) : rien n'est cree tant
    que « Comptabiliser les frais bancaires » n'est pas coche ; une ecriture au bon montant n'est pas
    refaite ; une ecriture remplacee retrouve son etat (soumise si elle l'etait, ou si le site soumet
    automatiquement). -> [{periode, statut, total, je}]"""
    from bank_retenue_sync.encaissement.pending import COMPANY
    from bank_retenue_sync.expenses import fees, journal

    lignes = frappe.db.get_all(
        registry.DOCTYPE, filters={"banque": BANQUE}, limit_page_length=0,
        fields=["name", "date", "reference", "debit", "credit", "sens", "categorie", "statut", "ecart", "ignore_manuel"])
    periodes = [periode] if periode else sorted({periode_de(ligne.date) for ligne in lignes
                                                 if ligne.categorie == "frais_bancaires" or ecart_a_absorber(ligne)})
    if not fees.is_enabled():
        return [{"periode": p, "statut": "inactif"} for p in periodes]
    bilan = []
    for p in periodes:
        cumul = cumul_frais(lignes, p)
        existant = frappe.db.get_value("Journal Entry", {"cheque_no": cumul["cle"], "docstatus": ["<", 2]},
                                       ["name", "total_debit", "docstatus"], as_dict=True)
        if abs(cumul["total"]) < 0.0005:
            # Rien a porter (aucun frais, ou des ecarts qui les compensent au millime) : une ecriture restee
            # d'un passage precedent n'a plus lieu d'etre.
            if existant and insert and (cumul["nb"] or cumul["nb_ecarts"]):
                fees._supprimer_ecriture(existant.name)
                existant = None
            bilan.append({"periode": p, "statut": "vide", "total": 0.0, "je": existant.name if existant else None})
            continue
        # L'ecriture en place est la bonne si elle porte le meme montant DANS LE MEME SENS (credit du compte
        # BIAT quand le total est positif).
        en_place = None
        if existant:
            credit_banque = flt(frappe.db.sql(
                """select ifnull(sum(credit_in_account_currency - debit_in_account_currency), 0)
                   from `tabJournal Entry Account` where parent = %s and account = %s""", (existant.name, compte()))[0][0], 3)
            en_place = abs(credit_banque - cumul["total"]) < 0.0005
        if existant and en_place:
            statut = "inchangee"
            if insert and existant.docstatus == 0 and journal._auto_submit_enabled():
                try:
                    frappe.get_doc("Journal Entry", existant.name).submit()
                    statut = "soumise"
                except Exception:
                    frappe.log_error(frappe.get_traceback(), "frais BIAT : soumission %s" % existant.name)
            bilan.append({"periode": p, "statut": statut, "total": cumul["total"], "je": existant.name})
            continue
        etait_soumise = bool(existant and existant.docstatus == 1)
        remplacee = "%s (%s)" % (existant.name, fees._supprimer_ecriture(existant.name)) if existant and insert else None
        jour = fees.date_ecriture(cumul["jour"])
        centre = frappe.db.get_value("Company", COMPANY, "cost_center")
        montant = abs(cumul["total"])
        # Total positif : la banque a pris des frais -> credit du compte BIAT. Negatif (les encaissements
        # ont rapporte plus que les frais n'ont coute) : l'ecriture s'inverse.
        cote_banque, cote_frais = ("credit", "debit") if cumul["total"] > 0 else ("debit", "credit")
        detail = "%d opérations bancaires cumulées : %.3f" % (cumul["nb"], cumul["frais"])
        if cumul["nb_ecarts"]:
            detail += "\nPetits écarts de %d encaissement(s), banque − comptabilité : %+.3f" % (cumul["nb_ecarts"], cumul["ecarts"])
        je = journal.build_journal_entry(
            COMPANY, jour,
            [{"account": compte(), cote_banque: montant},
             {"account": COMPTE_FRAIS, cote_frais: montant, "cost_center": centre}],
            remark="%s\n%s\nRéf. banque %s" % (cumul["cle"], detail, ", ".join(cumul["references"][:20])),
            cheque_no=cumul["cle"], cheque_date=jour)
        if insert:
            je.insert(ignore_permissions=True)
            if etait_soumise or journal._auto_submit_enabled():
                je.submit()
        bilan.append({"periode": p, "statut": "remplacee" if remplacee else "cree", "total": cumul["total"],
                      "frais": cumul["frais"], "ecarts": cumul["ecarts"],
                      "je": je.name if insert else "(essai)", "remplacee": remplacee})
    return bilan


def _importer(mouvements: list, origine: str) -> dict:
    out = registry.upsert_movements(mouvements, origine=origine, banque=BANQUE)
    out.pop("cles", None)
    out["encaissements"] = encaisser_remises()
    out["virements"] = encaisser_virements()
    out.update(classer_registre(persist=True))
    return out


def importer_fichier(contenu: bytes) -> dict:
    """Range au registre un export de mouvements BIAT (le fichier .xlsx produit par le service) puis
    reclasse. Idempotent : rejouer le meme fichier ne cree aucun doublon."""
    from bank_retenue_sync.bank.movements import _parse_movements_xlsx

    return _importer(_parse_movements_xlsx(contenu), "manuel")   # `origine` : export | bank_transaction | manuel


#: La derniere tentative de releve par le robot ({quand, ok, erreur, dernier_succes}), gardee en base pour le
#: bandeau de la page : un releve en panne doit se voir la ou l'on regarde le solde.
CLE_DERNIER_RELEVE = "brs_biat_dernier_releve"


def dernier_releve() -> dict:
    import json

    try:
        brut = frappe.db.sql("select defvalue from `tabDefaultValue` where parent = '__global' and defkey = %s limit 1",
                             CLE_DERNIER_RELEVE)
        return json.loads((brut[0][0] if brut else None) or "{}")
    except Exception:
        return {}


#: Ce que disent le passage et la page tant que le robot ne doit plus se connecter tout seul.
MESSAGE_SUSPENDU = ("relevé automatique suspendu : le portail MyBIAT attend une validation par code SMS "
                    "(Identification bancaire › BIAT › « Refaire la validation SMS du robot »)")
MESSAGE_REFUS = ("relevé automatique suspendu : MyBIAT a refusé la connexion (compte verrouillé ou identifiants "
                 "refusés). Faire rétablir l’accès par la banque, puis Identification bancaire › BIAT › « Refaire la "
                 "validation SMS du robot »")


def code_sms_demande(erreur) -> bool:
    """L'echec vient-il du portail qui REDEMANDE un code SMS (navigateur du robot plus reconnu) ? Pur."""
    return bool(re.search(r"code\s+sms|BiatCodeRequis", str(erreur or ""), re.IGNORECASE))


def compte_verrouille(erreur) -> bool:
    """Le portail dit-il que le COMPTE est verrouille (« Your account is locked. Please contact our support
    team… », vu en production le 17/11/2026) ? Pur. Seule la banque le debloque."""
    return bool(re.search(r"account (?:is|has been) (?:locked|blocked)|compte .{0,25}(?:verrouill|bloqu)",
                          str(erreur or ""), re.IGNORECASE))


def connexion_refusee(erreur) -> bool:
    """Le portail a-t-il REFUSE la connexion : compte verrouille, identifiants refuses ? Pur.
    (« Connexion BIAT refusee : … » est ce que le robot leve quand il reste sur la page de connexion.)"""
    return compte_verrouille(erreur) or bool(re.search(
        r"connexion biat refus|authentication failed|invalid (?:username|user name|password|credentials)",
        str(erreur or ""), re.IGNORECASE))


def motif_suspension(erreur) -> str | None:
    """Pourquoi le robot ne doit plus se connecter tout seul apres cet echec. Pur.
    -> « code » (le portail attend un code SMS), « refus » (connexion refusee, compte verrouille) ou None
    (panne ordinaire — service injoignable, delai depasse — qui se retente au passage suivant).

    POURQUOI (17/11/2026, production) : la banque COMPTE les codes SMS envoyes et non saisis (« 2 remaining
    attempts », puis 1). Le robot, plus reconnu, a retente a 05:00, 07:00 et 09:00 : trois codes sans suite, et
    le compte a ete VERROUILLE (« Your account is locked »). Une connexion qui demande un humain ne se retente
    donc JAMAIS toute seule, et un refus du portail non plus : retenter un mot de passe refuse verrouille aussi."""
    if code_sms_demande(erreur):
        return "code"
    if connexion_refusee(erreur):
        return "refus"
    return None


def etat_releve(avant: dict, ok: bool, erreur: str, maintenant: str) -> dict:
    """La trace du releve apres une tentative. Pur.
    `validation_requise` : une personne doit agir (`motif` dit pourquoi). Tant qu'elle est vraie, PLUS AUCUNE
    connexion n'est tentee toute seule. Seule la validation faite par la personne (`api/biat_robot`) ou un
    releve reussi la leve."""
    avant = avant or {}
    motif = None if ok else motif_suspension(erreur)
    return {"quand": maintenant, "ok": bool(ok), "erreur": None if ok else (erreur or "")[:300],
            "dernier_succes": maintenant if ok else avant.get("dernier_succes"),
            "validation_requise": motif is not None, "motif": motif}


def _noter_releve(ok: bool, erreur: str = None):
    import json

    try:
        frappe.db.set_global(CLE_DERNIER_RELEVE, json.dumps(
            etat_releve(dernier_releve(), ok, erreur, str(frappe.utils.now_datetime())[:19]), ensure_ascii=False))
    except Exception:
        pass                                           # une trace d'affichage ne fait jamais echouer le releve


def motif_d_apres(etat: dict) -> str | None:
    """Le motif de suspension que porte la trace du dernier releve, ou None si le robot peut se connecter. Pur.
    Une trace ecrite avant le 17/11/2026 n'a ni `validation_requise` ni `motif` : on lit alors son erreur, pour
    que le tout premier passage apres la mise a jour ne fasse pas partir une connexion de plus."""
    etat = etat or {}
    if etat.get("ok") is not False:
        return None
    if "validation_requise" in etat and not etat["validation_requise"]:
        return None                                     # levee par la personne
    return etat.get("motif") or motif_suspension(etat.get("erreur")) or ("code" if etat.get("validation_requise") else None)


def suspendu_d_apres(etat: dict) -> bool:
    return motif_d_apres(etat) is not None


def message_suspension(etat: dict) -> str:
    return MESSAGE_REFUS if motif_d_apres(etat) == "refus" else MESSAGE_SUSPENDU


def releve_suspendu() -> bool:
    """Une personne doit-elle agir avant la prochaine connexion ? Alors le robot ne se connecte pas tout seul."""
    return suspendu_d_apres(dernier_releve())


def lever_suspension():
    """Appele quand la personne vient de faire reconnaitre le navigateur du robot (code SMS accepte)."""
    import json

    try:
        etat = dernier_releve()
        if suspendu_d_apres(etat):
            etat["validation_requise"] = False
            frappe.db.set_global(CLE_DERNIER_RELEVE, json.dumps(etat, ensure_ascii=False))
    except Exception:
        pass


def rafraichir(lookback_days: int = None, timeout: int = 300) -> dict:
    """Demande au service un nouvel export BIAT, le range au registre, puis reclasse. Non fatal :
    si le service ne repond pas, ou si le portail redemande un code SMS, le registre reste tel quel
    et l'erreur est rapportee."""
    import requests

    from bank_retenue_sync.bank import movements as mv

    jours = int(lookback_days or 45)
    out = {"export": jours, "erreur": None}
    etat = dernier_releve()
    if suspendu_d_apres(etat):
        # Pas d'appel au service : une connexion de plus = un SMS de plus (ou un refus de plus sur un compte
        # verrouille), et toujours pas de releve.
        out.update(erreur=message_suspension(etat), suspendu=True, motif=motif_d_apres(etat))
        out.update(classer_registre(persist=True))
        return out
    try:
        job = mv.start_job("/jobs/biat/mouvements/export",
                           {"date_from": str(add_days(nowdate(), -jours)), "date_to": nowdate()})
        mv.wait_job(job, timeout=timeout)
        r = requests.get(mv._base_url() + "/biat/mouvements/export/latest", headers=mv._headers(), timeout=60)
        r.raise_for_status()
        out.update(_importer(mv._parse_movements_xlsx(r.content), "export"))
        try:
            out["solde"] = capturer_solde(timeout=timeout)       # le solde suit le releve ; son echec ne l'annule pas
        except Exception as e:
            out["solde"] = {"erreur": str(e)[:200]}
        _noter_releve(True)
    except Exception as e:
        out["erreur"] = str(e)[:300]
        _noter_releve(False, out["erreur"])
        frappe.log_error(f"rafraichissement BIAT echoue\n{e}", "brs page")
        out.update(classer_registre(persist=True))
    return out


# ---------------------------------------------------------------------------
# Encaissement des remises de cheques, a partir du bordereau collecte
# ---------------------------------------------------------------------------

def _bordereau(numero: str):
    """Le bordereau BIAT collecte sous ce numero, avec ses pieces — ou None."""
    name = frappe.db.get_value("BRS Bordereau Remise", {"banque": BANQUE, "numero": numero}, "name")
    if not name:
        return None
    doc = frappe.get_doc("BRS Bordereau Remise", name)
    return {"name": doc.name, "nature": doc.nature, "lecture_statut": doc.lecture_statut, "a_signaler": doc.a_signaler,
            "pieces": [{"numero": p.numero, "montant": p.montant, "paiement": p.paiement, "client": p.client}
                       for p in doc.pieces]}


def _etat_paiements(noms: list) -> dict:
    from bank_retenue_sync.encaissement import pending

    noms = [n for n in noms if n]
    if not noms:
        return {}
    deja = pending.already_encaissed_refs()
    return {pe.name: {"montant": pe.paid_amount, "compte": pe.paid_to, "docstatus": pe.docstatus,
                      "en_encaissement": pe.name in deja}
            for pe in frappe.get_all("Payment Entry", filters={"name": ["in", noms]},
                                     fields=["name", "paid_amount", "paid_to", "docstatus"])}


def _encaissement_en_cours(numero: str):
    """Un Encaissement Paiement en BROUILLON qui porte deja ce bordereau (soumission automatique coupee, ou
    soumission precedente en echec) — pour ne pas en creer un second."""
    lignes = frappe.db.sql(
        """select distinct c.parent from `tabListe Cheque` c
           join `tabEncaissement Paiement` e on e.name = c.parent
           where c.parenttype = 'Encaissement Paiement' and c.bon_remise = %s and e.docstatus = 0
             and ifnull(e.custom_banque_depot, '') = %s""", (numero, BANQUE))
    return lignes[0][0] if lignes else None


def _soumission_automatique() -> bool:
    return bool(frappe.utils.cint(frappe.db.get_single_value("Bank Retenue Sync Settings", "encaissement_auto_submit")))


def encaisser_remises(insert: bool = True) -> dict:
    """LE FLUX CONTINU (demande du 16/11/2026 : « je veux tout automatiser, pas de validation ; s'il y a des
    ecarts c'est facile a trouver »). Pour chaque remise de cheques BIAT creditee dont le bordereau est
    collecte et bon : un Encaissement Paiement est cree avec ses cheques, banque de depot BIAT, puis soumis
    (meme interrupteur `encaissement_auto_submit` que Zitouna). Le server script « Traitement des
    encaissement » fait le reste : chaque cheque quitte « Chèques - A&S » pour le compte BIAT et porte
    « BR:<n°> », ce que le classement reconnait.

    Idempotent : une remise deja encaissee (paiements « BR:<n°> ») ou deja en brouillon n'est pas refaite.
    Rien n'est force : ce qui n'est pas sur est laisse, avec sa raison sur la ligne de l'identification.
    -> {encaisses: [{bordereau, encaissement, soumis, total, ecart}], refus: {n°: raison}, erreurs: [...]}"""
    from bank_retenue_sync.encaissement import builder
    from bank_retenue_sync.encaissement import ecarts as _ecarts

    out = {"encaisses": [], "refus": {}, "erreurs": []}
    if not frappe.db.exists("DocType", "BRS Bordereau Remise") \
            or not frappe.db.has_column("Encaissement Paiement", "custom_banque_depot"):
        return out                                     # pas encore migre : rien a faire, rien a casser
    # ⚠️ LE SCRIPT COMPTABLE DOIT CONNAITRE LA BANQUE DE DEPOT. C'est lui (customization_app, « Traitement des
    # encaissement ») qui choisit le compte ; une version qui ne lit pas `custom_banque_depot` enverrait une
    # remise BIAT sur le compte Zitouna. Les deux apps se deploient separement : on verifie, on ne suppose pas.
    script = frappe.db.get_value("Server Script", {"name": "Traitement des encaissement", "disabled": 0}, "script") or ""
    if "custom_banque_depot" not in script:
        out["erreurs"].append({"erreur": "le script « Traitement des encaissement » ne lit pas encore la banque de "
                                         "dépôt (customization_app à déployer) : aucune remise BIAT n’est encaissée"})
        return out
    lignes = frappe.db.get_all(
        registry.DOCTYPE, filters={"banque": BANQUE, "sens": "Credit", "ignore_manuel": 0}, limit_page_length=0,
        order_by="date asc, creation asc",
        fields=["name as cle", "date", "operation", "reference", "credit", "montant", "sens", "lien_manuel"])
    auto = _soumission_automatique()
    utilisateur = frappe.session.user
    if insert:
        # UN SEUL PASSAGE A LA FOIS. Le depot d'un bordereau et le passage planifie peuvent tomber ensemble :
        # sans verrou, chacun verrait la remise « pas encore encaissee » et creerait le sien.
        from frappe.utils.file_lock import LockTimeoutError
        from frappe.utils.synchronization import filelock

        try:
            with filelock("brs_biat_encaissement", timeout=120):
                return _encaisser(lignes, auto, utilisateur, out, True, builder, _ecarts)
        except LockTimeoutError:
            out["erreurs"].append({"erreur": "un autre passage BIAT est en cours : remis au suivant"})
            return out
    return _encaisser(lignes, auto, utilisateur, out, False, builder, _ecarts)


def _encaisser(lignes, auto, utilisateur, out, insert, builder, _ecarts) -> dict:
    try:
        if insert:
            # Le server script annule et recree des paiements : il lui faut les droits de la comptabilite,
            # que n'a pas la personne dont le depot de bordereau declenche ce passage.
            frappe.set_user("Administrator")
        for ligne in lignes:
            if ligne.lien_manuel:
                continue
            vu = detecter(ligne.operation, "Credit")
            numero = vu["numero"]
            if vu["regle"] != "enc_cheque" or not numero:
                continue
            if frappe.db.sql("select name from `tabPayment Entry` where docstatus < 2 and reference_no regexp %s limit 1",
                             ("BR:[[:space:]]*" + numero + "([^0-9]|$)",)):
                continue                               # deja encaissee
            brouillon = _encaissement_en_cours(numero)
            if not brouillon:
                bordereau = _bordereau(numero)
                decision = decider_encaissement(
                    ligne, numero, bordereau, _etat_paiements([p.get("paiement") for p in (bordereau or {}).get("pieces", [])]))
                if not decision["faire"]:
                    out["refus"][numero] = decision["raison"]
                    continue
                if not insert:
                    out["encaisses"].append({"bordereau": numero, "encaissement": "(essai)", "soumis": False,
                                             "total": decision["total"], "ecart": decision["ecart"]})
                    continue
                doc = builder.build_encaissement(decision["lignes"], insert=False)
                doc.custom_banque_depot = BANQUE
                doc.insert(ignore_permissions=True)
                _ecarts.persister(doc.name, decision["ecarts"])
                frappe.db.commit()                     # le brouillon existe, quoi qu'il arrive a la soumission
                brouillon, total, ecart = doc.name, decision["total"], decision["ecart"]
            else:
                total = ecart = None
                if not insert:
                    continue
            soumis = False
            if auto:
                try:
                    frappe.get_doc("Encaissement Paiement", brouillon).submit()
                    frappe.db.commit()
                    soumis = True
                except Exception as e:
                    frappe.db.rollback()
                    frappe.log_error(title=f"BIAT : soumission de l'encaissement {brouillon}", message=frappe.get_traceback())
                    out["erreurs"].append({"bordereau": numero, "encaissement": brouillon, "erreur": str(e)[:300]})
            out["encaisses"].append({"bordereau": numero, "encaissement": brouillon, "soumis": soumis,
                                     "total": total, "ecart": ecart})
    finally:
        if insert and frappe.session.user != utilisateur:
            frappe.set_user(utilisateur)
    return out


def actif() -> bool:
    try:
        return bool(frappe.db.get_single_value("Bank Retenue Sync Settings", "enabled"))
    except Exception:
        return False


def passage(refresh: bool = None, courriels: bool = None) -> dict:
    """Un passage BIAT complet : releve (si le robot est actif), encaissement des remises, classement.
    Appele par le planificateur apres chaque verification Zitouna, et par le depot d'un bordereau."""
    if not actif():
        return {}
    # Les encaissements faits sur le site de developpement avant la mise en production se rejouent ici, une
    # fois (cf. `bank/biat_reprise`) : pas au migrate, ou aucun server script ne tourne.
    from bank_retenue_sync.bank import biat_reprise

    reprise = biat_reprise.rejouer()
    # Cartes (terminal de paiement) : les courriels de la banque sont lus AVANT le classement, pour qu'un credit
    # arrive le meme jour que son avis soit reconnu au meme passage. Seulement au passage planifie : un depot de
    # bordereau ou un « Reclassifier » ne relit pas la boite.
    cartes = None
    if courriels if courriels is not None else refresh is None:
        from bank_retenue_sync.bank import monetique

        cartes = monetique.collecter_sans_echec()
    if refresh is None:
        try:
            refresh = bool(frappe.utils.cint(frappe.db.get_single_value("Bank Retenue Sync Settings", "biat_robot_actif")))
        except Exception:
            refresh = False                            # reglage pas encore migre : pas de releve
    suspendu = bool(refresh) and releve_suspendu()
    if refresh and not suspendu:
        out = rafraichir()                             # importe, encaisse et classe
        out["robot"] = True
        if reprise:
            out["reprise"] = reprise
        if cartes is not None:
            out["cartes"] = cartes
        return out
    # Sans releve (robot non active, ou suspendu en attendant un code SMS) : ce qui est deja au registre
    # s'encaisse et se classe quand meme.
    out = {"robot": suspendu, "encaissements": encaisser_remises(), "virements": encaisser_virements()}
    if suspendu:
        out.update(suspendu=True, motif=motif_d_apres(dernier_releve()))
    out.update(classer_registre(persist=True))
    if reprise:
        out["reprise"] = reprise
    if cartes is not None:
        out["cartes"] = cartes
    return out


def passage_job():
    """Declenche par le depot (ou la reverification) d'un bordereau BIAT : la remise est peut-etre deja
    creditee, auquel cas elle s'encaisse tout de suite. Jamais de releve ici — le robot a ses heures."""
    try:
        out = passage(refresh=False)
        frappe.db.commit()
        return out
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title="BIAT : passage après dépôt d'un bordereau", message=frappe.get_traceback())


# ---------------------------------------------------------------------------
# Tableau de bord : solde de la banque, solde ERPNext, ecart
# ---------------------------------------------------------------------------
DOCTYPE_SOLDE = "BRS Solde Bancaire"


def dernier_solde_lu():
    """Le dernier solde BIAT lu sur le portail par le robot (`BRS Solde Bancaire`), ou None."""
    if not frappe.db.has_column(DOCTYPE_SOLDE, "banque"):
        return None
    rows = frappe.db.get_all(DOCTYPE_SOLDE, filters={"banque": BANQUE}, limit_page_length=1,
                             order_by="capture_datetime desc, creation desc",
                             fields=["name", "solde_banque", "date_solde", "capture_datetime", "compte", "type_solde"])
    if not rows:
        return None
    r = rows[0]
    return {"solde": flt(r.solde_banque, 3), "date": str(r.date_solde), "lu_le": str(r.capture_datetime or ""),
            "compte": r.compte, "name": r.name}


def tableau_solde() -> dict:
    """Ce que la page d'identification affiche pour la BIAT. Lecture seule."""
    from bank_retenue_sync.bank import solde as S

    le_compte = compte()
    lignes = frappe.db.get_all(
        registry.DOCTYPE, filters={"banque": BANQUE}, limit_page_length=0, order_by="date asc, creation asc",
        fields=["date", "sens", "debit", "credit", "statut", "categorie", "ignore_manuel", "ecart", "operation", "raison"])
    comptabilises = set()
    for e in frappe.db.get_all("Journal Entry", filters={"cheque_no": ["like", PREFIXE_FRAIS + "%"], "docstatus": 1},
                               fields=["cheque_no"]):
        mois, annee = e.cheque_no[len(PREFIXE_FRAIS):].split("-")[:2]
        comptabilises.add(f"{annee}-{mois}")
    out = resume_solde(lignes, S.solde_erpnext(compte=le_compte), dernier_solde_lu(), comptabilises)
    releve = dernier_releve()
    releve["motif"] = motif_d_apres(releve)             # « code » ou « refus » : la page le dit simplement
    releve["suspendu"] = releve["motif"] is not None
    releve["verrouille"] = bool(releve["suspendu"] and compte_verrouille(releve.get("erreur")))
    out.update(banque=BANQUE, compte=le_compte, releve=releve)
    try:
        from bank_retenue_sync.bank import monetique

        out["cartes"] = monetique.resume()
    except Exception:
        frappe.log_error(title="BIAT : résumé des cartes", message=frappe.get_traceback())
        out["cartes"] = None
    try:
        out["robot"] = bool(frappe.utils.cint(frappe.db.get_single_value("Bank Retenue Sync Settings", "biat_robot_actif")))
    except Exception:
        out["robot"] = False
    return out


def archiver_solde(lu: dict) -> dict:
    """Range un solde lu par le robot (un element de `GET /biat/soldes/latest`) dans `BRS Solde Bancaire`.
    Le solde est lu EN CHIFFRES sur le portail (pas de capture a faire lire par l'IA) : une seule source,
    exacte. Idempotent sur le nom du fichier. -> {statut, name?}"""
    from bank_retenue_sync.bank import solde as S

    solde = lu.get("solde_comptable")
    if solde is None:
        raise ValueError("le service n'a pas rendu de solde comptable pour la BIAT")
    fichier = lu.get("filename") or f"biat-{lu.get('lu_le') or frappe.utils.now()}"
    if frappe.db.exists(DOCTYPE_SOLDE, {"banque": BANQUE, "fichier": fichier}):
        return {"statut": "deja_archive", "fichier": fichier}
    jour = str(lu.get("date_solde") or lu.get("lu_le") or frappe.utils.nowdate())[:10].replace("/", "-")
    cumul = frappe.db.sql("select ifnull(sum(credit - debit), 0), max(date) from `tabBRS Bank Movement` where banque = %s", BANQUE)[0]
    erpnext = S.solde_erpnext(jour, compte=compte())
    doc = frappe.get_doc({
        "doctype": DOCTYPE_SOLDE, "banque": BANQUE, "date_solde": frappe.utils.getdate(jour), "compte": lu.get("compte"),
        "type_solde": "Comptable", "solde_banque": flt(solde, 3), "devise": lu.get("devise") or "TND",
        "capture_datetime": str(lu.get("lu_le") or frappe.utils.now()).replace("T", " "), "fichier": fichier,
        "concordance": 1, "confiance": 1, "montant_service": flt(solde, 3), "derniere_operation": cumul[1],
        "solde_erpnext": erpnext, "ecart": flt(flt(solde) - erpnext, 3), "solde_cumule": flt(cumul[0], 3),
        "ecart_import": flt(flt(solde) - flt(cumul[0]), 3),
        "notes": "Solde lu en chiffres sur le portail MyBIAT par le robot."})
    doc.insert(ignore_permissions=True)
    return {"statut": "archive", "name": doc.name, "solde_banque": doc.solde_banque}


def capturer_solde(timeout: int = 300) -> dict:
    """Demande au robot de lire le solde du compte sur le portail, et l'archive. Leve si le service ne
    repond pas (robot pas encore deploye, portail qui redemande un code SMS)."""
    import requests

    from bank_retenue_sync.bank import movements as mv

    etat = dernier_releve()
    if suspendu_d_apres(etat):
        raise RuntimeError(message_suspension(etat))
    try:
        mv.wait_job(mv.start_job("/jobs/biat/soldes", {}), timeout=timeout)
    except Exception as e:
        if motif_suspension(e):
            _noter_releve(False, str(e))
        raise
    r = requests.get(mv._base_url() + "/biat/soldes/latest", headers=mv._headers(), timeout=60)
    r.raise_for_status()
    comptes = [c for c in (r.json() or []) if c.get("solde_comptable") is not None]
    if not comptes:
        raise RuntimeError("aucun solde BIAT rendu par le service")
    return archiver_solde(comptes[0])


# ---------------------------------------------------------------------------
# Virements recus : la meme logique que Zitouna
# ---------------------------------------------------------------------------

def _script_sait_la_banque(marque: str = "custom_banque_depot") -> bool:
    script = frappe.db.get_value("Server Script", {"name": "Traitement des encaissement", "disabled": 0}, "script") or ""
    return marque in script


def encaisser_virements(insert: bool = True, use_ai: bool = True) -> dict:
    """Virements recus a la BIAT -> dettes client soldees, comme pour Zitouna (demande du 16/11/2026 :
    « implemente la meme logique que Zitouna pour la BIAT »).

    C'est LE MEME CODE que le flux Zitouna, applique aux lignes BIAT du registre :
      - le client est identifie sur le libelle (`encaissement.party.identify` : noms appris des virements
        deja rattaches — des DEUX banques —, alias forces, ressemblance, IA en dernier recours) ;
      - le montant est reparti sur ses dettes (`encaissement.allocation.allocate` : dette exacte,
        sous-ensemble exact, FIFO partiel ; un excedent n'est jamais reparti) ;
      - un Encaissement Paiement est cree PAR VIREMENT, banque de depot BIAT, puis soumis (interrupteur
        `encaissement_auto_submit`) : le server script solde les dettes et porte le paiement au compte
        BIAT, avec « <reference bancaire> - BIAT » pour reference.
    Idempotent (`consumed` : reference deja dans un encaissement ; `booked` : deja saisie a la main).
    Ce qui n'est pas tranche est laisse, avec sa raison sur la ligne de l'identification.
    -> {encaisses: [{reference, client, encaissement, soumis, total}], refus: {reference: raison}, erreurs: []}"""
    from bank_retenue_sync.encaissement import builder, matching, party, pending

    out = {"encaisses": [], "refus": {}, "erreurs": []}
    if not frappe.db.has_column("Encaissement Paiement", "custom_banque_depot"):
        return out
    lignes = frappe.db.get_all(
        registry.DOCTYPE, filters={"banque": BANQUE, "sens": "Credit", "ignore_manuel": 0}, limit_page_length=0,
        order_by="date asc, creation asc", fields=["date", "operation", "reference", "credit", "lien_manuel"])
    virements = [{"date": l.date, "operation": l.operation, "reference": l.reference, "credit": flt(l.credit, 3)}
                 for l in lignes if not l.lien_manuel and l.reference
                 and detecter(l.operation, "Credit")["regle"] == "vir_recu" and not credit_de_terminal(l.operation)]
    if not virements:
        if insert and _refus_virements():
            _garder_refus_virements({})
        return out
    # ⚠️ Meme garde que pour les remises, sur la branche VIREMENT du script : sans elle, le paiement d'un
    # virement recu a la BIAT serait porte au compte Zitouna.
    if not _script_sait_la_banque("pe.paid_to=compte_depot"):
        out["erreurs"].append({"erreur": "le script « Traitement des encaissement » ne porte pas encore les virements "
                                         "à la banque de dépôt (customization_app à déployer) : aucun virement BIAT n’est encaissé"})
        return out

    diag = []
    exclude = pending.already_encaissed_refs()
    dettes = pending.get_pending_dettes(exclude) + pending.get_pending_dettes_aramex(exclude)
    consumed = pending.consumed_bank_keys()["virement"]
    booked = pending.bank_refs_already_booked([v["reference"] for v in virements])
    # Les noms appris le sont sur les virements des DEUX banques : un client qui payait a Zitouna et vire
    # maintenant a la BIAT est reconnu du premier coup.
    historique = [m for m in (registry.registry_as_movements() or []) if matching.is_virement_credit(m)]
    aliases = dict(party.learned_aliases(historique + virements))
    aliases.update(party.manual_aliases())
    resolver = None
    if use_ai:
        from bank_retenue_sync.ai import party_match
        resolver = party_match.resolver(diag)
    lots, d = matching.match_virements(virements, dettes, consumed=consumed, booked=booked,
                                       ai_resolver=resolver, aliases=aliases)
    diag += d
    faits = {lot["n_virement"] for lot in lots}
    for e in diag:
        if e.get("type") == "virement" and e.get("ref") and e["ref"] not in faits and e.get("reason"):
            out["refus"][e["ref"]] = e["reason"] + (f" ({e['client']})" if e.get("client") else "")
    if not insert:
        out["encaisses"] = [{"reference": lot["n_virement"], "client": lot["client"], "encaissement": "(essai)",
                             "soumis": False, "total": lot["total"]} for lot in lots]
        return out
    _garder_refus_virements(out["refus"])

    from frappe.utils.file_lock import LockTimeoutError
    from frappe.utils.synchronization import filelock

    auto = _soumission_automatique()
    utilisateur = frappe.session.user
    try:
        with filelock("brs_biat_encaissement", timeout=120):
            frappe.set_user("Administrator")           # cf. `encaisser_remises` : droits de la comptabilite
            deja = pending.consumed_bank_keys()["virement"]     # relu SOUS le verrou
            for lot in lots:
                if lot["n_virement"] in deja:
                    continue
                lot["banque"] = BANQUE                 # « <reference> - BIAT » sur le paiement
                doc = builder.build_encaissement(None, None, None, [lot], insert=False)
                doc.custom_banque_depot = BANQUE
                doc.insert(ignore_permissions=True)
                frappe.db.commit()                     # le brouillon existe, quoi qu'il arrive a la soumission
                soumis = False
                if auto:
                    try:
                        frappe.get_doc("Encaissement Paiement", doc.name).submit()
                        frappe.db.commit()
                        soumis = True
                    except Exception as e:
                        frappe.db.rollback()
                        frappe.log_error(title=f"BIAT : soumission de l'encaissement {doc.name}", message=frappe.get_traceback())
                        out["erreurs"].append({"reference": lot["n_virement"], "encaissement": doc.name, "erreur": str(e)[:300]})
                out["encaisses"].append({"reference": lot["n_virement"], "client": lot["client"], "encaissement": doc.name,
                                         "soumis": soumis, "total": lot["total"]})
    except LockTimeoutError:
        out["erreurs"].append({"erreur": "un autre passage BIAT est en cours : remis au suivant"})
    finally:
        if frappe.session.user != utilisateur:
            frappe.set_user(utilisateur)
    return out


# ---------------------------------------------------------------------------
# Remise SANS bordereau : l'humain designe les cheques
# ---------------------------------------------------------------------------

def _ligne_remise(cle: str):
    """La ligne du registre, si c'est bien une remise de cheques BIAT pas encore encaissee. Leve sinon."""
    ligne = frappe.db.get_value(registry.DOCTYPE, cle, ["name as cle", "banque", "date", "operation", "reference",
                                                       "credit", "montant", "sens", "ignore_manuel"], as_dict=True)
    if not ligne or ligne.banque != BANQUE:
        frappe.throw("Ce mouvement n’est pas une ligne BIAT.")
    numero = detecter(ligne.operation, "Credit")
    if ligne.sens != "Credit" or numero["regle"] != "enc_cheque" or not numero["numero"]:
        frappe.throw("Ce mouvement n’est pas une remise de chèques.")
    numero = numero["numero"]
    if frappe.db.sql("select name from `tabPayment Entry` where docstatus < 2 and reference_no regexp %s limit 1",
                     ("BR:[[:space:]]*" + numero + "([^0-9]|$)",)) or _encaissement_en_cours(numero):
        frappe.throw(f"La remise {numero} est déjà encaissée (ou son encaissement est en brouillon).")
    return ligne, numero


def cheques_pour_remise(cle: str) -> dict:
    """Ce qu'il faut pour encaisser a la main une remise sans bordereau : la ligne, les cheques encore en
    portefeuille, et ceux que le montant designe. Lecture seule."""
    from bank_retenue_sync.encaissement import pending

    ligne, numero = _ligne_remise(cle)
    deja = pending.already_encaissed_refs()
    cheques = [{"name": pe["name"], "numero": pe.get("numero"), "reference": pe.get("reference_no"), "client": pe.get("party"),
                "montant": flt(pe.get("paid_amount"), 3), "date": str(pe.get("reference_date") or "")}
               for pe in pending._pending_pes(pending.CHEQUE_ACCOUNT, deja)]
    cheques.sort(key=lambda c: (c["date"], c["name"]), reverse=True)
    return {"cle": ligne.cle, "numero": numero, "date": str(ligne.date), "credit": flt(ligne.credit, 3),
            "bordereau": frappe.db.get_value("BRS Bordereau Remise", {"banque": BANQUE, "numero": numero}, "name"),
            "tolerance": ECART_PAR_PIECE, "cheques": cheques,
            "proposes": proposer_cheques(ligne.credit, cheques)}


def encaisser_remise_manuelle(cle: str, paiements: list, par: str = None) -> dict:
    """Encaisse une remise de cheques BIAT dont on n'a pas le bordereau : la personne designe les cheques.
    Memes controles que le flux automatique (`decider_encaissement`) — cheques encore en portefeuille, credit
    de la banque egal aux paiements a `ECART_PAR_PIECE` pres par cheque — et meme resultat : un Encaissement
    Paiement, banque de depot BIAT, SOUMIS (c'est un geste humain, il n'attend pas l'interrupteur).
    -> {encaissement, total, ecart}"""
    from frappe.utils.file_lock import LockTimeoutError
    from frappe.utils.synchronization import filelock

    from bank_retenue_sync.encaissement import builder
    from bank_retenue_sync.encaissement import ecarts as _ecarts

    paiements = [p for p in dict.fromkeys(paiements or []) if p]
    if not paiements:
        frappe.throw("Choisissez au moins un chèque.")
    if not _script_sait_la_banque():
        frappe.throw("Le script « Traitement des encaissement » ne lit pas encore la banque de dépôt "
                     "(customization_app à déployer).")
    utilisateur = frappe.session.user
    try:
        with filelock("brs_biat_encaissement", timeout=60):
            ligne, numero = _ligne_remise(cle)
            etats = _etat_paiements(paiements)
            pieces = []
            for nom in paiements:
                pe = frappe.db.get_value("Payment Entry", nom, ["reference_no", "party", "paid_amount"], as_dict=True)
                if not pe:
                    frappe.throw(f"Paiement introuvable : {nom}.")
                chiffres = re.search(r"\d{4,}", pe.reference_no or "")
                pieces.append({"numero": chiffres.group(0) if chiffres else (pe.reference_no or nom), "montant": pe.paid_amount,
                               "paiement": nom, "client": pe.party})
            decision = decider_encaissement(
                ligne, numero, {"name": "saisie manuelle", "nature": "Chèques", "lecture_statut": "Lue", "a_signaler": None,
                                "pieces": pieces}, etats)
            if not decision["faire"]:
                frappe.throw("Encaissement refusé : " + decision["raison"].replace(
                    "les chèques du bordereau dans ERPNext", "les chèques choisis") + ".")
            frappe.set_user("Administrator")            # cf. `encaisser_remises` : droits de la comptabilite
            doc = builder.build_encaissement(decision["lignes"], insert=False)
            doc.custom_banque_depot = BANQUE
            doc.insert(ignore_permissions=True)
            _ecarts.persister(doc.name, decision["ecarts"])
            doc.add_comment("Comment", f"Remise BIAT n° {numero} encaissée sans bordereau : chèques désignés par {utilisateur}."
                            if not par else f"Remise BIAT n° {numero} encaissée sans bordereau : {par}.")
            doc.submit()
            frappe.db.commit()
    except LockTimeoutError:
        frappe.throw("Un passage BIAT est en cours : réessayez dans une minute.")
    finally:
        if frappe.session.user != utilisateur:
            frappe.set_user(utilisateur)
    classer_registre(persist=True)
    return {"encaissement": doc.name, "total": decision["total"], "ecart": decision["ecart"]}
