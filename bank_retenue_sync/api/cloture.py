"""API de la page « Facturation mensuelle ».

Un onglet, une methode. Le JS ne calcule aucun montant : il affiche ce que ces methodes rendent,
comme le fait deja `api/mouvements.py` pour l'identification bancaire.

⚠️ AUCUNE METHODE DE CE MODULE N'ECRIT EN COMPTABILITE. Les seules qui touchent a quoi que ce
soit touchent des FICHIERS — `generer_dossier` en produit un, `supprimer_dossier` en efface un —
ou l'etat d'envoi du mois. Les gestes qui touchent aux pieces — l'ecriture de journal du bilan
partenaire, la logique de dette — ne sont pas ici, et c'est delibere : ils sont irreversibles et
meritent leur propre ecran de confirmation.
"""
from __future__ import annotations

import frappe
from frappe import _

from bank_retenue_sync.facturation import caisse as M_caisse
from bank_retenue_sync.facturation import charges as M_charges
from bank_retenue_sync.facturation import dossier as M_dossier
from bank_retenue_sync.facturation import factures as M_factures
from bank_retenue_sync.facturation import periode
from bank_retenue_sync.facturation import retards as M_retards

ROLES_LECTURE = ["System Manager", "Accounts Manager", "Accounts User"]
ROLES_ECRITURE = ["System Manager", "Accounts Manager"]


def _guard(ecriture: bool = False):
    frappe.only_for(ROLES_ECRITURE if ecriture else ROLES_LECTURE)


@frappe.whitelist()
def get_contexte() -> dict:
    """Ce dont la page a besoin a l'ouverture : les mois offerts et celui propose par defaut."""
    _guard()
    mois = periode.normaliser(None)
    return {
        "mois": mois,
        "libelle": periode.libelle(mois),
        "mois_offerts": periode.derniers(24),
        "peut_generer": any(r in frappe.get_roles() for r in ROLES_ECRITURE),
    }


@frappe.whitelist()
def get_caisse(mois=None) -> dict:
    _guard()
    return M_caisse.situation(mois)


@frappe.whitelist()
def get_banque(mois=None) -> dict:
    """TOUT le releve du mois, plus la couverture et les ecarts.

    ⚠️ PAS D'APERCU TRONQUE ICI. Une premiere version ne servait que quinze lignes, en reprenant
    la pagination de la page « Identification bancaire » — qui, elle, feuillette un registre sans
    fin. Un dossier mensuel, non : il doit montrer le mois ENTIER, sinon on ne peut ni le
    controler ni le remettre. Un mois de releve se compte en dizaines de lignes, pas en milliers.

    Le registre est lu une seule fois ; couverture, orphelins et ecarts se deduisent des memes
    lignes. Aucun rapprochement n'est refait : `BRS Bank Movement` porte deja statut, raison,
    piece liee et ecart, tenus a jour cinq fois par jour.
    """
    _guard()
    from bank_retenue_sync.api import mouvements as M
    from bank_retenue_sync.bank import registry

    mois = periode.normaliser(mois)
    debut, fin = periode.bornes(mois)

    lignes = frappe.db.get_all(registry.DOCTYPE,
                               filters={"date": ["between", [debut, fin]]},
                               fields=M._FIELDS, order_by="`date` asc, creation asc",
                               limit_page_length=0)

    kpi = {}
    for l in lignes:
        case = kpi.setdefault(l.get("sens") or "Debit", {}).setdefault(
            l.get("statut") or "Orphelin", {"nb": 0, "montant": 0.0})
        case["nb"] += 1
        case["montant"] = frappe.utils.flt(case["montant"] + frappe.utils.flt(l.get("montant")), 3)

    # ⚠️ « ACC-PAY-2026-00123 » NE DIT PAS CE QUI A ETE REGLE. On rattache a chaque mouvement le
    # detail des factures que son paiement solde — la logique d'enrichissement de l'outil
    # externe, en un lot au lieu d'un appel HTTP par ligne.
    from bank_retenue_sync.facturation import reglement

    details = reglement.details_par_mouvement(lignes)
    for l in lignes:
        l["reglement"] = details.get(l.get("cle"))

    seuil = M.seuil_ecart()
    hors_seuil = [l for l in lignes if abs(frappe.utils.flt(l.get("ecart"), 3)) > seuil]
    orphelins = [l for l in lignes if (l.get("statut") or "") == "Orphelin"]

    return {
        "mois": mois,
        "libelle": periode.libelle(mois),
        "periode": {"debut": debut, "fin": fin},
        "kpi": kpi,
        "total": len(lignes),
        # Date du dernier mouvement CONNU du registre, tous mois confondus : c'est elle qui dit
        # si le releve du mois affiche est complet ou si l'export bancaire a pris du retard.
        "asof": frappe.db.sql("select max(`date`) from `tab%s`" % registry.DOCTYPE)[0][0],
        "ecarts": {"nb": len(hors_seuil),
                   "montant": frappe.utils.flt(
                       sum(abs(frappe.utils.flt(l.get("ecart"), 3)) for l in hors_seuil), 3)},
        "seuil_ecart": seuil,
        "mouvements": lignes,
        "orphelins": orphelins,
        "orphelins_total": len(orphelins),
    }


@frappe.whitelist()
def get_factures(mois=None) -> dict:
    _guard()
    return M_factures.liste(mois)


@frappe.whitelist()
def get_charges(mois=None) -> dict:
    """Les charges du mois, enrichies des controles DEJA passes — aucun PDF n'est relu ici.

    S'y ajoutent les charges RATTRAPEES : celles d'un mois anterieur rattachees a ce dossier.
    Elles sortent a part, groupees par mois d'origine et avec leur propre sous-total, exactement
    comme dans le classeur — jamais fondues dans les blocs ni dans le total du mois.
    """
    _guard()
    from bank_retenue_sync.facturation import controle

    mois = periode.normaliser(mois)
    donnees = controle.attacher_aux_lignes(M_charges.liste(mois))
    donnees["envoi"] = _etat_envoi(_dossier_doc(mois))
    donnees["peut_rattacher"] = any(r in frappe.get_roles() for r in ROLES_ECRITURE)
    donnees["retards"] = [{"mois": g["mois"], "libelle": _libelle_mois(g["mois"]),
                           "totaux": g["totaux"], "lignes": g["lignes"]}
                          for g in M_dossier.retardataires_du_dossier(mois)]
    return donnees


@frappe.whitelist()
def controler_justificatif(mois=None, document_type=None, document_name=None, file_url=None,
                           force=0) -> dict:
    """Lit UN justificatif avec le modele et le confronte a l'ecriture. Appel payant."""
    _guard(ecriture=True)
    from bank_retenue_sync.facturation import controle

    return controle.verifier(mois, document_type, document_name, file_url,
                             force=bool(frappe.utils.cint(force)))


@frappe.whitelist()
def controler_le_mois(mois=None) -> dict:
    """Met en file le controle de toutes les pieces non encore lues du mois."""
    _guard(ecriture=True)
    from bank_retenue_sync.facturation import controle

    mois = periode.normaliser(mois)
    frappe.enqueue("bank_retenue_sync.facturation.controle.verifier_le_mois",
                   queue="long", timeout=3600, mois=mois)
    return {"mois": mois,
            "message": _("Contrôle lancé pour {0}. Rechargez l'onglet dans quelques minutes.")
            .format(periode.libelle(mois))}


@frappe.whitelist()
def get_dossier(mois=None) -> dict:
    """L'etat de constitution du dossier, et les archives deja produites pour ce mois."""
    _guard()
    mois = periode.normaliser(mois)
    etat = M_dossier.lire_etat(mois)

    # ⚠️ UN ETAT SURVIT A SON ARCHIVE. L'etat vit dans le cache six heures, le fichier dans la
    # base : supprimer l'archive laissait le bandeau annoncer « Dossier constitué — 48 pieces,
    # 12,6 Mo » alors que la seule archive listee en pesait 24,5 et datait d'une heure plus tot.
    # Deux chiffres qui se contredisent valent moins que pas de chiffre du tout.
    if etat.get("statut") == "termine":
        fichier = etat.get("fichier_id")
        existe = frappe.db.exists("File", fichier) if fichier else \
            frappe.db.exists("File", {"file_url": etat.get("fichier")})
        if not existe:
            etat = {}

    return {"mois": mois, "etat": etat, "archives": M_dossier.archives(mois)}


def _archive_du_mois(mois: str, fichier: str):
    """Le fichier d'une archive de CE mois, ou une erreur claire. Jamais autre chose.

    ⚠️ ON NE TOUCHE QUE DES DOSSIERS. Le nom du fichier est verifie contre celui qu'une
    constitution produit : sans ce controle, les methodes qui s'appuient dessus deviendraient une
    porte ouverte sur n'importe quel fichier prive du site — servi, ou supprime. La regle de nom
    elle-meme vit dans `archive.est_archive_du_mois`, ou elle est testee sans site.
    """
    from bank_retenue_sync.facturation import archive as M_archive

    doc = frappe.db.get_value("File", fichier,
                              ["name", "file_name", "is_private", "creation"], as_dict=True) \
        if fichier else None
    if not doc or not M_archive.est_archive_du_mois(doc.file_name, mois):
        frappe.throw(_("Archive introuvable pour {0}.").format(mois))
    return doc


@frappe.whitelist()
def telecharger_dossier(mois=None, fichier=None):
    """Sert l'archive du mois, avec les droits de la PAGE et non ceux du fichier.

    ⚠️ UN FICHIER PRIVE SANS PIECE JOINTE N'APPARTIENT QU'A SON AUTEUR. L'archive est creee par
    le worker — donc par Administrator — et ne s'accroche a aucun document : Frappe la refuse
    alors a tout autre utilisateur, y compris le gestionnaire comptable qui vient de la demander.
    Le lien direct rendait « 403 Forbidden ». On la sert donc ici, sous la meme regle que le
    reste de l'ecran.
    """
    _guard()
    mois = periode.normaliser(mois)
    doc = _archive_du_mois(mois, fichier)

    frappe.response["filecontent"] = frappe.get_doc("File", doc.name).get_content()
    frappe.response["filename"] = doc.file_name
    frappe.response["type"] = "binary"


@frappe.whitelist(methods=["POST"])
def supprimer_dossier(mois=None, fichier=None) -> dict:
    """Supprime UNE archive du mois. Irreversible : le fichier physique part avec le document.

    ⚠️ C'EST LA SEULE METHODE DESTRUCTRICE DE CET ECRAN. `File.on_trash` efface le fichier de
    `private/files` : une archive supprimee n'est pas recuperable, et c'est une piece qui a pu
    etre remise au comptable. D'ou les droits d'ecriture, la confirmation cote ecran, et le meme
    controle de nom que le telechargement — on ne supprime jamais qu'un dossier de CE mois, un a
    la fois.

    ⚠️ ET EN POST SEULEMENT. `@frappe.whitelist()` sans argument ouvre GET, POST, PUT et DELETE :
    une destruction definitive devenait alors atteignable en NAVIGUANT vers une URL. Une image
    distante, un lien colle dans un message, un onglet reouvert — et l'archive partait avec la
    session du gestionnaire, sans confirmation et hors de la protection CSRF que Frappe
    n'applique qu'aux requetes non idempotentes.

    L'etat en cache n'est pas touche : `get_dossier` efface deja de lui-meme un etat « terminé »
    dont le fichier a disparu, et deux endroits qui oublient la meme chose finissent par diverger.
    """
    _guard(ecriture=True)
    mois = periode.normaliser(mois)
    doc = _archive_du_mois(mois, fichier)
    nom = doc.file_name
    frappe.delete_doc("File", doc.name, ignore_permissions=True)
    frappe.db.commit()
    return {"mois": mois, "fichier": doc.name, "nom_fichier": nom,
            "message": _("Archive {0} supprimée.").format(nom)}


@frappe.whitelist()
def comparer_archive(mois=None, fichier=None) -> dict:
    """Ce que cette archive N'A PAS des charges du mois — et ce qu'elle a en trop.

    ⚠️ LA QUESTION QU'UN GEL EMPECHE DE POSER. Le ZIP remis au comptable fige un etat ; les
    charges, elles, continuent d'arriver. Six semaines plus tard, personne ne peut plus dire ce
    qui manquait a l'envoi : reconstituer le mois rend une AUTRE liste, forcement plus longue.
    On confronte donc le contenu reel de l'archive choisie aux charges d'aujourd'hui.

    ⚠️ « MANQUANTE » N'EST PAS « A RATTRAPER ». Une charge de ce mois rattachee au dossier d'un
    mois POSTERIEUR est bien absente de cette archive-ci, et pourtant deja chez le comptable :
    chaque ligne porte donc son mois porteur, s'il y en a un. Aucun rattachement n'est fait ici —
    cette methode ne fait que lire.
    """
    _guard()
    from bank_retenue_sync.facturation import archive as M_archive

    mois = periode.normaliser(mois)
    doc = _archive_du_mois(mois, fichier)
    try:
        octets = frappe.get_doc("File", doc.name).get_content()
    except Exception as e:
        frappe.throw(_("Archive illisible : {0}").format(str(e)[:200]))

    try:
        manifeste = M_archive.lire_manifeste(octets)
        # ⚠️ ON NE SE FIE PAS A UN MANIFESTE QU'ON N'A PAS VERIFIE. Un JSON valide mais incomplet
        # — un `{"version": 1}` — se lirait comme « archive sans aucune charge », et TOUTES les
        # charges du mois ressortiraient manquantes : l'ecran enverrait rattraper un dossier
        # complet. Un manifeste douteux vaut donc un manifeste absent, et on retombe sur le
        # classeur, qui est de la donnee et non une promesse de structure.
        defaut = M_archive.defaut_du_manifeste(manifeste, mois)
        if not defaut:
            methode = M_archive.METHODE_MANIFESTE
            entrees = M_archive.entrees_du_manifeste(manifeste)
        else:
            # Repli pour les archives constituees avant le manifeste : le classeur des charges,
            # que tous les dossiers portent depuis toujours.
            entrees = M_archive.lire_classeur_charges(octets)
            methode = M_archive.METHODE_EMPREINTE
    except Exception as e:
        frappe.throw(_("Archive illisible : {0}").format(str(e)[:200]))

    if entrees is None:
        frappe.throw(_message_archive_incomparable(defaut))

    # Les charges d'AUJOURD'HUI, sans les controles IA : ce qui se compare ici, c'est la
    # presence d'une piece dans l'archive, pas la lecture de son PDF.
    donnees = M_charges.liste(mois)
    lignes_mois = M_archive.entrees_des_blocs(donnees)
    resultat = M_archive.comparer(lignes_mois, entrees, methode)

    # Ou sont parties les manquantes, si elles sont parties quelque part : une piece rattachee au
    # dossier d'un autre mois est deja chez le comptable, elle n'est pas a rattraper.
    manquantes = resultat["manquantes"]
    porteurs = _rattachements_des_pieces([(e["document_type"], e["document_name"])
                                          for e in manquantes if e.get("document_name")])
    detail = {(l.get("document_type"), l.get("document_name")): l
              for bloc in donnees["blocs"] for l in bloc["lignes"]}

    return {
        "mois": mois,
        "libelle": periode.libelle(mois),
        "fichier": doc.name,
        "nom_fichier": doc.file_name,
        "creation": str(doc.creation) if doc.creation else None,
        "methode": resultat["methode"],
        # Pourquoi le manifeste n'a pas servi, quand il n'a pas servi : « sans manifeste » et
        # « manifeste refuse » ne se lisent pas de la meme facon devant une liste de manquantes.
        "manifeste_defaut": defaut,
        "manifeste_message": _message_manifeste(defaut) if defaut else "",
        "manquantes": [_vue_manquante(e, detail, porteurs, mois) for e in manquantes],
        "disparues": resultat["disparues"],
        "totaux_manquantes": resultat["totaux_manquantes"],
        "totaux_disparues": resultat["totaux_disparues"],
        "nb_mois": resultat["nb_mois"],
        "nb_archive": resultat["nb_archive"],
    }


def _message_manifeste(defaut: str) -> str:
    """Pourquoi le manifeste de l'archive n'a pas servi. Les cles viennent de `archive.py`."""
    from bank_retenue_sync.facturation import archive as M_archive

    return {
        M_archive.DEFAUT_ABSENT:
            _("cette archive a été constituée avant le manifeste"),
        M_archive.DEFAUT_VERSION:
            _("son manifeste porte une version que cette page ne sait pas lire"),
        M_archive.DEFAUT_MOIS:
            _("son manifeste ne porte pas le mois affiché"),
        M_archive.DEFAUT_CHARGES:
            _("son manifeste n'a pas de liste de charges exploitable"),
        M_archive.DEFAUT_PIECES:
            _("son manifeste ne nomme pas les pièces qu'il liste"),
    }.get(defaut) or _("son manifeste est inexploitable")


def _message_archive_incomparable(defaut: str) -> str:
    """Ni manifeste utilisable, ni classeur : on dit ce qui manque plutot qu'une erreur 500."""
    return _("Cette archive ne peut pas être comparée : {0}, et elle ne porte pas de "
             "« Liste des Charges ».").format(_message_manifeste(defaut))


def _vue_manquante(e: dict, detail: dict, porteurs: dict, mois: str) -> dict:
    """Une charge absente de l'archive, avec de quoi la juger : sa piece, et ou elle est partie."""
    cle = (e.get("document_type"), e.get("document_name"))
    ligne = detail.get(cle) or {}
    porteur = porteurs.get(cle)
    return dict(e,
                justificatifs=ligne.get("justificatifs") or [],
                exemption=ligne.get("exemption") or "",
                avec_justificatif=bool(ligne.get("justificatifs")),
                rattachee_a=porteur,
                libelle_porteur=_libelle_mois(porteur) if porteur else "",
                # Rattachee au dossier d'un AUTRE mois : deja remise, rien a faire. Sinon, elle
                # se rattrape depuis le mois suivant, onglet Charges.
                deja_remise=bool(porteur and porteur != mois))


@frappe.whitelist()
def generer_dossier(mois=None, avec_pdf=1, avec_releve=0) -> dict:
    """Met en file la constitution de l'archive du mois. Ne bloque pas la requete.

    `avec_releve` fait telecharger le releve de la banque par le service TEJ : c'est la seule
    piece qui sort du bench, elle prend plusieurs minutes et peut echouer sans que le dossier
    en patisse.
    """
    _guard(ecriture=True)
    mois = periode.normaliser(mois)
    etat = M_dossier.lancer(mois, avec_pdf=bool(frappe.utils.cint(avec_pdf)),
                            avec_releve=bool(frappe.utils.cint(avec_releve)))
    return {"mois": mois, "etat": etat,
            "message": _("Constitution lancée pour {0}.").format(periode.libelle(mois))}


# ------------------------------------------------------------------ envoi au comptable & retards
#
# ⚠️ CE BLOC EST LE SEUL A ECRIRE EN BASE — ET JAMAIS EN COMPTABILITE. Il touche `BRS Dossier
# Mensuel` (l'etat d'envoi du mois) et sa table enfant (les retardataires rattachees), rien
# d'autre. Aucune ecriture, aucun paiement, aucune retenue n'est cree ni modifie ici.

DOCTYPE_MENSUEL = "BRS Dossier Mensuel"
NB_MOIS_RETARDS = 12


def _dossier_doc(mois: str, creer: bool = False):
    """La fiche mensuelle du mois, ou None. `creer=True` la fabrique (non enregistree) si absente."""
    if frappe.db.exists(DOCTYPE_MENSUEL, mois):
        return frappe.get_doc(DOCTYPE_MENSUEL, mois)
    if not creer:
        return None
    return frappe.get_doc({"doctype": DOCTYPE_MENSUEL, "mois": mois,
                           "libelle": periode.libelle(mois), "statut": "Brouillon"})


def _etat_envoi(doc) -> dict:
    """L'etat d'envoi tel que l'ecran le montre.

    `date_envoi` reste brute — c'est elle qui sert aux comparaisons — mais la banniere affiche
    `date_envoi_libelle`, au format de l'utilisateur : « 07-09-2026 10:32:15 » plutot que
    « 2026-09-07 10:32:15.482913 », dont les microsecondes n'apprennent rien a personne.
    """
    if not doc:
        return {"statut": "Brouillon", "date_envoi": None, "date_envoi_libelle": None,
                "envoye_par": None}
    return {"statut": doc.statut or "Brouillon",
            "date_envoi": str(doc.date_envoi) if doc.date_envoi else None,
            "date_envoi_libelle": frappe.utils.format_datetime(doc.date_envoi)
            if doc.date_envoi else None,
            "envoye_par": doc.envoye_par}


def _envois_map(nb_mois: int | None = None) -> dict:
    """{ mois -> date d'envoi } des mois marques « Envoyé », bornes aux N derniers mois clos."""
    rows = frappe.get_all(DOCTYPE_MENSUEL, filters={"statut": "Envoyé"},
                          fields=["mois", "date_envoi"], limit_page_length=0)
    envois = {r.mois: r.date_envoi for r in rows if r.date_envoi}
    if nb_mois:
        fenetre = {m["cle"] for m in periode.derniers(nb_mois)}
        envois = {m: d for m, d in envois.items() if m in fenetre}
    return envois


def _rattachees_globales(sauf_mois: str | None = None) -> set:
    """Les cles de toutes les pieces deja rattachees a un dossier — hors celui de `sauf_mois`."""
    filtres = {}
    if sauf_mois:
        filtres["parent"] = ["!=", sauf_mois]
    rows = frappe.get_all("BRS Dossier Retardataire", filters=filtres,
                          fields=["document_type", "document_name"], limit_page_length=0)
    return {M_retards.cle_voucher(r.document_type, r.document_name) for r in rows}


def _pool_retardataires(nb_mois: int = NB_MOIS_RETARDS) -> list:
    """Les charges retardataires du moment : mois envoye, saisie apres l'envoi, non rattachees.

    Le vivier est le meme pour la pastille, la section « À rattraper » et le controle global —
    une seule source, donc des compteurs qui ne se contredisent pas d'un ecran a l'autre.
    """
    envois = _envois_map(nb_mois)
    if not envois:
        return []
    rattachees = _rattachees_globales()
    # Le plan comptable ne change pas d'un mois a l'autre : on le resout une fois pour toute la
    # fenetre plutot qu'a chaque tour de boucle.
    comptes = M_charges.comptes_charges()
    candidats = []
    for mois, envoi in envois.items():
        vouchers = M_charges.vouchers_charges_du_mois(mois, comptes=comptes)
        if not vouchers:
            continue
        creations = M_charges.creations_des_vouchers(vouchers)

        # ⚠️ ON TRIE AVANT DE LIRE, PAS APRES. La date de saisie suffit a ecarter la quasi-totalite
        # des pieces d'un mois, et elle est deja la : lire les lignes completes de TOUT le mois —
        # justificatifs, comptes de journal, contrats de leasing — pour n'en garder ensuite qu'une
        # poignee coutait douze mois de lecture inutile a chaque ouverture de la page.
        tardifs = [v for v in vouchers
                   if M_retards.est_en_retard(creations.get(v), envoi)
                   and M_retards.cle_voucher(*v) not in rattachees]
        if not tardifs:
            continue
        for l in M_charges.lignes_par_vouchers(tardifs):
            l["mois_origine"] = mois
            l["creation"] = creations.get((l["document_type"], l["document_name"]))
            candidats.append(l)

    # La regle reste portee par la fonction pure — ce filtre amont n'est qu'une optimisation, et
    # `detecter_retardataires` doit rester le seul endroit ou « qu'est-ce qu'une retardataire »
    # se decide. Sur les lignes deja triees, c'est un passe-plat ; s'il cessait de l'etre, c'est
    # le filtre amont qui aurait tort.
    return M_retards.detecter_retardataires(candidats, envois, rattachees)


def _vue_pool(l: dict) -> dict:
    """Une ligne du vivier, reduite a ce que l'ecran montre d'une retardataire."""
    mois = l.get("mois_origine")
    return {
        "mois_origine": mois,
        "libelle_origine": periode.libelle(mois) if mois else "",
        "document_type": l.get("document_type"),
        "document_name": l.get("document_name"),
        "tiers": l.get("tiers"),
        "reference": l.get("reference_export") or l.get("ref"),
        "montant": l.get("ttc"),
        "avec_justificatif": bool(l.get("justificatifs")),
    }


def _vue_child(r) -> dict:
    """Une retardataire deja rattachee, lue depuis la table enfant (instantane du rattachement)."""
    return {
        "mois_origine": r.mois_origine,
        "libelle_origine": periode.libelle(r.mois_origine) if r.mois_origine else "",
        "document_type": r.document_type,
        "document_name": r.document_name,
        "tiers": r.tiers,
        "reference": r.reference,
        "montant": r.montant,
        "avec_justificatif": bool(r.avec_justificatif),
    }


@frappe.whitelist()
def get_dossier_mensuel(mois=None, nb_mois=NB_MOIS_RETARDS) -> dict:
    """L'etat d'envoi du mois, ses retardataires rattachees, et le vivier a rattacher.

    ⚠️ LE VIVIER EST GLOBAL, LES CANDIDATES NE LE SONT PAS. Une charge ne se rattrape que sur un
    mois POSTERIEUR au sien : proposer une piece d'aout dans l'ecran de juillet, c'est offrir une
    case que `rattacher` refusera — et gonfler la pastille de pieces que ce mois-ci ne peut pas
    prendre. Le controle global, lui, garde la vue complete.
    """
    _guard()
    mois = periode.normaliser(mois)
    nb_mois = frappe.utils.cint(nb_mois) or NB_MOIS_RETARDS
    doc = _dossier_doc(mois)
    candidats = [_vue_pool(l) for l in _pool_retardataires(nb_mois)
                 if (l.get("mois_origine") or "") < mois]
    rattaches = [_vue_child(r) for r in (doc.retardataires if doc else [])]
    return {
        "mois": mois,
        "libelle": periode.libelle(mois),
        "envoi": _etat_envoi(doc),
        "peut_envoyer": any(r in frappe.get_roles() for r in ROLES_ECRITURE),
        "candidats": candidats,
        "rattaches": rattaches,
        "nb_candidats": len(candidats),
        "nb_rattaches": len(rattaches),
    }


@frappe.whitelist()
def marquer_envoye(mois=None) -> dict:
    """Fige le mois comme envoye au comptable. Idempotent : deja envoye -> aucune modification."""
    _guard(ecriture=True)
    mois = periode.normaliser(mois)
    doc = _dossier_doc(mois, creer=True)
    if (doc.statut or "Brouillon") != "Envoyé":
        doc.statut = "Envoyé"
        doc.date_envoi = frappe.utils.now_datetime()
        doc.envoye_par = frappe.session.user
        doc.libelle = periode.libelle(mois)
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return {"mois": mois, "envoi": _etat_envoi(doc)}


@frappe.whitelist()
def annuler_envoi(mois=None) -> dict:
    """Remet le mois en brouillon. Le contraire strict de `marquer_envoye`, tout aussi idempotent.

    ⚠️ ANNULER UN ENVOI NE RAPPELLE PAS CE QUI EST DEJA PARTI. Des charges de ce mois ont pu etre
    rattrapees par le dossier d'un mois posterieur : elles sont chez le comptable, et le
    rattachement les tient hors du vivier. Repasser le mois en brouillon ne defait rien de tout
    cela — on ne refuse donc pas l'annulation, mais on rend le compte de ces pieces pour que
    l'ecran le dise au lieu de laisser croire que le mois repart d'une page blanche.
    """
    _guard(ecriture=True)
    mois = periode.normaliser(mois)
    doc = _dossier_doc(mois)
    if doc and doc.statut == "Envoyé":
        doc.statut = "Brouillon"
        doc.date_envoi = None
        doc.envoye_par = None
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return {"mois": mois, "envoi": _etat_envoi(doc),
            "parties_ailleurs": len(M_dossier.deja_remises_ailleurs(mois))}


@frappe.whitelist()
def rattacher(mois=None, document_type=None, document_name=None, attacher=1, manuel=0) -> dict:
    """Rattache (ou detache) une charge retardataire au dossier du mois. Persiste, idempotent.

    ⚠️ ON NE RATTACHE QU'UNE CHARGE, ET NULLE PART AILLEURS. Le type est borne aux deux familles de
    charge, et une piece deja rattachee a un AUTRE mois est refusee : sans quoi la meme facture
    partirait dans deux dossiers, et serait comptee deux fois.

    ⚠️ ET PAS SUR UN MOIS DEJA ENVOYE. Rattacher a un dossier deja parti fait DISPARAITRE la piece
    du vivier — pastille, section, controle des non-envoyees — sans qu'elle ait jamais ete
    transmise : le trou meme que ce ticket vient boucher. Il faut annuler l'envoi d'abord.

    `manuel=1` est le rattrapage CHOISI depuis l'onglet Charges : l'utilisateur designe lui-meme
    une piece d'un mois anterieur qui n'est jamais partie. La regle de detection automatique
    (mois d'origine envoye, saisie apres l'envoi) ne s'y applique pas ; tous les autres garde-fous,
    si. Sans ce drapeau, le comportement est celui d'avant, au detail pres.
    """
    _guard(ecriture=True)
    mois = periode.normaliser(mois)
    attacher = bool(frappe.utils.cint(attacher))
    manuel = bool(frappe.utils.cint(manuel))
    if document_type not in M_charges.TYPES_CHARGE:
        frappe.throw(_("Type de pièce non éligible au rattrapage : {0}.").format(document_type))

    doc = _dossier_doc(mois, creer=True)
    if (doc.statut or "Brouillon") == "Envoyé":
        frappe.throw(_("Ce mois est déjà envoyé : annulez l'envoi avant de modifier "
                       "ses rattachements."))
    existe = next((r for r in doc.retardataires
                   if r.document_type == document_type and r.document_name == document_name), None)

    if attacher and not existe:
        cle = M_retards.cle_voucher(document_type, document_name)
        if cle in _rattachees_globales(sauf_mois=mois):
            frappe.throw(_("Cette pièce est déjà rattachée au dossier d'un autre mois."))
        desc = _decrire_voucher(document_type, document_name)
        _exiger_retardataire(desc, mois, manuel=manuel)
        doc.append("retardataires", desc)
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    elif not attacher and existe:
        doc.remove(existe)
        doc.save(ignore_permissions=True)
        frappe.db.commit()

    return {"mois": mois, "attache": attacher, "nb_rattaches": len(doc.retardataires)}


def _exiger_retardataire(desc: dict, mois: str, manuel: bool = False) -> None:
    """Refuse tout ce qui n'est pas une VRAIE retardataire d'un mois anterieur deja envoye.

    ⚠️ L'ECRAN NE PROPOSE QUE DES CANDIDATES — L'API, ELLE, EST OUVERTE. Rattacher une piece de
    juin au dossier d'aout alors que juin n'est pas envoye la ferait sortir DEUX fois : dans le
    dossier de juin a sa place normale, et dans le sous-bloc « Retards de juin » d'aout. Double
    comptage cote comptable, et la piece quitte le vivier pour toujours via `_rattachees_globales`.

    ⚠️ EN MODE MANUEL, C'EST PRECISEMENT CE CAS QUE L'UTILISATEUR DEMANDE — et il l'assume. Le
    montant, lui, ne double jamais : le sous-bloc « Retards » reste hors du total du mois porteur,
    et reconstituer le mois d'origine marquera sa ligne « DÉJÀ REMISE ». On ne lit donc ni envoi
    ni date de saisie : la regle pure ne les regarde pas, et deux requetes de moins.
    """
    origine = desc.get("mois_origine") or ""
    cle = (desc["document_type"], desc["document_name"])
    envoi = None if manuel else _envois_map().get(origine)
    creation = None if manuel else M_charges.creations_des_vouchers([cle]).get(cle)

    # La regle elle-meme vit dans le module pur, ou elle est testee : ici on ne fait que lire ce
    # dont elle a besoin, et traduire son verdict.
    raison = M_retards.raison_de_refus(origine, mois, envoi, creation, manuel=manuel)
    if raison:
        frappe.throw(_message_refus(raison, origine))

    # ⚠️ ET C'EST BIEN UNE CHARGE DU DOSSIER. Une ecriture de journal validee n'est pas forcement
    # une depense : un virement de caisse a banque en est une, et rien dans son entete ne le dit.
    # Rattachee par appel direct, elle sortirait dans le ZIP comme une charge, avec son
    # `total_credit` en guise de TTC. Le grand livre du mois d'origine tranche : si la piece ne
    # touche aucun compte de charge du dossier, elle n'a rien a y faire.
    if cle not in set(M_charges.vouchers_charges_du_mois(origine)):
        frappe.throw(_("Cette pièce n'est pas une charge du dossier de {0} : elle ne touche aucun "
                       "compte de dépense ni d'achat.").format(_libelle_mois(origine)))


def _libelle_mois(mois: str) -> str:
    """« 2026-07 » -> « juillet 2026 ». Rend la cle telle quelle si elle est illisible."""
    try:
        return periode.libelle(mois)
    except Exception:
        return mois or "—"


def _message_refus(raison: str, origine: str) -> str:
    """Le motif de refus, en une phrase pour l'utilisateur. Les cles viennent de `retards.py`."""
    lib = _libelle_mois(origine)
    return {
        M_retards.REFUS_ORIGINE_INCONNUE:
            _("Le mois de comptabilisation de cette pièce est illisible."),
        M_retards.REFUS_PAS_ANTERIEUR:
            _("Une charge ne se rattrape que sur un mois postérieur au sien."),
        M_retards.REFUS_MOIS_NON_ENVOYE:
            _("{0} n'est pas encore marqué comme envoyé : cette charge partira avec le dossier "
              "de son propre mois.").format(lib),
        M_retards.REFUS_SAISIE_AVANT_ENVOI:
            _("Cette pièce a été saisie avant l'envoi de {0} : elle est déjà partie avec le "
              "dossier de son mois.").format(lib),
    }.get(raison) or _("Cette pièce n'est pas une retardataire d'un mois envoyé.")


def _decrire_voucher(document_type: str, document_name: str) -> dict:
    """L'instantane d'une piece a rattacher : mois d'origine, tiers, reference, montant, piece."""
    lignes = M_charges.lignes_par_vouchers([(document_type, document_name)])
    if not lignes:
        frappe.throw(_("Pièce introuvable ou sans charge : {0} {1}.")
                     .format(document_type, document_name))
    l = lignes[0]
    return {
        "mois_origine": (l.get("date") or "")[:7],
        "document_type": document_type,
        "document_name": document_name,
        "tiers": l.get("tiers"),
        "reference": l.get("reference_export") or l.get("ref"),
        "montant": l.get("ttc"),
        "avec_justificatif": 1 if l.get("justificatifs") else 0,
    }


def _rattachements_des_pieces(vouchers) -> dict:
    """{ (document_type, nom) -> mois porteur } pour les pieces donnees, en une requete."""
    noms = [dn for _, dn in vouchers]
    if not noms:
        return {}
    rows = frappe.get_all("BRS Dossier Retardataire",
                          filters={"document_name": ["in", noms]},
                          fields=["document_type", "document_name", "parent"],
                          limit_page_length=0)
    return {(r.document_type, r.document_name): r.parent for r in rows}


@frappe.whitelist()
def get_charges_anterieures(mois=None, mois_origine=None) -> dict:
    """Les depenses et achats d'un mois ANTERIEUR, pour un rattrapage choisi a la main.

    ⚠️ CE N'EST PAS LE VIVIER AUTOMATIQUE, ET C'EST TOUT L'INTERET. « À rattraper » ne propose que
    les charges d'un mois envoye saisies APRES cet envoi — celles dont on peut prouver qu'elles
    manquent. Restent celles qu'aucune regle ne trouve : un mois jamais marque envoye, une piece
    saisie avant l'envoi mais absente de l'archive effectivement remise. L'utilisateur, lui, sait
    lesquelles ; cet ecran lui rend le mois entier et le laisse designer.

    Chaque ligne dit ou elle en est : deja rattachee a ce mois-ci, rattachee au dossier d'un autre
    mois (donc verrouillee), ou candidate automatique. Rien n'est filtre — c'est un choix, pas une
    detection —, et rien n'est ecrit : cette methode ne fait que lire.
    """
    _guard()
    mois = periode.normaliser(mois)
    # ⚠️ `normaliser` RETOMBE SUR LE MOIS PRECEDENT QUAND LA CLE EST ILLISIBLE. Lister en silence
    # un autre mois que celui demande, sur un ecran qui sert a rattacher des pieces, c'est inviter
    # au geste de trop : on refuse plutot que de deviner. La cle rendue doit etre celle recue.
    origine = periode.normaliser(mois_origine)
    if origine != (mois_origine or "").strip() or origine >= mois:
        frappe.throw(_("Choisissez un mois strictement antérieur à {0}.")
                     .format(_libelle_mois(mois)))

    vouchers = M_charges.vouchers_charges_du_mois(origine)
    lignes = M_charges.lignes_par_vouchers(vouchers)
    creations = M_charges.creations_des_vouchers(vouchers)
    porteurs = _rattachements_des_pieces(vouchers)
    envoi = _envois_map().get(origine)

    out = []
    for l in lignes:
        cle = (l.get("document_type"), l.get("document_name"))
        porteur = porteurs.get(cle)
        out.append({
            "document_type": cle[0],
            "document_name": cle[1],
            "date": l.get("date"),
            "tiers": l.get("tiers"),
            "categorie": l.get("categorie"),
            "reference": l.get("reference_export") or l.get("ref"),
            "montant": l.get("ttc"),
            "justificatifs": l.get("justificatifs") or [],
            "exemption": l.get("exemption") or "",
            "avec_justificatif": bool(l.get("justificatifs")),
            # Rattachee ici, ailleurs, ou nulle part : c'est ce qui decide de l'etat de la case.
            "rattachee_a": porteur,
            "libelle_porteur": _libelle_mois(porteur) if porteur else "",
            "rattachee_ici": porteur == mois,
            # Le badge « saisie après l'envoi » : cette piece est DEJA une candidate automatique,
            # la cocher ici ne fait que devancer l'onglet Dossier.
            "saisie_apres_envoi": M_retards.est_en_retard(creations.get(cle), envoi),
        })

    doc = _dossier_doc(mois)
    return {
        "mois": mois,
        "libelle": periode.libelle(mois),
        "mois_origine": origine,
        "libelle_origine": periode.libelle(origine),
        "origine_envoyee": bool(envoi),
        "envoi": _etat_envoi(doc),
        "peut_rattacher": any(r in frappe.get_roles() for r in ROLES_ECRITURE),
        "lignes": out,
        "nb": len(out),
        "nb_rattachees_ici": sum(1 for l in out if l["rattachee_ici"]),
    }


@frappe.whitelist()
def verifier_non_envoyees(nb_mois=NB_MOIS_RETARDS) -> dict:
    """Toutes les charges des mois envoyes saisies apres l'envoi et non encore rattachees.

    Le meme vivier que « À rattraper », mais groupe par mois d'origine et vu sur N mois d'un coup :
    de quoi ne pas devoir ouvrir mois par mois pour trouver ce qui manque au comptable.
    """
    _guard()
    nb_mois = frappe.utils.cint(nb_mois) or NB_MOIS_RETARDS
    pool = _pool_retardataires(nb_mois)
    groupes = M_retards.grouper_par_mois_origine(pool)
    return {
        "nb": len(pool),
        "nb_mois": nb_mois,
        "groupes": [{"mois": g["mois"],
                     "libelle": periode.libelle(g["mois"]) if g["mois"] else "",
                     "totaux": g["totaux"],
                     "lignes": [_vue_pool(l) for l in g["lignes"]]}
                    for g in groupes],
    }
