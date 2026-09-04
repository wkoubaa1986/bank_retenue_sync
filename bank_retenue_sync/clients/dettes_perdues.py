"""Les dettes qu'un encaissement PARTIEL a fait disparaître : constat, et réparation.

LE DÉFAUT D'ORIGINE
-------------------
Le Server Script « Traitement des encaissement » suppose qu'un encaissement de dette est
TOUJOURS total. Il supprime la pièce « Dette non payée » en entier (`delete_doc`, `force=True`)
et n'en recrée une que du montant encaissé. Dès que le client paie moins que sa dette, la
différence disparaît sans trace ni avertissement — 77 commandes depuis 2023, dont 16 en 2026.
Cas de référence : FM WATER PLUS, 04/09/2026, dette de 585,231, encaissé 43,800, perdu 541,431.

OÙ LA DETTE DOIT SE RATTACHER
-----------------------------
⚠️ SUR LA PIÈCE QUI PORTE LA CRÉANCE, ET SUR ELLE SEULE. Mesuré sur la base : 137 dettes
pointent une commande (57 004 DT), 47 une facture (28 183 DT), AUCUNE ne flotte sans
affectation, et AUCUNE commande facturée ne porte encore de dette. La convention est donc déjà
celle-ci, et on la respecte : tant que la commande n'est pas facturée, la dette est sur la
commande ; dès qu'elle l'est, la créance a migré sur la facture et la dette doit suivre.

CE QUE LA MODIFICATION D'UNE COMMANDE IMPLIQUE
----------------------------------------------
⚠️ UNE COMMANDE PEUT AVOIR RÉTRÉCI DEPUIS. Recréer aveuglément le montant d'origine poserait une
dette supérieure à ce qui reste dû — et ERPNext l'accepterait, faussant l'encours du client dans
l'autre sens. On ne recrée donc jamais un montant mémorisé : on recalcule le TROU d'aujourd'hui,
et on le plafonne à ce que la pièce doit encore. Une commande annulée, ou remplacée par un
amendement, ne reçoit rien du tout : sa créance a changé de document.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

PRECISION = 3
MODE_DETTE = "Dette non payée"
COMPTE_DETTE = "Dettes - A&S"
COMPTE_CLIENT = "Débiteurs - A&S"
TOLERANCE = 1.0


def _commandes_encaissees() -> list:
    """Les commandes qui ont connu au moins un encaissement de dette validé."""
    return [r.commande for r in frappe.db.sql(
        """SELECT DISTINCT ld.bl AS commande
           FROM `tabListe Dettes` ld
           INNER JOIN `tabEncaissement Paiement` e ON e.name = ld.parent
           WHERE e.docstatus = 1 AND ld.bl IS NOT NULL AND ld.bl != ''""", as_dict=True)]


def _factures_de(commandes) -> dict:
    """{commande: [{facture, reste}]} — la créance a migré là quand la commande est facturée."""
    if not commandes:
        return {}
    ph = ",".join(["%s"] * len(commandes))
    out = {}
    for r in frappe.db.sql(
            f"""SELECT DISTINCT sii.sales_order AS commande, si.name AS facture,
                       si.outstanding_amount AS reste
                FROM `tabSales Invoice Item` sii
                INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
                WHERE si.docstatus = 1 AND sii.sales_order IN ({ph})""",
            tuple(commandes), as_dict=True):
        out.setdefault(r.commande, []).append({"facture": r.facture,
                                               "reste": flt(r.reste, PRECISION)})
    return out


def _remises_par_commande(commandes) -> dict:
    """Les réductions accordées par écriture de journal, rattachées à la commande.

    Elles comblent une partie du trou en toute légitimité : les ignorer ferait recréer une dette
    déjà effacée par un avoir (cas SAL-ORD-2023-00551, remise de 191 sur un trou de 382).
    """
    if not commandes:
        return {}
    ph = ",".join(["%s"] * len(commandes))
    return {r.commande: flt(r.net, PRECISION) for r in frappe.db.sql(
        f"""SELECT jea.reference_name AS commande, SUM(jea.credit - jea.debit) AS net
            FROM `tabJournal Entry Account` jea
            INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
            WHERE je.docstatus = 1 AND jea.reference_type = 'Sales Order'
              AND jea.reference_name IN ({ph})
            GROUP BY jea.reference_name""", tuple(commandes), as_dict=True)}


def _dettes_vivantes(commandes) -> set:
    if not commandes:
        return set()
    ph = ",".join(["%s"] * len(commandes))
    return {r.commande for r in frappe.db.sql(
        f"""SELECT DISTINCT per.reference_name AS commande
            FROM `tabPayment Entry Reference` per
            INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
            WHERE pe.docstatus = 1 AND pe.mode_of_payment = %s
              AND per.reference_name IN ({ph})""",
        (MODE_DETTE,) + tuple(commandes), as_dict=True)}


def diagnostic() -> list:
    """Le trou d'AUJOURD'HUI sur chaque commande, et où la dette devrait se rattacher.

    Ne lit rien d'autre que l'état courant : aucun montant historique n'est rejoué, parce que la
    commande a pu changer entre-temps.
    """
    commandes = _commandes_encaissees()
    if not commandes:
        return []
    ph = ",".join(["%s"] * len(commandes))
    infos = frappe.db.sql(
        f"""SELECT name, customer, grand_total, advance_paid, docstatus,
                   amended_from, transaction_date
            FROM `tabSales Order` WHERE name IN ({ph})""", tuple(commandes), as_dict=True)
    factures = _factures_de(commandes)
    remises = _remises_par_commande(commandes)
    vivantes = _dettes_vivantes(commandes)

    out = []
    for v in infos:
        # Une commande annulee ou amendee ne recoit rien : sa creance a change de document.
        if v.docstatus != 1 or v.amended_from:
            continue
        if v.name in vivantes:
            continue
        trou = flt(v.grand_total) - flt(v.advance_paid) - remises.get(v.name, 0.0)
        if trou <= TOLERANCE:
            continue

        # ⚠️ `per_billed` NE PEUT PAS SERVIR. Mesuré sur la base : vingt commandes affichent
        # « 0 % facturé » alors que leur facture existe, est validée et SOLDÉE — le champ n'a
        # jamais été remis à jour. S'y fier faisait proposer une dette de 898 DT sur
        # SAL-ORD-2026-02770, dont la facture ACC-SINV-2026-01274 est pourtant payée. On
        # regarde donc les FACTURES elles-mêmes, jamais le pourcentage de la commande.
        siennes = factures.get(v.name, [])
        if siennes:
            ouvertes = [f for f in siennes if f["reste"] > TOLERANCE]
            if not ouvertes:
                continue          # facturée et soldée : il n'y a plus aucune créance
            # La créance a migré sur la facture : la dette suit, plafonnée à ce qu'elle doit.
            cible = "Sales Invoice"
            nom_cible = ouvertes[0]["facture"]
            reste_cible = min(trou, ouvertes[0]["reste"])
        else:
            cible, nom_cible, reste_cible = "Sales Order", v.name, trou

        out.append({
            "client": v.customer,
            "commande": v.name,
            "date": str(v.transaction_date),
            "total": flt(v.grand_total, PRECISION),
            "paye": flt(v.advance_paid, PRECISION),
            "remise": remises.get(v.name, 0.0),
            "cible_type": cible,
            "cible": nom_cible,
            "montant": flt(reste_cible, PRECISION),
        })
    return _qualifier(sorted(out, key=lambda x: -x["montant"]))


def _qualifier(cas: list) -> list:
    """Ajoute le VERDICT du client — et c'est lui qui décide si une dette doit être recréée.

    ⚠️ UNE PIÈCE « DETTE NON PAYÉE » EST UNE PIÈCE DE RÈGLEMENT. La créer solde la commande et
    augmente le « réglé » du client. Chez un client dont les comptes tombent déjà juste, cela
    inventerait de l'argent : LIMPID'EAU est à 0,200 près de l'équilibre, lui poser 2 912,5 de
    dette le ferait ressortir surpayé d'autant.

    Le trou de la commande y est alors un défaut d'IMPUTATION, pas une créance : l'argent est
    encaissé, il est simplement rattaché ailleurs. Le remède n'est pas de créer une dette mais de
    ré-affecter les règlements existants — un autre geste, qui ne s'improvise pas ici.
    """
    from bank_retenue_sync.clients import rapprochement as R

    lignes = {l["client"]: l for l in R.lignes(masquer_ignores=0)}
    trous = {}
    for c in cas:
        trous[c["client"]] = flt(trous.get(c["client"], 0.0) + c["montant"], PRECISION)

    for c in cas:
        l = lignes.get(c["client"])
        manque = -flt((l or {}).get("delta_paiement"), PRECISION) if l else None
        c["client_manque"] = manque
        c["client_trous"] = trous[c["client"]]

        # ⚠️ ET LE TOTAL DES TROUS NE PEUT PAS DÉPASSER CE QUE LE CLIENT DOIT. Nizar Maddouri
        # ne doit que 30 DT, mais ses commandes cumulent 3 520 DT de trous : lui recréer ces
        # dettes inventerait 3 490 DT de créance. Quand l'écart du compte est très inférieur à
        # la somme des trous, c'est que l'argent est là et mal imputé — recréer une dette
        # aggraverait au lieu de réparer, et on ne peut pas deviner LAQUELLE des commandes
        # porte les 30 DT réels.
        combien = len([x for x in cas if x["client"] == c["client"]])
        if manque is None or manque <= TOLERANCE:
            c["a_recreer"] = False
            c["motif"] = "compte du client équilibré : imputation à corriger, pas une dette"
        elif trous[c["client"]] <= manque + TOLERANCE:
            # Les trous expliquent la dette du client : on les recrée tels quels.
            c["a_recreer"] = True
            c["motif"] = "le client doit encore %s DT" % manque
        elif combien == 1:
            # Un seul trou chez ce client : la dette réelle lui revient sans ambiguïté, mais
            # PLAFONNÉE à ce qu'il doit. Sur FM WATER PLUS, le trou de la commande vaut 541,431
            # et le compte n'en réclame que 534,781 — recréer 541,431 lui inventerait 6,650 DT.
            c["montant"] = min(c["montant"], manque)
            c["a_recreer"] = True
            c["motif"] = "seul trou du client, plafonné à ce qu-il doit (%s DT)" % manque
        else:
            # ⚠️ PLUSIEURS TROUS POUR UNE DETTE PLUS PETITE : on ne peut pas deviner laquelle
            # des commandes la porte. Nizar Maddouri ne doit que 30 DT pour 3 520 DT de trous
            # répartis sur 38 commandes ; recréer aveuglément lui inventerait 3 490 DT. Ce cas
            # se tranche à la main, ou en ré-affectant les règlements existants.
            c["a_recreer"] = False
            c["motif"] = ("le client ne doit que %s DT pour %s DT de trous sur %d commandes : "
                          "à répartir à la main" % (manque, trous[c["client"]], combien))
    return cas


def _creer_dette(cas: dict):
    """Une pièce « Dette non payée » du montant manquant, sur la pièce qui porte la créance."""
    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.party_type = "Customer"
    pe.party = cas["client"]
    pe.posting_date = frappe.utils.nowdate()
    pe.mode_of_payment = MODE_DETTE
    pe.paid_from = COMPTE_CLIENT
    pe.paid_to = COMPTE_DETTE
    pe.paid_amount = cas["montant"]
    pe.received_amount = cas["montant"]
    pe.reference_no = cas["cible"]
    pe.reference_date = frappe.utils.nowdate()
    # ⚠️ SANS `custom_remarks`, ERPNEXT RÉÉCRIT LA REMARQUE. `set_remarks()` regénère le texte
    # standard (« Amount TND … received from … ») à la validation et efface l'explication —
    # constaté sur ACC-PAY-2026-06659, où le motif du rétablissement avait disparu. La case
    # `custom_remarks` est précisément là pour figer un texte écrit à la main.
    pe.custom_remarks = 1
    pe.remarks = ("Dette rétablie le %s : un encaissement partiel avait supprimé la pièce "
                  "entière au lieu d'en laisser le reste (commande %s)."
                  % (frappe.utils.nowdate(), cas["commande"]))
    pe.append("references", {
        "reference_doctype": cas["cible_type"],
        "reference_name": cas["cible"],
        "allocated_amount": cas["montant"],
    })
    pe.flags.ignore_permissions = True
    pe.insert()
    pe.submit()
    _rafraichir_echeancier(cas)
    return pe.name


def _rafraichir_echeancier(cas: dict):
    """Remet l'échéancier de la commande en accord avec la dette rétablie.

    ⚠️ SANS CELA, LE PROCHAIN ENCAISSEMENT NE FERA RIEN, EN SILENCE. Le Server Script cherche
    une ligne d'échéancier dont le montant ÉGALE la dette annoncée :

        if iterm.mode_of_payment == "Dette non payée" and iterm.payment_amount == ipay.valeur:

    Sur SAL-ORD-2026-03046, l'échéancier porte encore la ligne d'origine — « Dette non payée
    708,00 » — alors que la dette réelle vaut 534,781. Aucune correspondance : le script
    passerait sans rien faire et sans le dire.

    On ne touche QUE la ligne de dette, et seulement si elle est seule de son espèce : un
    échéancier composite se rectifie à la main, pas au jugé.
    """
    if cas["cible_type"] != "Sales Order":
        return None
    lignes = frappe.get_all(
        "Payment Schedule", filters={"parent": cas["cible"], "parenttype": "Sales Order"},
        fields=["name", "mode_of_payment", "payment_amount"], limit_page_length=0)
    dettes = [l for l in lignes if l["mode_of_payment"] == MODE_DETTE]
    if len(dettes) != 1:
        return None
    ligne = dettes[0]
    avant = flt(ligne["payment_amount"])
    # ⚠️ NE PAS SORTIR TOT SI LA LIGNE DE DETTE EST DÉJÀ BONNE. Le complément qui fait retomber
    # l'échéancier sur le total est une AUTRE question : une première version rendait la main
    # dès que la dette était au bon montant et laissait l'échéancier à 534,781 pour une commande
    # de 708,00.
    if abs(avant - cas["montant"]) >= 0.005:
        frappe.db.set_value("Payment Schedule", ligne["name"],
                            {"payment_amount": cas["montant"],
                             "description": "Reste de la dette après encaissement partiel"},
                            update_modified=False)

    # ⚠️ ET L'ÉCHÉANCIER DOIT RETOMBER SUR LE TOTAL DE LA COMMANDE. Réduire la seule ligne de
    # dette de 708 à 534,781 laisserait un échéancier qui ne somme plus au grand total — ERPNext
    # le refuse à la première réouverture de la commande. On pose donc en face une ligne de ce
    # qui a DÉJÀ été encaissé, celle que le script aurait dû écrire lui-même.
    autres = sum(flt(l["payment_amount"]) for l in lignes if l["name"] != ligne["name"])
    deja = flt(flt(cas["total"]) - autres - flt(cas["montant"]), PRECISION)
    pose = None
    if deja > 0.005:
        pose = frappe.get_doc({
            "doctype": "Payment Schedule",
            "parent": cas["cible"], "parenttype": "Sales Order",
            "parentfield": "payment_schedule", "docstatus": 1,
            "due_date": frappe.utils.nowdate(),
            "mode_of_payment": "Espèces",
            "payment_amount": deja,
            # Le libellé ne surestime rien : cette ligne porte ce qui a déjà été réglé ET,
            # le cas échéant, le reliquat que le compte du client ne réclamait pas (6,650 DT
            # sur FM WATER PLUS, écart entre le trou de la commande et la dette réelle).
            "description": "Déjà réglé, et reliquat non réclamé, avant rétablissement de la dette",
        })
        pose.flags.ignore_permissions = True
        pose.insert()
    return {"ligne": ligne["name"], "avant": avant, "apres": cas["montant"],
            "deja_encaisse": deja, "ligne_posee": pose.name if pose else None}


def reparer(insert=False, limite=None, clients=None) -> dict:
    """Rétablit les dettes manquantes. `insert=False` par défaut : on regarde avant d'écrire.

    ⚠️ LE DÉFAUT EST L'ESSAI À BLANC. Cette fonction crée des pièces comptables validées ; elle
    ne doit jamais partir sur un malentendu de paramètre.
    """
    cas = [c for c in diagnostic() if c["a_recreer"]]
    if clients:
        garde = set(clients if isinstance(clients, (list, set, tuple)) else [clients])
        cas = [c for c in cas if c["client"] in garde]
    if limite:
        cas = cas[:frappe.utils.cint(limite)]

    faits, erreurs = [], []
    for c in cas:
        if not frappe.utils.cint(insert):
            faits.append(dict(c, piece="(essai à blanc)"))
            continue
        try:
            faits.append(dict(c, piece=_creer_dette(c)))
        except Exception as e:
            erreurs.append(dict(c, erreur=str(e)[:200]))
    if frappe.utils.cint(insert):
        frappe.db.commit()
    return {"cas": len(cas), "total": flt(sum(c["montant"] for c in cas), PRECISION),
            "faits": faits, "erreurs": erreurs, "insert": bool(frappe.utils.cint(insert))}
