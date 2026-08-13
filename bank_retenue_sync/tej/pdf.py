"""Le PDF du certificat : le telecharger chez le service, et le poser sur la bonne piece.

OU LE PDF DOIT ALLER, ET POURQUOI
---------------------------------
Sur la FACTURE quand elle existe, sinon sur la COMMANDE — et des que la commande est facturee, le
PDF est DEPLACE vers la facture. Ce n'est pas un detail d'archivage : la page « Retenue a la
source — Ventes » compte ses justificatifs en lisant `tabFile` sur les factures et les ecritures
de paiement. Un certificat range ailleurs est un certificat que le tableau declare manquant.

Aujourd'hui, 141 ecritures de retenue portent 3 justificatifs. Chaque PDF pose ici sort une
facture de ce compteur.

ON NE TELECHARGE QUE CE QU'ON PEUT RANGER
-----------------------------------------
Chaque PDF coute une session de scraping au portail. Les telecharger tous couterait 92 sessions
pour des documents qu'on ne saurait ou poser. Seuls les certificats rapproches sont servis, et par
lots plafonnes : une tache quotidienne ne doit pas durer une heure.
"""
from __future__ import annotations

import re

import frappe
import requests
from frappe.utils import flt

from bank_retenue_sync.bank.movements import _base_url, _headers

DOCTYPE = "Retenue Certificate"
ROUTE_JOB_PDF = "/jobs/tej/certificats-recus/pdf"
ROUTE_PDF = "/tej/certificats/%s/pdf"

LIMITE_PAR_PASSAGE = 20


def nom_fichier(reference: str) -> str:
    """Nom stable : c'est lui qui rend l'attachement idempotent."""
    return "certificat_ras_%s.pdf" % reference


def motif_fichier(reference: str) -> str:
    """Le motif qui reconnait ce certificat, SUFFIXE COMPRIS.

    ⚠️ FRAPPE NE GARDE PAS TOUJOURS LE NOM DEMANDE. Quand un fichier du meme nom existe deja sur le
    disque, `save_file` y ajoute l'heure : `certificat_ras_<ref>.pdf` devient
    `certificat_ras_<ref>101352.pdf`. Chercher l'egalite stricte ne retrouvait donc pas le fichier
    pose la veille, et le certificat repartait en telechargement pour finir en double exemplaire
    sur la meme facture — deux fois le meme justificatif au controle fiscal.
    """
    return "certificat_ras_%s%%" % reference


def telecharger(reference: str, declarant: str = None, timeout: int = 600) -> bytes:
    """Demande le PDF au service (scraping a la demande), puis le recupere."""
    from bank_retenue_sync.bank import movements

    body = {"reference": reference}
    if declarant:
        body["declarant"] = declarant
    job = movements.start_job(ROUTE_JOB_PDF, body)
    movements.wait_job(job, timeout=timeout)
    r = requests.get(_base_url() + ROUTE_PDF % reference, headers=_headers(), timeout=120)
    if r.status_code == 409:
        # Defaut cote service : deux fichiers pour la meme reference (un nomme avec le declarant,
        # l'autre sans), et la route refuse de choisir. Aucune route ne permet d'en designer un.
        # A signaler a TEJ ; ici on nomme le probleme au lieu de rendre un « erreur 409 » opaque.
        raise ValueError("le service detient plusieurs PDF pour %s et ne peut en choisir un "
                         "(a corriger cote tej-bank-service)" % reference)
    r.raise_for_status()
    if not r.content or not r.content.startswith(b"%PDF"):
        # Un HTML d'erreur enregistre comme PDF serait un justificatif illisible au controle
        # fiscal — mieux vaut echouer maintenant.
        raise ValueError("le service n'a pas rendu un PDF pour %s" % reference)
    return r.content


def cible(cert: dict) -> tuple:
    """Ou ranger le certificat : la facture d'abord, la commande a defaut. -> (doctype, nom)."""
    if cert.get("sales_invoice"):
        return ("Sales Invoice", cert["sales_invoice"])
    if cert.get("sales_order"):
        return ("Sales Order", cert["sales_order"])
    if cert.get("payment_entry"):
        # Dernier recours : l'ecriture elle-meme, que la page sait lire aussi.
        return ("Payment Entry", cert["payment_entry"])
    return (None, None)


def _fichier_existant(doctype: str, nom: str, reference: str):
    return frappe.db.get_value("File", {"attached_to_doctype": doctype, "attached_to_name": nom,
                                        "file_name": ["like", motif_fichier(reference)]},
                               ["name", "file_url"], as_dict=1)


def attacher(cert: dict, contenu: bytes, doctype: str, nom: str) -> dict:
    """Pose le PDF sur la piece. Idempotent : le certificat n'est jamais attache deux fois a la
    meme piece — meme si Frappe a renomme le fichier au passage (cf. `motif_fichier`)."""
    from frappe.utils.file_manager import save_file

    fichier = nom_fichier(cert["reference"])
    existant = _fichier_existant(doctype, nom, cert["reference"])
    if existant:
        return {"statut": "deja attache", "file_url": existant.file_url,
                "cible": "%s: %s" % (doctype, nom)}
    f = save_file(fichier, contenu, doctype, nom, is_private=1)
    return {"statut": "attache", "file_url": f.file_url, "cible": "%s: %s" % (doctype, nom)}


def a_traiter(limite: int = None) -> list:
    """Certificats rapproches, dans le perimetre, dont le PDF n'est pas encore range."""
    return frappe.db.get_all(
        DOCTYPE,
        filters={"hors_perimetre": 0, "pdf_attached_to_pe": 0, "anomalie": 0,
                 "match_status": ["in", ["Auto Matched", "Manually Matched"]]},
        fields=["name", "reference", "declarant", "sales_invoice", "sales_order", "payment_entry"],
        order_by="date_paiement desc", limit_page_length=limite or LIMITE_PAR_PASSAGE)


CHAMPS = ["name", "reference", "declarant", "sales_invoice", "sales_order", "payment_entry",
          "pdf_attached_to_pe", "match_status", "anomalie", "hors_perimetre"]


def _justificatif_manuel(doctype: str, nom: str):
    """Une piece jointe qui SE NOMME comme un certificat de retenue, deposee a la main.

    Meme regle que le verdict « certificat manuel » des ecritures orphelines : un seul endroit
    decide de ce qui merite le nom de justificatif, sinon les deux ecrans se contrediraient.
    """
    from bank_retenue_sync.tej import orphelines as O

    for f in frappe.db.get_all("File", filters={"attached_to_doctype": doctype,
                                                "attached_to_name": nom},
                               fields=["name", "file_name", "file_url"], order_by="creation"):
        if f.file_url and not f.file_name.lower().startswith("certificat_ras_") \
                and O.nomme_un_certificat(f.file_name):
            return f
    return None


def _pdf_auto() -> bool:
    return bool(frappe.db.get_single_value("Bank Retenue Sync Settings",
                                           "pdf_auto_apres_rapprochement"))


def traiter_un(certificat: str, insert: bool = True) -> dict:
    """Telecharge et range le PDF d'UN certificat. -> {statut, ...}.

    Appele juste apres un rapprochement ou une regularisation : la retenue vient d'etre logee, il
    lui manque sa preuve, et c'est le seul moment ou l'utilisateur pense encore a ce certificat.
    Les memes garde-fous que le traitement par lots — un certificat en anomalie ou hors perimetre
    n'a pas de justificatif a ranger.
    """
    cert = frappe.db.get_value(DOCTYPE, certificat, CHAMPS, as_dict=1)
    if not cert:
        return {"statut": "certificat inconnu", "reference": certificat}
    if cert.pdf_attached_to_pe:
        return {"statut": "deja attache", "reference": cert.reference}
    if cert.anomalie or cert.hors_perimetre:
        return {"statut": "ecarte", "reference": cert.reference}
    doctype, nom = cible(cert)
    if not doctype:
        return {"statut": "sans piece ou ranger", "reference": cert.reference}

    # LE JUSTIFICATIF POSE A LA MAIN COMPTE. Quand le portail refuse de rendre son PDF — il en
    # detient deux pour la meme reference et sa route ne sait pas choisir — le seul recours est de
    # deposer le certificat sur la piece soi-meme. Le reconnaitre evite de reclamer indefiniment un
    # document qui est deja la, et de relancer un client qui a deja fourni.
    manuel = _justificatif_manuel(doctype, nom)
    if manuel:
        frappe.db.set_value(DOCTYPE, cert.name, {
            "pdf_file": manuel.file_url, "pdf_attached_to_pe": 1,
            "pdf_attache_a": "%s: %s (depose a la main)" % (doctype, nom)}, update_modified=False)
        frappe.db.commit()
        return {"statut": "justificatif manuel deja present", "reference": cert.reference,
                "file_url": manuel.file_url, "cible": "%s: %s" % (doctype, nom)}

    if not insert:
        return {"statut": "a telecharger", "reference": cert.reference,
                "cible": "%s: %s" % (doctype, nom)}
    try:
        res = attacher(cert, telecharger(cert.reference, cert.declarant), doctype, nom)
        frappe.db.set_value(DOCTYPE, cert.name, {
            "pdf_file": res["file_url"], "pdf_attached_to_pe": 1,
            "pdf_attache_a": res["cible"]}, update_modified=False)
        frappe.db.commit()
        return {**res, "reference": cert.reference}
    except Exception as e:
        # En tache de fond, personne ne lit l'exception : elle doit rester dans le journal, et le
        # certificat repassera au lot suivant.
        frappe.log_error(title="PDF certificat RAS %s" % cert.reference,
                         message=frappe.get_traceback())
        return {"statut": "erreur", "reference": cert.reference, "message": str(e)[:160]}


def demander(certificat: str) -> dict:
    """Met le telechargement EN FILE plutot que de le faire tout de suite.

    ⚠️ CE N'EST PAS UN DETAIL DE CONFORT. Chaque PDF demande au service une session de scraping du
    portail : plusieurs minutes. Fait dans la requete, l'ecran de regularisation resterait fige tout
    ce temps, et un timeout HTTP laisserait croire a un echec alors que l'ecriture comptable, elle,
    est bien passee. `enqueue_after_commit` garantit en outre que le job ne demarre pas avant que la
    transaction qui vient de rapprocher le certificat ne soit ecrite.
    """
    if not _pdf_auto():
        return {"statut": "desactive"}
    frappe.enqueue("bank_retenue_sync.tej.pdf.traiter_un", queue="long", timeout=1800,
                   enqueue_after_commit=True, certificat=certificat, insert=True)
    return {"statut": "demande"}


def traiter(limite: int = None, insert: bool = True) -> dict:
    """Telecharge et range les PDF manquants. -> {traites, attaches, erreurs, detail}."""
    limite = limite or LIMITE_PAR_PASSAGE
    out = {"traites": 0, "attaches": 0, "erreurs": 0, "detail": []}
    for cert in a_traiter(limite):
        doctype, nom = cible(cert)
        if not doctype:
            out["detail"].append({"reference": cert["reference"], "statut": "sans piece ou ranger"})
            continue
        # ⚠️ MEME GARDE QUE `traiter_un`, ET C'EST ICI QU'IL COMPTE LE PLUS. Ce lot tourne tous les
        # jours sans personne devant l'ecran : sans lui, une facture portant deja le certificat
        # depose a la main en recevrait un second, telecharge au portail. Deux exemplaires du meme
        # justificatif sur une meme piece, c'est un credit d'impot qui parait justifie deux fois.
        manuel = _justificatif_manuel(doctype, nom)
        if manuel:
            out["deja_justifies"] = out.get("deja_justifies", 0) + 1
            if insert:
                frappe.db.set_value(DOCTYPE, cert["name"], {
                    "pdf_file": manuel.file_url, "pdf_attached_to_pe": 1,
                    "pdf_attache_a": "%s: %s (depose a la main)" % (doctype, nom)},
                    update_modified=False)
            out["detail"].append({"reference": cert["reference"],
                                  "statut": "justificatif manuel deja present",
                                  "cible": "%s: %s" % (doctype, nom),
                                  "file_url": manuel.file_url})
            continue
        out["traites"] += 1
        if not insert:
            out["detail"].append({"reference": cert["reference"], "statut": "a telecharger",
                                  "cible": "%s: %s" % (doctype, nom)})
            continue
        try:
            contenu = telecharger(cert["reference"], cert.get("declarant"))
            res = attacher(cert, contenu, doctype, nom)
            frappe.db.set_value(DOCTYPE, cert["name"], {
                "pdf_file": res["file_url"], "pdf_attached_to_pe": 1,
                "pdf_attache_a": res["cible"]}, update_modified=False)
            out["attaches"] += 1
            out["detail"].append({"reference": cert["reference"], **res})
        except Exception as e:
            # Un PDF indisponible n'invalide pas le rapprochement : on reessaiera demain. Mais il
            # doit LAISSER UNE TRACE — l'orchestrateur ne remonte que des compteurs, et sans journal
            # un certificat restait sans justificatif sans que rien ne dise pourquoi.
            out["erreurs"] += 1
            frappe.log_error(title="PDF certificat RAS %s" % cert["reference"],
                             message=frappe.get_traceback())
            out["detail"].append({"reference": cert["reference"], "statut": "erreur",
                                  "message": str(e)[:160]})
    return out


def texte_pdf(file_url: str) -> str:
    """Le texte d'un PDF, ou None s'il est illisible.

    None a un sens precis et il est important : fichier absent du disque, ou scan sans couche texte.
    Dans ces deux cas on ne peut RIEN prouver, donc on ne supprime rien.
    """
    import io
    import os

    if not file_url:
        return None
    chemin = frappe.get_site_path("private" if file_url.startswith("/private") else "public",
                                  *file_url.strip("/").split("/")[1:])
    if not os.path.exists(chemin):
        return None
    try:
        from pypdf import PdfReader

        with open(chemin, "rb") as f:
            lecteur = PdfReader(io.BytesIO(f.read()))
            return "\n".join((p.extract_text() or "") for p in lecteur.pages)
    except Exception:
        return None


def textes_concordent(a: str, b: str) -> bool:
    """Deux PDF portent-ils le meme texte ? Fonction pure : c'est elle qu'on teste.

    ⚠️ COMPARER LES OCTETS NE SERT A RIEN. Le portail regenere le certificat a chaque demande et y
    inscrit la date de generation : deux exemplaires du MEME document different d'une centaine
    d'octets de metadonnees (43 606 contre 43 722) et n'ont jamais la meme empreinte. Le texte, lui,
    est identique au caractere pres — verifie sur les couples reels.
    """
    if not a or not b:
        return False
    ecraser = lambda t: re.sub(r"\s+", " ", t).strip()
    return ecraser(a) == ecraser(b)


def comparer_justificatifs(url_portail: str, url_manuel: str) -> dict:
    """Le fichier du portail et le depot manuel sont-ils le meme document ? -> {identique, verdict}."""
    a, b = texte_pdf(url_portail), texte_pdf(url_manuel)
    if a is None or b is None:
        quel = "du portail" if a is None else "manuel"
        return {"identique": False,
                "verdict": "illisible : le fichier %s ne rend aucun texte (absent du disque ou "
                           "scan sans couche texte) — rien ne peut etre prouve" % quel}
    if textes_concordent(a, b):
        return {"identique": True, "verdict": "texte identique au caractere pres"}
    return {"identique": False,
            "verdict": "textes differents (%s contre %s caracteres) — ce ne sont pas les memes "
                       "documents" % (len(a), len(b))}


def doublons(insert: bool = False, garder: str = "portail") -> dict:
    """Les pieces qui portent DEUX fois le meme justificatif : le PDF du portail et un depot manuel.

    Le garde-fou ci-dessus empeche d'en creer de nouveaux ; celui-ci montre ceux d'avant. Deux
    exemplaires du meme certificat sur une facture, c'est un credit d'impot qui parait justifie
    deux fois au controle — et personne ne le voit, puisque les deux fichiers sont valables.

    ⚠️ ON NE SUPPRIME QUE CE QU'ON A PROUVE IDENTIQUE. Chaque couple est confronte par son TEXTE
    (`comparer_justificatifs`) : tant que les deux documents ne se sont pas reveles identiques, les
    deux restent en place et la ligne est signalee. Un justificatif detruit ne se recupere pas, et
    la charge de la preuve est du cote de celui qui efface.

    `garder` : « portail » (defaut) conserve l'exemplaire telecharge, machine-verifiable et
    retelechargeable ; « manuel » conserve le depot de l'equipe. Rien ne se supprime sans
    `insert=1`.
    """
    if garder not in ("portail", "manuel"):
        frappe.throw("garder doit valoir « portail » ou « manuel »")
    out = {"pieces": 0, "supprimes": 0, "verifies": 0, "a_verifier": 0, "garder": garder,
           "detail": []}
    certs = frappe.db.get_all(DOCTYPE, filters={"pdf_attached_to_pe": 1},
                              fields=["name", "reference", "sales_invoice", "sales_order",
                                      "payment_entry", "customer"], limit_page_length=0)
    for cert in certs:
        doctype, nom = cible(cert)
        if not doctype:
            continue
        fichiers = frappe.db.get_all("File", filters={"attached_to_doctype": doctype,
                                                      "attached_to_name": nom},
                                     fields=["name", "file_name", "file_url"], order_by="creation")
        from bank_retenue_sync.tej import orphelines as O

        portail = [f for f in fichiers
                   if (f.file_name or "").lower().startswith("certificat_ras_")]
        manuels = [f for f in fichiers
                   if f not in portail and O.nomme_un_certificat(f.file_name)]
        if not (portail and manuels):
            continue
        out["pieces"] += 1
        comparaison = comparer_justificatifs(portail[0].file_url, manuels[0].file_url)
        ligne = {"reference": cert["reference"], "customer": cert.get("customer"),
                 "cible": "%s: %s" % (doctype, nom),
                 "portail": [f.file_name for f in portail],
                 "manuel": [f.file_name for f in manuels],
                 "identique": comparaison["identique"], "verdict": comparaison["verdict"]}
        if comparaison["identique"]:
            out["verifies"] += 1
        else:
            out["a_verifier"] += 1
            ligne["statut"] = "conserve en double : identite non prouvee"
            out["detail"].append(ligne)
            continue
        if insert:
            a_retirer = portail if garder == "manuel" else manuels
            reste = manuels[0] if garder == "manuel" else portail[0]
            for f in a_retirer:
                frappe.delete_doc("File", f.name, ignore_permissions=True)
                out["supprimes"] += 1
            frappe.db.set_value(DOCTYPE, cert["name"], {
                "pdf_file": reste.file_url,
                "pdf_attache_a": "%s: %s%s" % (doctype, nom,
                                               " (depose a la main)" if garder == "manuel" else "")},
                update_modified=False)
            ligne["statut"] = ("exemplaire %s retire, %s conserve"
                               % ("du portail" if garder == "manuel" else "manuel", garder))
        out["detail"].append(ligne)
    return out


def deplacer_vers_facture(insert: bool = True) -> dict:
    """Deplace vers la facture les PDF poses sur une commande depuis facturee.

    Demande explicitement : la comptabilite travaille sur les factures, et un justificatif reste
    sur la commande y serait invisible. Le fichier est DEPLACE, pas copie — deux exemplaires du
    meme certificat sur deux pieces se compteraient deux fois au controle.
    """
    out = {"deplaces": 0, "detail": []}
    certs = frappe.db.get_all(DOCTYPE, filters={"sales_order": ["is", "set"],
                                                "pdf_attached_to_pe": 1},
                              fields=["name", "reference", "sales_order", "sales_invoice"],
                              limit_page_length=0)
    for cert in certs:
        if cert.get("sales_invoice"):
            continue
        facture = frappe.db.sql("""select distinct sii.parent from `tabSales Invoice Item` sii
                                   join `tabSales Invoice` si on si.name = sii.parent
                                   where si.docstatus = 1 and sii.sales_order = %s""",
                                cert["sales_order"], as_dict=1)
        if len(facture) != 1:
            # Zero facture : rien a faire. Plusieurs : la commande a ete facturee en morceaux, et
            # choisir a la place de l'utilisateur rangerait la preuve au mauvais endroit.
            continue
        cible_facture = facture[0].parent
        fichier = _fichier_existant("Sales Order", cert["sales_order"], cert["reference"])
        if not fichier:
            continue
        out["deplaces"] += 1
        out["detail"].append({"reference": cert["reference"], "de": cert["sales_order"],
                              "vers": cible_facture})
        if insert:
            frappe.db.set_value("File", fichier.name, {"attached_to_doctype": "Sales Invoice",
                                                       "attached_to_name": cible_facture},
                                update_modified=False)
            frappe.db.set_value(DOCTYPE, cert["name"], {
                "sales_invoice": cible_facture,
                "pdf_attache_a": "Sales Invoice: %s" % cible_facture}, update_modified=False)
    return out
