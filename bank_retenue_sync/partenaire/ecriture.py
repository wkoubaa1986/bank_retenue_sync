"""L'ecriture de journal du bilan mensuel du partenaire — lecture et brouillon.

Une ecriture par mois depuis octobre 2024, libellee « Bilan activite MM-YYYY Economiq Aqua
Solution ». Sa structure, lue sur les remarques de chaque ligne :

    numero de reference (cheque_no)  « Bilan activite MM-YYYY Economiq Aqua Solution »
    date de reference   (cheque_date) dernier jour du mois

    debit   Depenses non declarees   benefice Aqua World du mois
    debit   Depenses non declarees   achats Economiq du mois
    debit   Depenses non declarees   charges portees par Aqua (salaire, corrections)
    credit  Economiq Aqua Solution   ventes Economiq du mois
    credit  Debiteurs (tiers)        AJUSTEMENT — la ligne d equilibre, rattachee a une commande

⚠️ LE LIBELLE VA DANS LE NUMERO DE REFERENCE, PAS SEULEMENT DANS LA REMARQUE. C'est `cheque_no`
qui porte « Bilan activite MM-YYYY Economiq Aqua Solution » sur les dix ecritures relevees, et
c'est de la qu'ERPNext compose sa remarque automatique (« Référence #… datée du … »). Le laisser
vide donnerait une piece que rien ne rattache visuellement a la serie.

⚠️ L'AJUSTEMENT EST UNE REDUCTION SUR LES COMMANDES OUVERTES, PAS UN RATTACHEMENT A UNE PIECE.
Il vient effacer le credit restant du partenaire, commande par commande, de la plus ancienne a
la plus recente, chacune plafonnee a ce qu'elle doit encore. Avril 2026 le montre en trois
lignes : 67,925 sur SAL-ORD-2026-00650 (27/02), 376,500 sur SAL-ORD-2026-00769 (10/03, absorbee
entierement), 55,075 sur SAL-ORD-2026-01026 (01/04) — 499,500 au total, dans l'ordre des dates.
Mai et juin n'ont qu'une ligne parce qu'une seule commande suffisait.

⚠️ ET UNE COMMANDE SATUREE FAIT ECHOUER LA PIECE ENTIERE. ERPNext refuse qu'une avance depasse
le total de la commande ; celles deja couvertes par une piece de dette n'ont plus de marge. En
juillet 2026, SAL-ORD-2026-02304 porte 1 919,556 de dette pour un total de 1 919,556 : l'y
imputer rendait « Advance paid ... cannot be greater than Grand Total » sans rien expliquer.

⚠️ LA LIGNE D EQUILIBRE EST L AJUSTEMENT, ET ELLE FAIT FOI. Elle vaut
`benefice Aqua − benefice Economiq + charges` — exactement la formule du code, puisque
`benefice Economiq = ventes − achats`. Mais elle est calculee sur les montants REELS, la ou le
bilan recalcule aujourd hui repose sur des achats sous-evalues : `tabItem Price` est vide (0
ligne pour 1 069 articles actifs) et `_purchase_price_resolver` rend 0,0 sans rien signaler.
Sur juin 2026, l ecriture donne 389,450 la ou le calcul donne 652,000 — 262,550 d ecart, qui se
deverserait tel quel sur la premiere echeance du partenaire.

⚠️ LE BROUILLON N EST JAMAIS SOUMIS. `creer` pose un Journal Entry en docstatus 0 et s arrete la.
Deux des cinq lignes — benefice Aqua et achats Economiq — dependent des prix d achat manquants ;
elles sont donc SAISIES par l appelant, pre-remplies avec le calcul et signalees comme
douteuses. Soumettre automatiquement graverait en comptabilite un chiffre que personne n a
regarde, et l ajustement relu ensuite viendrait de ce meme chiffre : l erreur se validerait
toute seule.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

from bank_retenue_sync.facturation import periode

PRECISION = 3

CLIENT = "ECONOMIQ AQUA SOLUTIONS"
COMPTE_PARTENAIRE = "Economiq Aqua Solution - A&S"
COMPTE_DEBITEURS = "Débiteurs - A&S"
COMPTE_CHARGES = "Dépenses non déclarées - A&S"


def ajustement_des_lignes(lignes: list) -> float:
    """Somme des credits portes au debiteur du partenaire. Fonction pure.

    ⚠️ PLUSIEURS LIGNES POSSIBLES. L ecriture d avril 2026 en porte trois (376,500 + 67,925 +
    55,075) ; ne lire que la premiere sous-estimerait l ajustement de 123,000.
    """
    return round(sum(
        float(l.get("credit") or 0) for l in (lignes or [])
        if (l.get("account") or "") == COMPTE_DEBITEURS and (l.get("party") or "") == CLIENT
    ), PRECISION)


def lire(mois: str) -> dict | None:
    """L ecriture de bilan du mois, ou None. -> dict sans document Frappe autour.

    Reconnue a sa STRUCTURE, pas a son libelle : une ligne au debiteur du partenaire et une
    ligne sur son compte. Les remarques ont change de forme au fil des mois — « Bilan activité »,
    « Bilan 'activité », « Bilan d'activité » — et deux d entre elles portent meme une annee
    fausse. S y fier ferait manquer l ecriture sans le dire.
    """
    mois = periode.normaliser(mois)
    debut, fin = periode.bornes(mois)

    noms = frappe.db.sql("""
        select distinct je.name
        from `tabJournal Entry` je
        join `tabJournal Entry Account` d on d.parent = je.name
        join `tabJournal Entry Account` p on p.parent = je.name
        where je.docstatus = 1 and je.posting_date between %s and %s
          and d.account = %s and d.party = %s and d.credit_in_account_currency > 0
          and p.account = %s
        order by je.posting_date desc
    """, (debut, fin, COMPTE_DEBITEURS, CLIENT, COMPTE_PARTENAIRE), as_dict=True)
    if not noms:
        return None

    nom = noms[0].name
    lignes = [{
        "compte": l.account,
        "account": l.account,
        "party": l.party or "",
        "debit": flt(l.debit_in_account_currency, PRECISION),
        "credit": flt(l.credit_in_account_currency, PRECISION),
        "remarque": (l.user_remark or "").strip(),
    } for l in frappe.get_all(
        "Journal Entry Account", filters={"parent": nom},
        fields=["account", "party", "debit_in_account_currency",
                "credit_in_account_currency", "user_remark"],
        order_by="idx asc", limit_page_length=0)]

    entete = frappe.db.get_value("Journal Entry", nom,
                                 ["posting_date", "user_remark"], as_dict=True)
    return {
        "journal_entry": nom,
        "date": str(entete.posting_date or ""),
        "libelle": (entete.user_remark or "").strip().splitlines()[0] if entete.user_remark else "",
        "autres": [n.name for n in noms[1:]],
        "lignes": lignes,
        "ajustement": ajustement_des_lignes(lignes),
        "charges": flt(sum(l["debit"] for l in lignes if l["compte"] == COMPTE_CHARGES),
                       PRECISION),
        "ventes_partenaire": flt(sum(l["credit"] for l in lignes
                                     if l["compte"] == COMPTE_PARTENAIRE), PRECISION),
    }


SOCIETE = "Aquaworld & Servicing"
CENTRE_DE_COUT = "Principal - A&S"


def equilibre(benefice_aqua, achats_partenaire, charges, ventes_partenaire) -> float:
    """La ligne au debiteur : ce qui reste pour equilibrer. Fonction pure.

    ⚠️ ELLE NE SE SAISIT PAS, ELLE SE DEDUIT. C'est l'ajustement qui reduira l'echeancier ; le
    laisser saisir permettrait d'annoncer au partenaire un montant que l'ecriture ne porte pas.
    """
    return round(
        float(benefice_aqua or 0) + float(achats_partenaire or 0) + float(charges or 0)
        - float(ventes_partenaire or 0), PRECISION)


def repartir(ajustement: float, ordres: list, ordre: str = "anciennes") -> tuple[list, float]:
    """Repartit l'ajustement sur les commandes ouvertes. Fonction pure.

    Rend (plan, non impute). Le plan est une liste de {sales_order, montant}, chacune plafonnee
    au credit restant de la commande. `ordre` vaut "anciennes" (par defaut) ou "recentes".

    ⚠️ PLAFONNER A `disponible` N'EST PAS UNE PRECAUTION, C'EST LA CONDITION POUR QUE LA PIECE
    PASSE. ERPNext rejette toute avance superieure au total de la commande, et le rejet porte sur
    l'ecriture ENTIERE : une seule ligne en trop et rien n'est enregistre.

    ⚠️ ET L'ORDRE NE SE DEVINE PAS DEPUIS LES PIECES EXISTANTES. Le versement de 2 700,000 du
    14/07/2026 s'est reparti sur des commandes de mai-juin 2026 alors que trois commandes de
    novembre 2023 etaient ouvertes et non soldees : ni « plus ancienne d'abord » ni « plus
    recente d'abord » ne rend compte de tous les cas releves. L'appelant tranche, l'ecran montre
    le resultat avant d'ecrire.
    """
    reste = round(float(ajustement or 0), PRECISION)
    plan = []
    cle = lambda o: (o.get("date") or "", o.get("sales_order") or "")  # noqa: E731
    for o in sorted(ordres or [], key=cle, reverse=(ordre == "recentes")):
        if reste <= 0.001:
            break
        part = min(reste, round(float(o.get("disponible") or 0), PRECISION))
        if part <= 0.001:
            continue
        part = round(part, PRECISION)
        plan.append({"sales_order": o["sales_order"], "date": o.get("date"),
                     "total": o.get("total"), "disponible": o.get("disponible"),
                     "montant": part})
        reste = round(reste - part, PRECISION)
    return plan, max(0.0, reste)


def ouvertes(jusqu_au: str) -> list:
    """Les commandes qui portent une DETTE, a la date donnee. Ce sont elles que l'ajustement reduit.

    ⚠️ UNE COMMANDE SANS PIECE DE DETTE N'EST PAS UNE DETTE. Filtrer sur `advance_paid <
    grand_total` remontait 40 commandes ouvertes, dont des commandes d'octobre 2023 jamais
    rapprochees : l'ajustement serait alle reduire un engagement de trois ans que personne ne
    reclame, au lieu de la dette du mois. Seul le compte des dettes fait foi.

    ⚠️ BORNEES A LA DATE DE L'ECRITURE. Une ecriture datee du 30/06 ne peut pas reduire une
    commande de juillet : elle reclamerait une reduction sur un engagement qui n'existait pas.
    """
    from bank_retenue_sync.partenaire.economiq import COMPTE_DETTES, MODE_DETTE

    lignes = frappe.db.sql("""
        select so.name, so.transaction_date, so.grand_total, so.advance_paid,
               round(sum(r.allocated_amount), 3) dette
        from `tabSales Order` so
        join `tabPayment Entry Reference` r on r.reference_name = so.name and r.docstatus = 1
        join `tabPayment Entry` pe on pe.name = r.parent and pe.docstatus = 1
        where so.customer = %s and so.docstatus = 1 and so.transaction_date <= %s
          and pe.mode_of_payment = %s and pe.paid_to = %s
        group by so.name
        order by so.transaction_date asc, so.name asc
    """, (CLIENT, jusqu_au, MODE_DETTE, COMPTE_DETTES), as_dict=True)

    return [{"sales_order": l.name, "date": str(l.transaction_date or ""),
             "total": flt(l.grand_total, PRECISION),
             "dette": flt(l.dette, PRECISION),
             "disponible": max(0.0, flt(flt(l.grand_total, PRECISION)
                                        - flt(l.advance_paid, PRECISION), PRECISION))}
            for l in lignes]


def simuler_liberation(ordres: list, detruites: list) -> list:
    """Le credit restant des commandes SI les pieces citees etaient supprimees. Fonction pure.

    ⚠️ SANS CETTE SIMULATION, ON DETRUIT PUIS ON DECOUVRE. La verification « est-ce que ca
    passera ? » doit se faire sur l'etat d'apres, pas sur l'etat d'avant : sur l'etat d'avant,
    toutes les commandes en dette sont saturees et la reponse est toujours non.
    """
    rendu = {}
    for d in detruites or []:
        cle = d.get("sales_order")
        rendu[cle] = round(rendu.get(cle, 0.0) + float(d.get("montant") or 0), PRECISION)
    return [{**o, "disponible": round(float(o.get("disponible") or 0)
                                      + rendu.get(o.get("sales_order"), 0.0), PRECISION)}
            for o in (ordres or [])]


def capacites(noms: list) -> list:
    """Le credit restant de commandes DESIGNEES, sans exiger qu'elles portent encore une dette.

    ⚠️ APRES LIBERATION, `ouvertes` NE VOIT PLUS LA COMMANDE LIBEREE. Elle exige une piece de
    dette ; or on vient justement de la supprimer. Recalculer la capacite avec `ouvertes` faisait
    disparaitre du perimetre la seule commande qu'on venait de degager, concluait « rien n'est
    imputable », et s'arretait APRES la destruction : la piece etait perdue et rien n'etait cree.
    C'est arrive une fois, sur ACC-PAY-2026-04403. D'ou cette fonction, qui travaille sur un
    ensemble fige AVANT toute suppression.
    """
    noms = [n for n in dict.fromkeys(noms or []) if n]
    if not noms:
        return []
    lignes = frappe.get_all(
        "Sales Order", filters={"name": ["in", noms], "docstatus": 1},
        fields=["name", "transaction_date", "grand_total", "advance_paid"],
        order_by="transaction_date asc, name asc", limit_page_length=0)
    return [{"sales_order": l.name, "date": str(l.transaction_date or ""),
             "total": flt(l.grand_total, PRECISION),
             "disponible": max(0.0, flt(flt(l.grand_total, PRECISION)
                                        - flt(l.advance_paid, PRECISION), PRECISION))}
            for l in lignes]


def bloquantes(jusqu_au: str) -> list:
    """Les pieces de dette qui saturent une commande et l'empechent d'absorber l'ajustement.

    ⚠️ ON LES SIGNALE, ON NE LES ANNULE PAS ICI. Annuler une Payment Entry soumise est
    irreversible et sort du perimetre d'une preparation de brouillon.
    """
    from bank_retenue_sync.partenaire.economiq import COMPTE_DETTES, MODE_DETTE

    lignes = frappe.db.sql("""
        select pe.name, pe.posting_date, pe.paid_amount, r.reference_name commande,
               r.allocated_amount, so.grand_total, so.advance_paid
        from `tabPayment Entry` pe
        join `tabPayment Entry Reference` r on r.parent = pe.name
        join `tabSales Order` so on so.name = r.reference_name
        where pe.party = %s and pe.docstatus = 1 and pe.mode_of_payment = %s
          and pe.paid_to = %s and so.transaction_date <= %s
          and so.advance_paid >= so.grand_total - 0.001
        order by pe.posting_date asc
    """, (CLIENT, MODE_DETTE, COMPTE_DETTES, jusqu_au), as_dict=True)
    return [{"payment_entry": l.name, "date": str(l.posting_date or ""),
             "montant": flt(l.paid_amount, PRECISION),
             "sales_order": l.commande, "impute": flt(l.allocated_amount, PRECISION),
             "total_commande": flt(l.grand_total, PRECISION)} for l in lignes]


def proposer(mois: str) -> dict:
    """Les cinq lignes proposees pour le mois, sans rien ecrire.

    Les deux montants issus des prix d'achat sont rendus tels quels avec `fiable: False` — a
    l'appelant de les corriger. Les masquer les ferait passer pour verifies.
    """
    from bank_retenue_sync.partenaire import dette as M_dette, economiq

    mois = periode.normaliser(mois)
    tableau = economiq.tableau(mois)
    if not tableau.get("disponible"):
        frappe.throw(tableau.get("message") or "Bilan indisponible.")

    aqua = tableau["bilan"]["aqua"]
    partenaire = tableau["bilan"]["partenaire"]
    charges = [{"libelle": c["libelle"], "montant": flt(c["montant"], PRECISION)}
               for c in (tableau.get("charges_libres") or [])]
    total_charges = flt(sum(c["montant"] for c in charges), PRECISION)
    mm, aaaa = mois[5:7], mois[:4]
    equilibre_du_mois = equilibre(aqua["benefice"], partenaire["achats"], total_charges,
                                  partenaire["ventes"])

    ordres = ouvertes(periode.bornes(mois)[1])
    plan, non_impute = repartir(equilibre_du_mois, ordres)

    return {
        "mois": mois,
        "date": periode.bornes(mois)[1],
        "commandes": ordres,
        "repartition": plan,
        "non_impute": non_impute,
        "dettes_bloquantes": bloquantes(periode.bornes(mois)[1]) if non_impute else [],
        "auto_validation": M_dette.auto_validation(),
        "libelle": "Bilan activité {0}-{1} Economiq Aqua Solution".format(mm, aaaa),
        "existante": lire(mois),
        "societe": SOCIETE,
        "benefice_aqua": {"montant": flt(aqua["benefice"], PRECISION), "fiable": False,
                          "remarque": "Bénéfice Aqua World {0}/{1}".format(mm, aaaa)},
        "achats_partenaire": {"montant": flt(partenaire["achats"], PRECISION), "fiable": False,
                              "remarque": "Achats Economiq {0}/{1}".format(mm, aaaa)},
        "ventes_partenaire": {"montant": flt(partenaire["ventes"], PRECISION), "fiable": True,
                              "remarque": "Ventes Economiq {0}/{1}".format(mm, aaaa)},
        "charges": charges,
        "total_charges": total_charges,
        "equilibre": equilibre(aqua["benefice"], partenaire["achats"], total_charges,
                               partenaire["ventes"]),
    }


def creer(mois: str, benefice_aqua, achats_partenaire, ventes_partenaire,
          charges=None, repartition=None, force: int = 0) -> dict:
    """Pose le brouillon d'ecriture du mois. Ne soumet jamais. -> {journal_entry, ajustement}."""
    mois = periode.normaliser(mois)
    if not frappe.utils.cint(force):
        deja = lire(mois)
        if deja:
            frappe.throw("Une écriture de bilan existe déjà sur {0} : {1}.".format(
                mois, deja["journal_entry"]))

    charges = [{"libelle": (c.get("libelle") or "").strip(),
                "montant": flt(c.get("montant"), PRECISION)}
               for c in (charges or []) if flt(c.get("montant"), PRECISION)]
    total_charges = flt(sum(c["montant"] for c in charges), PRECISION)
    ajustement = equilibre(benefice_aqua, achats_partenaire, total_charges, ventes_partenaire)
    if ajustement <= 0:
        frappe.throw(
            "L’équilibre au débiteur vaut {0} : l’écriture réclamerait au partenaire un montant "
            "nul ou négatif. Vérifie les montants avant de créer le brouillon.".format(
                frappe.utils.fmt_money(ajustement, currency="TND")))

    mm, aaaa = mois[5:7], mois[:4]
    doc = frappe.new_doc("Journal Entry")
    doc.voucher_type = "Journal Entry"
    doc.company = SOCIETE
    doc.posting_date = periode.bornes(mois)[1]
    libelle = "Bilan activité {0}-{1} Economiq Aqua Solution".format(mm, aaaa)
    doc.user_remark = libelle
    doc.cheque_no = libelle
    doc.cheque_date = doc.posting_date

    def ligne(compte, debit=0.0, credit=0.0, remarque="", tiers=False, commande=None):
        return {"account": compte, "cost_center": CENTRE_DE_COUT,
                "debit_in_account_currency": flt(debit, PRECISION),
                "credit_in_account_currency": flt(credit, PRECISION),
                "user_remark": remarque,
                **({"party_type": "Customer", "party": CLIENT} if tiers else {}),
                **({"reference_type": "Sales Order", "reference_name": commande}
                   if tiers and commande else {})}

    doc.append("accounts", ligne(COMPTE_CHARGES, debit=benefice_aqua,
                                 remarque="Bénéfice Aqua World {0}/{1}".format(mm, aaaa)))
    doc.append("accounts", ligne(COMPTE_CHARGES, debit=achats_partenaire,
                                 remarque="Achats Economiq {0}/{1}".format(mm, aaaa)))
    for c in charges:
        doc.append("accounts", ligne(COMPTE_CHARGES, debit=c["montant"], remarque=c["libelle"]))
    doc.append("accounts", ligne(COMPTE_PARTENAIRE, credit=ventes_partenaire,
                                 remarque="Ventes Economiq {0}/{1}".format(mm, aaaa)))
    # ⚠️ UNE LIGNE PAR COMMANDE REDUITE. L'ajustement efface le credit restant du partenaire ;
    # le porter en une seule ligne sans reference ne reduirait aucune commande et laisserait le
    # solde du partenaire inchange malgre l'ecriture.
    plan = [{"sales_order": p.get("sales_order"), "montant": flt(p.get("montant"), PRECISION)}
            for p in (repartition or []) if flt(p.get("montant"), PRECISION) > 0.001]
    reparti = flt(sum(p["montant"] for p in plan), PRECISION)
    if plan and abs(reparti - ajustement) > 0.001:
        frappe.throw(
            "La répartition totalise {0} pour un ajustement de {1} : l’écriture ne serait pas "
            "équilibrée.".format(reparti, ajustement))

    if plan:
        for p in plan:
            doc.append("accounts", ligne(
                COMPTE_DEBITEURS, credit=p["montant"], tiers=True, commande=p["sales_order"],
                remarque="Ajust. bilan {0}/{1} — {2}".format(mm, aaaa, p["sales_order"])))
    else:
        doc.append("accounts", ligne(COMPTE_DEBITEURS, credit=ajustement, tiers=True,
                                     remarque="Ajust. bilan {0}/{1}".format(mm, aaaa)))

    doc.flags.ignore_permissions = True
    doc.insert()          # docstatus 0 — le brouillon s arrete ici, volontairement.
    frappe.db.commit()
    return {"journal_entry": doc.name, "ajustement": ajustement, "date": str(doc.posting_date)}


def executer(mois: str, benefice_aqua, achats_partenaire, ventes_partenaire,
             charges=None, liberer: int = 0, force: int = 0) -> dict:
    """Le geste complet : liberer les dettes, poser l'ecriture, recreer le reste de dette.

    ⚠️ L'ORDRE EST LA SEULE CHOSE QUI FAIT MARCHER CE BLOC. Recreer la dette avant l'ecriture
    ressature la commande et fait rejeter l'ecriture, apres que les pieces d'origine ont deja
    ete detruites. Rien ne rattraperait cet etat.

    ⚠️ ET ON NE DETRUIT RIEN SI LE COMPTE N'Y EST PAS. Si les pieces disponibles ne degagent pas
    l'ajustement, on s'arrete AVANT la premiere suppression : mieux vaut un mois non traite
    qu'un mois a moitie defait.
    """
    from bank_retenue_sync.partenaire import dette as M_dette

    mois = periode.normaliser(mois)
    if not frappe.utils.cint(force):
        deja = lire(mois)
        if deja:
            frappe.throw("Une écriture de bilan existe déjà sur {0} : {1}.".format(
                mois, deja["journal_entry"]))

    charges = charges or []
    total_charges = flt(sum(flt(c.get("montant"), PRECISION) for c in charges), PRECISION)
    ajustement = equilibre(benefice_aqua, achats_partenaire, total_charges, ventes_partenaire)
    fin = periode.bornes(mois)[1]
    valider = M_dette.auto_validation()

    # ⚠️ LE PERIMETRE DES COMMANDES SE FIGE AVANT LA PREMIERE SUPPRESSION. Le recalculer ensuite
    # avec `ouvertes` exclurait la commande qu'on vient de liberer, puisqu'elle n'a plus de piece
    # de dette : on detruirait pour rien, puis on echouerait.
    eligibles = ouvertes(fin)
    perimetre = [o["sales_order"] for o in eligibles]
    plan_libere, non_impute = repartir(ajustement, eligibles)
    supprimees, recreees = [], []

    if non_impute > 0.001:
        if not frappe.utils.cint(liberer):
            frappe.throw(
                "{0} ne peut pas être imputé : les commandes en dette sont saturées. Relance "
                "en autorisant la libération des pièces de dette.".format(non_impute))
        plan_dette = M_dette.plan(mois, non_impute)
        if not plan_dette["suffisant"]:
            frappe.throw(
                "Les pièces de dette disponibles ne dégagent que {0} sur les {1} nécessaires. "
                "Rien n’a été supprimé.".format(plan_dette["degage"], non_impute))

        detruites = plan_dette["a_supprimer"]
        perimetre += [d["sales_order"] for d in detruites]

        # ⚠️ ON VERIFIE QUE CA PASSERA AVANT DE DETRUIRE. Simuler la liberation coute une
        # addition ; s'en passer coute une piece comptable perdue.
        _, apres = repartir(ajustement, simuler_liberation(capacites(perimetre), detruites))
        if apres > 0.001:
            frappe.throw(
                "Même après libération, {0} resterait non imputable. Rien n’a été supprimé."
                .format(apres))

        supprimees = M_dette.supprimer(detruites)                        # 1. destruction
        plan_libere, non_impute = repartir(ajustement, capacites(perimetre))  # 2. repartition
        if non_impute > 0.001:
            frappe.throw(
                "Après libération, {0} reste non imputable. Les pièces {1} ont été supprimées : "
                "recrée-les à la main avant de recommencer.".format(
                    non_impute, ", ".join(supprimees)))

        credite = {}
        for p in plan_libere:
            credite[p["sales_order"]] = flt(credite.get(p["sales_order"], 0)
                                            + p["montant"], PRECISION)
        restes = [{"sales_order": d["sales_order"],
                   "montant": flt(d["montant"] - credite.get(d["sales_order"], 0), PRECISION),
                   "date": d["date"], "reference": d["sales_order"]}
                  for d in detruites]
    else:
        restes = []

    piece = creer(mois, benefice_aqua, achats_partenaire, ventes_partenaire,   # 3. l ecriture
                  charges, repartition=plan_libere, force=1)
    if valider:
        doc = frappe.get_doc("Journal Entry", piece["journal_entry"])
        doc.flags.ignore_permissions = True
        doc.submit()
        frappe.db.commit()

    if restes:
        recreees = M_dette.recreer(restes, valider=valider)           # 4. la dette restante

    return {**piece, "repartition": plan_libere, "supprimees": supprimees,
            "recreees": recreees, "validee": bool(valider)}
