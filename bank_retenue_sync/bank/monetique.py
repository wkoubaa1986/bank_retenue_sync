"""Cartes bancaires (TPE) de la BIAT : le releve et l'avis de credit que la banque envoie par courriel.

Demande du 17/11/2026 : « j'ai ajoute un terminal avec la BIAT ; la BIAT envoie des releves des
operations par email (dsi.monetique@biat.com.tn) ; ca doit etre utilise pour faire la reconnaissance
des transactions. Attention : le releve ne veut pas dire necessairement date valeur. »

CE QUE LA BANQUE ENVOIE (constate sur les courriels du 17/11/2026, boite comptable)
--------------------------------------------------------------------------------
Deux courriels le meme jour, chacun avec un PDF de texte (aucune IA necessaire) :

  « Relevé Monétique / Journée Du 16/11/26 »   RLV_<societe>_<JJMMAA>.pdf
      le DETAIL : par bordereau (= une remise du terminal), chaque transaction avec sa date d'achat, son
      montant, sa commission, le numero de carte masque et le N° D'AUTORISATION ;

  « Avis Crédit / Journée Du 16/11/26 »        AVIS_<societe>_<JJMMAA>.pdf
      ce que la banque VERSE : par remise, brut − commission − TVA sur la commission = NET.

LES DEUX CLES
-------------
  - le n° d'autorisation est ce que la caisse saisit comme reference du paiement par carte (« 061800- ») :
    c'est lui qui relie une transaction de la banque a un Payment Entry d'ERPNext ;
  - le NET de l'avis est le montant que la banque porte au compte : c'est lui qui relie une remise a une
    ligne du releve bancaire.

LA DATE N'EST PAS UNE CLE. Achats des 12 et 13/11, telecollecte et traitement le 16/11, credit au compte
un jour que ni le releve ni l'avis ne donnent. Une ligne du releve bancaire se reconnait donc par son
MONTANT (au millime), dans une fenetre de jours autour de la date de traitement — jamais par l'egalite
des dates.

CE QUE LA BANQUE FAIT AU COMPTE (releve reel du 17/11/2026)
----------------------------------------------------------
    Reglement Affilie BIAT TPE : 0010010006 / Remise : 20261116-0811990019-1990010006   credit du BRUT
    COM TVA REF REMISE AFF 0010010006                                                 debit de la commission
Le credit porte la date de traitement, l'affiliation et le n° de BORDEREAU : la remise se designe sans
ambiguite. La commission est prelevee a part, sous la reference du terminal ; la TVA annoncee par l'avis
n'etait pas encore prelevee ce jour-la. Le montant reste le filet (net, journee) si la banque change.

LA COMPTABILITE (decision du 17/11/2026 : « option A »)
------------------------------------------------------
Un paiement RECU par carte va a un compte d'attente (« Cartes à encaisser », comme « Chèques - A&S ») :
`diriger_paiement_carte`, branche sur Payment Entry — une seule regle pour tous les ecrans de saisie.
Quand la banque credite la remise, `comptabiliser` passe l'ecriture :

    Dr  BIAT - A&S               ce que la banque a credite
    Dr  Frais bancaire - A&S     brut − credit (seulement si la banque credite le net)
    Cr  Cartes à encaisser       brut (= les paiements de la remise)

La commission prelevee a part est un frais bancaire comme un autre : elle rejoint l'ecriture mensuelle
« BIAT frais bancaires MM-AAAA » (`bank/biat.sync_frais`). Le compte BIAT d'ERPNext retombe sur la banque,
et le solde du compte d'attente dit ce que la banque n'a pas encore verse.

Les fonctions du haut sont pures (aucun site) : `tests/test_monetique.py`.
"""
from __future__ import annotations

import datetime
import re
import unicodedata

BANQUE = "BIAT"
DOCTYPE = "BRS Remise TPE"
DOCTYPE_TRANSACTION = "BRS Remise TPE Transaction"
#: Cle de la source dans `mail/config.py`.
SOURCE = "biat_monetique"

#: Jours autour de la date de TRAITEMENT ou le credit de la banque peut tomber. Large a dessein : la date
#: de traitement n'est pas la date de valeur, et un montant au millime ne se confond pas.
FENETRE_AVANT = 3
FENETRE_APRES = 15
#: Jours autour de la date d'ACHAT ou chercher le paiement saisi a la caisse.
FENETRE_PAIEMENT = 5
#: Au-dela, un paiement saisi « par carte » que la banque n'a toujours pas releve est a signaler.
DELAI_SANS_RELEVE = 7
TOLERANCE = 0.0015
SETTINGS = "Bank Retenue Sync Settings"
#: Reglage qui porte le compte d'attente des cartes. Vide : rien n'est dirige ni comptabilise.
CHAMP_COMPTE_ATTENTE = "compte_cartes_attente"
NOM_COMPTE_ATTENTE = "Cartes à encaisser"
COMPTE_FRAIS = "Frais bancaire - A&S"
#: Ecart de carte ACCEPTE (20/11/2026) : ou va la difference entre ce que la banque a encaisse et le paiement saisi.
CHAMP_COMPTE_PERTE = "compte_ecart_cartes_perte"        # la banque a encaisse MOINS : une perte
CHAMP_COMPTE_PRODUIT = "compte_ecart_cartes_produit"    # la banque a encaisse PLUS : le compte CREDITE (vide = refuse)
#: Decision du 20/11/2026 : les deux sens vont sur le meme compte — au DEBIT quand la banque a encaisse moins, au
#: CREDIT quand elle a encaisse plus (« ajoute ca dans perte de non paiement mais en credit »). Le compte porte ainsi
#: le solde net des ecarts de carte acceptes.
COMPTE_PERTE_DEFAUT = "Perte de non paiement - A&S"
MOTIF_MIN = 5
#: Pose par le patch `cartes_compte_attente` : les paiements par carte saisis AVANT la regle (sur un compte de
#: banque) sont ramenes une fois au compte d'attente, au premier passage.
CLE_RAPATRIER = "brs_cartes_a_rapatrier"
#: Jours pendant lesquels une remise est suivie (rapprochee du releve bancaire, comptee dans le bandeau).
SUIVI = 120

ETAT_ATTENTE = "En attente du crédit"
ETAT_CREDITEE = "Créditée"

TROUVE = "Trouvé"
TROUVE_MONTANT = "Trouvé par le montant"
MONTANT_DIFFERENT = "Montant différent"
INTROUVABLE = "Introuvable"

BON = "Tout est bon"
A_SIGNALER = "À signaler"
SANS_RELEVE = "En attente du relevé"

_MONTANT = r"-?\d{1,3}(?:,\d{3})+\.\d{3}|-?\d+\.\d{3}"
_MOIS = {"JAN": 1, "JANV": 1, "FEB": 2, "FEV": 2, "FEVR": 2, "MAR": 3, "MARS": 3, "APR": 4, "AVR": 4, "AVRIL": 4,
         "MAY": 5, "MAI": 5, "JUN": 6, "JUIN": 6, "JUL": 7, "JUIL": 7, "AUG": 8, "AOU": 8, "AOUT": 8,
         "SEP": 9, "SEPT": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_MOIS_IMAP = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: Libelles qui disent « remise du terminal » sur un releve bancaire. Le libelle exact de la BIAT n'etait pas
#: encore connu le 17/11/2026 : le montant de l'avis suffit, ces mots ne font qu'elargir a une ligne qu'une
#: autre regle aurait prise (un « virement »).
MOTIF_TPE = re.compile(r"\b(TPE|MONETIQUE|TELE ?COLLECTE|REMISES? CARTES?|AFFILIE)\b")


# ---------------------------------------------------------------------------
# Lecture des deux PDF — pur (a partir des lignes de texte)
# ---------------------------------------------------------------------------

def _m(valeur) -> float:
    """Un montant au millime (pas `flt` : hors site il rend 0)."""
    try:
        return round(float(valeur or 0), 3)
    except (TypeError, ValueError):
        return 0.0


def _norme(texte) -> str:
    sans = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode()
    return " ".join(sans.upper().split())


def montant(texte) -> float:
    """« 1,149.000 » -> 1149.0 : la virgule separe les milliers, le point les millimes."""
    return _m(str(texte or "").replace(",", "").strip() or 0)


def _date_releve(texte):
    """« 16/11/2026 » ou « 16/11/26 » -> date."""
    trouve = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})$", str(texte or "").strip())
    if not trouve:
        return None
    jour, mois, annee = (int(g) for g in trouve.groups())
    try:
        return datetime.date(annee + 2000 if annee < 100 else annee, mois, jour)
    except ValueError:
        return None


def _date_avis(texte):
    """« 16-NOV-26 » -> date. Le mois est une abreviation (anglaise ou francaise)."""
    trouve = re.match(r"(\d{1,2})-([A-Z.]+)-(\d{2,4})$", _norme(texte))
    if not trouve:
        return None
    mois = _MOIS.get(trouve.group(2).strip("."))
    annee = int(trouve.group(3))
    try:
        return datetime.date(annee + 2000 if annee < 100 else annee, mois, int(trouve.group(1))) if mois else None
    except ValueError:
        return None


_TRANSACTION = re.compile(
    r"^(\d{6,})\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+(" + _MONTANT + r")\s+(" + _MONTANT + r")\s+([0-9X*]{8,})"
    r"(?:\s+(.*?))??\s+((?=[A-Z0-9]*\d)[A-Z0-9]{4,})$")
_LIGNE_AVIS = re.compile(
    r"^(\w+)\s+(\d{1,2}-[A-Z.]+-\d{2,4})\s+(\d+)\s+(\d{1,2}-[A-Z.]+-\d{2,4})\s+(" + _MONTANT + r")\s+("
    + _MONTANT + r")\s+(" + _MONTANT + r")\s+(" + _MONTANT + r")$")


def _identifiant(lignes: list, mot: str) -> str:
    """Le numero qui suit `mot` (« AFFILIATION », « COMPTE ») : sur la meme ligne, ou seul en tete de la
    ligne suivante — selon le document, la valeur est imprimee a cote du libelle ou juste dessous."""
    normees = [_norme(l) for l in lignes]
    for i, l in enumerate(normees):
        if not re.search(r"\b" + mot + r"\b", l):
            continue
        trouve = re.search(r"\b" + mot + r"(?:\s+AFFILIE)?\s+([A-Z]?\d{6,})\b", l)
        if trouve:
            return trouve.group(1)
        suivante = re.match(r"([A-Z]?\d{6,})\b", normees[i + 1]) if i + 1 < len(normees) else None
        if suivante:
            return suivante.group(1)
    return ""


def nature_de(lignes: list) -> str | None:
    """« releve », « avis » ou None, d'apres le titre du document."""
    for brute in lignes:
        l = _norme(brute)
        if "RELEVE DES OPERATIONS MONETIQUE" in l:
            return "releve"
        if "AVIS D'OPERATIONS" in l or "REMISES TPE" in l:
            return "avis"
    return None


def lire_releve(lignes: list) -> dict:
    """Le releve des operations monetiques. -> {type, affiliation, compte, date_traitement, bordereaux:
    [{bordereau, terminal, remise, date_traitement, transactions: [{transaction, date_achat, montant,
    commission, carte, type_carte, autorisation}], nombre, total, commission}], nombre, total, commission,
    anomalies}.

    Rien n'est devine : chaque total imprime est RECALCULE (somme des transactions d'un bordereau, somme des
    bordereaux de la journee). Un total qui ne retombe pas est une lecture incomplete, dite dans `anomalies`."""
    out = {"type": "releve", "affiliation": "", "compte": "", "date_traitement": None, "bordereaux": [],
           "nombre": 0, "total": 0.0, "commission": 0.0, "anomalies": []}
    out["affiliation"], out["compte"] = _identifiant(lignes, "AFFILIATION"), _identifiant(lignes, "COMPTE")
    courant, jour, journee = None, None, None
    for brute in lignes:
        l = _norme(brute)
        trouve = re.search(r"DATE TRAITEMENT\s+(\d{1,2}/\d{1,2}/\d{2,4})", l)
        if trouve:
            jour = _date_releve(trouve.group(1))
            out["date_traitement"] = out["date_traitement"] or jour
            continue
        if "TOTAL BORDEREAU" in l:
            if courant:
                annonces = re.findall(_MONTANT, l)
                if len(annonces) >= 2:
                    courant["total_annonce"], courant["commission_annoncee"] = montant(annonces[0]), montant(annonces[1])
            continue
        if "TOTAL JOURNEE" in l:
            annonces = re.findall(_MONTANT, l)
            if len(annonces) >= 2:
                journee = (montant(annonces[0]), montant(annonces[1]))
            continue
        trouve = re.search(r"BORDEREAU N?\s*(\d{4,})", l)
        if trouve:
            terminal = re.search(r"TERMINAL\s+(\w+)", l)
            remise = re.search(r"REMISE\s+(\d+)", l)
            courant = {"bordereau": trouve.group(1), "terminal": terminal.group(1) if terminal else "",
                       "remise": remise.group(1) if remise else "", "date_traitement": jour, "transactions": []}
            out["bordereaux"].append(courant)
            continue
        trouve = _TRANSACTION.match(l)
        if trouve and courant is not None:
            courant["transactions"].append({
                "transaction": trouve.group(1), "date_achat": _date_releve(trouve.group(2)),
                "montant": montant(trouve.group(3)), "commission": montant(trouve.group(4)),
                "carte": trouve.group(5).lower(), "type_carte": (trouve.group(6) or "").strip().capitalize(),
                "autorisation": trouve.group(7)})

    for b in out["bordereaux"]:
        b["nombre"] = len(b["transactions"])
        b["total"] = _m(sum(t["montant"] for t in b["transactions"]))
        b["commission"] = _m(sum(t["commission"] for t in b["transactions"]))
        quoi = f"bordereau {b['bordereau']}"
        if not b["transactions"]:
            out["anomalies"].append(f"{quoi} : aucune transaction lue")
        if not b["remise"] or not b["date_traitement"]:
            out["anomalies"].append(f"{quoi} : n° de remise ou date de traitement illisible")
        annonce = b.pop("total_annonce", None)
        if annonce is not None and abs(annonce - b["total"]) > TOLERANCE:
            out["anomalies"].append(f"{quoi} : les transactions lues font {b['total']:.3f}, le relevé annonce {annonce:.3f}")
        annonce = b.pop("commission_annoncee", None)
        if annonce is not None and abs(annonce - b["commission"]) > TOLERANCE:
            out["anomalies"].append(f"{quoi} : les commissions lues font {b['commission']:.3f}, le relevé annonce {annonce:.3f}")
    out["nombre"] = sum(b["nombre"] for b in out["bordereaux"])
    out["total"] = _m(sum(b["total"] for b in out["bordereaux"]))
    out["commission"] = _m(sum(b["commission"] for b in out["bordereaux"]))
    if not out["bordereaux"]:
        out["anomalies"].append("aucun bordereau lu")
    if journee and abs(journee[0] - out["total"]) > TOLERANCE:
        out["anomalies"].append(f"les bordereaux lus font {out['total']:.3f}, le relevé annonce {journee[0]:.3f} pour la journée")
    return out


def lire_avis(lignes: list) -> dict:
    """L'avis de credit des remises TPE. -> {type, affiliation, compte, edite_le, remises: [{terminal,
    date_telecollecte, remise, date_traitement, brut, commission, tva, net}], brut, commission, tva, net,
    anomalies}. Controle : brut − commission − TVA = net, ligne par ligne, et le total imprime."""
    out = {"type": "avis", "affiliation": "", "compte": "", "edite_le": None, "remises": [],
           "brut": 0.0, "commission": 0.0, "tva": 0.0, "net": 0.0, "anomalies": []}
    out["affiliation"], out["compte"] = _identifiant(lignes, "AFFILIATION"), _identifiant(lignes, "COMPTE")
    total = None
    for brute in lignes:
        l = _norme(brute)
        trouve = re.search(r"EDITE LE\s*:?\s*(\d{1,2}-[A-Z.]+-\d{2,4})", l)
        if trouve:
            out["edite_le"] = _date_avis(trouve.group(1))
        trouve = re.match(r"^TOTAL\s+(" + _MONTANT + r")\s+(" + _MONTANT + r")\s+(" + _MONTANT + r")\s+(" + _MONTANT + r")$", l)
        if trouve:
            total = tuple(montant(g) for g in trouve.groups())
            continue
        trouve = _LIGNE_AVIS.match(l)
        if trouve:
            r = {"terminal": trouve.group(1), "date_telecollecte": _date_avis(trouve.group(2)), "remise": trouve.group(3),
                 "date_traitement": _date_avis(trouve.group(4)), "brut": montant(trouve.group(5)),
                 "commission": montant(trouve.group(6)), "tva": montant(trouve.group(7)), "net": montant(trouve.group(8))}
            out["remises"].append(r)
            quoi = f"remise {r['remise']}"
            if not r["date_traitement"]:
                out["anomalies"].append(f"{quoi} : date de traitement illisible")
            if abs(r["brut"] - r["commission"] - r["tva"] - r["net"]) > TOLERANCE:
                out["anomalies"].append(f"{quoi} : brut − commission − TVA ne donne pas le net imprimé")
    for champ in ("brut", "commission", "tva", "net"):
        out[champ] = _m(sum(r[champ] for r in out["remises"]))
    if not out["remises"]:
        out["anomalies"].append("aucune remise lue")
    if total and abs(total[3] - out["net"]) > TOLERANCE:
        out["anomalies"].append(f"les remises lues font {out['net']:.3f} net, l’avis annonce {total[3]:.3f}")
    return out


def lire_lignes(lignes: list) -> dict:
    """Le document, quel qu'il soit. `type` vaut None quand ce n'est ni un releve ni un avis."""
    nature = nature_de(lignes)
    if nature == "releve":
        return lire_releve(lignes)
    if nature == "avis":
        return lire_avis(lignes)
    return {"type": None, "anomalies": ["document non reconnu : ni relevé monétique ni avis de crédit"]}


def lignes_du_pdf(contenu: bytes, ecart: float = 4.0) -> list:
    """Les lignes de texte d'un PDF, reconstituees par la POSITION des mots.

    L'ordre du texte dans le fichier ne suit pas la page (les colonnes sortent l'une apres l'autre : tous les
    montants, puis toutes les cartes…). On regroupe donc les mots par hauteur, puis de gauche a droite."""
    import pymupdf

    lignes = []
    with pymupdf.open(stream=contenu, filetype="pdf") as pdf:
        for page in pdf:
            mots = sorted((round((m[1] + m[3]) / 2, 1), m[0], m[4]) for m in page.get_text("words"))
            courante, repere = [], None
            for hauteur, gauche, mot in mots:
                if repere is not None and hauteur - repere > ecart:
                    lignes.append(" ".join(m for _x, m in sorted(courante)))
                    courante = []
                    repere = None
                if repere is None:
                    repere = hauteur
                courante.append((gauche, mot))
            if courante:
                lignes.append(" ".join(m for _x, m in sorted(courante)))
    return lignes


def lire_pdf(contenu: bytes) -> dict:
    return lire_lignes(lignes_du_pdf(contenu))


def nom_remise(date_traitement, terminal, remise) -> str:
    """« TPE-BIAT-20261116-01-0006 » : une remise = un terminal, un numero, un jour de traitement (le numero
    seul repasse par les memes valeurs)."""
    jour = date_traitement.strftime("%Y%m%d") if hasattr(date_traitement, "strftime") else str(date_traitement or "").replace("-", "")
    return f"TPE-{BANQUE}-{jour}-{str(terminal or '').strip() or '00'}-{str(remise or '').strip()}"


# ---------------------------------------------------------------------------
# Transaction de la banque <-> paiement saisi a la caisse — pur
# ---------------------------------------------------------------------------

def autorisation_de(reference) -> str:
    """Le n° d'autorisation tel qu'il se compare : « 061800- » -> « 61800 », « 0000810046 » -> « 810046 ».
    La caisse saisit « <n°>-<banque> » (convention des cheques) et laisse la banque vide ; les zeros de tete
    sautent parfois a la saisie."""
    for morceau in re.split(r"[\s\-/_.:]+", str(reference or "").strip().upper()):
        propre = re.sub(r"[^A-Z0-9]", "", morceau)
        if len(propre) >= 4 and re.search(r"\d", propre):
            return propre.lstrip("0")
    return ""


def autorisations_de(reference) -> set:
    """Tous les numeros qu'une reference saisie porte : « Ticket TPE 061800 - BIAT » -> {« 61800 »}. Les
    ecrans de la caisse n'ecrivent pas tous la reference de la meme facon ; le numero y est toujours."""
    out = set()
    for morceau in re.split(r"[\s\-/_.:]+", str(reference or "").strip().upper()):
        propre = re.sub(r"[^A-Z0-9]", "", morceau)
        if len(propre) >= 4 and re.search(r"\d", propre):
            out.add(propre.lstrip("0"))
    return out


def _jours(a, b) -> int | None:
    try:
        return abs((_en_date(a) - _en_date(b)).days)
    except Exception:
        return None


def _en_date(valeur):
    if isinstance(valeur, datetime.datetime):
        return valeur.date()
    if isinstance(valeur, datetime.date):
        return valeur
    return datetime.date.fromisoformat(str(valeur)[:10])


def rapprocher_transactions(transactions: list, paiements: list, deja_pris=()) -> list:
    """Pour chaque transaction du releve, le paiement d'ERPNext qui lui correspond. Pur.

    `transactions` : [{autorisation, montant, date_achat}] ; `paiements` : [{name, reference, montant, date,
    client, carte (vrai si le mode de paiement est la carte)}] ; `deja_pris` : paiements deja rattaches a une
    autre remise.
    -> une reponse par transaction, dans l'ordre : {etat, paiement, client, montant_erp, remarque}.

    1. Par le N° D'AUTORISATION (la reference saisie) : c'est la cle. Meme montant -> « Trouvé » ; montant
       different -> « Montant différent » (le paiement est designe, l'ecart est a regarder).
    2. Sinon par le MONTANT, parmi les paiements par carte des jours voisins que personne n'a pris : un seul
       candidat -> « Trouvé par le montant » (la reference saisie n'est pas l'autorisation) ; plusieurs ->
       on ne choisit pas.
    3. Sinon « Introuvable » : la banque a encaisse une carte que la caisse n'a pas saisie (ou pas en carte)."""
    pris = set(deja_pris or ())
    reponses = [None] * len(transactions)
    par_autorisation = {}
    for p in paiements:
        if p.get("name") in pris:
            continue
        for cle in autorisations_de(p.get("reference")):
            par_autorisation.setdefault(cle, []).append(p)

    for i, t in enumerate(transactions):
        candidats = [p for p in par_autorisation.get(autorisation_de(t.get("autorisation")), [])
                     if p["name"] not in pris and (_jours(p.get("date"), t.get("date_achat")) or 0) <= 4 * FENETRE_PAIEMENT]
        if not candidats:
            continue
        exacts = [p for p in candidats if abs(_m(p.get("montant")) - _m(t.get("montant"))) <= TOLERANCE]
        p = (exacts or candidats)[0]
        pris.add(p["name"])
        remarque = ""
        if not exacts:
            remarque = f"{_m(t.get('montant')):.3f} à la banque, {_m(p.get('montant')):.3f} dans ERPNext"
        elif len(exacts) > 1:
            remarque = f"{len(exacts)} paiements portent cette autorisation : saisi deux fois ?"
        reponses[i] = {"etat": TROUVE if exacts else MONTANT_DIFFERENT, "paiement": p["name"], "client": p.get("client") or "",
                       "montant_erp": _m(p.get("montant")), "remarque": remarque}

    for i, t in enumerate(transactions):
        if reponses[i]:
            continue
        candidats = [p for p in paiements
                     if p.get("carte") and p["name"] not in pris
                     and abs(_m(p.get("montant")) - _m(t.get("montant"))) <= TOLERANCE
                     and (_jours(p.get("date"), t.get("date_achat")) or 0) <= FENETRE_PAIEMENT]
        if len(candidats) == 1:
            p = candidats[0]
            pris.add(p["name"])
            reponses[i] = {"etat": TROUVE_MONTANT, "paiement": p["name"], "client": p.get("client") or "",
                           "montant_erp": _m(p.get("montant")),
                           "remarque": f"référence saisie « {p.get('reference') or ''} », autorisation de la banque {t.get('autorisation')}"}
        else:
            reponses[i] = {"etat": INTROUVABLE, "paiement": None, "client": "", "montant_erp": 0.0,
                           "remarque": (f"{len(candidats)} paiements par carte de ce montant : à désigner" if candidats
                                        else "aucun paiement par carte de ce montant dans ERPNext")}
    return reponses


def _jour_fr(valeur) -> str:
    try:
        return _en_date(valeur).strftime("%d/%m/%Y")
    except Exception:
        return "?"


def _nombre(valeur) -> str:
    """« 1 149,000 » : l'ecriture d'ici, pas celle du PDF."""
    return f"{_m(valeur):,.3f}".replace(",", " ").replace(".", ",")


def ecart_de(t: dict) -> float:
    """Banque − ERPNext pour une transaction dont le paiement est designe : negatif = la banque a encaisse moins. Pur."""
    return _m(_m(t.get("montant")) - _m(t.get("montant_erp")))


def ecart_est_accepte(t: dict) -> bool:
    """Demande du 20/11/2026 (« comment accepter l'ecart et le comptabiliser ») : une transaction « Montant
    différent » dont quelqu'un a ACCEPTE l'ecart compte comme rapprochee. L'acceptation vaut pour l'ecart qu'il a
    vu : si le paiement ou la transaction change de montant ensuite, elle tombe et la ligne redevient a signaler. Pur."""
    return (t.get("etat") == MONTANT_DIFFERENT and bool(t.get("paiement")) and bool(int(t.get("ecart_accepte") or 0))
            and abs(_m(t.get("ecart_montant")) - ecart_de(t)) <= TOLERANCE)


def verdict(transactions: list, anomalies=()) -> dict:
    """Le verdict d'une remise, pour un ecran qui dit une seule chose (« Tout est bon » / « À signaler »).
    `transactions` : les lignes avec leur `etat`. -> {controle, a_signaler: [phrases]}."""
    phrases = [f"Lecture du PDF : {a}." for a in (anomalies or ())]
    if not transactions:
        return {"controle": A_SIGNALER if phrases else SANS_RELEVE, "a_signaler": phrases}
    for t in transactions:
        quoi = f"Carte du {_jour_fr(t.get('date_achat'))} de {_nombre(t.get('montant'))} (autorisation {t.get('autorisation')})"
        if t.get("etat") == INTROUVABLE:
            phrases.append(f"{quoi} : {t.get('remarque') or 'aucun paiement dans ERPNext'}.")
        elif t.get("etat") == MONTANT_DIFFERENT and not ecart_est_accepte(t):
            phrases.append(f"{quoi} : montant différent du paiement {t.get('paiement')} ({_nombre(t.get('montant_erp'))} dans ERPNext).")
    return {"controle": A_SIGNALER if phrases else BON, "a_signaler": phrases}


# ---------------------------------------------------------------------------
# Ligne du releve bancaire <-> remise — pur
# ---------------------------------------------------------------------------

def _dans_la_fenetre(jour_ligne, traitement) -> bool:
    try:
        ecart = (_en_date(jour_ligne) - _en_date(traitement)).days
    except Exception:
        return False
    return -FENETRE_AVANT <= ecart <= FENETRE_APRES


def affecter(lignes: list, remises: list) -> dict:
    """Quelle ligne de CREDIT du releve bancaire paie quelle(s) remise(s). Pur.

    `lignes`  : [{cle, date, montant, bordereau?, jour_remise?}] — les credits du terminal, et ceux que rien
                d'autre ne reconnait ; `bordereau` et `jour_remise` quand le libelle de la banque les donne ;
    `remises` : [{name, remise, bordereau, date_traitement, brut, commission, tva, net, nombre, mouvement}]
                (`net` nul tant que l'avis de credit n'est pas arrive : seul le brut se compare alors).
    -> {cle de ligne: {remises: [name], nature, brut, commission, tva, net, nombre, date_traitement}}

    LE BORDEREAU D'ABORD : quand le libelle nomme la remise (« Remise : 20261116-…-1990010006 »), c'est elle,
    pourvu que le montant le confirme (brut ou net).

    PAR LE MONTANT, JAMAIS PAR LA DATE : la date ne sert qu'a borner la recherche (`FENETRE_AVANT` jours avant
    la date de traitement, `FENETRE_APRES` apres). Dans l'ordre : le NET d'une remise (ce que dit l'avis), le
    net de toutes les remises traitees le meme jour (un seul credit pour la journee), puis les memes en BRUT
    (commission prelevee a part). Une remise ne paie qu'une ligne ; un rattachement deja fait (`mouvement`)
    est garde tant que le montant le confirme."""
    def somme(groupe, champ):
        return _m(sum(_m(r.get(champ)) for r in groupe))

    def essais(groupe):
        out = []
        if all(r.get("net") for r in groupe):
            out.append(("net", somme(groupe, "net")))
        out.append(("brut", somme(groupe, "brut")))
        return [(nature, total) for nature, total in out if total > 0]

    def fiche(groupe, nature):
        return {"remises": [r["name"] for r in groupe], "nature": nature, "brut": somme(groupe, "brut"),
                "commission": somme(groupe, "commission"), "tva": somme(groupe, "tva"), "net": somme(groupe, "net"),
                "nombre": sum(int(r.get("nombre") or 0) for r in groupe),
                "date_traitement": str(groupe[0].get("date_traitement") or "")[:10],
                "numeros": [str(r.get("remise") or "") for r in groupe],
                "par_remise": {r["name"]: _m(r.get("net") if nature == "net" else r.get("brut")) for r in groupe}}

    resultat, prises = {}, set()
    par_cle = {l["cle"]: l for l in lignes}
    # 1. La remise nommee par le libelle, puis les rattachements deja faits — tant que le montant les confirme.
    deja = {}
    for l in lignes:
        if not l.get("bordereau"):
            continue
        nommees = [r for r in remises if str(r.get("bordereau") or "") == str(l["bordereau"])
                   and (not l.get("jour_remise") or str(r.get("date_traitement") or "")[:10] == l["jour_remise"])]
        if nommees:
            deja[l["cle"]] = nommees[:1]
    for r in remises:
        if r.get("mouvement") in par_cle and r["mouvement"] not in deja \
                and not any(r in groupe for groupe in deja.values()):
            deja.setdefault(r["mouvement"], []).append(r)
    for cle, groupe in deja.items():
        for nature, total in essais(groupe):
            if abs(total - _m(par_cle[cle].get("montant"))) <= TOLERANCE:
                resultat[cle] = fiche(groupe, nature)
                prises.update(r["name"] for r in groupe)
                break
    # 2. Les autres lignes, de la plus ancienne a la plus recente ; les remises, de la plus ancienne aussi.
    ordonnees = sorted(remises, key=lambda r: (str(r.get("date_traitement") or ""), str(r.get("remise") or "")))
    for l in sorted(lignes, key=lambda l: str(l.get("date") or "")):
        if l["cle"] in resultat:
            continue
        libres = [r for r in ordonnees if r["name"] not in prises and _dans_la_fenetre(l.get("date"), r.get("date_traitement"))]
        journees = {}
        for r in libres:
            journees.setdefault(str(r.get("date_traitement") or "")[:10], []).append(r)
        groupes = [[r] for r in libres] + [g for g in journees.values() if len(g) > 1]
        trouve = None
        for voulue in ("net", "brut"):
            for groupe in groupes:
                if any(nature == voulue and abs(total - _m(l.get("montant"))) <= TOLERANCE for nature, total in essais(groupe)):
                    trouve = (groupe, voulue)
                    break
            if trouve:
                break
        if trouve:
            resultat[l["cle"]] = fiche(*trouve)
            prises.update(r["name"] for r in trouve[0])
    for cle, f in resultat.items():
        f["terminal"] = par_cle[cle].get("terminal") or ""       # la reference que la commission reprendra
    return resultat


def raison_credit(tpe: dict) -> str:
    """La phrase portee par la ligne du releve reconnue comme remise du terminal."""
    numeros = ", ".join(n for n in tpe.get("numeros") or [] if n)
    quoi = ("remise carte (TPE) n° " if len(tpe.get("remises") or []) == 1 else "remises carte (TPE) n° ") + numeros
    detail = f"{quoi} traitée{'s' if len(tpe.get('remises') or []) > 1 else ''} le {_jour_fr(tpe.get('date_traitement'))}"
    if tpe.get("nombre"):
        detail += f" : {tpe['nombre']} paiement(s) par carte"
    if tpe.get("nature") == "net":
        detail += (f", brut {_nombre(tpe['brut'])} − commission {_nombre(tpe['commission'])} − TVA {_nombre(tpe['tva'])}"
                   f" = net {_nombre(tpe['net'])} (avis de crédit de la banque)")
    else:
        detail += f", crédit du brut {_nombre(tpe['brut'])} (commission {_nombre(tpe['commission'])} prélevée à part)"
    ecritures = tpe.get("ecritures") or []
    if ecritures and not tpe.get("refus"):
        return detail + " — " + ("écriture " if len(ecritures) == 1 else "écritures ") + ", ".join(e["name"] for e in ecritures)
    if tpe.get("refus"):
        return detail + " — pas comptabilisée : " + tpe["refus"]
    return detail + " — reconnue, pas encore comptabilisée"


# ---------------------------------------------------------------------------
# Lecture du libelle bancaire, prelevement de la commission, ecriture de la remise — pur
# ---------------------------------------------------------------------------

_LIBELLE_CREDIT = re.compile(r"TPE\s*:?\s*(\d{6,}).*?REMISE\s*:?\s*(\d{8})\s*-\s*(\d+)\s*-\s*(\d+)")
_LIBELLE_TERMINAL = re.compile(r"(?:TPE|REMISE AFF\w*)\s*:?\s*(\d{6,})")


def lire_libelle(operation) -> dict:
    """Ce que le libelle bancaire dit de la remise. Pur.
    « Reglement Affilie BIAT TPE : 0010010006\\Remise : 20261116-0811990019-1990010006 »
        -> {terminal: « 0010010006 », jour_remise: « 2026-11-16 », affiliation, bordereau: « 1990010006 »}
    « COM TVA REF REMISE AFF 0010010006 » -> {terminal: « 0010010006 »}"""
    texte = _norme(operation)
    trouve = _LIBELLE_CREDIT.search(texte)
    if trouve:
        j = trouve.group(2)
        return {"terminal": trouve.group(1), "jour_remise": f"{j[:4]}-{j[4:6]}-{j[6:]}",
                "affiliation": trouve.group(3), "bordereau": trouve.group(4)}
    trouve = _LIBELLE_TERMINAL.search(texte)
    return {"terminal": trouve.group(1)} if trouve else {}


def remise_du_prelevement(prelevement: dict, remises: list, par_terminal: dict = None) -> dict | None:
    """La remise dont la banque preleve ici la commission, et CE qu'elle preleve. Pur.

    `prelevement` : {date, montant, terminal} (la reference du terminal lue sur le libelle) ;
    `par_terminal` : {reference du terminal: name de la remise} — tire des credits deja reconnus, qui portent
    la meme reference ; a defaut, la remise dont le n° de bordereau finit comme elle (« …10006 »).
    -> {name, remise, quoi (« commission », « commission et TVA », « TVA » ou None si le montant ne
        correspond a rien d'annonce), commission, tva} ou None."""
    terminal = str(prelevement.get("terminal") or "")
    candidates = []
    nom = (par_terminal or {}).get(terminal)
    if nom:
        candidates = [r for r in remises if r["name"] == nom]
    if not candidates and len(terminal) >= 5:
        candidates = [r for r in remises if str(r.get("bordereau") or "")[-5:] == terminal[-5:]
                      and _dans_la_fenetre(prelevement.get("date"), r.get("date_traitement"))]
    if not candidates:
        return None
    montant = _m(prelevement.get("montant"))

    def quoi(r):
        for nom_frais, valeur in (("commission", _m(r.get("commission"))),
                                  ("commission et TVA", _m(_m(r.get("commission")) + _m(r.get("tva")))),
                                  ("TVA", _m(r.get("tva")))):
            if valeur > 0 and abs(valeur - montant) <= TOLERANCE:
                return nom_frais
        return None

    candidates.sort(key=lambda r: (quoi(r) is None, _jours(prelevement.get("date"), r.get("date_traitement")) or 0))
    r = candidates[0]
    return {"name": r["name"], "remise": str(r.get("remise") or ""), "quoi": quoi(r),
            "commission": _m(r.get("commission")), "tva": _m(r.get("tva"))}


def taux(frais, base) -> float:
    """Le taux d'un frais par rapport au montant des transactions, en % (2 decimales). 0 sans base. Pur.
    Demande du 17/11/2026 : « a chaque fois que tu identifies un frais bancaire relatif au TPE, calcule son
    taux par rapport a la transaction »."""
    base = _m(base)
    return round(100.0 * _m(frais) / base, 2) if base > 0 else 0.0


def _pourcent(valeur) -> str:
    return f"{valeur:.2f}".replace(".", ",") + " %"


def raison_prelevement(montant, trouve: dict | None, brut=None) -> str:
    """La phrase d'une ligne de frais du terminal : ce que la banque preleve, sur quelle remise, et A QUEL
    TAUX par rapport au montant des transactions de cette remise. Pur."""
    if not trouve:
        return "commission du terminal de paiement (cartes) : remise non retrouvée dans les avis de la banque"
    quoi = trouve.get("quoi") or "frais"
    phrase = f"{quoi} du terminal de paiement, remise carte n° {trouve.get('remise')} : {_nombre(montant)}"
    if brut:
        phrase += f" = {_pourcent(taux(montant, brut))} des {_nombre(brut)} de transactions"
    if not trouve.get("quoi"):
        phrase += (f" — l’avis de la banque annonçait commission {_nombre(trouve.get('commission'))}"
                   f" et TVA {_nombre(trouve.get('tva'))}")
    return phrase


def decider_ecriture(remise: dict, paiements: dict, attente: str, banque: str, comptes_ecart: dict = None) -> dict:
    """Faut-il passer l'ecriture d'une remise creditee, et laquelle ? Pur.

    `remise` : {name, brut, montant_credite, controle, transactions: [{paiement, montant, etat, …}]} ;
    `paiements` : {name: {docstatus, compte, montant}} — l'etat ACTUEL des paiements designes ;
    `attente` : le compte d'attente des cartes ; `banque` : le compte de la banque ;
    `comptes_ecart` : {"perte", "produit"} — ou vont les ecarts ACCEPTES (`ecart_est_accepte`).
    -> {faire, raison, lignes: [{account, debit|credit}], frais, perte, produit}

    On ne comptabilise que ce qui est certain : toutes les transactions ont leur paiement, au bon montant (ou
    avec un ecart que quelqu'un a accepte), soumis, ET sur le compte d'attente — sinon l'ecriture crediterait un
    compte que le paiement n'a jamais debite. Tout refus dit pourquoi ; la remise reste visible, « pas
    comptabilisée ».

    Avec un ecart accepte, le compte d'attente est credite de ce que les PAIEMENTS y ont porte, et la difference
    avec le brut de la banque va en perte (la banque a encaisse moins) ou en produit (elle a encaisse plus) :
        Dr banque (credite)  Dr frais (brut − credite)  Dr perte        Cr attente (paiements)  Cr produit"""
    non = lambda raison: {"faire": False, "raison": raison, "lignes": [], "frais": 0.0, "perte": 0.0, "produit": 0.0}   # noqa: E731
    if not attente:
        return non("le compte d’attente des cartes n’est pas réglé (Bank Retenue Sync Settings)")
    comptes_ecart = comptes_ecart or {}
    brut, credite = _m(remise.get("brut")), _m(remise.get("montant_credite"))
    transactions = remise.get("transactions") or []
    bonne = lambda t: t.get("etat") in (TROUVE, TROUVE_MONTANT) or ecart_est_accepte(t)   # noqa: E731
    manquantes = len([t for t in transactions if not bonne(t)])
    if manquantes or remise.get("controle") != BON or not transactions:
        if manquantes:
            return non(f"{manquantes} transaction(s) sans paiement au bon montant dans ERPNext : voir la fiche")
        return non("le relevé des transactions n’est pas encore arrivé par courriel" if not transactions
                   else "la remise est « à signaler » : voir la fiche")
    total, perte, produit = 0.0, 0.0, 0.0
    for t in transactions:
        p = paiements.get(t.get("paiement")) or {}
        if int(p.get("docstatus") or 0) != 1:
            return non(f"le paiement {t.get('paiement')} n’est plus validé")
        if p.get("compte") != attente:
            return non(f"le paiement {t.get('paiement')} n’est pas sur le compte d’attente des cartes (il est sur {p.get('compte')})")
        total = _m(total + _m(p.get("montant")))
        if ecart_est_accepte(t):
            # l'ecart accepte est celui du paiement TEL QU'IL EST aujourd'hui, pas d'un montant retenu ailleurs
            ecart = _m(_m(t.get("montant")) - _m(p.get("montant")))
            if abs(ecart - _m(t.get("ecart_montant"))) > TOLERANCE:
                return non(f"le paiement {t.get('paiement')} a changé de montant depuis que son écart a été accepté : voir la fiche")
            perte, produit = _m(perte + max(-ecart, 0.0)), _m(produit + max(ecart, 0.0))
    if abs(_m(total - perte + produit) - brut) > TOLERANCE:
        return non(f"les paiements font {_nombre(total)}, la remise {_nombre(brut)}")
    if perte > TOLERANCE and not comptes_ecart.get("perte"):
        return non("le compte de perte des écarts de carte n’est pas réglé (Bank Retenue Sync Settings)")
    if produit > TOLERANCE and not comptes_ecart.get("produit"):
        return non("le compte à créditer pour les écarts de carte n’est pas réglé (Bank Retenue Sync Settings)")
    if credite <= 0 or credite > brut + TOLERANCE:
        return non(f"montant crédité incohérent ({_nombre(credite)} pour un brut de {_nombre(brut)})")
    frais = _m(brut - credite) if brut - credite > TOLERANCE else 0.0
    lignes = [{"account": banque, "debit": credite}]
    if frais:
        lignes.append({"account": COMPTE_FRAIS, "debit": frais})
    if perte > TOLERANCE:
        lignes.append({"account": comptes_ecart["perte"], "debit": perte})
    lignes.append({"account": attente, "credit": total})
    if produit > TOLERANCE:
        lignes.append({"account": comptes_ecart["produit"], "credit": produit})
    return {"faire": True, "raison": "", "lignes": lignes, "frais": frais,
            "perte": perte if perte > TOLERANCE else 0.0, "produit": produit if produit > TOLERANCE else 0.0}


def resume_remises(remises: list, sans_releve: list = (), aujourd_hui=None) -> dict:
    """Ce que la page affiche en une ligne. Pur.
    -> {nb, en_attente: {nb, net}, creditees, a_signaler, sans_releve: {nb, montant, en_retard}, phrase}"""
    attente = [r for r in remises if (r.get("etat") or ETAT_ATTENTE) != ETAT_CREDITEE]
    signaler = [r for r in remises if r.get("controle") == A_SIGNALER]
    net = _m(sum(_m(r.get("net") or r.get("brut")) for r in attente))
    retard = [p for p in sans_releve if aujourd_hui and (_jours(aujourd_hui, p.get("date")) or 0) > DELAI_SANS_RELEVE]
    morceaux = []
    if not remises:
        morceaux.append("aucune remise reçue de la banque pour l’instant")
    else:
        morceaux.append(f"{len(attente)} remise(s) en attente de crédit ({_nombre(net)})" if attente
                        else "toutes les remises reçues sont créditées")
        if signaler:
            morceaux.append(f"⚠ {len(signaler)} à signaler")
    bloquees = [r for r in remises if r.get("etat") == ETAT_CREDITEE and not r.get("ecriture") and r.get("comptabilisation")]
    if bloquees:
        morceaux.append(f"⚠ {len(bloquees)} créditée(s) pas comptabilisée(s)")
    if sans_releve:
        morceaux.append(f"{len(sans_releve)} paiement(s) par carte pas encore au relevé de la banque"
                        + (f", dont {len(retard)} de plus de {DELAI_SANS_RELEVE} jours" if retard else ""))
    return {"nb": len(remises), "en_attente": {"nb": len(attente), "net": net}, "creditees": len(remises) - len(attente),
            "a_signaler": len(signaler), "non_comptabilisees": len(bloquees),
            "sans_releve": {"nb": len(sans_releve), "montant": _m(sum(_m(p.get("montant")) for p in sans_releve)),
                            "en_retard": len(retard)},
            "phrase": " · ".join(morceaux)}


# ---------------------------------------------------------------------------
# Avec le site
# ---------------------------------------------------------------------------

def pret() -> bool:
    """Vrai quand le site porte les fiches de remise (app migree)."""
    import frappe

    try:
        return bool(frappe.db.exists("DocType", DOCTYPE))
    except Exception:
        return False


def _depuis_imap(jours: int) -> str:
    jour = datetime.date.today() - datetime.timedelta(days=jours)
    return f"{jour.day:02d}-{_MOIS_IMAP[jour.month - 1]}-{jour.year}"


def collecter(jours: int = 15) -> dict:
    """Lit les courriels de la banque (LECTURE SEULE de la boite) et range chaque remise dans sa fiche, puis
    cherche les paiements de chaque transaction. Idempotent : relire les memes courriels ne change rien.
    -> {messages, pieces, nouvelles: [name], completees: [name], illisibles: [nom de fichier]}

    Ne comptabilise rien. Leve si la boite est injoignable (`collecter_sans_echec` pour le planificateur)."""
    import frappe

    from bank_retenue_sync.mail import config as mail_config

    out = {"messages": 0, "pieces": 0, "nouvelles": [], "completees": [], "illisibles": []}
    if not pret():
        return out
    extension = (mail_config.get_source(SOURCE).get("extension") or ".pdf").lower()
    for msg in mail_config.fetch(SOURCE, since=_depuis_imap(jours)):
        out["messages"] += 1
        for nom, contenu in msg.get("attachments") or []:
            if not nom.lower().endswith(extension):
                continue
            out["pieces"] += 1
            try:
                piece = lire_pdf(contenu)
            except Exception:
                frappe.log_error(title="Cartes BIAT : PDF illisible (%s)" % nom[:80], message=frappe.get_traceback())
                out["illisibles"].append(nom)
                continue
            if piece.get("type") == "releve":
                bilan = _ranger_releve(piece, nom, contenu)
            elif piece.get("type") == "avis":
                bilan = _ranger_avis(piece, nom, contenu)
            else:
                out["illisibles"].append(nom)
                continue
            out["nouvelles"] += [n for n in bilan["nouvelles"] if n not in out["nouvelles"]]
            out["completees"] += [n for n in bilan["completees"] if n not in out["completees"] + out["nouvelles"]]
    out["rapprochees"] = rapprocher_en_attente()
    return out


def collecter_sans_echec(jours: int = 15) -> dict:
    """La collecte, pour le passage planifie : une boite injoignable ne doit ni arreter le passage ni
    alerter sept fois par jour — elle est notee, et dite dans le journal des verifications."""
    import frappe

    try:
        return collecter(jours)
    except Exception as e:
        frappe.log_error(title="Cartes BIAT : courriels non lus", message=frappe.get_traceback())
        return {"erreur": str(e)[:200], "nouvelles": [], "completees": [], "illisibles": []}


def _fiche(date_traitement, terminal, remise, affiliation=""):
    """La fiche de la remise, creee si elle n'existe pas. -> (doc, nouvelle)"""
    import frappe

    nom = nom_remise(date_traitement, terminal, remise)
    if frappe.db.exists(DOCTYPE, nom):
        return frappe.get_doc(DOCTYPE, nom), False
    doc = frappe.new_doc(DOCTYPE)
    doc.update({"banque": BANQUE, "terminal": terminal, "remise": remise, "date_traitement": date_traitement,
                "affiliation": affiliation, "etat": ETAT_ATTENTE, "controle": SANS_RELEVE})
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc, True


def _joindre(doc, champ: str, nom: str, contenu: bytes):
    """Garde le PDF de la banque sur la fiche (fichier prive). Sans effet si la piece y est deja."""
    import frappe

    if doc.get(champ):
        return
    try:
        fichier = frappe.get_doc({"doctype": "File", "file_name": nom, "content": contenu, "is_private": 1,
                                  "attached_to_doctype": DOCTYPE, "attached_to_name": doc.name,
                                  "attached_to_field": champ})
        fichier.flags.ignore_permissions = True
        fichier.insert()
        doc.set(champ, fichier.file_url)
    except Exception:
        frappe.log_error(title="Cartes BIAT : pièce non jointe (%s)" % nom[:80], message=frappe.get_traceback())


def _ranger_releve(piece: dict, nom: str, contenu: bytes) -> dict:
    bilan = {"nouvelles": [], "completees": []}
    for b in piece["bordereaux"]:
        if not b.get("remise") or not b.get("date_traitement"):
            continue
        doc, nouvelle = _fiche(b["date_traitement"], b["terminal"], b["remise"], piece.get("affiliation"))
        if doc.get("transactions") and len(doc.transactions) == b["nombre"] and doc.get("releve"):
            continue                                   # deja range : rien a refaire
        doc.update({"bordereau": b["bordereau"], "nombre": b["nombre"], "affiliation": doc.affiliation or piece.get("affiliation"),
                    "lecture_anomalies": "\n".join(piece.get("anomalies") or [])})
        if not _m(doc.brut):
            doc.brut = b["total"]
        if not _m(doc.commission):
            doc.commission = b["commission"]
        doc.set("transactions", [])
        for t in b["transactions"]:
            doc.append("transactions", dict(t, etat=""))
        _joindre(doc, "releve", nom, contenu)
        doc.flags.ignore_permissions = True
        doc.save()
        bilan["nouvelles" if nouvelle else "completees"].append(doc.name)
    return bilan


def _ranger_avis(piece: dict, nom: str, contenu: bytes) -> dict:
    bilan = {"nouvelles": [], "completees": []}
    for r in piece["remises"]:
        if not r.get("remise") or not r.get("date_traitement"):
            continue
        doc, nouvelle = _fiche(r["date_traitement"], r["terminal"], r["remise"], piece.get("affiliation"))
        if _m(doc.net) and doc.get("avis"):
            continue
        doc.update({"date_telecollecte": r["date_telecollecte"], "brut": r["brut"], "commission": r["commission"],
                    "tva": r["tva"], "net": r["net"], "affiliation": doc.affiliation or piece.get("affiliation")})
        _joindre(doc, "avis", nom, contenu)
        doc.flags.ignore_permissions = True
        doc.save()
        bilan["nouvelles" if nouvelle else "completees"].append(doc.name)
    return bilan


def _est_carte(mode) -> bool:
    return _norme(mode).startswith("CARTE")


def _paiements(debut, fin) -> list:
    """Les encaissements d'ERPNext sur la periode, au format de `rapprocher_transactions`."""
    import frappe

    lignes = frappe.db.sql(
        """select name, reference_no as reference, paid_amount as montant, posting_date as date,
                  ifnull(party_name, party) as client, mode_of_payment as mode, paid_to as compte, docstatus
           from `tabPayment Entry`
           where docstatus < 2 and payment_type = 'Receive' and posting_date between %s and %s""",
        (debut, fin), as_dict=True)
    for l in lignes:
        l["carte"] = _est_carte(l.mode)
    return lignes


def _pris_ailleurs(nom: str) -> set:
    import frappe

    return {p for (p,) in frappe.db.sql(
        f"select paiement from `tab{DOCTYPE_TRANSACTION}` where ifnull(paiement, '') != '' and parent != %s", (nom,))}


def rapprocher(nom: str) -> dict:
    """Cherche le paiement de chaque transaction d'une remise et pose le verdict sur la fiche.
    -> {name, controle, a_signaler}"""
    import frappe

    doc = frappe.get_doc(DOCTYPE, nom)
    transactions = [t.as_dict() for t in doc.get("transactions") or []]
    if transactions:
        jours = [t["date_achat"] for t in transactions if t.get("date_achat")] or [doc.date_traitement]
        paiements = _paiements(min(jours) - datetime.timedelta(days=4 * FENETRE_PAIEMENT),
                               max(jours) + datetime.timedelta(days=4 * FENETRE_PAIEMENT))
        for ligne, reponse in zip(doc.transactions, rapprocher_transactions(transactions, paiements, _pris_ailleurs(nom))):
            ligne.update(reponse)
            if int(ligne.get("ecart_accepte") or 0) and not ecart_est_accepte(ligne.as_dict()):
                # le paiement a ete corrige, ou son montant a change : l'ecart accepte n'est plus celui-la
                ligne.update({"ecart_accepte": 0, "ecart_montant": 0, "ecart_motif": "", "ecart_par": "", "ecart_le": None})
    avis = verdict([t.as_dict() for t in doc.get("transactions") or []],
                   [a for a in (doc.get("lecture_anomalies") or "").split("\n") if a.strip()])
    doc.controle = avis["controle"]
    doc.a_signaler = "\n".join(avis["a_signaler"])
    doc.flags.ignore_permissions = True
    doc.save()
    return {"name": doc.name, "controle": doc.controle, "a_signaler": avis["a_signaler"]}


def rapprocher_en_attente(jours: int = 45) -> int:
    """Rejoue le controle des remises recentes qui ne sont pas « Tout est bon » : la caisse saisit parfois le
    paiement apres le passage de la banque. -> nombre de fiches revues."""
    import frappe

    noms = frappe.db.get_all(DOCTYPE, filters={"controle": ["!=", BON],
                                               "date_traitement": [">=", frappe.utils.add_days(frappe.utils.nowdate(), -jours)]},
                             pluck="name")
    for nom in noms:
        rapprocher(nom)
    return len(noms)


def _debut_suivi():
    import frappe

    return frappe.utils.add_days(frappe.utils.nowdate(), -SUIVI)


def _remises_pour_affectation() -> list:
    import frappe

    champs = ["name", "remise", "bordereau", "date_traitement", "brut", "commission", "tva", "net", "nombre", "mouvement"]
    if frappe.db.has_column(DOCTYPE, "ecriture"):
        champs += ["ecriture", "comptabilisation"]
    return frappe.db.get_all(DOCTYPE, filters={"banque": BANQUE, "date_traitement": [">=", _debut_suivi()]},
                             fields=champs, limit_page_length=0)


def affectations(lignes: list) -> dict:
    """`affecter`, sur les remises du site. `lignes` : les credits BIAT que rien d'autre ne reconnait
    ([{cle, date, montant}]). {} tant que l'app n'est pas migree ou qu'aucune remise n'est arrivee."""
    import frappe

    if not lignes or not pret():
        return {}
    remises = _remises_pour_affectation()
    if not remises:
        return {}
    out = affecter(lignes, remises)
    # L'ecriture de chaque remise (ou la raison pour laquelle elle n'est pas passee) suit la ligne.
    par_nom = {r["name"]: r for r in remises}
    noms = [r.get("ecriture") for r in remises if r.get("ecriture")]
    etats = dict(frappe.db.get_all("Journal Entry", filters={"name": ["in", noms or [""]]}, fields=["name", "docstatus"],
                                   as_list=True)) if noms else {}
    for fiche in out.values():
        ecritures, refus = [], []
        for nom in fiche["remises"]:
            r = par_nom.get(nom) or {}
            if r.get("ecriture") and r["ecriture"] in etats and int(etats[r["ecriture"]]) < 2:
                ecritures.append({"name": r["ecriture"], "docstatus": int(etats[r["ecriture"]])})
            elif r.get("comptabilisation"):
                refus.append(r["comptabilisation"])
        fiche["ecritures"] = ecritures if len(ecritures) == len(fiche["remises"]) else []
        fiche["refus"] = " ; ".join(refus)
    return out


def prelevements(lignes: list, tpe: dict = None) -> dict:
    """Pour chaque DEBIT de commission du terminal ([{cle, date, montant, terminal}]), la remise concernee et le
    taux. `tpe` : le resultat d'`affectations`, dont les credits portent la reference du terminal
    ([{cle: {..., terminal}}]). -> {cle: {name, remise, quoi, commission, tva, brut, taux, raison}}"""
    if not lignes or not pret():
        return {}
    remises = _remises_pour_affectation()
    par_nom = {r["name"]: r for r in remises}
    par_terminal = {}
    for fiche in (tpe or {}).values():
        if fiche.get("terminal") and len(fiche["remises"]) == 1:
            par_terminal[fiche["terminal"]] = fiche["remises"][0]
    out = {}
    for l in lignes:
        trouve = remise_du_prelevement(l, remises, par_terminal)
        brut = _m((par_nom.get(trouve["name"]) or {}).get("brut")) if trouve else 0.0
        out[l["cle"]] = dict(trouve or {}, brut=brut, taux=taux(l.get("montant"), brut),
                             raison=raison_prelevement(l.get("montant"), trouve, brut))
    return out


def noter_credits(tpe: dict, lignes: list):
    """Reporte sur les fiches ce que le classement a reconnu : remise creditee (quelle ligne, quel jour), ou
    de nouveau en attente si la ligne a disparu du rattachement."""
    import frappe

    if not pret():
        return
    par_cle = {l.get("cle") or l.get("name"): l for l in lignes}
    creditees, parts = {}, {}
    for cle, fiche in (tpe or {}).items():
        for nom in fiche["remises"]:
            creditees[nom] = cle
            # Une ligne peut payer plusieurs remises (un credit pour la journee) : a chacune sa part.
            parts[nom] = _m((fiche.get("par_remise") or {}).get(nom))
    for nom, cle in creditees.items():
        ligne = par_cle.get(cle) or {}
        valeurs = {"etat": ETAT_CREDITEE, "mouvement": cle, "date_credit": ligne.get("date"),
                   "montant_credite": parts.get(nom) or _m(ligne.get("credit") or ligne.get("montant"))}
        if frappe.db.get_value(DOCTYPE, nom, ["etat", "mouvement", "montant_credite"]) != \
                (ETAT_CREDITEE, cle, valeurs["montant_credite"]):
            frappe.db.set_value(DOCTYPE, nom, valeurs)
    # Une remise notee creditee que le classement ne rattache plus (ligne ecartee, montant corrige) redevient
    # « en attente ». SEULEMENT parmi celles que le classement a regardees : une remise sortie de la fenetre de
    # suivi n'est plus comparee a rien, et garde donc l'etat qu'elle avait.
    for nom in frappe.db.get_all(DOCTYPE, filters={"etat": ETAT_CREDITEE, "banque": BANQUE,
                                                   "date_traitement": [">=", _debut_suivi()],
                                                   "name": ["not in", list(creditees) or [""]]}, pluck="name"):
        frappe.db.set_value(DOCTYPE, nom, {"etat": ETAT_ATTENTE, "mouvement": None, "date_credit": None, "montant_credite": 0})


def noter_frais(prelevements_tpe: dict, lignes: list):
    """Reporte sur chaque fiche ce que la banque a REELLEMENT preleve pour elle (somme des debits reconnus) et
    le taux que cela fait par rapport au montant de ses transactions."""
    import frappe

    if not pret() or not frappe.db.has_column(DOCTYPE, "frais_preleves"):
        return
    par_cle = {l.get("cle") or l.get("name"): l for l in lignes}
    totaux = {}
    for cle, p in (prelevements_tpe or {}).items():
        if p.get("name") and cle in par_cle:
            totaux[p["name"]] = _m(totaux.get(p["name"], 0.0) + _m(par_cle[cle].get("debit") or par_cle[cle].get("montant")))
    for r in frappe.db.get_all(DOCTYPE, filters={"banque": BANQUE, "date_traitement": [">=", _debut_suivi()]},
                               fields=["name", "brut", "frais_preleves"]):
        preleve = totaux.get(r.name, 0.0)
        if abs(_m(r.frais_preleves) - preleve) > 0.0005:
            frappe.db.set_value(DOCTYPE, r.name, {"frais_preleves": preleve, "taux_frais": taux(preleve, r.brut)})


# ---------------------------------------------------------------------------
# Comptabilite : compte d'attente des cartes, ecriture de la remise
# ---------------------------------------------------------------------------

def compte_attente() -> str | None:
    """Le compte d'attente des cartes, ou None tant qu'il n'est pas regle (rien n'est alors dirige ni
    comptabilise)."""
    import frappe

    try:
        compte = frappe.db.get_single_value(SETTINGS, CHAMP_COMPTE_ATTENTE)
    except Exception:
        return None
    return compte if compte and facturation_prete(compte) else None


def comptes_ecart() -> dict:
    """{"perte", "produit"} : les comptes regles pour les ecarts de carte acceptes (None si non regle ou disparu)."""
    import frappe

    out = {}
    for cle, champ in (("perte", CHAMP_COMPTE_PERTE), ("produit", CHAMP_COMPTE_PRODUIT)):
        compte = None
        if frappe.get_meta(SETTINGS).has_field(champ):
            compte = frappe.db.get_single_value(SETTINGS, champ) or None
        out[cle] = compte if compte and frappe.db.exists("Account", {"name": compte, "is_group": 0}) else None
    return out


SCRIPT_FACTURATION = "Facturation Auto"


def facturation_prete(compte: str) -> bool:
    """La facturation de customization_app sait-elle que ce compte est de l'argent acquis ?

    ⚠️ ORDRE DE DEPLOIEMENT. « Facturation Auto » facture les commandes dont le paiement est sur un compte de
    sa liste (Zitouna, BIAT). Un paiement par carte porte au compte d'attente AVANT que cette liste ne le
    connaisse sortirait de la facturation. Tant que le script en base ne nomme pas le compte, la regle reste
    donc eteinte : rien n'est dirige, ramene ni comptabilise — pousser customization_app d'abord, ou avec.
    Sans ce script sur le site, il n'y a rien a proteger."""
    import frappe

    cle = "brs_cartes_facturation_prete:" + compte
    connu = frappe.cache().get_value(cle)
    if connu is None:
        script = frappe.db.get_value("Server Script", SCRIPT_FACTURATION, "script")
        connu = 1 if (script is None or compte in script) else 0
        frappe.cache().set_value(cle, connu, expires_in_sec=300)
    return bool(connu)


def comptes_de_banque() -> set:
    """Les comptes comptables des banques de la societe (Zitouna, BIAT) et le compte par defaut du mode carte."""
    import frappe

    comptes = set(frappe.db.get_all("Bank Account", filters={"is_company_account": 1}, pluck="account"))
    comptes.update(frappe.db.sql_list(
        "select default_account from `tabMode of Payment Account` where parent like 'Carte%%'"))
    return {c for c in comptes if c}


def diriger_paiement_carte(doc, method=None):
    """Payment Entry, `before_validate` : un paiement RECU par carte va au compte d'attente des cartes.

    Les ecrans de la caisse posent tous un paiement par carte directement sur un compte de banque (le compte
    par defaut du mode « Carte de crédit », ou le compte ecrit en dur dans le script d'encaissement). Or
    l'argent n'y est pas : la banque le verse quelques jours plus tard, par remise. Une seule regle, ici, pour
    tous ces ecrans. Elle ne touche ni un paiement EMIS par carte (depenses), ni un paiement deja place
    ailleurs que sur une banque. Ne leve jamais : un encaissement ne doit pas echouer pour elle."""
    import frappe

    try:
        if doc.docstatus != 0 or doc.payment_type != "Receive" or not _est_carte(doc.mode_of_payment):
            return
        attente = compte_attente()
        if not attente or doc.paid_to == attente or doc.paid_to not in comptes_de_banque():
            return
        devise, nature = frappe.get_cached_value("Account", attente, ["account_currency", "account_type"])
        doc.paid_to = attente
        doc.paid_to_account_currency = devise or doc.paid_to_account_currency
        if doc.meta.has_field("paid_to_account_type"):
            doc.paid_to_account_type = nature
    except Exception:
        frappe.log_error(title="Cartes : paiement non dirigé vers le compte d’attente", message=frappe.get_traceback())


def _deplacer_paiement(nom: str, vers: str) -> str:
    """Porte un paiement VALIDE sur un autre compte de depot en reecrivant ses ecritures comptables (annulation
    des anciennes, creation des nouvelles — la mecanique de « Repost Accounting Ledger » d'ERPNext). Le
    paiement garde son nom, sa date, ses references et ses affectations. -> l'ancien compte."""
    import frappe

    pe = frappe.get_doc("Payment Entry", nom)
    ancien = pe.paid_to
    pe.make_gl_entries(cancel=1)
    devise = frappe.get_cached_value("Account", vers, "account_currency")
    frappe.db.set_value("Payment Entry", nom, {"paid_to": vers, "paid_to_account_currency": devise or pe.paid_to_account_currency},
                        update_modified=False)
    pe = frappe.get_doc("Payment Entry", nom)
    pe.make_gl_entries()
    pe.add_comment("Info", f"Compte de dépôt corrigé : {ancien} → {vers} (paiement par carte : l’argent arrive "
                           "par les remises du terminal de paiement, pas directement en banque).")
    return ancien


def rapatrier_paiements(force: bool = False) -> dict:
    """Ramene au compte d'attente les paiements par carte saisis AVANT la regle, restes sur un compte de banque.
    Une seule fois (drapeau `CLE_RAPATRIER` pose par le patch), au premier passage : jamais pendant un migrate.
    On ne remonte pas avant la premiere transaction connue du terminal (les cartes de 2025 passaient par un
    autre terminal et sont restees ou elles etaient). -> {deplaces: [{name, de}], erreurs: []}"""
    import frappe

    out = {"deplaces": [], "erreurs": []}
    attente = compte_attente()
    if not attente or not pret() or frappe.flags.in_migrate:
        return out
    if not force and not frappe.db.get_global(CLE_RAPATRIER):
        return out
    debut = frappe.db.sql(f"select min(date_achat) from `tab{DOCTYPE_TRANSACTION}`")[0][0]
    if not debut:
        return out                                     # aucun releve encore : on reessaiera au passage suivant
    banques = comptes_de_banque() - {attente}
    for p in _paiements(debut, frappe.utils.getdate()):
        if not (p.carte and p.docstatus == 1 and p.compte in banques):
            continue
        try:
            out["deplaces"].append({"name": p.name, "de": _deplacer_paiement(p.name, attente), "montant": _m(p.montant)})
        except Exception as e:
            frappe.db.rollback()
            frappe.log_error(title="Cartes : paiement %s non ramené au compte d’attente" % p.name, message=frappe.get_traceback())
            out["erreurs"].append({"erreur": f"paiement {p.name} non ramené au compte d’attente : {str(e)[:120]}"})
            return out
    frappe.db.set_global(CLE_RAPATRIER, None)
    return out


def comptabiliser(insert: bool = True) -> dict:
    """Passe l'ecriture de chaque remise CREDITEE par la banque et pas encore comptabilisee (`decider_ecriture`).
    Idempotent : la reference de l'ecriture est le nom de la fiche. Soumise si le site soumet ses ecritures
    automatiquement (meme interrupteur que les frais). -> {ecritures: [{remise, je, soumise, frais}],
    refus: {remise: raison}, erreurs: []}"""
    import frappe

    from bank_retenue_sync.bank import biat
    from bank_retenue_sync.encaissement.pending import COMPANY
    from bank_retenue_sync.expenses import fees, journal

    out = {"ecritures": [], "refus": {}, "erreurs": []}
    attente = compte_attente()
    if not attente or not pret() or not frappe.db.has_column(DOCTYPE, "ecriture"):
        return out
    banque = biat.compte()
    ecarts = comptes_ecart()
    centre = frappe.db.get_value("Company", COMPANY, "cost_center")
    for nom in frappe.db.get_all(DOCTYPE, filters={"banque": BANQUE, "etat": ETAT_CREDITEE, "ecriture": ["is", "not set"]},
                                 order_by="date_traitement asc, remise asc", pluck="name"):
        try:
            existante = frappe.db.get_value("Journal Entry", {"cheque_no": nom, "docstatus": ["<", 2]}, "name")
            if existante:
                frappe.db.set_value(DOCTYPE, nom, {"ecriture": existante, "comptabilisation": ""})
                continue
            doc = frappe.get_doc(DOCTYPE, nom)
            transactions = [t.as_dict() for t in doc.get("transactions") or []]
            noms = [t.get("paiement") for t in transactions if t.get("paiement")]
            etats = {p.name: {"docstatus": p.docstatus, "compte": p.paid_to, "montant": p.paid_amount}
                     for p in frappe.db.get_all("Payment Entry", filters={"name": ["in", noms or [""]]},
                                                fields=["name", "docstatus", "paid_to", "paid_amount"])}
            decision = decider_ecriture(dict(doc.as_dict(), transactions=transactions), etats, attente, banque, ecarts)
            if not decision["faire"]:
                out["refus"][nom] = decision["raison"]
                if (doc.comptabilisation or "") != decision["raison"]:
                    frappe.db.set_value(DOCTYPE, nom, "comptabilisation", decision["raison"])
                continue
            jour = fees.date_ecriture(doc.date_credit or doc.date_traitement)
            # frais, perte et produit sont des comptes de resultat : ils portent le centre de cout de la societe
            lignes = [dict(l, cost_center=centre) if l["account"] not in (banque, attente) else l for l in decision["lignes"]]
            detail = (f"Remise carte (terminal de paiement) n° {doc.remise}, traitée par la banque le {_jour_fr(doc.date_traitement)}"
                      f" : {len(transactions)} paiement(s) par carte, brut {_nombre(doc.brut)}, crédité {_nombre(doc.montant_credite)}"
                      f" le {_jour_fr(doc.date_credit)}.\nPaiements : " + ", ".join(noms)
                      + (f"\nFrais retenus par la banque sur le crédit : {_nombre(decision['frais'])}" if decision["frais"] else
                         "\nLa commission est prélevée à part par la banque : elle est dans l’écriture mensuelle des frais BIAT."))
            acceptes = [t for t in transactions if ecart_est_accepte(t)]
            if acceptes:
                detail += "\nÉcart(s) accepté(s) : " + " ; ".join(
                    "%s — banque %s, ERPNext %s, %s par %s" % (t.get("paiement"), _nombre(t.get("montant")), _nombre(t.get("montant_erp")),
                                                              (t.get("ecart_motif") or "").strip(), t.get("ecart_par") or "?")
                    for t in acceptes)
            je = journal.build_journal_entry(COMPANY, jour, lignes, remark=f"{nom}\n{detail}", cheque_no=nom, cheque_date=jour)
            soumise = False
            if insert:
                je.insert(ignore_permissions=True)
                if journal._auto_submit_enabled():
                    je.submit()
                    soumise = True
                frappe.db.set_value(DOCTYPE, nom, {"ecriture": je.name, "comptabilisation": ""})
            out["ecritures"].append({"remise": nom, "je": je.name if insert else "(essai)", "soumise": soumise,
                                     "frais": decision["frais"], "montant": _m(doc.montant_credite)})
        except Exception as e:
            frappe.log_error(title="Cartes : écriture de la remise %s" % nom, message=frappe.get_traceback())
            out["erreurs"].append({"erreur": f"remise {nom} : {str(e)[:160]}"})
    return out


def accepter_ecart(remise: str, ligne: str, motif: str) -> dict:
    """Quelqu'un ACCEPTE l'ecart d'une transaction « Montant différent » : la banque n'a pas encaisse le montant du
    paiement saisi, et on ne corrigera pas le paiement. L'ecart ira en perte (banque < ERPNext) ou en produit
    (banque > ERPNext) dans l'ecriture de la remise, passee aussitot si la banque l'a deja creditee.
    Une transaction « Introuvable » ne s'accepte pas : aucun paiement n'a debite le compte d'attente.
    -> {remise, ecart, sens, controle, ecriture, raison}"""
    import frappe

    motif = (motif or "").strip()
    if len(motif) < MOTIF_MIN:
        frappe.throw("Dites en quelques mots pourquoi l’écart est accepté.")
    doc = frappe.get_doc(DOCTYPE, remise)
    if doc.get("ecriture"):
        frappe.throw(f"La remise {remise} est déjà comptabilisée ({doc.ecriture}).")
    t = next((x for x in doc.get("transactions") or [] if x.name == ligne), None)
    if not t:
        frappe.throw("Transaction introuvable sur cette remise.")
    if t.etat != MONTANT_DIFFERENT or not t.paiement:
        frappe.throw("Seul un écart de MONTANT s’accepte : cette transaction est « %s ». Sans paiement en face, "
                     "il faut d’abord saisir le paiement par carte." % (t.etat or "sans état"))
    actuel = frappe.db.get_value("Payment Entry", t.paiement, ["docstatus", "paid_amount"], as_dict=True)
    if not actuel or actuel.docstatus != 1:
        frappe.throw(f"Le paiement {t.paiement} n’est plus validé.")
    t.montant_erp = _m(actuel.paid_amount)
    ecart = ecart_de(t.as_dict())
    if abs(ecart) <= TOLERANCE:
        frappe.throw("Il n’y a plus d’écart : relancez la reconnaissance, la remise se comptabilisera seule.")
    comptes = comptes_ecart()
    sens = "perte" if ecart < 0 else "produit"
    if not comptes.get(sens):
        frappe.throw("La banque a encaissé MOINS que le paiement : réglez d’abord le compte de perte des écarts de carte "
                     "(Bank Retenue Sync Settings)." if sens == "perte" else
                     "La banque a encaissé PLUS que le paiement saisi. Aucun compte à créditer n’est réglé pour ce cas "
                     "(Bank Retenue Sync Settings) : corrigez plutôt le montant du paiement, ou réglez ce compte.")
    qui = frappe.utils.get_fullname(frappe.session.user)
    t.update({"ecart_accepte": 1, "ecart_montant": ecart, "ecart_motif": motif[:500], "ecart_par": qui[:140],
              "ecart_le": frappe.utils.now_datetime()})
    avis = verdict([x.as_dict() for x in doc.transactions],
                   [a for a in (doc.get("lecture_anomalies") or "").split("\n") if a.strip()])
    doc.controle, doc.a_signaler = avis["controle"], "\n".join(avis["a_signaler"])
    doc.flags.ignore_permissions = True
    doc.save()
    doc.add_comment("Info", "Écart accepté sur le paiement %s : %s à la banque, %s dans ERPNext → %s au %s de « %s ». Motif : %s — %s"
                    % (t.paiement, _nombre(t.montant), _nombre(t.montant_erp), _nombre(abs(ecart)),
                       "débit" if sens == "perte" else "crédit", comptes[sens], motif, qui))
    bilan = comptabiliser() if doc.etat == ETAT_CREDITEE else {"ecritures": [], "refus": {}}
    ecriture = next((e["je"] for e in bilan.get("ecritures") or [] if e["remise"] == remise), None)
    return {"remise": remise, "ecart": ecart, "sens": sens, "compte": comptes[sens], "controle": doc.controle,
            "ecriture": ecriture, "raison": (bilan.get("refus") or {}).get(remise) or
            ("" if ecriture else "la banque n’a pas encore crédité cette remise : l’écriture se passera à ce moment-là"
             if doc.etat != ETAT_CREDITEE else "")}


def annuler_ecart(remise: str, ligne: str) -> dict:
    """Revient sur un ecart accepte, tant que la remise n'est pas comptabilisee."""
    import frappe

    doc = frappe.get_doc(DOCTYPE, remise)
    if doc.get("ecriture"):
        frappe.throw(f"La remise {remise} est déjà comptabilisée ({doc.ecriture}) : annulez d’abord cette écriture.")
    t = next((x for x in doc.get("transactions") or [] if x.name == ligne), None)
    if not t or not int(t.get("ecart_accepte") or 0):
        frappe.throw("Aucun écart accepté sur cette transaction.")
    t.update({"ecart_accepte": 0, "ecart_montant": 0, "ecart_motif": "", "ecart_par": "", "ecart_le": None})
    avis = verdict([x.as_dict() for x in doc.transactions],
                   [a for a in (doc.get("lecture_anomalies") or "").split("\n") if a.strip()])
    doc.controle, doc.a_signaler = avis["controle"], "\n".join(avis["a_signaler"])
    doc.flags.ignore_permissions = True
    doc.save()
    doc.add_comment("Info", "Écart du paiement %s : acceptation annulée par %s" % (t.paiement, frappe.utils.get_fullname(frappe.session.user)))
    return {"remise": remise, "controle": doc.controle}


def paiements_sans_releve() -> list:
    """Les paiements saisis « par carte » que la banque n'a (pas encore) releves. Le releve arrive quelques
    jours apres l'achat : jeune, un tel paiement attend ; vieux, il est a signaler (saisi en carte, jamais
    passe au terminal ?). On ne remonte pas avant la premiere transaction connue du terminal."""
    import frappe

    debut = frappe.db.sql(f"select min(date_achat) from `tab{DOCTYPE_TRANSACTION}`")[0][0]
    if not debut:
        return []
    debut = max(debut, frappe.utils.getdate(_debut_suivi()))
    aujourd_hui = frappe.utils.getdate()
    lignes = frappe.db.sql(
        f"""select p.name, p.reference_no as reference, p.paid_amount as montant, p.posting_date as date,
                   ifnull(p.party_name, p.party) as client, p.mode_of_payment as mode, p.paid_to as compte
            from `tabPayment Entry` p
            where p.docstatus = 1 and p.payment_type = 'Receive' and p.posting_date between %s and %s
              and p.mode_of_payment like 'Carte%%'
              and not exists (select 1 from `tab{DOCTYPE_TRANSACTION}` t where t.paiement = p.name)
            order by p.posting_date""", (debut, aujourd_hui), as_dict=True)
    return [{"name": p.name, "date": str(p.date), "client": p.client, "montant": _m(p.montant), "reference": p.reference,
             "compte": p.compte, "jours": (aujourd_hui - p.date).days} for p in lignes if _est_carte(p.mode)]


def tableau(limite: int = 40) -> dict:
    """Ce que la page d'identification montre des cartes : les remises (les plus recentes d'abord) avec leurs
    transactions, les paiements pas encore au releve, et le resume. Lecture seule."""
    import frappe

    if not pret():
        return {"pret": False, "remises": [], "sans_releve": [], "resume": resume_remises([])}
    remises = frappe.db.get_all(
        DOCTYPE, filters={"banque": BANQUE}, order_by="date_traitement desc, remise desc", limit_page_length=limite,
        fields=["name", "remise", "terminal", "bordereau", "date_traitement", "date_telecollecte", "brut", "commission",
                "tva", "net", "nombre", "etat", "date_credit", "montant_credite", "mouvement", "controle", "a_signaler",
                "releve", "avis"] + (["ecriture", "comptabilisation", "frais_preleves", "taux_frais"]
                                     if frappe.db.has_column(DOCTYPE, "ecriture") else []))
    lignes = frappe.db.get_all(
        DOCTYPE_TRANSACTION, filters={"parent": ["in", [r.name for r in remises] or [""]]}, order_by="parent, idx",
        fields=["name", "parent", "date_achat", "montant", "commission", "carte", "autorisation", "etat", "paiement", "client",
                "montant_erp", "remarque"]
        + (["ecart_accepte", "ecart_montant", "ecart_motif", "ecart_par", "ecart_le"]
           if frappe.db.has_column(DOCTYPE_TRANSACTION, "ecart_accepte") else []), limit_page_length=0)
    par_remise = {}
    for l in lignes:
        par_remise.setdefault(l.parent, []).append(l)
    for r in remises:
        r["transactions"] = par_remise.get(r.name, [])
        for t in r["transactions"]:
            t["taux"] = taux(t.commission, t.montant)                 # commission annoncee / montant de la transaction
            t["ecart"] = ecart_de(t) if t.etat == MONTANT_DIFFERENT and t.paiement else 0.0
            t["ecart_accepte"] = 1 if ecart_est_accepte(t) else 0
        r["taux_annonce"] = taux(_m(r.commission) + _m(r.tva), r.brut)
        r["taux_commission"] = taux(r.commission, r.brut)
    attente = compte_attente()
    solde = None
    if attente:
        solde = _m(frappe.db.sql("select ifnull(sum(debit - credit), 0) from `tabGL Entry` where account = %s and is_cancelled = 0",
                                 (attente,))[0][0])
    return {"pret": True, "remises": remises, "sans_releve": paiements_sans_releve(), "resume": resume(),
            "compte_attente": attente, "solde_attente": solde,
            "comptes_ecart": comptes_ecart() if frappe.db.has_column(DOCTYPE_TRANSACTION, "ecart_accepte") else None}


def resume() -> dict:
    """Le resume seul (bandeau de la BIAT) : toutes les remises suivies, pas seulement celles que la page liste."""
    import frappe

    if not pret():
        return resume_remises([])
    remises = frappe.db.get_all(DOCTYPE, filters={"banque": BANQUE, "date_traitement": [">=", _debut_suivi()]},
                                fields=["name", "etat", "controle", "net", "brut"]
                                + (["ecriture", "comptabilisation"] if frappe.db.has_column(DOCTYPE, "ecriture") else []),
                                limit_page_length=0)
    return resume_remises(remises, paiements_sans_releve(), frappe.utils.getdate())
