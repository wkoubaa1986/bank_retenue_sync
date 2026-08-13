# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSBankMovement(Document):
    """Un mouvement du releve bancaire, memorise.

    C'est la SEULE memoire longue du rapprochement : `/banque/mouvements/export/latest` ne rend que
    le dernier fichier exporte et chaque nouvel export REMPLACE le precedent (cf.
    bank/movements.refresh_movements). Sans ce registre, « ce qui reste a identifier » se limiterait
    a la fenetre courante et tout arbitrage humain serait perdu au refresh suivant.
    """

    pass
