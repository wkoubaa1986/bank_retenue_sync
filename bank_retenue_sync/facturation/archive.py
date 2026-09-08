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
relit alors le classeur « Liste des Charges <mois>.xlsx » qu'ils portent tous.

Le rapprochement s'y fait par (date, tiers, valeur TTC), en MULTI-ENSEMBLE — deux depenses du meme
jour, du meme tiers et du meme montant sont deux lignes, pas une. C'est moins sur qu'une cle : une
piece corrigee apres l'envoi n'a plus la meme empreinte et ressortira « manquante ». L'ecran doit
donc TOUJOURS dire laquelle des deux methodes a servi.

⚠️ LES COLONNES DU CLASSEUR SE RETROUVENT PAR LEUR INTITULE, JAMAIS PAR LEUR RANG. Le fichier a
change de colonnes d'une version a l'autre — « Référence » et « Type » en sont sortis — et la
neuvieme colonne d'un dossier ancien porte la TVA 19 %, pas le TTC. Lues par position, ses lignes
arrivaient avec une date qui est une reference et un montant qui est une TVA : aucune ne se
rapprochait, et le mois entier ressortait « non envoye ». Les positions actuelles ne servent plus
que de repli, et un classeur dont rien n'est reconnu se declare illisible plutot que vide.

⚠️ ET LE JUSTIFICATIF EST UN INDICE, PAS UN VERDICT. Le nom des fichiers joints est connu des deux
cotes — la colonne « Justificatifs » du classeur, les membres du ZIP sous « Dépenses/ » — et il ne
bouge pas quand un montant se corrige. La tentation est d'en faire une identite : elle n'en est
pas une. « scan.pdf » est le nom que donne un telephone, et deux ecritures distinctes en portent
chacune un ; s'y fier ferait declarer « envoyee » une charge jamais partie, et disparaitre celle
qui l'etait. Chaque ligne rendue porte donc `indices_piece` — les justificatifs qu'on retrouve de
l'autre cote — comme une piste a verifier a la main. Rien n'est retire de la liste sur cette foi.

⚠️ TOUT ICI EST PUR : NI FRAPPE, NI BASE. Le module recoit des octets et des listes de lignes deja
lues, il rend des listes. C'est ce qui le rend testable sans site — comme `retards.py`.
"""
from __future__ import annotations

import io
import json
import re
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

# « RETARDS DE … » ouvre un sous-bloc de pieces d'un AUTRE mois : elles voyagent avec ce dossier
# mais n'appartiennent pas au mois compare — les compter ferait ressortir tout un mois anterieur
# en « disparues ». C'est le seul intitule reconnu au libelle, et il n'a pas d'homonyme possible
# en tete de reference d'export ; les totaux, eux, se reconnaissent a leur absence de date.
PREFIXE_RETARDS = "RETARDS DE "

# Une date de comptabilisation, telle que le classeur l'ecrit : « 2026-07-05 ».
_RX_JOUR = re.compile(r"^\d{4}-\d{2}-\d{2}")

# La position des colonnes dans « Liste des Charges <mois>.xlsx », telle que `dossier`
# `_feuille_charges` les ecrit AUJOURD'HUI. Ce n'est qu'un repli : les colonnes sont d'abord
# cherchees par leur intitule, parce qu'un dossier de l'an dernier n'a pas les memes.
POSITIONS_PAR_DEFAUT = {"reference": 0, "date": 1, "tiers": 2, "categorie": 3, "ttc": 9,
                        "pieces": 11}

# Les intitules acceptes pour chaque colonne, du plus precis au plus vague : « Référence » a
# longtemps coexiste avec « Référence export », et c'est la seconde qu'on veut.
LIBELLES = {
    "reference": ("référence export", "reference export", "référence", "reference"),
    "date": ("date",),
    "tiers": ("tiers", "fournisseur"),
    "categorie": ("catégorie", "categorie"),
    "ttc": ("valeur ttc", "total ttc", "ttc"),
    "pieces": ("justificatifs", "justificatif", "pièces jointes", "pieces jointes"),
}

# Le sous-dossier ou la constitution range les justificatifs des charges, et le prefixe des
# sous-dossiers de retardataires — dont les pieces appartiennent a un AUTRE mois.
SOUS_DOSSIER_PIECES = "Dépenses"
PREFIXE_SOUS_RETARDS = "Retards "

# « facture.pdf » oui, « Exempté : note de frais » non. La colonne « Justificatifs » porte tantot
# des noms de fichiers, tantot le motif d'exemption ou « AUCUN » : seule une extension tranche.
_RX_FICHIER = re.compile(r"\.[A-Za-z0-9]{2,5}$")
SEPARATEUR_PIECES = "·"
MENTION_DEJA_REMISE = "DÉJÀ REMISE"


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


def noms_des_pieces(ligne: dict) -> list:
    """Les noms de fichier des justificatifs d'une ligne, d'ou qu'elle vienne. Fonction pure.

    ⚠️ C'EST LA CLE LA PLUS SURE DU REPLI. Une charge et sa ligne de classeur peuvent avoir des
    montants, des tiers ou des dates qui ont diverge depuis l'envoi ; le nom du fichier joint, lui,
    est le meme des deux cotes — et ce fichier est physiquement dans le ZIP.
    """
    if ligne.get("justificatifs"):
        return [_texte(j.get("file_name")) for j in ligne["justificatifs"]
                if _texte(j.get("file_name"))]
    return [_texte(p) for p in ligne.get("pieces") or () if _texte(p)]


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
        # Les noms des justificatifs voyagent avec l'entree : c'est sur eux que le repli
        # rapproche une charge de sa ligne de classeur, et du fichier present dans le ZIP.
        "pieces": noms_des_pieces(ligne),
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


def pieces_de_l_archive(octets: bytes) -> set:
    """Les noms des justificatifs PHYSIQUEMENT presents dans le ZIP, sous « Dépenses/ ».

    Ce que le dossier remis contient vraiment, au-dela de ce que son classeur raconte. Sert a
    poser les INDICES : une charge manquante dont le justificatif est dans l'archive merite une
    verification a la main. Jamais a conclure — un nom de fichier n'est pas une identite, et deux
    ecritures peuvent porter chacune leur « scan.pdf ».

    ⚠️ SAUF LES SOUS-DOSSIERS « Retards … ». Ils portent les pieces d'un mois ANTERIEUR rattachees
    a ce dossier : les prendre pour des pieces du mois designerait des indices trompeurs.

    Seule la liste des membres est lue — rien n'est decompresse.
    """
    with zipfile.ZipFile(io.BytesIO(octets)) as zf:
        noms = zf.namelist()

    out = set()
    for chemin in noms:
        segments = chemin.split("/")
        dossiers, fichier = segments[:-1], segments[-1]
        if not fichier or SOUS_DOSSIER_PIECES not in dossiers:
            continue
        if any(d.startswith(PREFIXE_SOUS_RETARDS) for d in dossiers):
            continue
        out.add(fichier)
    return out


def entrees_du_manifeste(donnees: dict | None) -> list[dict]:
    """Les charges DU MOIS d'un manifeste — jamais ses retards, qui sont d'autres mois."""
    return [entree(e) for e in (donnees or {}).get("charges") or []]


def lire_classeur_charges(octets: bytes) -> list[dict] | None:
    """Les lignes de charge du classeur « Liste des Charges … » d'un ZIP. -> None s'il est absent.

    ⚠️ ON NE LIT QUE LES BLOCS DU MOIS. Le classeur porte aussi les sous-blocs « RETARDS DE … »
    (des pieces d'un mois anterieur) et des lignes TOTAL / SOUS-TOTAL / TOTAL GÉNÉRAL, qui
    ressembleraient a des charges tres cheres. Les intitules de bloc, eux, se reconnaissent a leur
    colonne TTC vide.

    ⚠️ ET ON RETROUVE LES COLONNES PAR LEUR INTITULE. Le classeur a change de colonnes d'une
    version a l'autre — « Référence » et « Type » en sont sortis. Les lire par position rendait
    un dossier ancien entierement illisible : pas une seule valeur numerique la ou le TTC etait
    attendu, donc zero ligne lue, donc TOUT le mois annonce comme non envoye. -> None aussi quand
    aucun en-tete n'est reconnu et qu'aucune ligne n'est lue : mieux vaut dire qu'on ne sait pas
    lire ce classeur que de le declarer vide.
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


def _intitule(valeur) -> str:
    return " ".join(_texte(valeur).split()).casefold()


def positions_des_colonnes(cellules: list) -> dict | None:
    """Cette ligne est-elle l'en-tete du classeur ? -> ou lire chaque colonne, ou None.

    On exige la date ET le TTC : ce sont les deux colonnes sans lesquelles rien ne se compare, et
    les exiger ensemble evite de prendre pour un en-tete une ligne de charge dont le tiers
    s'appellerait « Date ». Fonction pure.
    """
    intitules = [_intitule(c) for c in cellules]
    trouve = {}
    for cle, acceptes in LIBELLES.items():
        for libelle in acceptes:
            if libelle in intitules:
                trouve[cle] = intitules.index(libelle)
                break
    if "date" not in trouve or "ttc" not in trouve:
        return None
    return trouve


def est_une_ligne_de_charge(valeur_date, valeur_ttc) -> bool:
    """Une charge, ou une ligne de synthese ? On tranche sur la STRUCTURE. Fonction pure.

    ⚠️ JAMAIS SUR LE PREFIXE « TOTAL ». La reference d'export d'un achat commence par le nom du
    fournisseur : « TotalEnergies FA-123 » est une facture de carburant, pas un sous-total. Ecarter
    sur le libelle la faisait disparaitre de la lecture du classeur — donc annoncer comme jamais
    envoyee une charge qui est dans le ZIP. Et il y a un « Total Assurance » derriere chaque
    « TotalEnergies ».

    Ce qui distingue vraiment une ligne de synthese, c'est qu'elle n'a PAS DE DATE : « TOTAL
    Dépenses » laisse la colonne vide, « TOTAL GÉNÉRAL » y ecrit « 12 ligne(s) », un intitule de
    bloc aussi. Une charge, elle, porte toujours sa date de comptabilisation.

    ⚠️ ET ON N'EXIGE PAS DE TIERS. Une ecriture de journal dont aucun compte n'est credite sort
    avec un tiers vide : l'exiger ecarterait une vraie charge, exactement le defaut qu'on corrige.
    La date et un TTC numerique suffisent, et ce sont les deux seules valeurs dont la comparaison
    a besoin.
    """
    if isinstance(valeur_ttc, bool) or not isinstance(valeur_ttc, (int, float)):
        return False
    if isinstance(valeur_date, (datetime, date)):
        return True
    return bool(_RX_JOUR.match(_texte(valeur_date)))


def _pieces_du_classeur(valeur) -> list:
    """« facture.pdf · avoir.pdf — DÉJÀ REMISE avec … » -> ['facture.pdf', 'avoir.pdf'].

    La colonne porte tantot des noms de fichiers, tantot le motif d'exemption, tantot « AUCUN » :
    seule une extension distingue un fichier d'une phrase. Fonction pure.
    """
    texte = _texte(valeur)
    if not texte:
        return []
    # La mention est introduite par un tiret cadratin — que la coupe laisse en fin de nom.
    texte = texte.split(MENTION_DEJA_REMISE)[0]
    return [nom for nom in (p.strip().strip("—").strip()
                            for p in texte.split(SEPARATEUR_PIECES))
            if nom and _RX_FICHIER.search(nom)]


def _lignes_de_la_feuille(rangs) -> list[dict] | None:
    """Le tri des rangs du classeur : intitules, totaux et retards ecartes. Fonction pure.

    -> None quand aucun en-tete n'a ete reconnu ET qu'aucune ligne n'a pu etre lue : le classeur
    n'est alors pas d'une forme qu'on sache lire, ce qui n'est pas la meme chose qu'un mois vide.
    """
    out, dans_les_retards, entete_vu = [], False, False
    positions = dict(POSITIONS_PAR_DEFAUT)
    for rang in rangs:
        cellules = list(rang or ())
        if not any(c not in (None, "") for c in cellules):
            continue

        def cellule(cle, defaut=None):
            i = positions.get(cle)
            return cellules[i] if i is not None and i < len(cellules) else defaut

        # Les intitules de bloc et les totaux sont toujours ecrits en premiere colonne, quelle que
        # soit la version du classeur : c'est la seule position sur laquelle on s'appuie.
        tete = _texte(cellules[0] if cellules else None).upper()
        if tete.startswith(PREFIXE_RETARDS):
            dans_les_retards = True
            continue

        trouvees = positions_des_colonnes(cellules)
        if trouvees:
            positions, entete_vu = trouvees, True
            continue

        ttc = cellule("ttc")
        if not est_une_ligne_de_charge(cellule("date"), ttc):
            # Un intitule de bloc (« DÉPENSES », « 12 ligne(s) ») ou une ligne de synthese : pas
            # une charge, et cela referme le sous-bloc des retards.
            if tete:
                dans_les_retards = False
            continue
        if dans_les_retards:
            continue
        out.append(entree({
            "reference": cellule("reference"),
            "date": cellule("date"),
            "tiers": cellule("tiers"),
            "categorie": cellule("categorie"),
            "ttc": ttc,
            "pieces": _pieces_du_classeur(cellule("pieces")),
        }))
    if not entete_vu and not out:
        return None
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


def _noms_des_pieces(entrees) -> set:
    """Tous les noms de justificatifs portes par un paquet d'entrees."""
    return {nom for e in entrees for nom in e.get("pieces") or ()}


def _avec_indices(entrees: list, ailleurs: set) -> list:
    """Annote chaque entree des justificatifs qu'on retrouve DE L'AUTRE COTE. Fonction pure.

    ⚠️ UN INDICE, PAS UN VERDICT. Un nom de fichier commun ne prouve pas que deux lignes sont la
    meme charge : « scan.pdf » est le nom que donne un telephone, et deux ecritures distinctes
    peuvent en porter chacune un. Croire le contraire ferait declarer « envoyee » une charge qui
    n'est jamais partie — et disparaitre du meme coup celle qui l'etait vraiment. Le rapprochement
    reste donc celui de l'empreinte (date, tiers, TTC) ; le justificatif ne fait que designer une
    ligne A VERIFIER a la main, et rien n'est retire de la liste sur cette seule foi.

    Les entrees sont rendues neuves : `entrees` n'est jamais modifie en place.
    """
    out = []
    for e in entrees:
        communs = sorted(nom for nom in e.get("pieces") or () if nom in ailleurs)
        out.append(dict(e, indices_piece=communs))
    return out


def comparer(lignes_mois: list, entrees_archive: list, methode: str,
             pieces_archive=None) -> dict:
    """Ce que l'archive n'a pas, et ce qu'elle a en trop. Fonction pure.

    · `lignes_mois` : les charges du mois d'AUJOURD'HUI, en entrees (`entrees_des_blocs`) ;
    · `entrees_archive` : ce que le ZIP porte, du manifeste ou du classeur ;
    · `methode` : `METHODE_MANIFESTE` (rapprochement par document, exact) ou `METHODE_EMPREINTE`
      (par date, tiers et montant, pour les archives d'avant le manifeste) ;
    · `pieces_archive` : les justificatifs physiquement presents dans le ZIP, s'ils ont ete lus.
      Ils n'entrent pas dans le verdict : ils enrichissent les INDICES.

    -> {methode, manquantes, disparues, totaux…}. « Manquante » ne veut pas dire « a rattraper » :
    une charge du mois partie avec le dossier d'un mois POSTERIEUR est absente d'ici et deja chez
    le comptable. C'est a l'appelant de le dire, il est le seul a lire les rattachements.

    Chaque ligne rendue porte `indices_piece` : les justificatifs qu'on retrouve de l'autre cote —
    dans l'archive pour une manquante, dans le mois pour une disparue. C'est une piste de
    verification, jamais une conclusion : le verdict reste celui de l'empreinte.
    """
    if methode == METHODE_MANIFESTE:
        manquantes, disparues = _par_cle(lignes_mois, entrees_archive)
    else:
        manquantes, disparues = _par_empreinte(lignes_mois, entrees_archive)

    # Ce qu'on retrouve de l'autre cote : les pieces nommees dans le classeur ou le manifeste, et
    # celles physiquement dans le ZIP — l'archive porte les deux, l'indice vaut pour les deux.
    cote_archive = _noms_des_pieces(entrees_archive) | set(pieces_archive or ())
    manquantes = _avec_indices(manquantes, cote_archive)
    disparues = _avec_indices(disparues, _noms_des_pieces(lignes_mois))

    return {
        "methode": methode,
        "manquantes": manquantes,
        "disparues": disparues,
        "totaux_manquantes": _totaux(manquantes),
        "totaux_disparues": _totaux(disparues),
        "nb_mois": len(lignes_mois),
        "nb_archive": len(entrees_archive),
        # Combien de manquantes portent un justificatif qu'on retrouve dans l'archive : autant de
        # lignes a verifier a la main avant de conclure qu'elles n'ont pas ete envoyees.
        "avec_indice_piece": sum(1 for e in manquantes if e["indices_piece"]),
    }
