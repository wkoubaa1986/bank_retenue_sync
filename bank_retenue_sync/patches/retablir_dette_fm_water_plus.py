"""Retablit la dette de 534,781 DT que l'encaissement du 04/09/2026 a effacee chez FM WATER PLUS.

CE QUI S'EST PASSE
------------------
ENC-04-09-2026-00001 a encaisse 43,800 DT en especes sur une dette de 585,231 posee sur
SAL-ORD-2026-03046. Le Server Script « Traitement des encaissement » a supprime la piece de
dette EN ENTIER (`delete_doc`, `force=True`) et n'en a recree qu'une du montant encaisse : les
541,431 DT restants ont disparu, et le compte du client s'est retrouve court de 534,781 DT.

⚠️ CE PATCH NE CORRIGE QUE CE CAS. Le defaut du script est intact : tout encaissement partiel
perd encore la difference. Les autres commandes touchees (60, 7 314 DT, toutes de 2023-2025)
sont de l'argent ENCAISSE MAIS MAL IMPUTE chez des clients a jour — leur remede n'est pas de
recreer une dette, ce qui leur inventerait une creance, mais de re-affecter les reglements
existants. Elles se traitent client par client, pas par un patch.

CE QU'IL FAIT
-------------
  - une piece « Dette non payee » de 534,781 (Debiteurs -> Dettes), affectee a la commande.
    534,781 et non 541,431 : le compte du client ne reclamait que cela, recreer le trou entier
    lui inventerait 6,650 DT ;
  - l'echeancier de la commande remis d'aplomb, sinon le PROCHAIN encaissement sur cette
    commande ne trouverait aucune ligne a son montant et passerait sans rien faire, en silence.

Idempotent : si une dette vit deja sur la commande, il ne fait rien.
"""

import frappe
from frappe.utils import flt

COMMANDE = "SAL-ORD-2026-03046"
CLIENT = "FM WATER PLUS"
MONTANT = 534.781


def execute():
    if not frappe.db.exists("Sales Order", COMMANDE):
        return
    from bank_retenue_sync.clients import dettes_perdues as D

    deja = frappe.db.sql("""
        SELECT COUNT(*) FROM `tabPayment Entry Reference` per
        INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
        WHERE pe.docstatus = 1 AND pe.mode_of_payment = %s AND per.reference_name = %s""",
        (D.MODE_DETTE, COMMANDE))[0][0]
    if deja:
        return

    v = frappe.db.get_value("Sales Order", COMMANDE,
                            ["customer", "grand_total", "advance_paid"], as_dict=True)
    if not v or v.customer != CLIENT:
        return
    # Le trou d'aujourd'hui, jamais un montant memorise : la commande a pu changer.
    trou = flt(v.grand_total) - flt(v.advance_paid)
    if trou <= D.TOLERANCE:
        return

    cas = {"client": CLIENT, "commande": COMMANDE, "cible_type": "Sales Order",
           "cible": COMMANDE, "total": flt(v.grand_total), "montant": min(MONTANT, trou)}
    piece = D._creer_dette(cas)
    frappe.db.commit()
    frappe.msgprint("Dette FM WATER PLUS rétablie : %s (%s DT)" % (piece, cas["montant"]))
