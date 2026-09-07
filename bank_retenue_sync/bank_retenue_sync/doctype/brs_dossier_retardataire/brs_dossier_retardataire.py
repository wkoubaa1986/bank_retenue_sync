# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSDossierRetardataire(Document):
    """Une charge d'un mois déjà envoyé, rattachée au dossier d'un mois postérieur.

    ⚠️ CETTE LIGNE NE DÉPLACE AUCUNE ÉCRITURE. Elle dit seulement « cette pièce de juillet part
    avec le dossier d'août » — c'est un rattachement de dossier, pas une reventilation comptable.
    Le montant et la présence de justificatif y sont figés pour l'affichage ; à la constitution du
    ZIP, les lignes sont relues à la source pour rester justes.
    """

    pass
