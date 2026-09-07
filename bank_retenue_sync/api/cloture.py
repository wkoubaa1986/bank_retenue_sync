"""API de la page « Facturation mensuelle ».

Un onglet, une methode. Le JS ne calcule aucun montant : il affiche ce que ces methodes rendent,
comme le fait deja `api/mouvements.py` pour l'identification bancaire.

⚠️ AUCUNE METHODE DE CE MODULE N'ECRIT EN COMPTABILITE. La seule qui ecrive quoi que ce soit,
`generer_dossier`, produit un fichier. Les gestes qui touchent aux pieces — l'ecriture de journal
du bilan partenaire, la logique de dette — ne sont pas ici, et c'est delibere : ils sont
irreversibles et meritent leur propre ecran de confirmation.
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
    """Les charges du mois, enrichies des controles DEJA passes — aucun PDF n'est relu ici."""
    _guard()
    from bank_retenue_sync.facturation import controle

    return controle.attacher_aux_lignes(M_charges.liste(mois))


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


@frappe.whitelist()
def telecharger_dossier(mois=None, fichier=None):
    """Sert l'archive du mois, avec les droits de la PAGE et non ceux du fichier.

    ⚠️ UN FICHIER PRIVE SANS PIECE JOINTE N'APPARTIENT QU'A SON AUTEUR. L'archive est creee par
    le worker — donc par Administrator — et ne s'accroche a aucun document : Frappe la refuse
    alors a tout autre utilisateur, y compris le gestionnaire comptable qui vient de la demander.
    Le lien direct rendait « 403 Forbidden ». On la sert donc ici, sous la meme regle que le
    reste de l'ecran.

    ⚠️ ET ON NE SERT QUE DES DOSSIERS. Le nom du fichier est verifie contre celui qu'une
    constitution produit : sans ce controle, la methode deviendrait une porte ouverte sur
    n'importe quel fichier prive du site.
    """
    _guard()
    mois = periode.normaliser(mois)
    doc = frappe.db.get_value("File", fichier, ["name", "file_name", "is_private"], as_dict=True) \
        if fichier else None
    attendu = "Dossier facturation %s " % mois
    if not doc or not (doc.file_name or "").startswith(attendu):
        frappe.throw(_("Archive introuvable pour {0}.").format(mois))

    frappe.response["filecontent"] = frappe.get_doc("File", doc.name).get_content()
    frappe.response["filename"] = doc.file_name
    frappe.response["type"] = "binary"


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
    if not doc:
        return {"statut": "Brouillon", "date_envoi": None, "envoye_par": None}
    return {"statut": doc.statut or "Brouillon",
            "date_envoi": str(doc.date_envoi) if doc.date_envoi else None,
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
    candidats = []
    for mois in envois:
        vouchers = M_charges.vouchers_charges_du_mois(mois)
        if not vouchers:
            continue
        creations = M_charges.creations_des_vouchers(vouchers)
        for l in M_charges.lignes_par_vouchers(vouchers):
            l["mois_origine"] = mois
            l["creation"] = creations.get((l["document_type"], l["document_name"]))
            candidats.append(l)
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
    """L'etat d'envoi du mois, ses retardataires rattachees, et le vivier a rattacher."""
    _guard()
    mois = periode.normaliser(mois)
    doc = _dossier_doc(mois)
    candidats = [_vue_pool(l) for l in _pool_retardataires(int(nb_mois))]
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
    """Remet le mois en brouillon. Le contraire strict de `marquer_envoye`, tout aussi idempotent."""
    _guard(ecriture=True)
    mois = periode.normaliser(mois)
    doc = _dossier_doc(mois)
    if doc and doc.statut == "Envoyé":
        doc.statut = "Brouillon"
        doc.date_envoi = None
        doc.envoye_par = None
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return {"mois": mois, "envoi": _etat_envoi(doc)}


@frappe.whitelist()
def rattacher(mois=None, document_type=None, document_name=None, attacher=1) -> dict:
    """Rattache (ou detache) une charge retardataire au dossier du mois. Persiste, idempotent.

    ⚠️ ON NE RATTACHE QU'UNE CHARGE, ET NULLE PART AILLEURS. Le type est borne aux deux familles de
    charge, et une piece deja rattachee a un AUTRE mois est refusee : sans quoi la meme facture
    partirait dans deux dossiers, et serait comptee deux fois.
    """
    _guard(ecriture=True)
    mois = periode.normaliser(mois)
    attacher = bool(frappe.utils.cint(attacher))
    if document_type not in M_charges.TYPES_CHARGE:
        frappe.throw(_("Type de pièce non éligible au rattrapage : {0}.").format(document_type))

    doc = _dossier_doc(mois, creer=True)
    existe = next((r for r in doc.retardataires
                   if r.document_type == document_type and r.document_name == document_name), None)

    if attacher and not existe:
        cle = M_retards.cle_voucher(document_type, document_name)
        if cle in _rattachees_globales(sauf_mois=mois):
            frappe.throw(_("Cette pièce est déjà rattachée au dossier d'un autre mois."))
        doc.append("retardataires", _decrire_voucher(document_type, document_name))
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    elif not attacher and existe:
        doc.remove(existe)
        doc.save(ignore_permissions=True)
        frappe.db.commit()

    return {"mois": mois, "attache": attacher, "nb_rattaches": len(doc.retardataires)}


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


@frappe.whitelist()
def verifier_non_envoyees(nb_mois=NB_MOIS_RETARDS) -> dict:
    """Toutes les charges des mois envoyes saisies apres l'envoi et non encore rattachees.

    Le meme vivier que « À rattraper », mais groupe par mois d'origine et vu sur N mois d'un coup :
    de quoi ne pas devoir ouvrir mois par mois pour trouver ce qui manque au comptable.
    """
    _guard()
    nb_mois = int(nb_mois)
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
