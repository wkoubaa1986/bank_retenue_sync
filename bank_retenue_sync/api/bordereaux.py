"""Collecte des bordereaux de remise (DocType `BRS Bordereau Remise`), depuis la caisse ou la liste.

Le bordereau est d'abord RANGE : banque, numero, date, nature, scan (une remise BIAT se reconnait a son
numero de bordereau, cf. `bank/biat.py`).

Puis, en arriere-plan et sans rien bloquer (demande du 16/11/2026 : « est-ce que l'IA peut aider a
identifier les cheques ou les traites, les lister et verifier s'ils existent dans l'ERP, a partir de la
caisse, sans bloquer »), l'IA lit le scan (`ai/bordereau_extract`), chaque piece lue est cherchee parmi les
paiements d'ERPNext (`rapprocher`) et le resultat est pose sur le bordereau. C'est un CONTROLE : rien n'est
comptabilise, rien n'empeche de collecter — une lecture ratee laisse le bordereau collecte.

Depuis la caisse, on ne remplit plus rien (demande du meme jour : « lorsque je charge le fichier ou prends
une photo, toutes les infos se font, c'est plus naturel ») : le scan est DEPOSE (`deposer`), l'IA y lit
aussi la banque, le numero, la date et la nature, et le bordereau est collecte tout seul
(`collecter_par_ia`). La saisie a la main reste le repli quand l'IA ne peut pas l'identifier.
"""
from __future__ import annotations

import json
import re
import unicodedata

import frappe

DOCTYPE = "BRS Bordereau Remise"
BANQUES = ("BIAT", "Zitouna")
NATURES = ("Chèques", "Traites", "Espèces")
#: Les natures dont le bordereau liste des pieces a retrouver (un versement d'especes n'en a pas).
NATURES_LUES = ("Chèques", "Traites")

LECTURE_EN_ATTENTE, LECTURE_LUE, LECTURE_ECHEC = "En attente", "Lue", "Échec"
ETAT_TROUVE, ETAT_A_VERIFIER, ETAT_INTROUVABLE = "Trouvé", "À vérifier", "Introuvable"
TOLERANCE = 0.0005
#: Ecart de montant TOLERE entre le bordereau et le paiement d'une meme piece : en dessous, c'est un arrondi
#: (cheque fait sur le total exact, paiement saisi sur le total arrondi : 0,247 sur le bordereau BIAT
#: 7473685), pas quelque chose a signaler. Meme seuil que `encaissement.matching.TOLERANCE_PAIEMENT`.
ECART_TOLERE = 1.0
#: Ecart de montant au-dela duquel deux numeros voisins ne sont plus « la meme piece mal lue ».
ECART_VOISIN = 5.0


# ---------------------------------------------------------------------------
# Pur
# ---------------------------------------------------------------------------

def numero_propre(numero) -> str:
    """Le numero du bordereau, chiffres seulement (« 7 473 685 » -> « 7473685 »). Leve s'il n'en a pas."""
    chiffres = "".join(c for c in str(numero or "") if c.isdigit())
    if not 4 <= len(chiffres) <= 14:
        raise ValueError("Le numéro du bordereau est une suite de 4 à 14 chiffres.")
    return chiffres


def nom_du_bordereau(banque: str, numero: str) -> str:
    if banque not in BANQUES:
        raise ValueError("Banque inconnue : choisissez BIAT ou Zitouna.")
    return f"BR-{banque}-{numero_propre(numero)}"


def lire_nom_fichier(nom: str) -> dict:
    """Ce que dit le nom d'un scan range a la facon de l'entreprise — « Ordre de versement Chèques
    Bq BIAT-7473685-du 09-11-2026.pdf » -> {banque, nature, numero, date_remise (YYYY-MM-DD)}. Une cle
    n'apparait que si elle est lue ; un nom quelconque rend {}. Le suffixe que Frappe colle au nom
    (« …-du 20-10-20267c37c5.pdf ») ne gene pas la lecture de la date."""
    brut = unicodedata.normalize("NFKD", str(nom or "")).encode("ascii", "ignore").decode()
    bas = brut.lower()
    out = {}
    if "biat" in bas:
        out["banque"] = "BIAT"
    elif "zitouna" in bas or "ezzitouna" in bas:
        out["banque"] = "Zitouna"
    for mot, nature in (("cheque", "Chèques"), ("traite", "Traites"), ("espece", "Espèces")):
        if mot in bas:
            out["nature"] = nature
            break
    date = re.search(r"du\s*(\d{2})[-./](\d{2})[-./](\d{4})", bas)
    if date and 1 <= int(date.group(2)) <= 12 and 1 <= int(date.group(1)) <= 31:
        out["date_remise"] = f"{date.group(3)}-{date.group(2)}-{date.group(1)}"
    # Le numero : le groupe de chiffres qui precede « -du », sinon le premier groupe long hors de la date.
    avant = re.search(r"(\d{4,14})\s*-\s*du\b", bas)
    if avant:
        out["numero"] = avant.group(1)
    else:
        sans_date = bas[:date.start()] if date else bas
        libre = re.findall(r"(?<!\d)\d{5,14}(?!\d)", sans_date)
        if libre:
            out["numero"] = libre[-1]
    return out


def _chiffres(numero) -> str:
    """Le numero d'une piece, comparable : chiffres seuls, sans zeros de tete (« 0001259 » = « 1259 »)."""
    return "".join(c for c in str(numero or "") if c.isdigit()).lstrip("0")


def numeros_voisins(a, b) -> bool:
    """Deux numeros qui ne different que d'UN chiffre — change, en trop ou en moins. C'est l'ecart que
    produit une ecriture manuscrite (« 4001764 » sur le papier, « 4001767 » saisi) ou une lecture
    hesitante. Les zeros de tete ne comptent pas (« 0001259 » et « 1250 » sont voisins) ; deux numeros
    courts (« 123 », « 124 ») ne sont pas rapproches ainsi : un chiffre y pese trop."""
    brut_a, brut_b = ("".join(c for c in str(n or "") if c.isdigit()) for n in (a, b))
    a, b = brut_a.lstrip("0"), brut_b.lstrip("0")
    if not a or not b or a == b or max(len(brut_a), len(brut_b)) < 5:
        return False
    longueur = max(len(a), len(b))
    if sum(x != y for x, y in zip(a.zfill(longueur), b.zfill(longueur))) == 1:
        return True
    if abs(len(a) - len(b)) != 1 or min(len(a), len(b)) < 5:
        return False
    court, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == court for i in range(len(long_)))


def _dt(montant) -> str:
    """« 1 695,127 » — pour les remarques lues a l'ecran."""
    entier, _, millimes = f"{float(montant or 0):,.3f}".replace(",", " ").partition(".")
    return f"{entier},{millimes}"


def _egaux(a, b) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= TOLERANCE


def rapprocher(pieces: list, portefeuille: list, ailleurs: list = ()) -> list:
    """Cherche chaque piece LUE sur le bordereau parmi les paiements d'ERPNext. Pur.

    `pieces` : [{numero, montant, …}] (ce que l'IA a lu, ou ce qu'un humain a corrige).
    `portefeuille` : les paiements encore en attente de remise — [{name, numero, montant, client,
    en_encaissement?}]. `ailleurs` : les paiements au meme numero deja sortis du portefeuille —
    [{name, numero, montant, client, compte}].
    -> les memes pieces, completees de {etat, paiement, client, montant_erp, remarque}.

    L'ordre des passes compte : tout ce qui est SUR (numero exact) est servi avant ce qui est probable,
    pour qu'un rapprochement approche ne prenne pas le paiement d'une autre ligne.
      1. meme numero — « Trouvé » si le montant est le meme ou a moins de `ECART_TOLERE` (un arrondi : il
         est note, pas signale), « À vérifier » sinon (l'ecart est montre) ;
      2. numero different d'un seul chiffre ET meme montant — « Trouvé », la difference est dite ;
      3. un seul paiement du portefeuille a exactement ce montant — « À vérifier » ;
      4. numero voisin, montant proche — « À vérifier » ;
      5. meme numero hors du portefeuille (deja en banque, impaye…) — le compte est dit ;
      sinon « Introuvable ».
    Un paiement ne sert qu'une fois."""
    libres = {c["name"]: c for c in portefeuille or []}
    out = [dict(p, etat=None, paiement=None, client=None, montant_erp=None, remarque="") for p in pieces or []]

    def prendre(piece, c, etat, remarque=""):
        suite = " — déjà porté sur un encaissement" if c.get("en_encaissement") else ""
        piece.update(etat=etat, paiement=c["name"], client=c.get("client"),
                     montant_erp=round(float(c.get("montant") or 0), 3), remarque=(remarque + suite).strip(" —"))
        libres.pop(c["name"], None)

    def ecart(piece, c) -> str:
        return (f"lu {_dt(piece['montant'])}, ERPNext {_dt(c.get('montant'))} "
                f"(écart {_dt(abs(float(piece['montant']) - float(c.get('montant') or 0)))})")

    # 1. Le meme numero.
    for piece in out:
        numero = _chiffres(piece.get("numero"))
        memes = [c for c in libres.values() if numero and _chiffres(c.get("numero")) == numero]
        if not memes:
            continue
        exacts = [c for c in memes if _egaux(piece.get("montant"), c.get("montant"))]
        if exacts:
            prendre(piece, exacts[0], ETAT_TROUVE)
        elif len(memes) > 1:
            piece.update(etat=ETAT_A_VERIFIER, remarque="plusieurs paiements portent ce numéro ("
                         + ", ".join(c["name"] for c in memes) + "), aucun au montant lu")
        elif piece.get("montant") is None:
            prendre(piece, memes[0], ETAT_TROUVE, "montant non lu sur le bordereau")
        elif abs(float(piece["montant"]) - float(memes[0].get("montant") or 0)) <= ECART_TOLERE:
            prendre(piece, memes[0], ETAT_TROUVE, "petit écart de montant, toléré : " + ecart(piece, memes[0]))
        else:
            prendre(piece, memes[0], ETAT_A_VERIFIER, "montant différent : " + ecart(piece, memes[0]))

    # 2. Un chiffre d'ecart sur le numero, le meme montant.
    for piece in out:
        if piece["etat"]:
            continue
        voisins = [c for c in libres.values() if numeros_voisins(piece.get("numero"), c.get("numero"))
                   and _egaux(piece.get("montant"), c.get("montant"))]
        if len(voisins) == 1:
            prendre(piece, voisins[0], ETAT_TROUVE,
                    f"même montant, numéro différent d’un chiffre : lu {piece.get('numero')}, "
                    f"ERPNext {voisins[0].get('numero')}")

    # 3. Le seul paiement du portefeuille a ce montant.
    for piece in out:
        if piece["etat"] or piece.get("montant") is None:
            continue
        memes = [c for c in libres.values() if _egaux(piece.get("montant"), c.get("montant"))]
        if len(memes) == 1:
            prendre(piece, memes[0], ETAT_A_VERIFIER,
                    f"aucun paiement ne porte le n° {piece.get('numero') or '?'} ; un seul du portefeuille a ce "
                    f"montant : n° {memes[0].get('numero') or '?'}")
        elif len(memes) > 1:
            piece.update(etat=ETAT_A_VERIFIER, remarque=f"aucun paiement ne porte le n° {piece.get('numero') or '?'} ; "
                         "plusieurs ont ce montant : " + ", ".join(f"n° {c.get('numero') or '?'}" for c in memes[:4]))

    # 4. Numero voisin, montant proche.
    for piece in out:
        if piece["etat"] or piece.get("montant") is None:
            continue
        voisins = [c for c in libres.values() if numeros_voisins(piece.get("numero"), c.get("numero"))
                   and abs(float(piece["montant"]) - float(c.get("montant") or 0)) <= ECART_VOISIN]
        if len(voisins) == 1:
            prendre(piece, voisins[0], ETAT_A_VERIFIER,
                    f"numéro et montant proches : lu {piece.get('numero')}, ERPNext {voisins[0].get('numero')} ; "
                    + ecart(piece, voisins[0]))

    # 5. Le meme numero, mais plus dans le portefeuille.
    pris = {p["paiement"] for p in out if p["paiement"]}
    for piece in out:
        if piece["etat"]:
            continue
        numero = _chiffres(piece.get("numero"))
        memes = [c for c in ailleurs or [] if numero and _chiffres(c.get("numero")) == numero and c["name"] not in pris]
        memes.sort(key=lambda c: not _egaux(piece.get("montant"), c.get("montant")))
        if memes:
            c = memes[0]
            juste = piece.get("montant") is None \
                or abs(float(piece["montant"]) - float(c.get("montant") or 0)) <= ECART_TOLERE
            piece.update(etat=ETAT_TROUVE if juste else ETAT_A_VERIFIER, paiement=c["name"], client=c.get("client"),
                         montant_erp=round(float(c.get("montant") or 0), 3),
                         remarque=f"plus dans le portefeuille : paiement sur « {c.get('compte') or '?'} »"
                         + ("" if juste else " ; montant différent : " + ecart(piece, c)))
            pris.add(c["name"])
        else:
            piece.update(etat=ETAT_INTROUVABLE, remarque="aucun paiement à ce numéro, ni un seul à ce montant")
    return out


def identite(indices: dict, lu: dict, aujourd_hui: str) -> dict:
    """De quoi nommer le bordereau depose, sans rien demander : banque, numero, nature, date. Pur.

    `indices` : ce que dit le nom du fichier (`lire_nom_fichier`) ; `lu` : ce que l'IA a lu sur le scan.
    Le nom du fichier n'est cru que s'il NOMME LA BANQUE, comme le fait la facon de ranger de l'entreprise
    (« … Bq BIAT-7473685-du 09-11-2026.pdf ») : une photo de telephone s'appelle « IMG_20261116_101500.jpg »,
    et ses chiffres ne sont pas un numero de bordereau.
    -> {banque, numero, nature, date_remise, date_source (« nom », « scan » ou « jour »), manque: [champs],
        conflit: str|None}"""
    indices = indices if (indices or {}).get("banque") else {}
    lu = lu or {}
    out = {"banque": indices.get("banque") or lu.get("banque_remise"), "numero": None,
           "nature": indices.get("nature") or lu.get("nature") or "Chèques", "manque": [], "conflit": None}
    du_nom, du_scan = _chiffres(indices.get("numero")), _chiffres(lu.get("numero"))
    if du_nom and du_scan and du_nom != du_scan:
        # Deux sources, deux numeros : on ne choisit pas a la place de la personne.
        out["conflit"] = (f"Le nom du fichier dit n° {indices['numero']}, le numéro imprimé lu sur le scan est "
                          f"{lu['numero']} : indiquez le bon.")
        out["numero"] = indices.get("numero")
        out["manque"].append("numero")
    else:
        try:
            out["numero"] = numero_propre(indices.get("numero") or lu.get("numero"))
        except ValueError:
            out["manque"].append("numero")
    if out["banque"] not in BANQUES:
        out["banque"] = None
        out["manque"].append("banque")
    # La date : celle du nom du fichier ; sinon celle lue, si elle est vraisemblable (une remise est
    # recente, et l'annee manuscrite se lit mal — « 2026 » lu « 2016 ») ; sinon aujourd'hui.
    out["date_remise"], out["date_source"] = indices.get("date_remise"), "nom"
    if not out["date_remise"]:
        lue = lu.get("date_remise")
        if lue and lue <= aujourd_hui and _jours_entre(lue, aujourd_hui) <= 60:
            out["date_remise"], out["date_source"] = lue, "scan"
        else:
            out["date_remise"], out["date_source"] = aujourd_hui, "jour"
    return out


def _jours_entre(a: str, b: str) -> int:
    from datetime import date

    return abs((date.fromisoformat(b) - date.fromisoformat(a)).days)


def message_d_echec(erreur: Exception) -> str:
    """Ce qu'on ecrit sur le bordereau quand la lecture echoue : une phrase pour la personne a la caisse,
    pas une trace technique (celle-ci va au journal des erreurs). Pur."""
    nom = type(erreur).__name__
    texte = re.sub(r"<[^>]+>", "", str(erreur)).strip()
    if isinstance(erreur, (FileNotFoundError, IsADirectoryError)):
        return "Le scan joint est introuvable sur le serveur : joignez-le à nouveau sur la fiche du bordereau."
    if isinstance(erreur, json.JSONDecodeError):
        return "L’IA n’a pas rendu une réponse exploitable : relancez la lecture."
    if "Authentication" in nom or "PermissionDenied" in nom:
        return "La clé OpenAI est refusée (AI Settings) : prévenez l’administrateur."
    if any(mot in nom for mot in ("Timeout", "Connection", "RateLimit", "InternalServer")):
        return "Le service d’IA n’a pas répondu : relancez la lecture dans un moment."
    return (texte or nom)[:400]


def bilan(pieces: list, lecture: dict = None, saisi: dict = None, nature: str = "Chèques") -> dict:
    """Le resultat en une ligne (`resume`) et ce qu'il faut regarder (`remarques`). Pur.
    `lecture` : {numero, total, nombre} lus sur le scan ; `saisi` : {numero, montant_total, nombre} de la
    fiche. -> {resume, remarques: [str], signalements: [str], trouves, a_verifier, introuvables}

    Deux lecteurs, deux listes. `signalements` s'adresse a la PERSONNE QUI REMET LE BORDEREAU (demande du
    16/11/2026 : « il ne faut pas l'embrouiller, l'idee est de dire tout est bon ou pas ») : vide, tout est
    bon ; sinon une phrase simple par chose a signaler. `remarques` garde le detail pour qui controle
    (ecart de total, numero lu different, doute de l'IA)."""
    lecture, saisi = lecture or {}, saisi or {}
    mot = "traite" if nature == "Traites" else "chèque"
    feminin = nature == "Traites"
    n = len(pieces)
    compte = {etat: sum(1 for p in pieces if p.get("etat") == etat)
              for etat in (ETAT_TROUVE, ETAT_A_VERIFIER, ETAT_INTROUVABLE)}
    if not n:
        resume = "aucune pièce lue sur le scan"
    elif compte[ETAT_TROUVE] == n:
        resume = (f"1 {mot} : " + ("trouvée" if feminin else "trouvé")) if n == 1 else \
                 (f"{n} {mot}s : " + ("toutes trouvées" if feminin else "tous trouvés"))
    else:
        parts = []
        if compte[ETAT_TROUVE]:
            parts.append(f"{compte[ETAT_TROUVE]} " + ("trouvée" if feminin else "trouvé") + ("s" if compte[ETAT_TROUVE] > 1 else ""))
        if compte[ETAT_A_VERIFIER]:
            parts.append(f"{compte[ETAT_A_VERIFIER]} à vérifier")
        if compte[ETAT_INTROUVABLE]:
            parts.append(f"{compte[ETAT_INTROUVABLE]} introuvable" + ("s" if compte[ETAT_INTROUVABLE] > 1 else ""))
        resume = f"{n} {mot}{'s' if n > 1 else ''} : " + ", ".join(parts)

    remarques = []
    somme = round(sum(float(p.get("montant") or 0) for p in pieces), 3)
    total = lecture.get("total")
    if n and total is not None and not _egaux(somme, total):
        remarques.append(f"La somme des montants lus ({_dt(somme)}) ne tombe pas sur le total écrit sur le bordereau "
                         f"({_dt(total)}) : un montant est mal lu — corrigez-le dans la fiche du bordereau.")
    if lecture.get("nombre") is not None and int(lecture["nombre"]) != n:
        remarques.append(f"Le bordereau annonce {int(lecture['nombre'])} pièce(s), {n} lue(s).")
    if lecture.get("numero") and saisi.get("numero") and _chiffres(lecture["numero"]) != _chiffres(saisi["numero"]):
        remarques.append(f"Le n° imprimé lu sur le scan ({lecture['numero']}) n’est pas celui saisi "
                         f"({saisi['numero']}) : est-ce le bon scan ?")
    if total is not None and saisi.get("montant_total") and not _egaux(total, saisi["montant_total"]):
        remarques.append(f"Montant total saisi : {_dt(saisi['montant_total'])} ; lu sur le scan : {_dt(total)}.")
    if n and all(p.get("paiement") for p in pieces):
        erp = round(sum(float(p.get("montant_erp") or 0) for p in pieces), 3)
        reference = total if total is not None else somme
        if not _egaux(erp, reference):
            remarques.append(f"Ces paiements font {_dt(erp)} dans ERPNext, le bordereau {_dt(reference)} : "
                             f"écart de {_dt(abs(erp - reference))}.")
    if any(p.get("doute") and p.get("etat") == ETAT_INTROUVABLE for p in pieces):
        # Une ligne « à vérifier » dit deja son ecart ; c'est pour l'introuvable qu'un chiffre mal lu est la piste.
        remarques.append("L’IA hésite sur un chiffre d’une ligne introuvable : comparez avec le scan, corrigez sur la fiche.")
    signalements = []
    for p in pieces:
        numero = f"{mot.capitalize()} n° {p.get('numero') or '?'}"
        if p.get("etat") == ETAT_INTROUVABLE:
            signalements.append(numero + (f" ({_dt(p['montant'])})" if p.get("montant") is not None else "")
                                + " : introuvable dans ERPNext.")
        elif p.get("etat") == ETAT_A_VERIFIER:
            if p.get("paiement") and p.get("montant") is not None and p.get("montant_erp") is not None \
                    and not _egaux(p["montant"], p["montant_erp"]) and "numéro" not in (p.get("remarque") or ""):
                signalements.append(f"{numero} : {_dt(p['montant'])} sur le bordereau, {_dt(p['montant_erp'])} dans ERPNext.")
            else:
                signalements.append(f"{numero} : à vérifier ({p.get('remarque') or 'rapprochement incertain'}).")
    if not n:
        signalements.append("Aucune pièce lue sur ce bordereau.")
    elif total is not None and not _egaux(somme, total):
        signalements.append(f"Le total écrit sur le bordereau ({_dt(total)}) ne correspond pas à la somme des "
                            f"montants ({_dt(somme)}).")
    if n and lecture.get("nombre") is not None and int(lecture["nombre"]) != n:
        signalements.append(f"Le bordereau annonce {int(lecture['nombre'])} pièce(s), {n} sont écrites.")
    return {"resume": resume, "remarques": remarques, "signalements": signalements, "trouves": compte[ETAT_TROUVE],
            "a_verifier": compte[ETAT_A_VERIFIER], "introuvables": compte[ETAT_INTROUVABLE]}


# ---------------------------------------------------------------------------
# Avec le site
# ---------------------------------------------------------------------------

@frappe.whitelist()
def lire_nom(nom: str) -> dict:
    """Preremplit le dialogue de collecte d'apres le nom du fichier choisi."""
    frappe.has_permission(DOCTYPE, "create", throw=True)
    return lire_nom_fichier(nom)


@frappe.whitelist()
def ajouter(banque: str, numero: str, date_remise: str, fichier: str, nature: str = "Chèques",
            montant_total=None, nombre=None, note: str = None) -> dict:
    """Range un bordereau. `fichier` : l'adresse du fichier deja televerse (champ Attach du dialogue).
    -> {name, deja} ; `deja` vrai si ce bordereau etait deja collecte (rien n'est alors modifie)."""
    frappe.has_permission(DOCTYPE, "create", throw=True)
    try:
        nom = nom_du_bordereau(banque, numero)
    except ValueError as e:
        frappe.throw(str(e))
    if nature not in NATURES:
        frappe.throw("Nature inconnue : Chèques, Traites ou Espèces.")
    if not fichier or not str(fichier).startswith(("/files/", "/private/files/")):
        frappe.throw("Joignez le scan ou la photo du bordereau.")
    if frappe.db.exists(DOCTYPE, nom):
        return {"name": nom, "deja": True}
    doc = frappe.get_doc({"doctype": DOCTYPE, "banque": banque, "numero": numero, "date_remise": date_remise,
                          "nature": nature, "fichier": fichier, "note": note,
                          "montant_total": frappe.utils.flt(montant_total) or None,
                          "nombre": frappe.utils.cint(nombre) or None}).insert()
    _rattacher(fichier, doc.name)
    return {"name": doc.name, "deja": False, "lecture": doc.nature in NATURES_LUES}


def _rattacher(fichier: str, name: str):
    """Le fichier televerse par le dialogue n'est rattache a rien : il devient la piece jointe du
    bordereau, donc lisible par qui peut lire le bordereau."""
    for f in frappe.get_all("File", filters={"file_url": fichier, "attached_to_name": ["is", "not set"],
                                             "owner": frappe.session.user}, pluck="name"):
        frappe.db.set_value("File", f, {"attached_to_doctype": DOCTYPE, "attached_to_name": name,
                                        "attached_to_field": "fichier"}, update_modified=False)


@frappe.whitelist()
def recents(limite: int = 12) -> list:
    """Les derniers bordereaux collectes, pour le dialogue de la caisse."""
    frappe.has_permission(DOCTYPE, "read", throw=True)
    return frappe.get_list(DOCTYPE, fields=["name", "banque", "nature", "numero", "date_remise", "montant_total",
                                            "fichier", "remis_par", "creation", "lecture_statut", "lecture_resume",
                                            "total_lu", "nombre_lu", "a_signaler"],
                           order_by="date_remise desc, creation desc", limit_page_length=min(frappe.utils.cint(limite) or 12, 50))


# ---------------------------------------------------------------------------
# Lecture par l'IA et recherche dans ERPNext (arriere-plan)
# ---------------------------------------------------------------------------

def lancer_lecture(name: str):
    """Met la lecture du bordereau en file d'attente. Ne leve jamais : collecter passe avant tout."""
    try:
        frappe.db.set_value(DOCTYPE, name, {"lecture_statut": LECTURE_EN_ATTENTE, "lecture_resume": None,
                                            "lecture_message": None, "a_signaler": None}, update_modified=False)
        frappe.enqueue("bank_retenue_sync.api.bordereaux.lire", queue="default", timeout=600,
                       enqueue_after_commit=True, bordereau=name)
    except Exception:
        frappe.log_error(title="Bordereau de remise : mise en file de la lecture", message=frappe.get_traceback())


def _contenu(fichier: str):
    """(octets, nom) du scan joint."""
    noms = frappe.get_all("File", filters={"file_url": fichier}, pluck="name", limit=1)
    if not noms:
        raise ValueError("Le scan joint est introuvable sur le serveur.")
    doc = frappe.get_doc("File", noms[0])
    return doc.get_content(), doc.file_name or fichier


def _paiements(nature: str, numeros: list):
    """(portefeuille, ailleurs) pour `rapprocher`. Lecture seule, sans droits : la reponse ne montre que le
    nom du paiement, le client et le montant des pieces que la personne a elle-meme remises."""
    from bank_retenue_sync.encaissement import pending

    if nature == "Traites":
        compte, extraire = pending.TRAITE_ACCOUNT, pending._extract_traite_number
    else:
        compte, extraire = pending.CHEQUE_ACCOUNT, pending._extract_number
    deja = pending.already_encaissed_refs()
    portefeuille = [{"name": pe["name"], "numero": pe.get("numero"), "montant": pe.get("paid_amount"),
                     "client": pe.get("party"), "en_encaissement": pe["name"] in deja}
                    for pe in pending._pending_pes(compte, set(), extractor=extraire)]
    ailleurs = []
    for numero in {_chiffres(n) for n in numeros if len(_chiffres(n)) >= 4}:
        lignes = frappe.db.sql(
            """select name, reference_no, paid_amount, party, paid_to from `tabPayment Entry`
               where docstatus = 1 and payment_type in ('Receive', 'Internal Transfer')
                 and paid_to != %s and reference_no like %s
               order by posting_date desc, creation desc limit 20""", (compte, f"%{numero}%"), as_dict=True)
        for pe in lignes:
            if _chiffres(extraire(pe.reference_no)) == numero:
                ailleurs.append({"name": pe.name, "numero": numero, "montant": pe.paid_amount,
                                 "client": pe.party, "compte": pe.paid_to})
    return portefeuille, ailleurs


CHAMPS_PIECE = ("numero", "banque", "tireur", "echeance", "montant", "doute")


def _poser(doc, pieces: list, lecture: dict = None, avant: list = ()):
    """Rapproche `pieces` et ecrit le resultat sur le bordereau. `avant` : remarques a mettre en tete."""
    portefeuille, ailleurs = _paiements(doc.nature, [p.get("numero") for p in pieces])
    rapprochees = rapprocher(pieces, portefeuille, ailleurs)
    if lecture is None:
        # Reverification apres correction a la main : ce qui a ete lu sur le scan reste la reference.
        lecture = {"total": doc.total_lu or None, "nombre": doc.nombre_lu or None}
    etat = bilan(rapprochees, lecture, {"numero": doc.numero, "montant_total": doc.montant_total, "nombre": doc.nombre},
                 doc.nature)
    doc.set("pieces", [])
    for p in rapprochees:
        doc.append("pieces", {**{champ: p.get(champ) for champ in CHAMPS_PIECE}, "doute": 1 if p.get("doute") else 0,
                              "etat": p["etat"], "paiement": p["paiement"], "client": p["client"],
                              "montant_erp": p["montant_erp"], "remarque": p["remarque"]})
    doc.lecture_statut = LECTURE_LUE
    doc.lecture_resume = etat["resume"]
    doc.lecture_message = "\n".join(list(avant) + etat["remarques"]) or None
    doc.a_signaler = "\n".join(etat["signalements"]) or None
    doc.flags.ignore_permissions = True
    doc.save()
    if doc.banque == "BIAT" and doc.nature == "Chèques":
        # La BIAT ne publie pas le detail de ses remises : ce bordereau est ce qui permet d'encaisser la
        # sienne. Si elle est deja creditee, elle part tout de suite (`bank/biat.encaisser_remises`).
        frappe.enqueue("bank_retenue_sync.bank.biat.passage_job", queue="default", timeout=900,
                       enqueue_after_commit=True, job_id="brs-biat-passage", deduplicate=True)
    return etat


def _aujourd_hui_lisible() -> str:
    return frappe.utils.formatdate(frappe.utils.today(), "dd/MM/yyyy")


def _appliquer_lecture(doc, lu: dict, avant: list = ()):
    doc.total_lu, doc.nombre_lu = lu.get("total"), lu.get("nombre")
    doc.lecture_modele, doc.lecture_le = lu.get("_model"), frappe.utils.now()
    return _poser(doc, lu.get("pieces") or [], lu, avant)


def lire(bordereau: str):
    """LA TACHE DE FOND : l'IA lit le scan, chaque piece est cherchee dans ERPNext, le resultat est pose
    sur le bordereau et l'ecran de la caisse est prevenu. Un echec est ecrit sur le bordereau, jamais leve."""
    from bank_retenue_sync.ai import bordereau_extract

    try:
        doc = frappe.get_doc(DOCTYPE, bordereau)
        # Le scan vient peut-etre d'etre lu au depot (bordereau que l'IA n'a pas su nommer seule, complete
        # a la main) : on ne paie pas une seconde lecture.
        lu = frappe.cache.get_value(CLE_LECTURE + (doc.fichier or ""))
        if lu:
            frappe.cache.delete_value(CLE_LECTURE + doc.fichier)
        else:
            contenu, nom = _contenu(doc.fichier)
            lu = bordereau_extract.lire(contenu, nom, doc.nature, aujourd_hui=_aujourd_hui_lisible())
        _appliquer_lecture(doc, lu)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Bordereau de remise : lecture par l'IA", message=frappe.get_traceback())
        frappe.db.set_value(DOCTYPE, bordereau, {"lecture_statut": LECTURE_ECHEC, "lecture_resume": "lecture impossible",
                                                 "lecture_message": message_d_echec(e)}, update_modified=False)
    frappe.db.commit()
    frappe.publish_realtime("brs_bordereau_lu", {"name": bordereau})


@frappe.whitelist()
def relire(name: str) -> dict:
    """Relance la lecture par l'IA (scan remplace, lecture ratee). En arriere-plan."""
    doc = frappe.get_doc(DOCTYPE, name)
    doc.check_permission("write")
    if doc.nature not in NATURES_LUES:
        frappe.throw("Un versement d’espèces n’a pas de pièces à lire.")
    lancer_lecture(doc.name)
    return detail(doc.name)


@frappe.whitelist()
def verifier(name: str) -> dict:
    """Cherche a nouveau dans ERPNext les pieces de la fiche, telles qu'elles y sont ecrites (apres une
    correction a la main, ou la saisie d'un paiement manquant). Sans IA, immediat."""
    doc = frappe.get_doc(DOCTYPE, name)
    doc.check_permission("write")
    pieces = [{champ: ligne.get(champ) for champ in CHAMPS_PIECE} for ligne in doc.pieces]
    for p in pieces:
        p["numero"] = "".join(c for c in str(p["numero"] or "") if c.isdigit())
        p["montant"] = frappe.utils.flt(p["montant"], 3) if p["montant"] not in (None, "") else None
    _poser(doc, pieces)
    return detail(doc.name)


@frappe.whitelist()
def detail(name: str) -> dict:
    """Le bordereau et ses pieces, pour le dialogue de la caisse."""
    doc = frappe.get_doc(DOCTYPE, name)
    doc.check_permission("read")
    return {"name": doc.name, "banque": doc.banque, "nature": doc.nature, "numero": doc.numero,
            "date_remise": doc.date_remise, "fichier": doc.fichier, "montant_total": doc.montant_total,
            "lecture_statut": doc.lecture_statut, "lecture_resume": doc.lecture_resume,
            "lecture_message": doc.lecture_message, "total_lu": doc.total_lu, "nombre_lu": doc.nombre_lu,
            "a_signaler": doc.a_signaler,
            "modifiable": bool(doc.has_permission("write")),
            "pieces": [{champ: ligne.get(champ) for champ in
                        ("numero", "banque", "tireur", "echeance", "montant", "doute", "etat", "paiement", "client",
                         "montant_erp", "remarque")} for ligne in doc.pieces]}


# ---------------------------------------------------------------------------
# Depot : on donne le scan, l'IA fait le reste
# ---------------------------------------------------------------------------
#: Ou en est un scan depose ({etat, …}), et la lecture gardee quand il a fallu demander un complement.
CLE_DEPOT, CLE_LECTURE = "brs_depot:", "brs_lecture:"
UNE_JOURNEE = 86400
DEPOT_EN_COURS, DEPOT_COLLECTE, DEPOT_DEJA = "en_cours", "collecte", "deja"
DEPOT_A_COMPLETER, DEPOT_ECHEC = "a_completer", "echec"


def _etat_depot(fichier: str, **etat):
    frappe.cache.set_value(CLE_DEPOT + fichier, etat, expires_in_sec=UNE_JOURNEE)
    frappe.publish_realtime("brs_bordereau_lu", {"fichier": fichier, **etat})


def completer_par_attendu(lu: dict, attendu: dict) -> tuple:
    """Ce que l'IA n'a pas su lire, complete par ce que la photo devait montrer. Pur.

    Une photo prise depuis la relance d'un bordereau manquant (`bank/bordereaux_manquants`) sait quel bordereau
    on attend : banque, numero, nature. On ne complete QUE ce qui manque a la lecture, jamais ce qu'elle a lu —
    la photo fait foi : si c'est un autre bordereau, c'est lui qu'on enregistre, et la relance reste ouverte.
    -> (lecture completee, [champs repris de la relance])"""
    lu, attendu, repris = dict(lu or {}), attendu or {}, []
    if attendu.get("banque") in BANQUES and not lu.get("banque_remise"):
        lu["banque_remise"] = attendu["banque"]
        repris.append("banque")
    if attendu.get("numero") and not _chiffres(lu.get("numero")):
        lu["numero"] = attendu["numero"]
        repris.append("numero")
    if attendu.get("nature") in NATURES and not lu.get("nature"):
        lu["nature"] = attendu["nature"]
        repris.append("nature")
    return lu, repris


def _attendu(attendu) -> dict:
    """L'argument `attendu` du dialogue (texte JSON ou dict), reduit a ce qui sert."""
    if isinstance(attendu, str):
        try:
            attendu = json.loads(attendu or "{}")
        except ValueError:
            attendu = {}
    attendu = attendu if isinstance(attendu, dict) else {}
    return {k: str(attendu[k]) for k in ("banque", "numero", "nature") if attendu.get(k)}


@frappe.whitelist()
def deposer(fichier: str, attendu=None) -> dict:
    """Le scan (ou la photo) vient d'etre televerse : l'IA le lit en arriere-plan — banque, numero, date,
    nature, pieces — et collecte le bordereau toute seule. Rend tout de suite ; suivre avec `etat_depot`
    ou le signal temps reel `brs_bordereau_lu`. `attendu` : {banque, numero, nature} quand la photo repond a
    la relance d'un bordereau manquant (`completer_par_attendu`)."""
    frappe.has_permission(DOCTYPE, "create", throw=True)
    if not fichier or not str(fichier).startswith(("/files/", "/private/files/")) \
            or not frappe.db.exists("File", {"file_url": fichier}):
        frappe.throw("Joignez le scan ou la photo du bordereau.")
    frappe.cache.set_value(CLE_DEPOT + fichier, {"etat": DEPOT_EN_COURS}, expires_in_sec=UNE_JOURNEE)
    frappe.enqueue("bank_retenue_sync.api.bordereaux.collecter_par_ia", queue="default", timeout=600,
                   enqueue_after_commit=True, fichier=fichier, attendu=_attendu(attendu) or None)
    return {"etat": DEPOT_EN_COURS}


@frappe.whitelist()
def etat_depot(fichier: str) -> dict:
    frappe.has_permission(DOCTYPE, "create", throw=True)
    return frappe.cache.get_value(CLE_DEPOT + (fichier or "")) or {"etat": "inconnu"}


def collecter_par_ia(fichier: str, attendu: dict = None):
    """LA TACHE DE FOND DU DEPOT. L'IA lit le scan ; si la banque et le numero en sortent, le bordereau est
    collecte et ses pieces cherchees dans ERPNext. Sinon (banque illisible, numeros contradictoires, panne
    de l'IA), rien n'est perdu : l'etat dit ce qui manque, et la caisse propose la saisie a la main.
    `attendu` : ce que la relance d'un bordereau manquant attendait (`completer_par_attendu`)."""
    from bank_retenue_sync.ai import bordereau_extract

    indices = {}
    try:
        contenu, nom = _contenu(fichier)
        indices = lire_nom_fichier(nom)
        indices = indices if indices.get("banque") else {}
        lu = bordereau_extract.lire(contenu, nom, indices.get("nature") or (attendu or {}).get("nature"),
                                    aujourd_hui=_aujourd_hui_lisible())
        lu, repris = completer_par_attendu(lu, attendu)
        qui = identite(indices, lu, frappe.utils.today())
        propose = {"banque": qui["banque"], "numero": qui["numero"], "nature": qui["nature"],
                   "date_remise": qui["date_remise"], "montant_total": lu.get("total"),
                   "nombre": lu.get("nombre") or len(lu.get("pieces") or []) or None}
        if qui["manque"]:
            # La lecture est gardee : quand la personne aura complete, `lire` la reprendra sans rappeler l'IA.
            frappe.cache.set_value(CLE_LECTURE + fichier, lu, expires_in_sec=UNE_JOURNEE)
            manque = " et ".join({"banque": "la banque", "numero": "le numéro du bordereau"}[m] for m in qui["manque"])
            _etat_depot(fichier, etat=DEPOT_A_COMPLETER, propose=propose,
                        message=qui["conflit"] or f"L’IA n’a pas pu lire {manque} : complétez, puis collectez.")
            return
        name = nom_du_bordereau(qui["banque"], qui["numero"])
        if frappe.db.exists(DOCTYPE, name):
            _etat_depot(fichier, etat=DEPOT_DEJA, name=name)
            return
        doc = frappe.get_doc({"doctype": DOCTYPE, "banque": qui["banque"], "numero": qui["numero"],
                              "nature": qui["nature"], "date_remise": qui["date_remise"], "fichier": fichier,
                              "montant_total": propose["montant_total"], "nombre": propose["nombre"]})
        doc.flags.lecture_faite = True                # `after_insert` ne relance pas une lecture
        doc.insert()
        _rattacher(fichier, doc.name)
        if doc.nature in NATURES_LUES:
            avant = [] if qui["date_source"] != "jour" else [
                "Date illisible sur le bordereau : la date du jour a été mise — corrigez-la sur la fiche si besoin."]
            if "numero" in repris:
                avant.append(f"N° du bordereau illisible sur la photo : repris de la relance (n° {doc.numero}).")
            _appliquer_lecture(doc, lu, avant)
        _etat_depot(fichier, etat=DEPOT_COLLECTE, name=doc.name)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Bordereau de remise : dépôt lu par l'IA", message=frappe.get_traceback())
        _etat_depot(fichier, etat=DEPOT_ECHEC, propose=indices,
                    message=message_d_echec(e) + " Vous pouvez saisir le bordereau à la main.")
    finally:
        frappe.db.commit()


# ---------------------------------------------------------------------------
# Relance des bordereaux manquants (barre de ceux qui ont le role « Relance bordereaux »)
# ---------------------------------------------------------------------------

def _relance_autorisee() -> bool:
    from bank_retenue_sync.bank import bordereaux_manquants

    return bordereaux_manquants.ROLE in frappe.get_roles()


@frappe.whitelist()
def manquants_barre():
    """Les bordereaux BIAT a photographier, pour la barre du Desk. None pour qui n'a pas le role de la relance :
    l'ecran cesse alors de demander."""
    from bank_retenue_sync.bank import bordereaux_manquants

    if not _relance_autorisee():
        return None
    lignes = bordereaux_manquants.liste()
    return {"n": len(lignes), "lignes": lignes, "motifs": list(bordereaux_manquants.MOTIFS),
            "photo": bool(frappe.has_permission(DOCTYPE, "create"))}


@frappe.whitelist()
def ecarter_manquant(cle: str, motif: str, note: str = None):
    """« Pas de bordereau » : la ligne ne revient plus (motif garde, avec qui et quand)."""
    from bank_retenue_sync.bank import bordereaux_manquants

    if not _relance_autorisee():
        frappe.throw("Réservé à la relance des bordereaux.", frappe.PermissionError)
    if not str(cle or "").startswith(bordereaux_manquants.BANQUE + "|"):
        frappe.throw("Ligne inconnue.")
    bordereaux_manquants.ecarter(cle, motif, note)
    frappe.db.commit()
    frappe.publish_realtime("brs_bordereaux_manquants", {})
    return manquants_barre()
