# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSOrdredePaiement(Document):
    """Un paiement attendu, cree en meme temps que son ecriture anticipee.

    Les salaires et le loyer sont comptabilises AVANT que la banque ne bouge (2 jours avant la
    fin du mois, le 15 tous les deux mois). L'ecriture seule ne dit donc pas si l'argent est
    reellement parti : cet ordre porte l'attente, et le rapprochement bancaire le passe a « Vire »
    quand le debit correspondant apparait au releve. Un ordre encore « En attente » apres sa date
    prevue est une alerte, pas un oubli silencieux.
    """

    pass
