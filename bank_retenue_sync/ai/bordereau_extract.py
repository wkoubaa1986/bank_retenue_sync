"""Lecture d'un bordereau de remise PAPIER (scan ou photo), rempli a la main — OpenAI, vision.

`ai/remise_extract` lit le bordereau que le portail de Zitouna fabrique : un PDF avec du texte. Celui-ci
est le carnet a souches que l'on remplit au stylo avant d'aller au guichet (BIAT : il n'existe rien
d'autre ; Zitouna : le double papier). C'est une image, l'ecriture est manuscrite : chaque page est
recadree sur le bordereau (`zone_utile`) puis envoyee en image au modele (API Responses).

La lecture est faite A L'AVEUGLE : on ne dit pas au modele quelles pieces ERPNext attend. Lui souffler la
liste lui ferait « lire » ce qu'on espere ; le rapprochement (`api/bordereaux.rapprocher`) tolere a sa
place un chiffre mal forme, et le montre.
"""
from __future__ import annotations

import json
import re

import frappe

from bank_retenue_sync.ai.invoice_extract import _get_client_model_temp

_SYSTEM = (
    "Tu lis un BORDEREAU DE REMISE d'une banque tunisienne (remise de cheques, ou remise d'effets / "
    "traites), rempli A LA MAIN au stylo, scanne ou photographie. Le formulaire est imprime, les "
    "valeurs sont manuscrites. Reponds STRICTEMENT en JSON (aucun texte hors JSON) avec ces cles : "
    "numero (str : le numero IMPRIME du bordereau, souvent en haut a droite apres « N° »), "
    "date_remise (YYYY-MM-DD ou null ; les dates sont ecrites jour/mois/annee), "
    "total (number ou null : le « Montant total » ecrit sur le bordereau), "
    "nombre (integer ou null : le nombre de pieces ecrit sur le bordereau), "
    "pieces (array, UNE entree par ligne REMPLIE du tableau, dans l'ordre, chacune avec : "
    "numero (str : numero du cheque ou de la traite, chiffres uniquement, zeros de tete conserves), "
    "banque (str ou null : etablissement payeur tel qu'ecrit, ex « Zitouna », « BT », « BNA »), "
    "tireur (str ou null : nom du tireur / tire tel qu'ecrit), "
    "echeance (YYYY-MM-DD ou null : seulement pour une traite), "
    "montant (number), "
    "doute (bool : true si un chiffre du numero ou du montant est mal forme et que tu hesites)). "
    "MONTANTS — format tunisien : la virgule est le separateur DECIMAL et il y a TROIS decimales "
    "(millimes) : « 1 045,000 » vaut 1045.0 ; « 270,000 » vaut 270.0, pas 270000. Rends des nombres a "
    "point decimal, sans separateur de milliers. Dans les cases du montant total, les trois dernieres "
    "cases sont les millimes. "
    "Lis chaque chiffre TEL QU'IL EST ECRIT, un par un. N'ajuste JAMAIS un montant pour que la somme "
    "tombe sur le total : un ecart est une information utile, un montant arrange est une faute. "
    "Un numero de cheque tunisien a 7 chiffres ; ne double pas un chiffre et n'en ajoute pas. "
    "N'invente jamais une ligne : les lignes vides du tableau ne sont pas des pieces. Ignore les tampons, "
    "le filigrane « exemplaire client », la signature et le numero de compte."
)


def _nombre(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(" ", "")
    if "," in s:                                   # format tunisien : point = milliers, virgule = decimale
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _date(v):
    s = str(v or "").strip()
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def json_du_modele(texte: str) -> dict:
    """Le JSON de la reponse, meme encadre d'un bloc de code. Pur."""
    texte = (texte or "").strip()
    if texte.startswith("```"):
        texte = texte.strip("`")
        texte = texte.split("\n", 1)[1] if texte.lower().startswith("json") else texte
        texte = texte.rsplit("```", 1)[0]
    return json.loads(texte)


def normaliser(data: dict) -> dict:
    """Met la reponse du modele en forme sure : numeros en chiffres, montants en nombres, lignes vides
    ecartees. Pur. -> {numero, date_remise, total, nombre, pieces: [{numero, banque, tireur, echeance,
    montant, doute}], somme, total_concorde}"""
    data = data if isinstance(data, dict) else {}
    pieces = []
    for p in data.get("pieces") or []:
        if not isinstance(p, dict):
            continue
        numero = "".join(c for c in str(p.get("numero") or "") if c.isdigit())
        montant = _nombre(p.get("montant"))
        if not numero and montant is None:
            continue
        pieces.append({"numero": numero, "banque": str(p.get("banque") or "").strip()[:140],
                       "tireur": str(p.get("tireur") or "").strip()[:140], "echeance": _date(p.get("echeance")),
                       "montant": round(montant, 3) if montant is not None else None,
                       "doute": bool(p.get("doute"))})
    total = _nombre(data.get("total"))
    somme = round(sum(p["montant"] or 0 for p in pieces), 3)
    try:
        nombre = int(data.get("nombre")) if data.get("nombre") not in (None, "") else None
    except (TypeError, ValueError):
        nombre = None
    return {"numero": "".join(c for c in str(data.get("numero") or "") if c.isdigit()),
            "date_remise": _date(data.get("date_remise")),
            "total": round(total, 3) if total is not None else None, "nombre": nombre, "pieces": pieces,
            "somme": somme, "total_concorde": total is not None and abs(somme - total) < 0.0005}


# ---------------------------------------------------------------------------
# L'image envoyee au modele
# ---------------------------------------------------------------------------
#: Plus grand cote de l'image envoyee. Au-dela le modele reduit de toute facon ; en deca il lit mal.
COTE_MAX = 2240
PAGES_MAX = 3


def zone_utile(im, pas: int = 40, part: float = 0.25):
    """Recadre une page scannee sur le bordereau. Pur (Pillow).

    ⚠️ C'EST CE QUI REND LA LECTURE POSSIBLE. Un carnet de remise fait un tiers de page A4 : envoye tel
    quel (PDF entier), le modele recoit une ecriture de quelques pixels de haut et rend n'importe quoi —
    essai du 16/11/2026 sur le bordereau BIAT 7473685 : numeros « 41001767, 21001266, 29056147 » pour
    4001767, 4001066, 8965167. Recadre, les trois numeros sortent justes a chaque essai.

    La page est decoupee en cases de `pas` pixels ; une ligne (puis une colonne) de cases est gardee si
    une part suffisante de ses cases porte de l'encre ou de la couleur. Une tache isolee, l'ombre de la
    reliure sur un bord, ne suffisent donc pas a elargir le cadre. Si rien de net ne ressort (photo de
    telephone, fond non blanc), l'image est rendue entiere."""
    from PIL import Image, ImageOps

    largeur, hauteur = max(1, im.width // pas), max(1, im.height // pas)
    if largeur < 4 or hauteur < 4:
        return im
    cases = (ImageOps.grayscale(im).point(lambda v: 255 if v < 225 else 0)
             .resize((largeur, hauteur), Image.BOX).point(lambda v: 1 if v > 20 else 0).tobytes())
    lignes = [y for y in range(hauteur) if sum(cases[y * largeur:(y + 1) * largeur]) / largeur >= part]
    if not lignes:
        return im
    y0, y1 = lignes[0], lignes[-1] + 1
    colonnes = [x for x in range(largeur)
                if sum(cases[y * largeur + x] for y in range(y0, y1)) / (y1 - y0) >= part]
    if not colonnes:
        return im
    x0, x1 = colonnes[0], colonnes[-1] + 1
    if (x1 - x0) * (y1 - y0) < 0.05 * largeur * hauteur:
        return im
    return im.crop((x0 * pas, y0 * pas, min(im.width, x1 * pas), min(im.height, y1 * pas)))


def _jpeg(im) -> bytes:
    import io

    im = zone_utile(im.convert("RGB"))
    if max(im.size) > COTE_MAX:
        im.thumbnail((COTE_MAX, COTE_MAX))
    tampon = io.BytesIO()
    im.save(tampon, "JPEG", quality=90)
    return tampon.getvalue()


def agrandissements(jpeg: bytes) -> list:
    """Quatre quarts de l'image, qui se recouvrent. Le modele reduit une grande image avant de la lire :
    lui donner aussi les quarts lui laisse des chiffres deux fois plus grands. Rien si l'image est petite."""
    import io

    from PIL import Image

    im = Image.open(io.BytesIO(jpeg))
    if min(im.size) < 700:
        return []
    largeur, hauteur = int(im.width * 0.56), int(im.height * 0.56)
    quarts = []
    for y in (0, im.height - hauteur):
        for x in (0, im.width - largeur):
            tampon = io.BytesIO()
            im.crop((x, y, x + largeur, y + hauteur)).save(tampon, "JPEG", quality=90)
            quarts.append(tampon.getvalue())
    return quarts


def images_du_document(contenu: bytes, nom: str) -> list:
    """Le scan (PDF, 3 pages au plus) ou la photo -> des JPEG recadres sur le bordereau.
    Leve ValueError si le format n'est pas lisible (photo HEIC, fichier bureautique…)."""
    import io

    from PIL import Image, ImageOps

    extension = (nom or "").rsplit(".", 1)[-1].lower() if "." in (nom or "") else ""
    if extension == "pdf" or contenu[:5] == b"%PDF-":
        import pymupdf

        with pymupdf.open(stream=contenu, filetype="pdf") as pdf:
            images = []
            for page in list(pdf)[:PAGES_MAX]:
                # 300 points par pouce : la definition habituelle d'un scanner de bureau.
                pix = page.get_pixmap(dpi=300)
                images.append(_jpeg(Image.frombytes("RGB", (pix.width, pix.height), pix.samples)))
            return images
    try:
        im = Image.open(io.BytesIO(contenu))
        im = ImageOps.exif_transpose(im)             # une photo de telephone tenue de travers
    except Exception:
        raise ValueError("Format non lisible par l’IA : joignez un PDF, un JPEG ou un PNG.")
    return [_jpeg(im)]


#: Le modele qui lit l'ecriture manuscrite quand le reglage est vide de toute valeur par defaut.
MODELE_PAR_DEFAUT = "gpt-6.1-sol"


def _modeles() -> list:
    """Les modeles a essayer, dans l'ordre : celui du reglage « Modèle IA de lecture des bordereaux », puis
    celui des AI Settings (le reglage peut nommer un modele que la cle n'a pas, ou qui a ete retire)."""
    _client, general, _temperature = _get_client_model_temp()
    choisi = None
    try:
        choisi = (frappe.db.get_single_value("Bank Retenue Sync Settings", "modele_lecture_bordereau") or "").strip()
    except Exception:
        pass                                          # champ pas encore migre : on prend le modele par defaut
    return list(dict.fromkeys(m for m in (choisi or MODELE_PAR_DEFAUT, general) if m))


def _modele_refuse(erreur: Exception) -> bool:
    """L'erreur dit-elle que le MODELE n'est pas utilisable (inconnu, sans acces, sans vision) ?"""
    texte = str(erreur).lower()
    return "model" in texte and any(mot in texte for mot in ("not found", "does not exist", "not have access",
                                                             "not supported", "unsupported", "invalid model"))


def lire(contenu: bytes, nom: str, nature: str = "Chèques", aujourd_hui: str = None) -> dict:
    """Scan (PDF) ou photo d'un bordereau -> `normaliser(...)` + `_model`.

    ⚠️ LE MODELE COMPTE PLUS QUE LA CONSIGNE. Essais du 16/11/2026 sur le bordereau BIAT 7473685, image
    recadree et agrandie : gpt-5.2 rend les trois numeros justes mais un montant faux une fois sur trois
    (« 270,570 », « 1 694,127 ») ; gpt-6.1-sol rend numeros ET montants justes quatre fois sur quatre.

    Une lecture dont la somme ne tombe pas sur le total ecrit est REFAITE une fois : le total, ecrit dans
    des cases, se lit sans peine et sert de preuve. Si la seconde ne tombe pas mieux, la premiere est
    gardee telle quelle — l'ecart sera montre, jamais maquille."""
    import base64

    client, _general, _temperature = _get_client_model_temp()
    if not hasattr(client, "responses"):
        raise ValueError("Le SDK OpenAI installé ne sait pas lire une image (API Responses absente).")
    images = images_du_document(contenu, nom)
    pieces = []
    for image in images:
        for vue in [image] + (agrandissements(image) if len(images) == 1 else []):
            pieces.append({"type": "input_image", "detail": "high",
                           "image_url": "data:image/jpeg;base64," + base64.b64encode(vue).decode()})
    consigne = "Bordereau de remise d’effets (traites)." if nature == "Traites" else "Bordereau de remise de chèques."
    if len(pieces) > len(images):
        consigne += (" La première image montre le bordereau entier ; les suivantes en sont des agrandissements "
                     "(quarts qui se recouvrent) : sers-t’en pour lire les chiffres, sans compter une ligne deux fois.")
    if aujourd_hui:
        # L'annee manuscrite est souvent illisible (« 2026 » lu « 2016 ») : un bordereau est recent.
        consigne += f" Nous sommes le {aujourd_hui} : la remise date de quelques jours au plus."
    entree = [{"role": "user", "content": pieces + [{"type": "input_text", "text": consigne}]}]

    def une_lecture(model):
        res = client.responses.create(model=model, instructions=_SYSTEM, input=entree)
        return normaliser(json_du_modele(res.output_text))

    modeles = _modeles()
    for rang, model in enumerate(modeles):
        try:
            out = une_lecture(model)
        except Exception as e:
            if rang + 1 < len(modeles) and _modele_refuse(e):
                continue
            raise
        if out["pieces"] and out["total"] is not None and not out["total_concorde"]:
            try:
                seconde = une_lecture(model)
                if seconde["total_concorde"]:
                    out = seconde
            except Exception:
                pass                                   # la premiere lecture reste valable
        out["_model"] = model
        return out
