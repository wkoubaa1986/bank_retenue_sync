"""Solde du compte : banque reelle contre ERPNext.

C'EST LA QUESTION QUI COMPTE
----------------------------
Tout le reste (registre, regles, ecritures) sert a ce que `STE430127B - Zitouna - A&S` dans ERPNext
soit le miroir du compte reel. Ce module mesure l'ecart et le decompose, pour qu'il soit reductible
plutot que constate.

TROIS VALEURS, PAS DEUX
-----------------------
  1. `banque_officiel`  : le solde affiche par le portail, lu sur une capture d'ecran via OpenAI.
     C'est la verite. L'export XLSX des mouvements n'a AUCUNE colonne solde — d'ou la capture.
  2. `banque_cumule`    : solde de depart + credits - debits du registre. S'il derive du solde
     officiel, c'est qu'il MANQUE des mouvements au registre (fenetre d'export trop courte).
  3. `erpnext`          : somme des ecritures du grand livre sur le compte.

L'ecart (1) - (3) est ce qu'il reste a comptabiliser. L'ecart (1) - (2) est ce qu'il reste a
importer. Les deux se corrigent differemment, d'ou la distinction.
"""
from __future__ import annotations

import json

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.encaissement.pending import BANK_ACCOUNT, COMPANY

DOCTYPE_MOUVEMENT = "BRS Bank Movement"


# ------------------------------------------------------------------ ERPNext

def solde_erpnext(date_max=None, compte: str = BANK_ACCOUNT) -> float:
    """Solde comptable du compte bancaire dans ERPNext (ecritures soumises uniquement)."""
    val = frappe.db.sql(
        """SELECT SUM(debit - credit) FROM `tabGL Entry`
           WHERE account = %(compte)s AND is_cancelled = 0
             AND (%(date_max)s IS NULL OR posting_date <= %(date_max)s)""",
        {"compte": compte, "date_max": getdate(date_max) if date_max else None})[0][0]
    return flt(val, 3)


# ------------------------------------------------------------------ registre

def flux_registre(date_min=None, date_max=None) -> dict:
    """Credits et debits du registre sur une periode."""
    filters = {}
    if date_min and date_max:
        filters["date"] = ["between", [getdate(date_min), getdate(date_max)]]
    elif date_max:
        filters["date"] = ["<=", getdate(date_max)]
    elif date_min:
        filters["date"] = [">=", getdate(date_min)]
    rows = frappe.db.get_all(DOCTYPE_MOUVEMENT, filters=filters, limit_page_length=0,
                             fields=["credit", "debit"])
    credits = flt(sum(flt(r.credit) for r in rows), 3)
    debits = flt(sum(flt(r.debit) for r in rows), 3)
    return {"credits": credits, "debits": debits, "net": flt(credits - debits, 3),
            "mouvements": len(rows)}


def solde_cumule(depart: float, depart_date, date_max=None) -> float:
    """Solde de depart, augmente des mouvements du registre posterieurs a sa date."""
    f = flux_registre(date_min=depart_date, date_max=date_max)
    return flt(flt(depart) + f["net"], 3)


# ------------------------------------------------------------------ capture bancaire

def fetch_capture_solde(filename: str = None) -> tuple:
    """Recupere l'image du solde produite par tej-bank-service. -> (filename, octets).

    Routes du service : `GET /banque/solde` (liste), `/banque/solde/latest`, `/banque/solde/{f}`.
    """
    import requests

    from bank_retenue_sync.bank import movements as mv

    base = mv._base_url()
    h = {k: v for k, v in mv._headers().items() if k != "Accept"}
    url = base + ("/banque/solde/%s" % filename if filename else "/banque/solde/latest")
    r = requests.get(url, headers=h, timeout=90)
    r.raise_for_status()
    nom = filename or (r.headers.get("Content-Disposition") or "").split("filename=")[-1].strip('"; ')
    return (nom or "solde.png", r.content)


def list_captures_solde() -> list:
    import requests

    from bank_retenue_sync.bank import movements as mv

    r = requests.get(mv._base_url() + "/banque/solde", headers=mv._headers(), timeout=60)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("captures", [])


def lire_solde_image(image: bytes, nom: str = "solde.png") -> dict:
    """Extrait le solde d'une capture d'ecran du portail, par OpenAI Vision.

    Rend {solde_comptable, solde_disponible, devise, compte, date_solde, confiance}.

    L'IA LIT, ELLE NE CALCULE PAS : on lui demande de recopier ce qui est affiche, jamais de
    deduire un solde. Un champ illisible doit revenir a null plutot qu'invente — c'est la seule
    facon de distinguer « la banque dit X » de « le modele a suppose X ».
    """
    import base64

    from bank_retenue_sync.ai.invoice_extract import _get_client_model_temp

    client, model, _ = _get_client_model_temp()
    b64 = base64.b64encode(image).decode()
    consigne = (
        "Tu lis une capture d'ecran d'un portail bancaire tunisien (Banque Zitouna). "
        "Recopie EXACTEMENT les valeurs affichees, sans jamais calculer ni deduire. "
        "Reponds en JSON strict avec les cles : compte (numero de compte affiche), "
        "solde_comptable (nombre), solde_disponible (nombre), devise, "
        "date_solde (AAAA-MM-JJ si une date de solde est affichee), confiance (0 a 1). "
        "Utilise le point comme separateur decimal et supprime les separateurs de milliers. "
        "Toute valeur non lisible avec certitude doit valoir null."
    )
    resp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": [
            {"type": "text", "text": consigne},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,%s" % b64, "detail": "high"}},
        ]}],
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    data["_source"] = nom
    return data


# ------------------------------------------------------------------ comparaison

def comparer(date_max=None, depart: float = None, depart_date=None,
             capture: bool = False) -> dict:
    """Confronte les trois valeurs et decompose l'ecart.

    `capture=True` lit le solde officiel sur la derniere capture du portail (appel OpenAI).
    `depart` / `depart_date` : solde de depart connu, pour reconstituer le cumul du registre.
    """
    date_max = getdate(date_max) if date_max else None
    out = {
        "date": str(date_max) if date_max else None,
        "compte": BANK_ACCOUNT,
        "erpnext": solde_erpnext(date_max),
        "registre": flux_registre(date_max=date_max),
        "banque_officiel": None,
        "banque_cumule": None,
        "ecarts": {},
        "diagnostics": [],
    }

    if depart is not None and depart_date:
        out["banque_cumule"] = solde_cumule(depart, depart_date, date_max)

    if capture:
        try:
            nom, image = fetch_capture_solde()
            lu = lire_solde_image(image, nom)
            out["capture"] = lu
            solde = lu.get("solde_comptable")
            if solde is not None:
                out["banque_officiel"] = flt(solde, 3)
            else:
                out["diagnostics"].append(
                    {"type": "capture", "raison": "solde comptable illisible sur la capture"})
        except Exception as e:
            out["diagnostics"].append({"type": "capture", "raison": str(e)[:200]})

    officiel, cumule = out["banque_officiel"], out["banque_cumule"]
    if officiel is not None:
        out["ecarts"]["banque_vs_erpnext"] = flt(officiel - out["erpnext"], 3)
        if cumule is not None:
            # Un ecart ici ne se corrige PAS par une ecriture : il signale des mouvements absents
            # du registre (fenetre d'export trop courte, tranche d'export perdue).
            out["ecarts"]["officiel_vs_cumule"] = flt(officiel - cumule, 3)
    if cumule is not None:
        out["ecarts"]["cumule_vs_erpnext"] = flt(cumule - out["erpnext"], 3)
    return out


def ecart_par_categorie(date_min=None, date_max=None) -> list:
    """Ou se loge l'ecart : montants du registre non encore identifies, par categorie.

    C'est la liste de travail — ce qui reste a comptabiliser pour que les deux soldes convergent.
    """
    filters = {"statut": ["!=", "Identifie"], "ignore_manuel": 0}
    if date_min and date_max:
        filters["date"] = ["between", [getdate(date_min), getdate(date_max)]]
    rows = frappe.db.get_all(
        DOCTYPE_MOUVEMENT, filters=filters, limit_page_length=0,
        fields=["categorie", "sens", "count(name) as nb", "sum(montant) as total"],
        group_by="categorie, sens", order_by="total desc")
    return [{"categorie": r.categorie or "(non classe)", "sens": r.sens,
             "nb": r.nb, "montant": flt(r.total, 3)} for r in rows]


def decomposition_ecart(date_max=None, limite: int = 40) -> dict:
    """D'ou vient l'ecart entre le solde banque et le solde ERPNext, poste par poste.

    CONVENTION DE SIGNE. L'ecart mesure `banque - ERPNext`. Comptabiliser un mouvement bancaire
    encore absent d'ERPNext le fait donc bouger ainsi :
      - un DEBIT non comptabilise  -> ERPNext baissera  -> l'ecart REMONTE  (effet +montant)
      - un CREDIT non comptabilise -> ERPNext montera   -> l'ecart DESCEND  (effet -montant)
    D'ou un effet attendu de `debits - credits`. Si l'ecart courant plus cet effet ne tombe pas
    a zero, le reliquat vient d'ecritures ERPNext sans contrepartie au releve, ou d'operations
    anterieures a la periode couverte par le registre : c'est le poste « inexplique ».

    Les ecarts de paiement sont comptes A PART : ces mouvements-la SONT comptabilises, mais pour
    un montant different de celui de la banque. Les compter avec les non-comptabilises reviendrait
    a les compter deux fois.
    """
    bornes = frappe.db.sql(
        "select min(`date`), max(`date`) from `tab%s`" % DOCTYPE_MOUVEMENT)[0]
    debut, fin = bornes[0], (getdate(date_max) if date_max else bornes[1])
    if not (debut and fin):
        return {"ecart": None, "postes": [], "lignes": []}

    # Flux vus par la BANQUE sur la periode couverte par le registre.
    bq = frappe.db.sql("""select round(sum(credit),3), round(sum(debit),3)
        from `tab%s` where `date` between %%s and %%s""" % DOCTYPE_MOUVEMENT,
        (debut, fin))[0]
    bq_in, bq_out = flt(bq[0], 3), flt(bq[1], 3)

    # Flux vus par ERPNext sur le MEME compte et la MEME periode. Sur un compte bancaire,
    # un debit comptable est une entree d'argent et un credit une sortie.
    erp = frappe.db.sql("""select round(sum(debit),3), round(sum(credit),3) from `tabGL Entry`
        where account=%s and is_cancelled=0 and posting_date between %s and %s""",
        (BANK_ACCOUNT, debut, fin))[0]
    erp_in, erp_out = flt(erp[0], 3), flt(erp[1], 3)

    manque_in, manque_out = flt(bq_in - erp_in, 3), flt(bq_out - erp_out, 3)

    postes = [
        {"cle": "sorties", "libelle": "Sorties vues en banque, absentes d'ERPNext",
         "banque": bq_out, "erpnext": erp_out, "montant": manque_out, "effet": manque_out},
        {"cle": "entrees", "libelle": "Entrées vues en banque, absentes d'ERPNext",
         "banque": bq_in, "erpnext": erp_in, "montant": manque_in, "effet": -manque_in},
    ]

    ecart_actuel = None
    dernier = dernier_solde()
    if dernier:
        ecart_actuel = flt(flt(dernier.solde_banque, 3) - solde_erpnext(dernier.date_solde), 3)

    effet = flt(sum(p["effet"] for p in postes), 3)
    # Ce que les deux postes n'expliquent pas : ecritures ERPNext anterieures au registre, ou
    # sans contrepartie au releve. On ne l'invente pas, on le nomme.
    inexplique = flt(-(ecart_actuel + effet), 3) if ecart_actuel is not None else None

    # Liste de travail : les mouvements que l'outil n'a rattaches a AUCUNE piece ERPNext.
    # On se fie a l'absence de document, pas au statut : « A verifier » designe un mouvement
    # categorise sans automatisation, ce qui ne veut pas dire qu'il manque en comptabilite.
    # Le test porte sur `document_type` et non `document_name` : un mouvement solde par un
    # Encaissement Paiement porte le type sans le nom (le document groupe plusieurs operations),
    # et il est bel et bien comptabilise.
    lignes = frappe.db.get_all(
        DOCTYPE_MOUVEMENT,
        filters={"ignore_manuel": 0, "document_type": ["is", "not set"],
                 "date": ["between", [debut, fin]]},
        fields=["name as cle", "date", "reference", "operation", "sens", "montant",
                "categorie", "statut", "raison"],
        order_by="montant desc", limit_page_length=limite)

    ecarts = frappe.db.get_all(
        DOCTYPE_MOUVEMENT, filters={"ignore_manuel": 0, "ecart": ["!=", 0]},
        fields=["name as cle", "date", "reference", "operation", "sens", "montant",
                "montant_document", "ecart"],
        order_by="abs(ecart) desc", limit_page_length=limite)

    return {"ecart": ecart_actuel, "periode": [str(debut), str(fin)], "postes": postes,
            "effet_attendu": effet, "inexplique": inexplique,
            "lignes": lignes, "ecarts": ecarts}


# ------------------------------------------------------------------ historisation

DOCTYPE_SOLDE = "BRS Solde Bancaire"


def capturer_solde(force: bool = False, depart: float = None, depart_date=None) -> dict:
    """Declenche une capture, la lit, et l'archive dans `BRS Solde Bancaire`.

    DEUX LECTURES, CONFRONTEES. Le service renvoie deja un montant parse cote scraper ; on lit en
    plus l'image par OpenAI. Quand les deux concordent, le solde est fiable ; quand ils divergent,
    on archive quand meme mais `concordance` reste a 0 et l'ecart doit etre tranche a la main.
    Une seule des deux lectures suffirait a se tromper en silence.
    """
    import requests

    from bank_retenue_sync.bank import movements as mv

    base, h = mv._base_url(), mv._headers()
    job = mv.wait_job(mv.start_job("/jobs/banque/solde/capture", {}), timeout=600)
    details = ((job.get("result") or {}).get("details")) or {}
    fichiers = mv.job_artifacts(job)
    if not fichiers:
        raise RuntimeError("capture du solde sans artefact")
    fichier = fichiers[-1]

    if not force and frappe.db.exists(DOCTYPE_SOLDE, {"fichier": fichier}):
        return {"statut": "deja_archive", "fichier": fichier}

    nom, image = fetch_capture_solde(fichier)
    lu = lire_solde_image(image, nom)
    return archiver_solde(fichier, image, lu, details, depart=depart, depart_date=depart_date)


def _horodatage_capture(fichier: str):
    """Instant de la capture, lu dans le nom du fichier produit par le service.

    Format : `solde_<compte>_<AAAA-MM-JJTHH-MM-SS>.png`. On prefere cette horloge a celle de
    Frappe : c'est le moment ou l'ecran a ete pris. Les deux conteneurs n'ont pas le meme fuseau
    (une heure d'ecart constatee), et dater la lecture avec la mauvaise horloge rendrait
    l'historique trompeur.
    """
    import re
    from datetime import datetime

    mo = re.search(r"(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})", fichier or "")
    if not mo:
        return None
    try:
        return datetime.strptime("%s %s:%s:%s" % mo.groups(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _parse_date_fr(v):
    """'10/08/2026' -> date. Le portail date en JJ/MM/AAAA."""
    from datetime import datetime

    s = str(v or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _montant_service(details: dict):
    """Montant parse par le scraper, converti depuis le format tunisien ('54 100,454')."""
    for brut in (details.get("montants") or []):
        try:
            return flt(str(brut).replace(" ", "").replace(" ", "").replace(",", "."), 3)
        except (TypeError, ValueError):
            continue
    return None


def archiver_solde(fichier: str, image: bytes, lu: dict, details: dict = None,
                   depart: float = None, depart_date=None) -> dict:
    """Cree le releve de solde, avec sa capture en piece jointe."""
    from frappe.utils.file_manager import save_file

    details = details or {}
    # /!\ NE PAS deduire le type de solde du champ que l'IA a rempli.
    # Constate en reel : la MEME capture, lue deux fois, a rendu une fois `solde_disponible`
    # et une fois `solde_comptable`, pour le meme montant (54 100,454). Le cadrage ne contient
    # aucun libelle, donc le modele devine — et son choix n'est pas stable. Le MONTANT, lui,
    # est fiable (deux lectures independantes concordent).
    # Le type n'est retenu que si le SERVICE a effectivement trouve des soldes libelles.
    ia = lu.get("solde_comptable")
    if ia is None:
        ia = lu.get("solde_disponible")
    ia = flt(ia, 3) if ia is not None else None

    libelles = {str(k).lower(): v for k, v in (details.get("soldes") or {}).items()}
    type_solde = "Non precise"
    for cle, libelle in (("comptable", "Comptable"), ("disponible", "Disponible")):
        if any(cle in k for k in libelles):
            type_solde = libelle
            break
    service = _montant_service(details)

    montant = ia if ia is not None else service
    if montant is None:
        raise RuntimeError("aucun solde lisible : ni l'IA ni le service n'ont rendu de montant")
    concordance = int(ia is not None and service is not None and abs(ia - service) < 0.005)
    # Le portail n'affiche pas toujours une date de solde : on retombe alors sur la date de
    # capture, faute de mieux. Mais on conserve a part la DERNIERE OPERATION vue par le portail,
    # qui dit jusqu'ou le solde court reellement — elle peut depasser le dernier mouvement
    # importe, et c'est alors du hors-registre, pas du non-comptabilise.
    date_solde = lu.get("date_solde") or details.get("date_solde") or frappe.utils.nowdate()
    derniere_op = _parse_date_fr(details.get("derniere_operation"))

    doc = frappe.new_doc(DOCTYPE_SOLDE)
    doc.update({
        "date_solde": getdate(date_solde),
        "compte": lu.get("compte") or details.get("compte"),
        "type_solde": type_solde,
        "solde_banque": montant,
        "devise": lu.get("devise") or "TND",
        "capture_datetime": _horodatage_capture(fichier) or frappe.utils.now_datetime(),
        "fichier": fichier,
        "concordance": concordance,
        "confiance": flt(lu.get("confiance") or 0),
        "montant_service": service,
        "derniere_operation": derniere_op,
    })
    doc.solde_erpnext = solde_erpnext(doc.date_solde)
    doc.ecart = flt(montant - doc.solde_erpnext, 3)
    if depart is not None and depart_date:
        doc.solde_cumule = solde_cumule(depart, depart_date, doc.date_solde)
        doc.ecart_import = flt(montant - doc.solde_cumule, 3)
    if not concordance:
        doc.notes = ("Lectures divergentes : IA=%s, service=%s. Trancher a la main."
                     % (ia, service))
    doc.insert(ignore_permissions=True)

    f = save_file(fichier.rsplit("/", 1)[-1], image, DOCTYPE_SOLDE, doc.name, is_private=1)
    doc.db_set("image", f.file_url, update_modified=False)
    frappe.db.commit()

    return {"statut": "archive", "name": doc.name, "date_solde": str(doc.date_solde),
            "derniere_operation": str(derniere_op) if derniere_op else None,
            "solde_banque": montant, "type_solde": type_solde,
            "solde_erpnext": doc.solde_erpnext, "ecart": doc.ecart,
            "concordance": bool(concordance), "confiance": doc.confiance,
            "fichier": fichier}


def dernier_solde():
    """Dernier releve archive, ou None."""
    rows = frappe.db.get_all(
        DOCTYPE_SOLDE, limit_page_length=1, order_by="date_solde desc, creation desc",
        fields=["name", "date_solde", "solde_banque", "solde_erpnext", "ecart", "type_solde",
                "concordance", "compte", "derniere_operation", "capture_datetime"])
    return rows[0] if rows else None


def historique(limite: int = 30) -> list:
    """Historique des soldes, du plus recent au plus ancien."""
    return frappe.db.get_all(
        DOCTYPE_SOLDE, limit_page_length=limite, order_by="date_solde desc, creation desc",
        fields=["name", "date_solde", "capture_datetime", "derniere_operation", "compte",
                "type_solde", "solde_banque", "solde_erpnext", "ecart", "ecart_import",
                "concordance", "confiance", "montant_service", "fichier"])
