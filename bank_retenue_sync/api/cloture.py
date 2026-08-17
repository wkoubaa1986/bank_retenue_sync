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
