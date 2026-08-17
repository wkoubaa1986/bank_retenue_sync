"""Saisir un reglement recu du partenaire : il eteint ses plus anciennes dettes de commandes.

Le consolide n'a pas besoin qu'on lui dise qu'un reglement est arrive : `get_consolide` relit les
Payment Entry a chaque affichage et les impute sur les echeances. Ce module cree la piece ; le
consolide suit tout seul au prochain chargement.

⚠️ UN REGLEMENT ETEINT LES PLUS ANCIENNES DETTES DE COMMANDES, RIEN D'AUTRE. Le partenaire a 40
commandes dont le total depasse l'avance encaissee, mais 33 sont en statut `Completed` — livrees,
facturees, soldees hors du mecanisme des references, dont des commandes d'octobre 2023 pour
78 655,295. Affecter dessus enverrait le reglement du mois eteindre une creance de trois ans.
Seules comptent les commandes portant une piece de dette au compte `Dettes - A&S` : six a ce
jour, toutes posterieures a juin 2026.

⚠️ ET CES COMMANDES SONT SATUREES : leur `advance_paid` egale leur total, la piece de dette
occupe toute la place. ERPNext refuse alors toute avance supplementaire. Encaisser revient donc a
REMPLACER de la dette par du reglement :

    1. annuler puis supprimer la piece de dette de la commande
    2. creer le reglement, affecte a hauteur de ce qui est paye
    3. recreer une piece de dette pour le solde restant

L'ordre est le meme que pour l'ecriture de bilan, et pour la meme raison : recreer la dette avant
le reglement ressature la commande et fait rejeter la piece, apres que l'originale a ete detruite.
"""
from __future__ import annotations

import hashlib

import frappe
from frappe.utils import flt

from bank_retenue_sync.partenaire.economiq import CLIENT

PRECISION = 3

SOCIETE = "Aquaworld & Servicing"
COMPTE_DEBITEURS = "Débiteurs - A&S"


def cibles(jusqu_au: str) -> list:
    """Les commandes portant une dette, de la plus ancienne a la plus recente."""
    from bank_retenue_sync.partenaire.economiq import COMPTE_DETTES, MODE_DETTE

    lignes = frappe.db.sql("""
        select so.name, so.transaction_date, so.grand_total,
               round(sum(r.allocated_amount), 3) dette,
               group_concat(distinct pe.name) pieces
        from `tabSales Order` so
        join `tabPayment Entry Reference` r on r.reference_name = so.name and r.docstatus = 1
        join `tabPayment Entry` pe on pe.name = r.parent and pe.docstatus = 1
        where so.customer = %s and so.docstatus = 1 and so.transaction_date <= %s
          and pe.mode_of_payment = %s and pe.paid_to = %s
        group by so.name
        order by so.transaction_date asc, so.name asc
    """, (CLIENT, jusqu_au, MODE_DETTE, COMPTE_DETTES), as_dict=True)
    return [{"sales_order": l.name, "date": str(l.transaction_date or ""),
             "total": flt(l.grand_total, PRECISION), "dette": flt(l.dette, PRECISION),
             "pieces": (l.pieces or "").split(",") if l.pieces else []} for l in lignes]


def planifier(montant, cibles_) -> tuple[list, float]:
    """Ce que le reglement eteint, dette par dette. Fonction pure. -> (plan, avance).

    ⚠️ CHAQUE LIGNE EST PLAFONNEE A LA DETTE DE SA COMMANDE. Regler au-dela ferait depasser
    l'avance le total de la commande, et ERPNext rejetterait la piece entiere.
    """
    reste = round(float(montant or 0), PRECISION)
    plan = []
    for c in cibles_ or []:
        if reste <= 0.001:
            break
        dette_ = round(float(c.get("dette") or 0), PRECISION)
        regle = round(min(reste, dette_), PRECISION)
        if regle <= 0.001:
            continue
        plan.append({"sales_order": c["sales_order"], "date": c.get("date"),
                     "dette_avant": dette_, "regle": regle,
                     "dette_apres": round(dette_ - regle, PRECISION),
                     "pieces": c.get("pieces") or []})
        reste = round(reste - regle, PRECISION)
    return plan, max(0.0, reste)


def empreinte(plan: list) -> str:
    """Empreinte du plan d'affectation. Fonction pure.

    ⚠️ C'EST CE QUE L'UTILISATEUR A CONFIRME, PAS CE QU'IL A DEMANDE. L'ecran montre nommement les
    pieces de dette qui vont etre detruites, puis l'ecriture recalcule le plan de son cote. Entre
    les deux, un reglement saisi ailleurs peut avoir change les dettes : sans empreinte, on
    detruirait des pieces que personne n'a vues, derriere une confirmation qui en nommait
    d'autres.

    ⚠️ LES PIECES SONT TRIEES AVANT D'ETRE HACHEES. Elles viennent d'un `group_concat` (cf.
    `cibles`), dont MariaDB ne garantit pas l'ordre : sans le tri, deux lectures du meme etat
    donneraient deux empreintes et le geste serait refuse au hasard.

    Le montant global n'entre PAS dans l'empreinte : il est deja porte par les lignes.
    """
    parts = []
    for ligne in plan or []:
        pieces = sorted((ligne.get("pieces") or []))
        parts.append("%s|%.3f|%.3f|%s" % (
            ligne.get("sales_order") or "",
            round(float(ligne.get("dette_avant") or 0), PRECISION),
            round(float(ligne.get("regle") or 0), PRECISION),
            ",".join(pieces),
        ))
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:20]


def concorde(plan: list, attendue: str) -> bool:
    """Le plan recalcule est-il celui qui a ete confirme ? Fonction pure.

    Une empreinte attendue vide vaut accord : le chemin d'appel qui n'en fournit pas (console,
    script de reprise) n'a rien confirme a l'ecran, donc rien a contredire.
    """
    return not attendue or empreinte(plan) == attendue


def comptes() -> list:
    """Les couples mode / compte reellement utilises, du plus recemment servi au plus ancien.

    ⚠️ ON NE PROPOSE QUE CE QUI EXISTE DEJA. Offrir la liste complete des modes de paiement
    laisserait choisir une combinaison que la banque ne connait pas, et le rapprochement
    bancaire ne retrouverait jamais la piece.

    ⚠️ ET ON EXCLUT LES DEUX MODES QUI NE SONT PAS DE L'ARGENT RECU. `encaissements.encaisse`
    ecarte « Dette non payee » portee aux dettes ET « Perte de paiement » ; proposer l'un des deux
    ici creerait une piece que le consolide refuserait ensuite d'imputer — un reglement qui
    disparait de l'echeancier sans laisser de trace, alors que l'ecran vient de dire qu'il a ete
    encaisse.
    """
    from bank_retenue_sync.partenaire.economiq import MODE_DETTE, MODE_PERTE

    lignes = frappe.db.sql("""
        select mode_of_payment mode, paid_to compte, count(*) n,
               max(posting_date) dernier
        from `tabPayment Entry`
        where party = %s and payment_type = 'Receive' and docstatus = 1
          and ifnull(mode_of_payment,'') != '' and mode_of_payment not in (%s, %s)
        group by mode, compte
        order by dernier desc, n desc
    """, (CLIENT, MODE_DETTE, MODE_PERTE), as_dict=True)
    return [{"mode": l.mode, "compte": l.compte, "nombre": l.n, "dernier": str(l.dernier or "")}
            for l in lignes]


def proposer(montant, date=None) -> dict:
    """Ce que le reglement eteindrait, sans rien ecrire."""
    from bank_retenue_sync.partenaire import dette

    date = date or frappe.utils.nowdate()
    montant = flt(montant, PRECISION)
    liste = cibles(date)
    plan, avance = planifier(montant, liste)
    return {
        "date": str(date), "montant": montant, "cibles": liste,
        "dette_totale": flt(sum(c["dette"] for c in liste), PRECISION),
        "repartition": plan, "avance": avance,
        # L'empreinte du plan EXACTEMENT tel qu'il est montre. L'ecran la renvoie a la creation,
        # qui refuse si la base a bouge entre-temps.
        "empreinte": empreinte(plan),
        "comptes": comptes(), "auto_validation": dette.auto_validation(),
    }


#: Nom du savepoint qui enveloppe les trois etapes. Un seul, nomme : les savepoints imbriques ne
#: servent a rien ici, et un nom fixe rend le rollback lisible dans les journaux.
SAVEPOINT = "brs_reglement_economiq"

#: Verrou de la sequence. Deux clics simultanes creeraient deux Payment Entry pour un seul
#: versement, apres avoir detruit les memes pieces de dette deux fois.
VERROU = "brs_reglement_economiq"


def creer(montant, date=None, mode=None, compte=None, reference=None, valider=None,
          empreinte_attendue=None) -> dict:
    """Remplace de la dette par du reglement. IRREVERSIBLE : supprime des pieces validees.

    ⚠️ RIEN N'EST DETRUIT SI LE REGLEMENT N'A RIEN A ETEINDRE. Un versement sans dette en face
    n'a pas besoin de liberer quoi que ce soit ; il part en avance, et aucune piece n'est touchee.

    ⚠️ LES TROIS ETAPES TIENNENT DANS UNE SEULE TRANSACTION. Le commit final est le seul ; en cas
    d'echec a l'etape 3, le savepoint ramene les pieces de dette detruites a l'etape 1. Sans lui,
    une erreur de recreation laissait la dette d'origine supprimee et son solde jamais recree :
    la commande paraissait soldee, sans la moindre trace de ce qui manquait.

    ⚠️ ET LE PLAN CONFIRME EST VERIFIE AVANT DE DETRUIRE. `empreinte_attendue` vient de l'ecran,
    qui a nomme les pieces ; si la base a bouge depuis, on refuse au lieu de detruire autre chose
    que ce qui a ete montre.
    """
    from frappe.utils.synchronization import filelock

    from bank_retenue_sync.partenaire import dette as M_dette

    date = date or frappe.utils.nowdate()
    montant = flt(montant, PRECISION)
    if montant <= 0.001:
        frappe.throw("Le montant du règlement doit être positif.")
    if not mode or not compte:
        frappe.throw("Le mode de règlement et le compte crédité sont obligatoires.")

    with filelock(VERROU, timeout=30):
        plan, avance = planifier(montant, cibles(date))
        if not concorde(plan, empreinte_attendue):
            commandes = ", ".join(p["sales_order"] for p in plan) or "aucune"
            frappe.throw(
                "Les dettes ont changé depuis l’aperçu : rien n’a été touché. "
                "Le règlement porterait maintenant sur %s. Ferme et rouvre la saisie pour "
                "revoir l’affectation avant de confirmer." % commandes)

        valider = (M_dette.auto_validation() if valider is None
                   else bool(frappe.utils.cint(valider)))

        frappe.db.savepoint(SAVEPOINT)
        try:
            supprimees = []
            for p in plan:                                           # 1. destruction
                supprimees += M_dette.supprimer(
                    [{"payment_entry": n} for n in p["pieces"]], commit=False)

            doc = frappe.new_doc("Payment Entry")                    # 2. le reglement
            doc.payment_type = "Receive"
            doc.party_type = "Customer"
            doc.party = CLIENT
            doc.company = SOCIETE
            doc.posting_date = date
            doc.mode_of_payment = mode
            doc.paid_from = COMPTE_DEBITEURS
            doc.paid_to = compte
            doc.paid_amount = montant
            doc.received_amount = montant
            doc.source_exchange_rate = 1
            doc.target_exchange_rate = 1
            doc.reference_no = reference or ""
            doc.reference_date = date
            for p in plan:
                doc.append("references", {"reference_doctype": "Sales Order",
                                          "reference_name": p["sales_order"],
                                          "allocated_amount": p["regle"]})
            doc.flags.ignore_permissions = True
            doc.insert()
            if valider:
                doc.submit()

            recreees = M_dette.recreer(                              # 3. le solde de dette
                [{"sales_order": p["sales_order"], "montant": p["dette_apres"], "date": date,
                  "reference": p["sales_order"]} for p in plan],
                valider=valider, commit=False)
        except Exception:
            frappe.db.rollback(save_point=SAVEPOINT)
            # ⚠️ TITRE COURT ET SUR UNE LIGNE. Au-dela de 140 caracteres `log_error` leve, et
            # l'exception d'origine — la seule interessante — serait remplacee par la sienne.
            frappe.log_error(
                title="Règlement Economiq : séquence annulée",
                message="montant=%s date=%s mode=%s compte=%s\nplan=%s"
                        % (montant, date, mode, compte, frappe.as_json(plan)))
            raise

        frappe.db.commit()

    return {"payment_entry": doc.name, "docstatus": doc.docstatus, "montant": montant,
            "repartition": plan, "avance": avance, "supprimees": supprimees,
            "recreees": recreees, "validee": bool(valider)}
