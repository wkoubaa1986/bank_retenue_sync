"""Traite 011644580146 de Sté EGES-PLUS : pièce remise au montant encaissé (693,885), fausse perte de 6 DT supprimée.

CONSTAT (copie de la prod du 04/11/2026)
  - Commande SAL-ORD-2026-02584 (687,885) envoyée par Aramex et payée par traite : son échéancier
    (« Remise Aramex du colis 51330111851 », traite n° 011644580146) a produit ACC-PAY-2026-07075, 687,885,
    « Traite bancaire LC », compte « Traite Bancaire - A&S ».
  - Facture ACC-SINV-2026-01236 : 693,885, 6 DT de plus que la commande. Ces 6 DT ont été passés en perte le
    07/10/2026 : ACC-PAY-2026-07627 (« Perte de paiement » → « Perte de non paiement - A&S », réf. « ajustement »).
  - La banque a crédité 693,885 le 03/11/2026 (« ENCAISSEMENT EFFET 011644580146 », FT26307YX9VX) : la traite
    couvrait TOUTE la facture, la perte n'a pas eu lieu. La ligne restait « Orphelin » (n° de traite mal lu, cf.
    pending._extract_traite_number) et, une fois lue, aurait laissé un écart de −6 DT qu'aucune résolution
    d'écart ne sait traiter (elles ne gèrent que la banque qui verse MOINS que la pièce).

CORRECTION — le geste maison de « Traitement des encaissement » (annuler, recopier, supprimer)
  1. ACC-PAY-2026-07075 est annulée et RECOPIÉE en une pièce NEUVE de 693,885 imputés sur la facture.
     `frappe.copy_doc` garde la commande d'origine et l'uid de la ligne d'échéancier (custom_source_*).
     Pas d'amendement : son `amended_from` pointerait vers une pièce supprimée.
  2. la perte ACC-PAY-2026-07627 et l'ancienne ACC-PAY-2026-07075 sont annulées puis SUPPRIMÉES (demande
     utilisateur du 04/11/2026), comme `cancel_delete_pe` du script de régénération : une copie de chacune
     reste dans « Deleted Document », et la note de correction est posée sur la nouvelle pièce et la facture.

  ⚠️ L'EMPREINTE EST RECALCULÉE, PAS RECOPIÉE. « re-generate payment after sales order » (Sales Order, After
  Save (Submitted)) régénère toute pièce non encaissée dont l'empreinte diffère de celle de sa ligne. Celle de
  07075 était DÉJÀ périmée (9366… contre 817a… pour la ligne actuelle) : constaté en répétition le 04/11/2026,
  un simple enregistrement de la commande remplaçait la pièce corrigée par une neuve à 687,885 ET recréait la
  facture. La pièce amendée reçoit donc l'empreinte ACTUELLE de sa ligne (même calcul que `row_fingerprint`
  du script) : il la tient pour à jour et n'y touche plus. L'empreinte décrit la ligne de la COMMANDE
  (687,885) ; le script ne compare que les empreintes, jamais le montant de la pièce, qui reste à 693,885.
  La vérification bancaire suivante crée alors le brouillon d'encaissement au centime, sans écart, et referme
  l'écart « Sans pièce » en attente (ecarts.fermer_sans_brouillon). Reste à le soumettre, comme d'habitude.

GARDE-FOUS
  - Rien n'est touché si l'état n'est plus EXACTEMENT celui constaté (corrigé à la main, traite déjà encaissée,
    montants ou imputations différents, autre site) : le patch le dit et s'arrête.
  - La correction se fait sous un point de sauvegarde : en cas d'erreur, tout est défait, l'erreur va dans
    l'Error Log et le patch NE LÈVE PAS — un patch qui lève bloque tout le `bench migrate`, donc le déploiement.
"""

import frappe
from frappe.utils import flt, getdate

FACTURE = "ACC-SINV-2026-01236"
TRAITE = "ACC-PAY-2026-07075"
PERTE = "ACC-PAY-2026-07627"
NUMERO = "011644580146"
COMPTE_TRAITE = "Traite Bancaire - A&S"
MONTANT_ENCAISSE = 693.885
MONTANT_PIECE = 687.885
MONTANT_PERTE = 6.0
POINT_DE_SAUVEGARDE = "traite_eges_plus_011644580146"

COMMANDE = "SAL-ORD-2026-02584"
UID_LIGNE = "2fd5747ecb9b"
CHAMP_NUMERO = "custom__n_chèque__transaction"

NOTE = ("Correction du 04/11/2026 (patch bank_retenue_sync) : la traite n° 011644580146 a été encaissée pour "
        "693,885 le 03/11/2026 (FT26307YX9VX), soit la facture ACC-SINV-2026-01236 entière. La perte de 6 DT "
        "ACC-PAY-2026-07627 et l'ancienne pièce de traite ACC-PAY-2026-07075 (687,885) ont été annulées puis "
        "supprimées (copie dans « Deleted Document ») ; {nouvelle} les remplace à 693,885.")


def _egal(a, b) -> bool:
    return abs(flt(a, 3) - flt(b, 3)) < 0.0005


def _references(pe: str) -> list:
    return frappe.get_all("Payment Entry Reference", filters={"parent": pe, "parenttype": "Payment Entry"},
                          fields=["reference_doctype", "reference_name", "allocated_amount"])


def empreinte_ligne(ligne) -> str:
    """Empreinte d'une ligne d'échéancier : MÊME calcul que row_fingerprint() du Server Script
    « re-generate payment after sales order » — sinon il régénère la pièce."""
    src = "|".join([
        "%.3f" % flt(ligne.get("payment_amount"), 3),
        ligne.get("mode_of_payment") or "",
        ligne.get("custom_banque") or "",
        ligne.get(CHAMP_NUMERO) or "",
        str(getdate(ligne.get("due_date"))) if ligne.get("due_date") else "",
    ])
    return frappe.utils.sha256_hash(src)


def ligne_echeancier():
    """La ligne d'échéancier de la commande dont la traite est issue (ou None)."""
    return frappe.db.get_value(
        "Payment Schedule", {"parenttype": "Sales Order", "parent": COMMANDE, "custom_row_uid": UID_LIGNE},
        ["payment_amount", "mode_of_payment", "custom_banque", CHAMP_NUMERO, "due_date"], as_dict=True)


def etat_inattendu():
    """None si l'état est celui constaté le 04/11/2026, sinon la raison de ne rien toucher."""
    facture = frappe.db.get_value("Sales Invoice", FACTURE, ["docstatus", "grand_total"], as_dict=True)
    if not facture:
        return "facture %s absente de ce site" % FACTURE
    if facture.docstatus != 1 or not _egal(facture.grand_total, MONTANT_ENCAISSE):
        return "facture %s modifiée depuis le constat" % FACTURE

    traite = frappe.db.get_value("Payment Entry", TRAITE, ["docstatus", "paid_amount", "paid_to", "reference_no"],
                                 as_dict=True)
    if not traite or traite.docstatus != 1:
        return "pièce %s plus active (déjà corrigée ou encaissée)" % TRAITE
    if (not _egal(traite.paid_amount, MONTANT_PIECE) or traite.paid_to != COMPTE_TRAITE
            or NUMERO not in (traite.reference_no or "")):
        return "pièce %s modifiée depuis le constat" % TRAITE
    refs = _references(TRAITE)
    if (len(refs) != 1 or refs[0].reference_doctype != "Sales Invoice" or refs[0].reference_name != FACTURE
            or not _egal(refs[0].allocated_amount, MONTANT_PIECE)):
        return "imputation de %s modifiée depuis le constat" % TRAITE
    if frappe.db.exists("Liste Traite Bancaire", {"ref_paiement": TRAITE}):
        return "%s déjà prise dans un encaissement" % TRAITE
    source = frappe.db.get_value("Payment Entry", TRAITE, ["custom_source_sales_order", "custom_source_row_uid"],
                                 as_dict=True)
    ligne = ligne_echeancier()
    # Sans sa ligne, la pièce serait une orpheline que le script de régénération SUPPRIME au prochain
    # enregistrement de la commande : on ne corrige pas une pièce promise à disparaître.
    if (source.custom_source_sales_order != COMMANDE or source.custom_source_row_uid != UID_LIGNE or not ligne
            or (ligne.get(CHAMP_NUMERO) or "").strip() != NUMERO):
        return "ligne d'échéancier de %s modifiée depuis le constat" % COMMANDE

    perte = frappe.db.get_value("Payment Entry", PERTE, ["docstatus", "paid_amount"], as_dict=True)
    if not perte or perte.docstatus != 1:
        return "perte %s plus active (déjà annulée)" % PERTE
    refs = _references(PERTE)
    if (not _egal(perte.paid_amount, MONTANT_PERTE) or len(refs) != 1 or refs[0].reference_name != FACTURE
            or not _egal(refs[0].allocated_amount, MONTANT_PERTE)):
        return "perte %s modifiée depuis le constat" % PERTE
    return None


def corriger() -> str:
    """Annule la perte et la traite, recopie la traite à 693,885, supprime les deux annulées.
    -> nom de la nouvelle pièce."""
    frappe.get_doc("Payment Entry", PERTE).cancel()

    source = frappe.get_doc("Payment Entry", TRAITE)
    source.cancel()
    nouvelle = frappe.copy_doc(source)         # garde custom_source_sales_order et custom_source_row_uid ; nom neuf
    nouvelle.custom_source_fingerprint = empreinte_ligne(ligne_echeancier())
    nouvelle.paid_amount = nouvelle.received_amount = MONTANT_ENCAISSE
    for ref in nouvelle.references:
        ref.allocated_amount = MONTANT_ENCAISSE
    nouvelle.insert(ignore_permissions=True)
    nouvelle.submit()

    for nom in (PERTE, TRAITE):
        frappe.delete_doc("Payment Entry", nom, ignore_permissions=True, force=True)

    note = NOTE.format(nouvelle=nouvelle.name)
    for doctype, name in (("Payment Entry", nouvelle.name), ("Sales Invoice", FACTURE)):
        frappe.get_doc(doctype, name).add_comment("Comment", note)
    return nouvelle.name


def execute():
    raison = etat_inattendu()
    if raison:
        print("Traite %s (Sté EGES-PLUS) : rien à corriger — %s." % (NUMERO, raison))
        return
    frappe.db.savepoint(POINT_DE_SAUVEGARDE)
    try:
        nom = corriger()
    except Exception:
        frappe.db.rollback(save_point=POINT_DE_SAUVEGARDE)
        frappe.log_error(title="Patch traite %s : correction NON appliquée" % NUMERO)
        print("Traite %s : correction NON appliquée, rien n'a été modifié (voir l'Error Log)." % NUMERO)
        return
    print("Traite %s : %s (perte) et %s annulées puis supprimées, remplacées par %s à %s DT."
          % (NUMERO, PERTE, TRAITE, nom, MONTANT_ENCAISSE))
