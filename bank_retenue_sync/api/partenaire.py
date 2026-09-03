"""API de la page « Economiq Aqua Solution ».

L'onglet vivait dans « Facturation mensuelle », ou il n'avait pas sa place : le dossier mensuel
est une remise au comptable, le compte du partenaire est une relation commerciale qui se suit
toute l'annee. Les deux se consultent a des moments differents, par des gestes differents.

⚠️ AUCUNE METHODE DE CE MODULE N'ECRIT. L'ecriture de journal du bilan et la saisie de paiement —
celle qui annule des Payment Entry validees — restent a porter, et auront leur propre ecran.
"""
from __future__ import annotations

import frappe

from bank_retenue_sync.facturation import periode
from bank_retenue_sync.partenaire import economiq as M_economiq

ROLES_LECTURE = ["System Manager", "Accounts Manager", "Accounts User"]


def _guard():
    frappe.only_for(ROLES_LECTURE)


@frappe.whitelist()
def get_contexte() -> dict:
    """Les mois offerts et celui propose par defaut."""
    _guard()
    mois = periode.normaliser(None)
    return {"mois": mois, "libelle": periode.libelle(mois),
            "mois_offerts": periode.derniers(24)}


@frappe.whitelist()
def get_tableau(mois=None) -> dict:
    _guard()
    return M_economiq.tableau(mois)


ROLES_ECRITURE = ["System Manager", "Accounts Manager"]


def _guard_ecriture():
    frappe.only_for(ROLES_ECRITURE)


@frappe.whitelist()
def enregistrer(mois=None, charges=None, valide=0) -> dict:
    """Enregistre les charges libres et l'ajustement du mois, et recalcule l'echeancier.

    ⚠️ AUCUNE ECRITURE COMPTABLE ICI NON PLUS. On ne touche qu'a l'historique du partenaire :
    ce qu'il doit, comment c'est etale. Le passage en comptabilite est un geste separe.
    """
    _guard_ecriture()
    from bank_retenue_sync.partenaire import historique

    if isinstance(charges, str):
        charges = frappe.parse_json(charges)
    tableau = M_economiq.tableau(mois)
    if not tableau.get("disponible"):
        frappe.throw(tableau.get("message") or "Bilan indisponible.")

    # ⚠️ L'AJUSTEMENT VIENT DE L'ECRITURE QUAND ELLE EXISTE, JAMAIS DE L'ECRAN. `tableau` l'a
    # deja resolu ; on se contente de tracer d'ou il sort, pour que le mois enregistre pointe
    # sur la piece qui l'a fixe.
    piece = tableau.get("ecriture") or {}
    return historique.enregistrer(mois, tableau["bilan"], charges or [],
                                  total_commandes=tableau["total_commandes"],
                                  valide=bool(frappe.utils.cint(valide)),
                                  ajustement_impose=piece.get("ajustement"),
                                  journal_entry=piece.get("journal_entry"))


@frappe.whitelist()
def get_consolide() -> dict:
    """Le solde consolide inter-mois : ce que le partenaire doit, echeance par echeance.

    ⚠️ LES REGLEMENTS NE SE POINTENT PAS, ILS SE LISENT. Les Payment Entry encaissees depuis
    l'ancrage sont imputees ici, de l'echeance la plus ancienne a la plus recente. Stocker un
    pointage a cote des pieces creerait une seconde comptabilite qui divergerait au premier
    reglement saisi directement dans ERPNext.
    """
    _guard()
    from bank_retenue_sync.partenaire import amorce, echeancier, encaissements, historique

    mois = [m for m in historique.tous() if m]
    versements = encaissements.recus(amorce.DEPUIS)
    lignes, excedent = echeancier.imputer(echeancier.consolider(mois), versements)
    return {
        "mois_enregistres": [m["mois"] for m in mois],
        "lignes": lignes,
        "depuis": amorce.DEPUIS,
        "versements": versements,
        "dettes": encaissements.dettes(amorce.DEPUIS),
        "excedent": excedent,
        "total": frappe.utils.flt(sum(l["montant"] for l in lignes), 3),
        "reste": frappe.utils.flt(sum(l["reste"] or 0 for l in lignes), 3),
        "paye": frappe.utils.flt(sum(l["paye"] or 0 for l in lignes), 3),
    }


@frappe.whitelist()
def apercu_rapport(mois=None) -> dict:
    """Le rapport du mois SANS appeler OpenAI et SANS rien poser. Pour le relire avant.

    Les chiffres y sont deja definitifs — ils viennent du meme calcul que l'ecran. Seules les
    phrases de commentaire sont les phrases deterministes, pas celles du modele.
    """
    _guard()
    from bank_retenue_sync.partenaire import rapport

    d = rapport.donnees(mois)
    # `markdown` est ce qui PARTIRA chez le partenaire (version courte) ; `markdown_detaille` est
    # le justificatif complet, replie a l'ecran. Montrer le long en apercu et poser le court
    # aurait ete le pire des deux : on relit un texte qui n'est pas celui qu'on envoie.
    texte = rapport.rendre_court(d)
    detaille = rapport.rendre(d)
    return {"mois": d["mois"], "libelle": d["libelle"], "client": rapport.CLIENT,
            "enregistre": d["enregistre"], "markdown": texte, "html": rapport.en_html(texte),
            "markdown_detaille": detaille, "html_detaille": rapport.en_html(detaille),
            "commentaire_existant": rapport.commentaire_existant(d["mois"])}


@frappe.whitelist()
def generer_rapport(mois=None) -> dict:
    """Fait rediger le rapport du mois par OpenAI et le pose en commentaire sur la fiche client.

    ⚠️ SEULES LES PHRASES VIENNENT DU MODELE. Tous les montants sont rendus depuis les donnees du
    mois, et une phrase citant un nombre absent de ces donnees est rejetee au profit de la phrase
    deterministe. Ce rapport part chez le partenaire : un chiffre invente y serait une facture
    fausse.

    ⚠️ ECRIT UN COMMENTAIRE, RIEN D'AUTRE. Ni piece comptable, ni echeance, ni historique. Le
    regenerer remplace le rapport du meme mois plutot que d'en empiler un second.
    """
    _guard_ecriture()
    from bank_retenue_sync.partenaire import rapport

    return rapport.generer(mois)


@frappe.whitelist()
def preparer_ecriture(mois=None) -> dict:
    """Les cinq lignes proposees pour l'ecriture de bilan du mois. N'ecrit rien."""
    _guard_ecriture()
    from bank_retenue_sync.partenaire import ecriture

    return ecriture.proposer(mois)


@frappe.whitelist()
def creer_ecriture(mois=None, benefice_aqua=0, achats_partenaire=0, ventes_partenaire=0,
                   charges=None, repartition=None, force=0) -> dict:
    """Pose le BROUILLON d'ecriture de bilan, puis reprend le mois sur son ajustement.

    ⚠️ RIEN N'EST SOUMIS. Le brouillon attend une relecture dans ERPNext : deux de ses lignes
    reposent sur des prix d'achat absents de la base. Le mois n'est reenregistre qu'apres la
    soumission, quand `ecriture.lire` retrouve la piece — c'est la soumission qui fait foi, pas
    la creation.
    """
    _guard_ecriture()
    from bank_retenue_sync.partenaire import ecriture

    if isinstance(charges, str):
        charges = frappe.parse_json(charges)
    if isinstance(repartition, str):
        repartition = frappe.parse_json(repartition)
    return ecriture.creer(mois, benefice_aqua, achats_partenaire, ventes_partenaire,
                          charges or [], repartition=repartition or [], force=force)


@frappe.whitelist()
def get_plan_liberation(mois=None, ajustement=0) -> dict:
    """Ce que la libération détruirait, sans rien détruire. Le dry-run du bouton."""
    _guard_ecriture()
    from bank_retenue_sync.partenaire import dette

    return dette.plan(mois, frappe.utils.flt(ajustement))


@frappe.whitelist()
def set_auto_validation(actif=0) -> dict:
    """Le réglage d'auto-validation de l'écriture et des pièces de dette recréées."""
    _guard_ecriture()
    from bank_retenue_sync.partenaire import dette

    return {"auto_validation": dette.definir_auto_validation(actif)}


@frappe.whitelist()
def executer_ecriture(mois=None, benefice_aqua=0, achats_partenaire=0, ventes_partenaire=0,
                      charges=None, liberer=0, force=0) -> dict:
    """Libère les dettes, pose l'écriture, recrée le reste de dette. IRRÉVERSIBLE.

    ⚠️ CETTE METHODE SUPPRIME DES PIECES VALIDEES. Elle n'est appelee que derriere une
    confirmation qui liste nommement chaque piece concernee ; l'appeler autrement detruirait
    des ecritures comptables sans que personne les ait vues.
    """
    _guard_ecriture()
    from bank_retenue_sync.partenaire import ecriture

    if isinstance(charges, str):
        charges = frappe.parse_json(charges)
    return ecriture.executer(mois, benefice_aqua, achats_partenaire, ventes_partenaire,
                             charges or [], liberer=liberer, force=force)


@frappe.whitelist()
def preparer_paiement(montant=0, date=None) -> dict:
    """L'affectation qu'aurait un règlement de ce montant. N'écrit rien."""
    _guard_ecriture()
    from bank_retenue_sync.partenaire import paiement

    return paiement.proposer(frappe.utils.flt(montant), date)


@frappe.whitelist()
def creer_paiement(montant=0, date=None, mode=None, compte=None, reference=None,
                   valider=None, empreinte=None) -> dict:
    """Crée la Payment Entry du règlement reçu et l'affecte aux commandes ouvertes.

    ⚠️ LE CONSOLIDÉ N'EST PAS MIS A JOUR ICI. Il relit les pièces à chaque affichage : la
    nouvelle Payment Entry s'y imputera d'elle-même au prochain chargement. Écrire aussi un
    pointage créerait une seconde vérité qui divergerait à la première saisie faite dans ERPNext.

    `empreinte` : celle rendue par `preparer_paiement`, c'est-à-dire le plan que l'utilisateur a
    vu et confirmé nommément. Sans elle, on détruirait des pièces de dette que personne n'a
    approuvées si la base a bougé entre l'aperçu et le clic.
    """
    _guard_ecriture()
    from bank_retenue_sync.partenaire import paiement

    return paiement.creer(frappe.utils.flt(montant), date, mode, compte, reference, valider,
                          empreinte_attendue=empreinte)
