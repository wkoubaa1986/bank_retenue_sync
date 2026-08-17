"""Constituer le dossier du mois : trois classeurs, les PDF des factures, le tout dans une archive.

Remplace l'arborescence que `get_invoices_pdf.py` deposait sur le poste de celui qui lancait le
script (`../MM-YYYY/`). Meme contenu, meme nommage — mais dans le bench, donc sauvegarde, datee,
et lisible par quelqu'un d'autre.

⚠️ UN DOSSIER EST UN GEL, PAS UNE VUE. Le registre bancaire bouge cinq fois par jour : un
orphelin d'aujourd'hui sera identifie demain. Regenerer mars en juin ne redonnerait donc PAS le
dossier remis au comptable en avril. L'archive porte l'horodatage de sa constitution et n'ecrase
jamais la precedente — c'est une version de plus, pas une correction.

⚠️ ET LES PDF NE SE DEMANDENT PAS EN HTTP. Le script d'origine appelait `download_pdf` sur son
propre site, une requete par facture. Ici c'est `frappe.get_print(as_pdf=True)`, et dans un job :
deux cents rendus wkhtmltopdf ne tiennent pas dans le temps d'une requete web.
"""
from __future__ import annotations

import io
import zipfile

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from bank_retenue_sync.bank import registry
from bank_retenue_sync.facturation import charges as M_charges
from bank_retenue_sync.facturation import factures as M_factures
from bank_retenue_sync.facturation import periode

PRECISION = 3

# Le format d'impression et l'en-tete du script d'origine. Absents du site, on retombe sur le
# format par defaut plutot que d'echouer : un PDF sans logo vaut mieux que pas de dossier.
FORMAT_IMPRESSION = "Aqua World Facture"
ENTETE = "Official"
LANGUE = "fr"

DOSSIER_PDF = "Chiffre D'affaire Facturé"
DUREE_ETAT = 6 * 3600


# ------------------------------------------------------------------ etat du job


def _cle_etat(mois: str) -> str:
    return "brs:dossier:%s" % mois


def _poser_etat(mois: str, **valeurs) -> dict:
    etat = dict(lire_etat(mois), **valeurs)
    frappe.cache().set_value(_cle_etat(mois), etat, expires_in_sec=DUREE_ETAT)
    return etat


def lire_etat(mois: str) -> dict:
    # `expires=True` : la valeur est posee avec une duree de vie, donc absente du cache
    # local — sans ce drapeau, un miss lu avant l ecriture masque l etat pour la requete.
    return frappe.cache().get_value(_cle_etat(mois), expires=True) or {}


def archives(mois: str) -> list:
    """Les archives deja constituees pour ce mois, la plus recente d'abord."""
    return frappe.get_all("File",
                          filters={"file_name": ["like", "Dossier facturation %s%%" % mois]},
                          fields=["name", "file_name", "file_url", "creation", "file_size"],
                          order_by="creation desc", limit_page_length=20)


# ------------------------------------------------------------------ classeurs


def _titre_feuille(titre: str) -> str:
    """Excel refuse : \\ / ? * [ ] dans un nom d'onglet, et le limite a 31 caracteres.

    Un bloc s'appelle « Retenues / Ventes » — le slash suffisait a faire echouer la constitution
    entiere apres le rendu de tous les PDF, donc au pire moment possible.
    """
    propre = "".join("-" if c in ':\\/?*[]' else c for c in str(titre or ""))
    return propre[:31].strip() or "Feuille"


def _classeur(feuilles: list) -> bytes:
    """[(titre, [ligne, …])] -> bytes xlsx. Les nombres restent des nombres."""
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for titre, lignes in feuilles:
        ws = wb.create_sheet(title=_titre_feuille(titre))
        for ligne in lignes:
            ws.append(list(ligne))
    flux = io.BytesIO()
    wb.save(flux)
    return flux.getvalue()


def _feuille_factures(donnees: dict) -> list:
    """Le recapitulatif des ventes, aux colonnes du fichier que le comptable connait.

    ⚠️ PAS DE COLONNE DE CONTROLE ICI. Les ecarts de ventilation, les especes presumees et le
    reste du sont des outils de RELECTURE : ils vivent a l ecran, ou on peut les creuser. Dans
    le classeur remis, ils encombrent — et une colonne de plus, c est une colonne que quelqu un
    additionnera un jour par erreur.
    """
    entete = ["Nom Facture", "posting_date", "customer", "Base 19%", "TVA 19%",
              "Base 7%", "TVA 7%", "Total TTC", "Paiements"]

    def par_taux(ventilation, taux):
        for t in ventilation["taux"]:
            if abs(t["taux"] - taux) < 0.01:
                return t
        return None

    lignes = [entete]
    for f in donnees["factures"]:
        v19 = par_taux(f["ventilation"], 19.0)
        v7 = par_taux(f["ventilation"], 7.0)
        lignes.append([
            f["nom_dossier"], f["date"], f["client_nom"],
            v19["base"] if v19 else 0.0, v19["tva"] if v19 else 0.0,
            v7["base"] if v7 else 0.0, v7["tva"] if v7 else 0.0,
            f["ttc"],
            _texte_paiements(f["paiements"]),
        ])

    t = donnees["totaux"]
    g19, g7 = par_taux(t, 19.0), par_taux(t, 7.0)
    lignes.append([])
    lignes.append(["TOTAL", "", "%d factures" % t["nombre"],
                   g19["base"] if g19 else 0.0, g19["tva"] if g19 else 0.0,
                   g7["base"] if g7 else 0.0, g7["tva"] if g7 else 0.0,
                   t["ttc"], ""])
    return lignes


def _texte_paiements(paiements: list) -> str:
    """« Chèque 0000797-BNA / réf. 90028077 / 1552.32 » — un règlement par ligne."""
    out = []
    for p in paiements or []:
        morceaux = [p["mode"]]
        if p.get("piece"):
            morceaux.append(p["piece"])
        elif p["nombre"] > 1:
            morceaux.append("%d versements" % p["nombre"])
        if p.get("banque"):
            morceaux.append("réf. %s" % p["banque"])
        morceaux.append(str(p["montant"]))
        if p.get("presume"):
            morceaux.append("dont %s présumé" % p["presume"])
        out.append(" / ".join(morceaux))
    return "\n".join(out)


def _feuille_charges(donnees: dict) -> list:
    """Les trois blocs de charges sur UNE feuille, separes par leur intitule.

    ⚠️ UNE FEUILLE PAR BLOC, C EST TROIS FILTRES A REFAIRE. Depenses, achats et retenues se
    lisent ensemble — on cherche une piece, pas une categorie. Un intertitre suffit a les
    distinguer, et le tri d Excel retrouve le reste.

    Ni « Document » ni « Justificatif requis » : le premier est un identifiant interne dont le
    comptable n a rien a faire, le second une consigne de saisie qui n a pas sa place dans une
    piece remise.
    """
    # « Référence », « Type » et les colonnes du controle IA sortent du fichier : la premiere
    # fait doublon avec la reference d export, les autres sont des outils de relecture.
    entete = ["Référence export", "Date", "Tiers", "Catégorie", "Mode",
              "Valeur HT", "TVA 7%", "TVA 19%", "TVA", "Valeur TTC", "Retenue", "Justificatifs"]
    lignes = [entete]
    for bloc in donnees["blocs"]:
        t = bloc["totaux"]
        lignes.append([])
        lignes.append([bloc["titre"].upper(), "%d ligne(s)" % t["nombre"]])
        for l in bloc["lignes"]:
            lignes.append([
                l.get("reference_export") or "",
                l["date"], l["tiers"], l["categorie"], l["mode"],
                l["ht"], l["tva7"], l["tva19"], l["tva"], l["ttc"], l["retenue"],
                " · ".join(j["file_name"] for j in l["justificatifs"])
                or (l.get("exemption") or "AUCUN"),
            ])
        lignes.append(["TOTAL %s" % bloc["titre"], "", "", "", "",
                       t["ht"], "", "", t["tva"], t["ttc"], t["retenue"],
                       "%d sans justificatif exigible" % t["sans_justificatif"]])

    g = donnees["totaux"]
    lignes.append([])
    lignes.append(["TOTAL GÉNÉRAL", "%d ligne(s)" % g["nombre"], "", "", "",
                   g["ht"], "", "", g["tva"], g["ttc"], g["retenue"],
                   "%d sans justificatif exigible · %d exemptée(s)"
                   % (g["sans_justificatif"], g["exemptes"])])
    return lignes


def _feuilles_caisse(mois: str) -> list:
    """La caisse : le mois en detail, puis sa derive depuis le 1er janvier.

    ⚠️ UN SOLDE DE CAISSE NE SE LIT PAS SUR UN MOIS. Ce qui se controle, c est sa DERIVE — une
    caisse qui monte de 3 000 DT par mois pendant six mois pose une question qu aucun mois pris
    isolement ne pose. D ou la feuille « Évolution », de janvier au mois clos.
    """
    from bank_retenue_sync.facturation import caisse as M_caisse

    d = M_caisse.situation(mois)
    if not d.get("disponible"):
        return [("Caisse", [["Caisse indisponible"], [d.get("message") or ""]])]

    t, o = d["totaux"], d.get("origine") or {}
    resume = [
        ["Indicateur", "Montant"],
        ["Solde d'ouverture au %s" % (o.get("veille") or o.get("date") or ""), t["ouverture"]],
        ["Entrées ventes espèces", t["entrees"]],
        ["Sorties achats", t["achats"]],
        ["Sorties dépenses", t["depenses"]],
        ["Versements en banque", t["versements"]],
        ["Mouvement du mois", t["mouvement"]],
        ["Solde de clôture", t["cloture"]],
        [],
        ["Origine de la caisse", o.get("date") or ""],
        ["Solde à l'origine", o.get("solde")],
        ["Cumul des mouvements jusqu'à l'ouverture", o.get("cumul_anterieur")],
    ]

    evo = M_caisse.evolution(mois)
    feuille_evo = [["Date", "Nature", "Libellé", "Pièce", "Entrée", "Sortie", "Solde"]]
    if evo.get("disponible"):
        feuille_evo.append(["Ouverture au %s" % evo["periode"]["debut"], "", "", "", "", "",
                            evo["ouverture"]])
        for pt in evo["points"]:
            feuille_evo.append([pt["date"], pt["nature"], pt["libelle"], pt["piece"],
                                pt["entree"] or "", pt["sortie"] or "", pt["solde"]])
        c = evo["cumul"]
        feuille_evo.append([])
        feuille_evo.append(["CUMUL %s" % evo["annee"], "", "", "",
                            c["entrees"], c["achats"] + c["depenses"] + c["versements"],
                            evo["cloture"]])
    else:
        feuille_evo.append([evo.get("message") or "indisponible"])

    def table(lignes, colonnes):
        return [[libelle for libelle, _ in colonnes]] + \
               [[l.get(champ) for _, champ in colonnes] for l in lignes]

    return [
        ("Résumé", resume),
        ("Évolution", feuille_evo),
        ("Entrées", table(d["entrees"], [("Date", "date"), ("N° facture", "invoice_number"),
                                         ("Client", "client"), ("Montant", "montant")])),
        ("Achats", table(d["achats"], [("Date", "date"), ("N° facture", "invoice_number"),
                                       ("Fournisseur", "supplier"), ("Montant", "montant")])),
        ("Dépenses", table(d["depenses"], [("Date", "date"),
                                           ("Écriture", "journal_entry_number"),
                                           ("Libellé", "description"), ("Montant", "montant")])),
        ("Versements", table(d["versements"] + [dict(v, description="ÉCARTÉ — %s"
                                                     % (v.get("description") or ""))
                                                for v in d.get("versements_ecartes") or []],
                             [("Date", "date"), ("Écriture", "journal_entry_number"),
                              ("Libellé", "description"), ("Montant", "montant")])),
    ]


def _feuilles_banque(mois: str) -> list:
    """Le releve du mois, tel que l'ecran le montre.

    Rien n'est recalcule : `BRS Bank Movement` porte deja statut, raison, ecart et piece liee,
    et le registre est tenu a jour cinq fois par jour. C'est une lecture, pas un rapprochement.
    """
    from bank_retenue_sync.facturation import reglement

    debut, fin = periode.bornes(mois)
    # ⚠️ CE QU ON LIT N EST PAS CE QU ON MONTRE. La colonne Règlement se construit a partir de
    # `document_type`, `document_name`, `regle` et `groupe` : les retirer de la requete — parce
    # qu ils n apparaissent pas dans le fichier — vidait la colonne sur toutes les lignes.
    # On les LIT donc, et on ne les EXPORTE pas.
    champs = ["name as cle", "date", "operation", "reference", "debit", "credit",
              "categorie", "regle", "groupe", "statut", "document_type", "document_name",
              "montant_document", "ecart", "raison"]
    entete = ["Date", "Libellé", "Référence", "Débit", "Crédit", "Règlement"]

    lignes = frappe.get_all(registry.DOCTYPE,
                            filters={"date": ["between", [debut, fin]]},
                            fields=champs, order_by="`date` asc, name asc",
                            limit_page_length=0)

    # Le detail de ce que chaque mouvement a paye, colonne en plus plutot qu'observation
    # ecrasee : le fichier remis au comptable doit porter les deux, la raison d'identification
    # ET ce qui a ete solde.
    details = reglement.details_par_mouvement(lignes)

    def en_tableau(rows):
        return [entete] + [[str(r.get("date") or ""), r.get("operation"), r.get("reference"),
                            r.get("debit"), r.get("credit"),
                            reglement.texte(details.get(r.get("cle")))]
                           for r in rows]

    # ⚠️ UNE SEULE FEUILLE. Les orphelins, les non-rapproches ERP et le resume sont des vues de
    # TRAVAIL : elles vivent a l ecran, ou on les traite. Dans le classeur remis, elles font
    # quatre onglets que personne n ouvre — et qui laissent croire a quatre listes a verifier.
    return [("Banque Movement", en_tableau(lignes))]


# ------------------------------------------------------------------ PDF


def _justificatifs_du_mois(donnees_charges: dict) -> list:
    """[(chemin dans l'archive, octets)] — les pieces jointes des charges, par sous-dossier.

    Meme arborescence que l'outil d'origine : tout dans « Dépenses/ », les certificats de
    retenue dans leur propre sous-dossier. C'est le classement que le comptable connait.
    """
    voulus = {}
    for bloc in donnees_charges["blocs"]:
        for ligne in bloc["lignes"]:
            for justificatif in ligne["justificatifs"]:
                # ⚠️ DEUX SOUS-DOSSIERS DE RETENUE, PAS UN. Le certificat d une retenue SUBIE
                # sur nos ventes et celui d une retenue QUE NOUS OPERONS sur un achat sont deux
                # pieces fiscales distinctes, rangees a part depuis toujours. Le nom du fichier
                # les separe : un certificat porte le prefixe `certificat_ras_`, et son bloc
                # d origine dit de quel cote il tombe.
                certificat = (justificatif["file_name"] or "").startswith("certificat_ras_")
                if bloc["cle"] == "retenues":
                    sous = "Retenue à la source Vente"
                elif certificat:
                    sous = "Retenue à la source Achat"
                else:
                    sous = ""
                voulus.setdefault(justificatif["file_url"],
                                  (sous, justificatif["file_name"]))
    if not voulus:
        return []

    out, vus = [], set()
    for f in frappe.get_all("File", filters={"file_url": ["in", list(voulus)]},
                            fields=["name", "file_url"], limit_page_length=0):
        sous, nom = voulus[f.file_url]
        chemin = "%s/%s" % (sous, nom) if sous else nom
        if chemin in vus:
            continue
        try:
            out.append((chemin, frappe.get_doc("File", f.name).get_content()))
            vus.add(chemin)
        except Exception:
            # Une piece illisible ne doit pas emporter le dossier : elle manquera, et la liste
            # des charges la nommera quand meme.
            continue
    return out


def _releve_bancaire(mois: str) -> list:
    """Le releve MENSUEL de la banque, en PDF, via le service TEJ.

    ⚠️ C'EST LA SEULE PIECE QUI SORT DU BENCH. Tout le reste du dossier se lit en base ; celle-ci
    peut demander au service de piloter le portail de la banque, ce qui prend plusieurs minutes
    et peut echouer pour des raisons qui ne nous regardent pas. L'echec est donc rendu, jamais
    fatal : un dossier sans releve reste un dossier, un dossier interrompu n'est rien.
    """
    from bank_retenue_sync.bank import movements

    return [("Relevé bancaire/releve_%s.pdf" % mois, movements.releve_pdf(mois))]


def _format_disponible() -> str | None:
    return FORMAT_IMPRESSION if frappe.db.exists("Print Format", FORMAT_IMPRESSION) else None


def _entete_disponible() -> str | None:
    return ENTETE if frappe.db.exists("Letter Head", ENTETE) else None


def _pdf(nom_facture: str, format_impression, entete) -> bytes:
    return frappe.get_print("Sales Invoice", nom_facture, print_format=format_impression,
                            letterhead=entete, no_letterhead=0 if entete else 1, as_pdf=True)


def _nettoyer(nom: str) -> str:
    """Un nom de fichier sur lequel aucun systeme ne trebuche."""
    interdits = '\\/:*?"<>|\r\n\t'
    return "".join(("-" if c in interdits else c) for c in (nom or "")).strip() or "sans-nom"


# ------------------------------------------------------------------ constitution


def constituer(mois: str, avec_pdf: bool = True, avec_releve: bool = False) -> dict:
    """Construit l'archive du mois et l'enregistre en fichier prive. Appele dans un job.

    ⚠️ UN ECHEC DOIT SE VOIR. Sans ce garde, une exception dans le job laissait l'etat bloque sur
    « en cours » : la barre de progression tournait indefiniment a l'ecran, et rien ne disait que
    la constitution etait morte. On enregistre donc l'echec avant de laisser l'exception remonter
    au journal des erreurs — l'ecran saura le dire, et le bouton se reactivera.
    """
    try:
        return _constituer(mois, avec_pdf, avec_releve)
    except Exception as e:
        _poser_etat(periode.normaliser(mois), statut="echec", etape="échec",
                    erreur=str(e)[:400], fin=str(now_datetime()))
        raise


def _constituer(mois: str, avec_pdf: bool, avec_releve: bool) -> dict:
    mois = periode.normaliser(mois)
    debut = now_datetime()
    _poser_etat(mois, statut="en cours", etape="lecture des factures", avancement=5,
                debut=str(debut), erreurs=[])

    donnees_factures = M_factures.liste(mois)
    _poser_etat(mois, etape="lecture des charges", avancement=15)
    # Les controles DEJA passes enrichissent le classeur ; aucun PDF n'est relu ici — la
    # constitution d'un dossier ne doit pas declencher des dizaines d'appels payants.
    from bank_retenue_sync.facturation import controle

    donnees_charges = controle.attacher_aux_lignes(M_charges.liste(mois))
    _poser_etat(mois, etape="lecture du registre bancaire", avancement=25)
    feuilles_banque = _feuilles_banque(mois)

    erreurs = []
    flux = io.BytesIO()
    with zipfile.ZipFile(flux, "w", zipfile.ZIP_DEFLATED) as archive:
        racine = mois
        archive.writestr("%s/Facturation %s.xlsx" % (racine, mois),
                         _classeur([("Facturation", _feuille_factures(donnees_factures))]))
        archive.writestr("%s/Liste des Charges %s.xlsx" % (racine, mois),
                         _classeur([("Charges", _feuille_charges(donnees_charges))]))
        archive.writestr("%s/Identification Bancaire %s.xlsx" % (racine, mois),
                         _classeur(feuilles_banque))
        archive.writestr("%s/Caisse espèces %s.xlsx" % (racine, mois),
                         _classeur(_feuilles_caisse(mois)))

        _poser_etat(mois, etape="justificatifs des charges", avancement=28)
        for chemin, octets in _justificatifs_du_mois(donnees_charges):
            archive.writestr("%s/Dépenses/%s" % (racine, chemin), octets)

        if avec_releve:
            _poser_etat(mois, etape="relevé bancaire (portail)", avancement=30)
            try:
                for chemin, octets in _releve_bancaire(mois):
                    archive.writestr("%s/%s" % (racine, chemin), octets)
            except Exception as e:
                erreurs.append("relevé bancaire : %s" % str(e)[:160])

        if avec_pdf:
            format_impression, entete = _format_disponible(), _entete_disponible()
            total = len(donnees_factures["factures"]) or 1
            langue = frappe.local.lang
            frappe.local.lang = LANGUE
            try:
                for i, f in enumerate(donnees_factures["factures"], 1):
                    nom = "%s/%s/%s - %s.pdf" % (racine, DOSSIER_PDF, _nettoyer(f["nom_dossier"]),
                                                 _nettoyer(f["client_nom"]))
                    try:
                        archive.writestr(nom, _pdf(f["facture"], format_impression, entete))
                    except Exception as e:
                        erreurs.append("%s : %s" % (f["facture"], str(e)[:160]))
                    if i % 5 == 0 or i == total:
                        _poser_etat(mois, etape="PDF %d/%d" % (i, total),
                                    avancement=30 + int(65 * i / total), erreurs=erreurs)
            finally:
                frappe.local.lang = langue

    with zipfile.ZipFile(flux) as _lecture:
        archive_noms = _lecture.namelist()
    _poser_etat(mois, etape="enregistrement de l'archive", avancement=97, erreurs=erreurs)
    horodatage = debut.strftime("%Y%m%d-%H%M")
    fichier = frappe.get_doc({
        "doctype": "File",
        "file_name": "Dossier facturation %s (%s).zip" % (mois, horodatage),
        "is_private": 1,
        "content": flux.getvalue(),
    }).insert(ignore_permissions=True)

    etat = _poser_etat(
        mois, statut="termine", etape="terminé", avancement=100, erreur=None,
        fichier=fichier.file_url, nom_fichier=fichier.file_name,
        fichier_id=fichier.name, fin=str(now_datetime()), erreurs=erreurs,
        resume={"factures": donnees_factures["totaux"]["nombre"],
                "charges": donnees_charges["totaux"]["nombre"],
                "sans_justificatif": donnees_charges["totaux"]["sans_justificatif"],
                "pieces": len(archive_noms), "taille": len(flux.getvalue()),
                "secondes": int((now_datetime() - debut).total_seconds())})
    frappe.db.commit()
    return etat


def _bloque(etat: dict) -> bool:
    """Un job en cours bloque un second lancement — mais seulement s'il vit encore.

    ⚠️ UN JOB MORT NE DOIT PAS VERROUILLER LE MOIS POUR SIX HEURES. Le drapeau « en cours » vit
    dans le cache : si le worker tombe, personne ne le remet a zero, et le bouton reste grise
    sans que rien n'explique pourquoi. Passe deux heures, on considere que le job est perdu et
    on autorise une nouvelle tentative.
    """
    if etat.get("statut") != "en cours":
        return False
    debut = etat.get("debut")
    if not debut:
        return True
    try:
        age = (now_datetime() - frappe.utils.get_datetime(debut)).total_seconds()
    except Exception:
        return True
    return age < 2 * 3600


def lancer(mois: str, avec_pdf: bool = True, avec_releve: bool = False) -> dict:
    """Met la constitution en file d'attente. -> l'etat initial."""
    mois = periode.normaliser(mois)
    if _bloque(lire_etat(mois)):
        frappe.throw(_("Un dossier est déjà en cours de constitution pour {0}.").format(mois))
    _poser_etat(mois, statut="en cours", etape="en file d'attente", avancement=0, erreurs=[],
                fichier=None, debut=str(now_datetime()))
    frappe.enqueue("bank_retenue_sync.facturation.dossier.constituer", queue="long",
                   timeout=3600, mois=mois, avec_pdf=avec_pdf, avec_releve=avec_releve)
    return lire_etat(mois)
