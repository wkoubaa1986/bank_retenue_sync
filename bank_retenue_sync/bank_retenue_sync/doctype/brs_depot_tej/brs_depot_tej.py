# Copyright (c) 2026, Wassim Koubaa and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BRSDepotTEJ(Document):
    """Un depot de certificat de retenue chez TEJ, entre la soumission et le certificat.

    ⚠️ CE DOCTYPE EXISTE PARCE QUE « VALIDER » NE CREE PAS UN CERTIFICAT. TEJ enregistre un DEPOT,
    qu'il analyse quand il veut ; le certificat et sa reference n'existent qu'ensuite. Entre les
    deux, la declaration est PARTIE — elle est chez l'administration fiscale — mais rien ne la
    porte cote ERPNext : pas de reference, donc pas de PDF, donc rien dans le nom d'un fichier
    attache, qui etait jusqu'ici la seule memoire du module.

    Une ligne par soumission, jamais par facture : un depot refuse puis resoumis en laisse deux,
    et l'historique de ce qui a ete envoye au fisc ne doit pas s'effacer.

    ⚠️ UNE LIGNE `en_analyse` INTERDIT TOUTE RESOUMISSION de la meme facture. C'est la quatrieme
    barriere anti-doublon, et la seule qui voie l'angle mort des trois autres : ni le PDF attache,
    ni la cle d'idempotence du service, ni l'export des certificats emis ne connaissent un depot
    qui n'a pas encore ete analyse.
    """

    pass
