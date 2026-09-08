"""Ce qui est DANS une archive remise, et ce qui n'y est pas : le manifeste, sa lecture, la
comparaison avec les charges du mois.

Un dossier est un GEL (voir `dossier.py`) : une fois le ZIP parti chez le comptable, plus rien ne
dit quelles pieces il contenait. Reconstituer le mois six semaines plus tard rend une autre liste
— des charges ont ete saisies depuis — et personne ne peut plus repondre a la seule question qui
compte : « qu'est-ce que le comptable n'a PAS recu ? »

D'ou le manifeste. Chaque constitution ecrit dans le ZIP un « <mois>/manifeste.json » qui nomme
les pieces embarquees par (document_type, document_name). Comparer un ZIP au mois d'aujourd'hui
devient alors une difference d'ensembles, exacte et sans ambiguite.

⚠️ ET UN REPLI POUR LES ARCHIVES D'AVANT. Les dossiers deja remis n'ont pas de manifeste : refuser
de les comparer, c'est refuser de repondre precisement pour les mois ou la question se pose. On
relit alors le classeur « Liste des Charges <mois>.xlsx » qu'ils portent tous, et on rapproche par
(date, tiers, valeur TTC) en MULTI-ENSEMBLE — deux depenses du meme jour, du meme tiers et du meme
montant sont deux lignes, pas une. C'est moins sur qu'une cle : une piece corrigee apres l'envoi
n'a plus la meme empreinte et ressortira « manquante ». L'ecran doit donc TOUJOURS dire laquelle
des deux methodes a servi.

⚠️ TOUT ICI EST PUR : NI FRAPPE, NI BASE. Le module recoit des octets et des listes de lignes deja
lues, il rend des listes. C'est ce qui le rend testable sans site — comme `retards.py`.
"""
from __future__ import annotations

import io
import json
import zipfile
from collections import Counter
from datetime import date, datetime

PRECISION = 3
VERSION = 1

NOM_MANIFESTE = "manifeste.json"
PREFIXE_CLASSEUR_CHARGES = "Liste des Charges"
PREFIXE_DOSSIER = "Dossier facturation "

# Pourquoi un manifeste ne peut PAS servir de reference, en cles stables — le module reste pur,
# c'est l'appelant qui les traduit. `None` veut dire « exploitable ».
DEFAUT_ABSENT = "absent"
DEFAUT_VERSION = "version"
DEFAUT_MOIS = "mois"
DEFAUT_CHARGES = "charges"
DEFAUT_PIECES = "pieces"

# Les deux facons de rapprocher une archive et un mois, en cles stables : le module reste pur,
# c'est l'ecran qui les traduit en phrase.
METHODE_MANIFESTE = "manifeste"
METHODE_EMPREINTE = "empreinte"

# Les intitules du classeur qui ne sont PAS des lignes de charge. « RETARDS DE … » ouvre un
# sous-bloc de pieces d'un AUTRE mois : elles voyagent avec ce dossier mais n'appartiennent pas
# au mois compare — les compter ferait ressortir tout un mois anterieur en « disparues ».
PREFIXE_RETARDS = "RETARDS DE "
PREFIXES_TOTAUX = ("TOTAL", "SOUS-TOTAL")

# La position des colonnes dans « Liste des Charges <mois>.xlsx », telle que `dossier`
# `_feuille_charges` les ecrit. Une colonne deplacee la-bas doit l'etre ici.
COL_REFERENCE = 0
COL_DATE = 1
COL_TIERS = 2
COL_CATEGORIE = 3
COL_TTC = 9


# ------------------------------------------------------------------ normalisation


def _texte(valeur) -> str:
    return str(valeur).strip() if valeur is not None else ""


def _jour(valeur) -> str:
    """« 2026-07-15 » quelle que soit la forme lue : chaine, date, ou datetime d'openpyxl."""
    if isinstance(valeur, (datetime, date)):
        return valeur.strftime("%Y-%m-%d")
    return _texte(valeur)[:10]


def _montant(valeur) -> float:
    try:
        return round(float(valeur or 0), PRECISION)
    except (TypeError, ValueError):
        return 0.0


def entree(ligne: dict, bloc: str = "", titre: str = "", mois_origine: str | None = None) -> dict:
    """Une ligne de charge reduite a ce qui identifie une piece remise. Fonction pure.

    C'est la MEME forme des deux cotes de la comparaison : ce qu'un manifeste porte, ce qu'un
    classeur rend, ce que les charges du mois deviennent. Une seule forme, donc une seule facon
    de comparer — et un manifeste qui reste lisible a l'oeil dans le ZIP.
    """
    e = {
        "document_type": _texte(ligne.get("document_type")),
        "document_name": _texte(ligne.get("document_name")),
        "bloc": bloc or _texte(ligne.get("bloc")),
        "bloc_titre": titre or _texte(ligne.get("bloc_titre")),
        "date": _jour(ligne.get("date")),
        "tiers": _texte(ligne.get("tiers")),
        "categorie": _texte(ligne.get("categorie")),
        "reference": _texte(ligne.get("reference_export") or ligne.get("reference")
                            or ligne.get("ref")),
        "ttc": _montant(ligne.get("ttc")),
    }
    if mois_origine:
        e["mois_origine"] = mois_origine
    return e


def entrees_des_blocs(donnees_charges: dict) -> list[dict]:
    """Les trois blocs de `charges.liste` aplatis en entrees comparables. Fonction pure."""
    out = []
    for bloc in donnees_charges.get("blocs") or []:
        for ligne in bloc.get("lignes") or []:
            out.append(entree(ligne, bloc.get("cle") or "", bloc.get("titre") or ""))
    return out


def entrees_des_retards(retardataires: list | None) -> list[dict]:
    """Les retardataires rattachees, groupees par mois d'origine, aplaties de meme."""
    out = []
    for groupe in retardataires or []:
        mois = groupe.get("mois") or ""
        for ligne in groupe.get("lignes") or []:
            out.append(entree(ligne, "retards", "Retards de %s" % (mois or "?"), mois))
    return out


# ------------------------------------------------------------------ le manifeste


def manifeste(mois: str, donnees_charges: dict, retardataires: list | None = None,
              genere_le=None) -> dict:
    """Le contenu de « <mois>/manifeste.json » : ce que cette archive embarque. Fonction pure.

    ⚠️ LES RETARDS SONT A PART, COMME PARTOUT AILLEURS. Une piece de juin partie avec le dossier
    d'aout est dans le ZIP d'aout mais n'est pas une charge d'aout : la fondre dans `charges`
    ferait ressortir tout juin en « disparue » a la premiere comparaison.
    """
    return {
        "version": VERSION,
        "mois": mois or "",
        "genere_le": _texte(genere_le),
        "charges": entrees_des_blocs(donnees_charges),
        "retards": entrees_des_retards(retardataires),
    }


def serialiser(donnees: dict) -> bytes:
    """Le manifeste en octets UTF-8, indente : il part chez le comptable, il doit se lire."""
    return json.dumps(donnees, ensure_ascii=False, indent=1, default=str).encode("utf-8")


def chemin_manifeste(mois: str) -> str:
    return "%s/%s" % (mois, NOM_MANIFESTE)


def est_archive_du_mois(nom_fichier, mois) -> bool:
    """« Dossier facturation 2026-07 (20260801-1030).zip » pour 2026-07, et rien d'autre.

    ⚠️ L'ESPACE APRES LE MOIS FAIT PARTIE DE LA REGLE. Sans elle, « Dossier facturation 2026-1 »
    laisserait passer le dossier de 2026-10 : deux mois qui n'ont rien a voir, dont l'un serait
    servi — ou supprime — au nom de l'autre. Fonction pure, pour qu'elle soit testee ailleurs que
    dans un site.
    """
    if not mois or not nom_fichier:
        return False
    return str(nom_fichier).startswith("%s%s " % (PREFIXE_DOSSIER, mois))


def defaut_du_manifeste(donnees, mois: str | None = None) -> str | None:
    """Ce manifeste peut-il servir de reference ? -> None si oui, la cle du defaut sinon.

    ⚠️ UN MANIFESTE INCOMPLET EST PIRE QU'UN MANIFESTE ABSENT. `{"version": 1}` se lit sans erreur
    et rend « zero charge dans l'archive » : TOUTES les charges du mois ressortent alors
    manquantes, et l'ecran envoie rattraper un dossier qui etait complet. Absent, au moins, on
    retombe sur le classeur. On exige donc que la structure soit entiere avant de s'y fier :

      · une version connue — un manifeste ecrit par une version plus recente ne se devine pas ;
      · le mois attendu — comparer le dossier de juillet a l'archive d'aout n'a aucun sens ;
      · une liste `charges` (vide est legitime : un mois peut n'avoir aucune charge) ;
      · et CHAQUE entree identifiee par son document — c'est la seule chose sur quoi la
        comparaison par piece s'appuie.

    Fonction pure.
    """
    if not isinstance(donnees, dict) or not donnees:
        return DEFAUT_ABSENT

    version = donnees.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= VERSION:
        return DEFAUT_VERSION

    porte = _texte(donnees.get("mois"))
    if not porte or (mois and porte != mois):
        return DEFAUT_MOIS

    charges = donnees.get("charges")
    if not isinstance(charges, list) or not isinstance(donnees.get("retards", []), list):
        return DEFAUT_CHARGES

    for e in charges:
        if not isinstance(e, dict) or not _texte(e.get("document_type")) \
                or not _texte(e.get("document_name")):
            return DEFAUT_PIECES
    return None


# ------------------------------------------------------------------ lecture d'une archive


def _membre(noms: list, predicat) -> str | None:
    for nom in noms:
        if predicat(nom):
            return nom
    return None


def lire_manifeste(octets: bytes) -> dict | None:
    """Le manifeste d'un ZIP en memoire, ou None s'il n'en porte pas (archive d'avant).

    Seul le membre JSON est decompresse : l'archive entiere pese jusqu'a une trentaine de Mo.
    Un manifeste illisible vaut un manifeste absent — on retombera sur le classeur.

    ⚠️ CETTE FONCTION NE JUGE PAS CE QU'ELLE LIT. Elle rend le JSON tel quel ; c'est
    `defaut_du_manifeste` qui dit s'il est exploitable, et l'appelant qui decide alors de retomber
    sur le classeur. Un `{"version": 1}` passe donc ici sans bruit — et se fait refuser la.
    """
    with zipfile.ZipFile(io.BytesIO(octets)) as zf:
        nom = _membre(zf.namelist(),
                      lambda n: n.rsplit("/", 1)[-1] == NOM_MANIFESTE)
        if not nom:
            return None
        try:
            return json.loads(zf.read(nom).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


def entrees_du_manifeste(donnees: dict | None) -> list[dict]:
    """Les charges DU MOIS d'un manifeste — jamais ses retards, qui sont d'autres mois."""
    return [entree(e) for e in (donnees or {}).get("charges") or []]


def lire_classeur_charges(octets: bytes) -> list[dict] | None:
    """Les lignes de charge du classeur « Liste des Charges … » d'un ZIP. -> None s'il est absent.

    ⚠️ ON NE LIT QUE LES BLOCS DU MOIS. Le classeur porte aussi les sous-blocs « RETARDS DE … »
    (des pieces d'un mois anterieur) et des lignes TOTAL / SOUS-TOTAL / TOTAL GÉNÉRAL, qui
    ressembleraient a des charges tres cheres. Les intitules de bloc, eux, se reconnaissent a leur
    colonne TTC vide.
    """
    import openpyxl

    with zipfile.ZipFile(io.BytesIO(octets)) as zf:
        nom = _membre(zf.namelist(),
                      lambda n: n.endswith(".xlsx")
                      and n.rsplit("/", 1)[-1].startswith(PREFIXE_CLASSEUR_CHARGES))
        if not nom:
            return None
        brut = zf.read(nom)

    wb = openpyxl.load_workbook(io.BytesIO(brut), read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        return _lignes_de_la_feuille(ws.iter_rows(values_only=True))
    finally:
        wb.close()


def _lignes_de_la_feuille(rangs) -> list[dict]:
    """Le tri des rangs du classeur : intitules, totaux et retards ecartes. Fonction pure."""
    out, dans_les_retards = [], False
    for rang in rangs:
        cellules = list(rang or ())
        if not any(c not in (None, "") for c in cellules):
            continue

        def cellule(i):
            return cellules[i] if i < len(cellules) else None

        tete = _texte(cellule(COL_REFERENCE)).upper()
        if tete.startswith(PREFIXE_RETARDS):
            dans_les_retards = True
            continue
        if tete.startswith(PREFIXES_TOTAUX):
            continue
        ttc = cellule(COL_TTC)
        if not isinstance(ttc, (int, float)):
            # Un intitule de bloc (« DÉPENSES », « 12 ligne(s) ») ou la ligne d'en-tete : pas une
            # charge. Un intitule referme les retards, un en-tete ne change rien.
            if tete and tete != "RÉFÉRENCE EXPORT":
                dans_les_retards = False
            continue
        if dans_les_retards:
            continue
        out.append(entree({
            "reference": cellule(COL_REFERENCE),
            "date": cellule(COL_DATE),
            "tiers": cellule(COL_TIERS),
            "categorie": cellule(COL_CATEGORIE),
            "ttc": ttc,
        }))
    return out


# ------------------------------------------------------------------ comparaison


def cle_piece(e: dict) -> tuple:
    return (e.get("document_type") or "", e.get("document_name") or "")


def empreinte(e: dict) -> tuple:
    """(date, tiers, TTC) — l'identite d'une ligne quand on n'a pas son nom de document.

    Le tiers est reduit a sa casse et a ses espaces : le classeur le porte tel qu'ecrit, et
    « Sté ALPHA  » ne doit pas differer de « Sté ALPHA ».
    """
    return (_jour(e.get("date")),
            " ".join(_texte(e.get("tiers")).split()).casefold(),
            _montant(e.get("ttc")))


def _totaux(entrees: list) -> dict:
    return {"nombre": len(entrees),
            "ttc": round(sum(_montant(e.get("ttc")) for e in entrees), PRECISION)}


def _par_cle(lignes_mois: list, entrees_archive: list) -> tuple:
    cles_archive = {cle_piece(e) for e in entrees_archive if e.get("document_name")}
    cles_mois = {cle_piece(e) for e in lignes_mois if e.get("document_name")}
    manquantes = [e for e in lignes_mois if cle_piece(e) not in cles_archive]
    disparues = [e for e in entrees_archive if cle_piece(e) not in cles_mois]
    return manquantes, disparues


def _par_empreinte(lignes_mois: list, entrees_archive: list) -> tuple:
    """Rapprochement en MULTI-ENSEMBLE : trois lignes identiques cote mois et deux cote archive
    laissent UNE manquante, pas zero."""
    def difference(gauche, droite):
        reste = Counter(empreinte(e) for e in droite)
        out = []
        for e in gauche:
            k = empreinte(e)
            if reste.get(k):
                reste[k] -= 1
            else:
                out.append(e)
        return out

    return difference(lignes_mois, entrees_archive), difference(entrees_archive, lignes_mois)


def comparer(lignes_mois: list, entrees_archive: list, methode: str) -> dict:
    """Ce que l'archive n'a pas, et ce qu'elle a en trop. Fonction pure.

    · `lignes_mois` : les charges du mois d'AUJOURD'HUI, en entrees (`entrees_des_blocs`) ;
    · `entrees_archive` : ce que le ZIP porte, du manifeste ou du classeur ;
    · `methode` : `METHODE_MANIFESTE` (rapprochement par piece) ou `METHODE_EMPREINTE`
      (par date, tiers et montant, pour les archives d'avant le manifeste).

    -> {methode, manquantes, disparues, totaux…}. « Manquante » ne veut pas dire « a rattraper » :
    une charge du mois partie avec le dossier d'un mois POSTERIEUR est absente d'ici et deja chez
    le comptable. C'est a l'appelant de le dire, il est le seul a lire les rattachements.
    """
    if methode == METHODE_MANIFESTE:
        manquantes, disparues = _par_cle(lignes_mois, entrees_archive)
    else:
        manquantes, disparues = _par_empreinte(lignes_mois, entrees_archive)
    return {
        "methode": methode,
        "manquantes": manquantes,
        "disparues": disparues,
        "totaux_manquantes": _totaux(manquantes),
        "totaux_disparues": _totaux(disparues),
        "nb_mois": len(lignes_mois),
        "nb_archive": len(entrees_archive),
    }
