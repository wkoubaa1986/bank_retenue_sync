# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSContratFinancement(Document):
    """Un contrat de pret ou de leasing, avec son echeancier.

    Les deux prets partagent le meme libelle bancaire (« TAMOUIL CHIRAET ») : seul le TOTAL
    MENSUEL constant (principal + interet du meme jour) permet de les distinguer. D'ou
    `total_mensuel`, qui n'est pas indicatif mais bien le critere d'appariement.

    `date_debut` + `nb_echeances` donnent le compteur du libelle (« Remboursement 6/10 »), que les
    ecritures manuelles portent depuis toujours.
    """

    pass
