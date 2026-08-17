"""L'etat de depart du compte partenaire : la situation arretee au 30/06/2026.

L'ecran ne sait calculer qu'a partir de ce qui est en base, et la base ne contenait rien avant
juillet 2026. Sans amorce, le consolide part de zero et ne reclame au partenaire que les mois
saisis depuis, en oubliant les echeances de mai et juin qui courent encore.

⚠️ CE MODULE NE CALCULE RIEN. Les deux echeances ci-dessous sont l'etat communique au
partenaire, repris tel quel. Les redériver du bilan donnerait d'autres montants — le bilan de
juin recalcule aujourd'hui rend une premiere echeance de 2 659,240 la ou 2 671,790 ont ete
annonces, parce que des pieces sont arrivees apres la cloture. Un echeancier deja annonce ne se
recalcule pas : il se reprend.

CE QUE L'AMORCE AFFIRME, et sur quoi elle repose :

  1. Tout ce qui precede est solde. Verifie en tresorerie : 130 676,670 DT de commandes signees
     avant juillet contre 133 541,333 DT encaisses (cheques 76 431 + especes Tawfir 26 923 +
     virements 24 194 + especes caisse 5 705), dont zero affecte a une commande de juillet ou
     apres. Le compte Debiteurs − A&S est crediteur de 42 704,647 DT sur cette partie.

  2. L'echeance de juin due au 30/06 valait 2 671,790 et a ete reglee par 2 700,000 en especes
     sur Tawfir le 14/07/2026 (ACC-PAY-2026-04654, ref TT26195M1956), soit 28,210 d'excedent.
     Elle est donc soldee et n'a pas a figurer dans ce qui reste du.

  3. L'excedent de 28,210 est porte en avance sur l'echeance suivante, celle du 31/07.

     ⚠️ IL EST PORTE EN REGLEMENT, PAS DEDUIT DU MONTANT. Retrancher 28,210 des 4 266,616
     donnerait une echeance de 4 238,406 que le partenaire n'a jamais recue, et ferait
     disparaitre la trace du trop-percu. Le montant annonce reste le montant annonce ; l'avance
     apparait en face, et c'est le reste qui vaut 4 238,406.

  4. Il ne reste ouvert, au 30/06/2026, que les deux echeances ci-dessous.

⚠️ LE MOIS EST VALIDE, DONC GELE. Sans cela, la premiere sauvegarde de 2026-06 depuis l'ecran
recalculerait l'echeancier a partir du bilan et effacerait cette reprise. `poser(force=1)` est le
seul chemin qui la reecrit.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

from bank_retenue_sync.facturation import periode
from bank_retenue_sync.partenaire import historique

PRECISION = 3

#: Le mois porteur de la reprise : le dernier mois clos.
MOIS = "2026-06"

#: Longueur du champ Note de « BRS Economiq Echeance » — un Data, pas un Text.
NOTE_MAX = 140

#: Date a partir de laquelle les reglements s'imputent sur l'echeancier.
#:
#: ⚠️ TOUT CE QUI PRECEDE EST DEJA DANS LA REPRISE. Le dernier reglement pris en compte est
#: ACC-PAY-2026-04654 du 14/07/2026 — ses 2 700,000 ont solde l'echeance de juin et leur
#: excedent de 28,210 figure en avance sur celle du 31/07. Remonter d'un jour de plus
#: recompterait ce versement et eteindrait 2 700,000 d'echeances deja soldees.
DEPUIS = "2026-07-15"

#: L'etat de depart, tel que communique. `avance` est du deja encaisse porte sur l'echeance.
ECHEANCES = [
    {"date": "2026-07-31", "montant": 4266.616, "avance": 28.210,
     "note": "Reprise — mai M+2 1 205,376 + juin M+1 3 061,240  |  "
             "avance 28,210 (excédent du règlement de juin)"},
    {"date": "2026-08-31", "montant": 3061.240, "avance": 0.0,
     "note": "Reprise — juin M+2 3 061,240"},
]


def etat() -> dict:
    """Ce qui est en base aujourd'hui pour le mois porteur, sans rien ecrire."""
    enregistre = historique.lire(MOIS)
    return {
        "mois": MOIS,
        "pose": bool(enregistre),
        "valide": bool((enregistre or {}).get("valide")),
        "echeances": (enregistre or {}).get("echeances") or [],
        "attendu": ECHEANCES,
    }


def poser(force: int = 0) -> dict:
    """Ecrit l'etat de depart. Idempotent : ne touche pas a un mois deja pose sauf `force`."""
    force = bool(frappe.utils.cint(force))
    enregistre = historique.lire(MOIS)
    if enregistre and not force:
        return {"action": "inchangé", "mois": MOIS, "echeances": enregistre["echeances"]}

    doc = (frappe.get_doc(historique.DOCTYPE, MOIS) if enregistre
           else frappe.new_doc(historique.DOCTYPE))
    doc.mois = MOIS
    doc.libelle = periode.libelle(MOIS)
    doc.valide = 0

    # ⚠️ AUCUN BILAN N'EST REPRIS. Y porter les ventes et achats de juin laisserait croire que
    # l'echeancier en decoule, alors qu'il est repris. Les zeros disent : rien n'a ete calcule.
    for champ in ("ventes_aqua", "achats_aqua", "benefice_aqua", "ventes_partenaire",
                  "achats_partenaire", "benefice_partenaire", "total_commandes", "solde_net",
                  "total_charges", "total_ajustement", "report_vers_consolide"):
        setattr(doc, champ, 0.0)

    doc.set("charges", [])
    doc.set("echeancier", [])
    for e in ECHEANCES:
        montant = flt(e["montant"], PRECISION)
        avance = flt(e.get("avance"), PRECISION)
        reste = flt(montant - avance, PRECISION)
        doc.append("echeancier", {
            "date": e["date"], "montant_brut": montant, "deduit": 0.0, "montant": montant,
            "note": e["note"], "paye": avance, "reste": reste,
            "statut": "payé" if reste <= 0.001 else ("partiel" if avance else "non_payé"),
        })

    doc.flags.ignore_permissions = True
    doc.save()
    doc.db_set("valide", 1)
    frappe.db.commit()
    return {"action": "posé", "mois": MOIS, "echeances": historique.lire(MOIS)["echeances"]}
