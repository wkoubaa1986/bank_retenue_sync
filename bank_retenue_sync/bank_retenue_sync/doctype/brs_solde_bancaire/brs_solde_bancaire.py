# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSSoldeBancaire(Document):
    """Un releve de solde date, avec sa piece justificative.

    Sans historique, un solde ne dit rien : on constate un ecart sans savoir quand il est apparu.
    Chaque lecture est donc conservee avec sa capture, la valeur lue, le solde ERPNext de la meme
    date et les deux ecarts — celui qui se comble par une ecriture, et celui qui se comble par un
    import de mouvements.

    `fichier` est unique : re-lire la meme capture ne cree pas de doublon.
    """

    pass
