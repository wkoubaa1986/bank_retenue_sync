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

CE MODULE NE COMPTABILISE RIEN. Il range (une fiche `BRS Remise TPE` par remise), controle (chaque
transaction a-t-elle son paiement ?) et reconnait (quelle ligne du releve paie quelle remise). Le
passage en comptabilite — compte d'attente, commission, TVA — est une decision de l'utilisateur.

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
        if propre:
            return propre.lstrip("0")
    return ""


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
        cle = autorisation_de(p.get("reference"))
        if cle and p.get("name") not in pris:
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
        elif t.get("etat") == MONTANT_DIFFERENT:
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

    `lignes`  : [{cle, date, montant}] — des credits que rien d'autre ne reconnait ;
    `remises` : [{name, remise, date_traitement, brut, commission, tva, net, nombre, mouvement}] (`net` nul
                tant que l'avis de credit n'est pas arrive : seul le brut se compare alors).
    -> {cle de ligne: {remises: [name], nature, brut, commission, tva, net, nombre, date_traitement}}

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
                "numeros": [str(r.get("remise") or "") for r in groupe]}

    resultat, prises = {}, set()
    par_cle = {l["cle"]: l for l in lignes}
    # 1. Les rattachements deja faits, tant que le montant de la ligne les confirme.
    deja = {}
    for r in remises:
        if r.get("mouvement") in par_cle:
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
    return detail + " — reconnue, pas encore comptabilisée"


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
    if sans_releve:
        morceaux.append(f"{len(sans_releve)} paiement(s) par carte pas encore au relevé de la banque"
                        + (f", dont {len(retard)} de plus de {DELAI_SANS_RELEVE} jours" if retard else ""))
    return {"nb": len(remises), "en_attente": {"nb": len(attente), "net": net}, "creditees": len(remises) - len(attente),
            "a_signaler": len(signaler),
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

    return frappe.db.get_all(
        DOCTYPE, filters={"banque": BANQUE, "date_traitement": [">=", _debut_suivi()]},
        fields=["name", "remise", "date_traitement", "brut", "commission", "tva", "net", "nombre", "mouvement"],
        limit_page_length=0)


def affectations(lignes: list) -> dict:
    """`affecter`, sur les remises du site. `lignes` : les credits BIAT que rien d'autre ne reconnait
    ([{cle, date, montant}]). {} tant que l'app n'est pas migree ou qu'aucune remise n'est arrivee."""
    if not lignes or not pret():
        return {}
    remises = _remises_pour_affectation()
    return affecter(lignes, remises) if remises else {}


def noter_credits(tpe: dict, lignes: list):
    """Reporte sur les fiches ce que le classement a reconnu : remise creditee (quelle ligne, quel jour), ou
    de nouveau en attente si la ligne a disparu du rattachement."""
    import frappe

    if not pret():
        return
    par_cle = {l.get("cle") or l.get("name"): l for l in lignes}
    creditees = {}
    for cle, fiche in (tpe or {}).items():
        for nom in fiche["remises"]:
            creditees[nom] = cle
    for nom, cle in creditees.items():
        ligne = par_cle.get(cle) or {}
        valeurs = {"etat": ETAT_CREDITEE, "mouvement": cle, "date_credit": ligne.get("date"),
                   "montant_credite": _m(ligne.get("credit") or ligne.get("montant"))}
        if frappe.db.get_value(DOCTYPE, nom, ["etat", "mouvement"]) != (ETAT_CREDITEE, cle):
            frappe.db.set_value(DOCTYPE, nom, valeurs)
    # Une remise notee creditee que le classement ne rattache plus (ligne ecartee, montant corrige) redevient
    # « en attente ». SEULEMENT parmi celles que le classement a regardees : une remise sortie de la fenetre de
    # suivi n'est plus comparee a rien, et garde donc l'etat qu'elle avait.
    for nom in frappe.db.get_all(DOCTYPE, filters={"etat": ETAT_CREDITEE, "banque": BANQUE,
                                                   "date_traitement": [">=", _debut_suivi()],
                                                   "name": ["not in", list(creditees) or [""]]}, pluck="name"):
        frappe.db.set_value(DOCTYPE, nom, {"etat": ETAT_ATTENTE, "mouvement": None, "date_credit": None, "montant_credite": 0})


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
                "releve", "avis"])
    lignes = frappe.db.get_all(
        DOCTYPE_TRANSACTION, filters={"parent": ["in", [r.name for r in remises] or [""]]}, order_by="parent, idx",
        fields=["parent", "date_achat", "montant", "commission", "carte", "autorisation", "etat", "paiement", "client",
                "montant_erp", "remarque"], limit_page_length=0)
    par_remise = {}
    for l in lignes:
        par_remise.setdefault(l.parent, []).append(l)
    for r in remises:
        r["transactions"] = par_remise.get(r.name, [])
    return {"pret": True, "remises": remises, "sans_releve": paiements_sans_releve(), "resume": resume()}


def resume() -> dict:
    """Le resume seul (bandeau de la BIAT) : toutes les remises suivies, pas seulement celles que la page liste."""
    import frappe

    if not pret():
        return resume_remises([])
    remises = frappe.db.get_all(DOCTYPE, filters={"banque": BANQUE, "date_traitement": [">=", _debut_suivi()]},
                                fields=["name", "etat", "controle", "net", "brut"], limit_page_length=0)
    return resume_remises(remises, paiements_sans_releve(), frappe.utils.getdate())
