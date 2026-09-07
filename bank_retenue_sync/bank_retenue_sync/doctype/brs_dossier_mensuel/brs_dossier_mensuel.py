# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSDossierMensuel(Document):
    """L'état durable d'un mois de facturation : envoyé au comptable, ou non — et ses rattrapages.

    ⚠️ CE DOCTYPE EST LE SEUL ÉCRIT DURABLE DE LA PAGE. L'état de constitution du ZIP vit dans le
    cache (six heures), volatil par nature ; l'envoi au comptable, lui, est un fait qui doit
    survivre à tout : rechargement, redémarrage, mois suivant. C'est pourquoi il est en base et
    non en cache — sans quoi « ce mois est parti » se perdrait à la première expiration.

    Une fiche par mois (`autoname: field:mois`, `unique`). Le passage à « Envoyé » fige la date :
    c'est elle qui, comparée à la date de saisie d'une charge, désigne les retardataires.
    """

    pass
