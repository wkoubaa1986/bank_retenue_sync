# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSDossierRetardataire(Document):
    """Une charge d'un mois déjà envoyé, rattachée au dossier d'un mois postérieur.

    ⚠️ CETTE LIGNE NE DÉPLACE AUCUNE ÉCRITURE. Elle dit seulement « cette pièce de juillet part
    avec le dossier d'août » — c'est un rattachement de dossier, pas une reventilation comptable.
    Le montant et la présence de justificatif y sont figés pour l'affichage ; à la constitution du
    ZIP, les lignes sont relues à la source pour rester justes.

    ⚠️ `document_name` EST UN `Data`, PAS UN `Dynamic Link`. Un lien ferait passer la ligne par
    `Document._validate_links`, qui lève `CancelledLinkError` dès que la pièce visée est au
    docstatus 2. Une facture d'achat annulée puis amendée — le geste le plus banal qui soit —
    bloquerait alors TOUTE sauvegarde du dossier du mois : plus moyen de marquer l'envoi, de
    l'annuler, ni de rattacher quoi que ce soit, jusqu'à suppression manuelle de la ligne. Les
    pièces annulées sont de toute façon ignorées à la constitution du ZIP, où `_lignes_achat` et
    `_lignes_journal` filtrent sur `docstatus: 1`.
    """

    pass
