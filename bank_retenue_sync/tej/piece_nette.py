"""La piece remise en banque dit le VRAI montant d'un reglement : le lien entre le bordereau et la retenue a la source.

POURQUOI (27/11/2026). `tej/paiements.ajuster` tient pour un fait le montant d'un cheque ou d'une traite : « de
l'argent compte ne se reduit pas » (`argent_compte`). Quand le client declare une retenue sur une facture reglee au
TTC entier, la retenue devient donc un credit EN PLUS : la part liberee reste non affectee, notee comme avoir.

C'est juste quand la piece vaut vraiment le TTC. C'est faux quand elle a ete SAISIE au montant de la facture sans
lire le papier : MH2C regle la facture ACC-SINV-2026-02010 (4 005,000) par une traite de 3 965,000 — nette de sa
retenue de 40,040 — saisie a 4 005,000. La regularisation du 17/11/2026 lui a donc ouvert un avoir de 40,040 qu'il
n'a jamais verse, et le portefeuille de traites portait 40 DT de trop. Le bordereau de remise (BIAT 0132601), ecrit
en chiffres et en toutes lettres, dit 3 965,000 : c'est la preuve qui manquait a `ajuster`.

Sa demande : « il faut une correction automatique dans ces cas de figure ; comment on peut relier ca au mecanisme
de la retenue a la source ».

LE LIEN, DANS LES DEUX SENS
---------------------------
  1. LE BORDEREAU ARRIVE APRES LA REGULARISATION (`corriger_depuis_bordereau`, appele par `api/bordereaux._poser`) :
     une piece du bordereau designe un reglement dont une part est un avoir ne d'une retenue, et le papier vaut ce
     reglement MOINS cet avoir -> le reglement est repris au montant du papier, l'avoir est retire.
  2. LA RETENUE ARRIVE APRES LE BORDEREAU (`montant_du_papier`, lu par `tej/paiements.ajuster`) : le reglement a
     reprendre est un cheque ou une traite dont le bordereau dit qu'il vaut le TTC moins la retenue -> il est repris
     a ce montant tout de suite, sans jamais ouvrir d'avoir.

GARDE-FOUS — rien n'est corrige sur une simple ressemblance :
  - la lecture du bordereau est PROUVEE : la somme des montants lus tombe sur le total ecrit sur le papier ;
  - le papier vaut le reglement moins la retenue a `TOLERANCE` pres (l'arrondi d'une traite : 3 964,960 -> 3 965) ;
  - le reglement est encore dans le portefeuille, valide, sur aucun encaissement ;
  - l'avoir n'a pas ete utilise (la part non affectee du reglement le couvre encore en entier) ;
  - rien ne se fait si « valider d'office les ajustements de retenue » est decoche (`paiements._auto_submit`) :
    la piece reste « a verifier », nommee, pour une personne.
Reprendre un reglement, c'est l'annuler puis le refaire (memes regles que `ajuster`, dont `suppression_permise`).
"""
from __future__ import annotations

#: Ecart admis entre le papier et « reglement − retenue » : l'arrondi d'une piece au dinar (3 964,960 -> 3 965,000).
TOLERANCE = 1.0


def _m(valeur) -> float:
    try:
        return round(float(valeur or 0), 3)
    except (TypeError, ValueError):
        return 0.0


def _dt(montant) -> str:
    entier, _, millimes = f"{float(montant or 0):,.3f}".replace(",", " ").partition(".")
    return f"{entier},{millimes}"


def lecture_prouvee(pieces: list, total) -> bool:
    """La somme des montants lus tombe sur le total ecrit sur le bordereau. Pur."""
    if total in (None, "") or not pieces or any(p.get("montant") in (None, "") for p in pieces):
        return False
    return abs(_m(sum(_m(p.get("montant")) for p in pieces)) - _m(total)) <= 0.0005


def decider(papier, prouvee: bool, reglement: dict, avoir) -> dict:
    """Faut-il ramener ce reglement au montant du papier ? Pur.

    `papier` : le montant de la piece lu sur le bordereau ; `prouvee` : cf. `lecture_prouvee` ;
    `reglement` : {paid_amount, unallocated_amount, docstatus, en_encaissement} ;
    `avoir` : l'avoir ne d'une retenue note sur ce reglement (0 s'il n'y en a pas).
    -> {faire, raison, avant, apres, baisse, reste} — `reste` : ce qui demeure non affecte (l'arrondi)."""
    non = lambda raison: {"faire": False, "raison": raison}  # noqa: E731
    papier, avoir = _m(papier), _m(avoir)
    paye, libre = _m(reglement.get("paid_amount")), _m(reglement.get("unallocated_amount"))
    if avoir <= 0.0005:
        return non("aucun avoir né d’une retenue sur ce règlement")
    if not prouvee:
        return non("la lecture du bordereau n’est pas prouvée par son total")
    if int(reglement.get("docstatus") or 0) != 1:
        return non("le règlement n’est pas validé")
    if reglement.get("en_encaissement"):
        return non("le règlement est déjà sur un encaissement")
    if libre < avoir - 0.0005:
        return non("l’avoir né de la retenue a déjà été utilisé")
    baisse = _m(paye - papier)
    if baisse <= 0.0005:
        return non("le papier ne vaut pas moins que le règlement")
    if abs(papier - (paye - avoir)) > TOLERANCE + 0.0005:
        return non(f"le papier ({_dt(papier)}) ne vaut pas le règlement moins la retenue ({_dt(paye - avoir)})")
    reste = _m(libre - baisse)
    if reste < -0.0005:
        return non("le papier vaut moins que ce qui est imputé aux factures")
    return {"faire": True, "raison": "", "avant": paye, "apres": papier, "baisse": baisse, "reste": max(reste, 0.0)}


# ---------------------------------------------------------------------------
# Avec le site
# ---------------------------------------------------------------------------

def _certificat_de_l_avoir(reglement: str):
    import frappe

    from bank_retenue_sync.tej.paiements import DOCTYPE

    if not frappe.db.has_column(DOCTYPE, "avoir_reglement"):
        return None
    certs = frappe.get_all(DOCTYPE, filters={"avoir_reglement": reglement, "avoir_retenue": [">", 0]},
                           fields=["name", "avoir_retenue", "montant_retenue", "match_raison"], limit=2)
    return certs[0] if len(certs) == 1 else None


def corriger(reglement: str, papier: float, bordereau: str, prouvee: bool, en_encaissement: bool = False,
             insert: bool = True) -> dict:
    """Ramene un reglement au montant de la piece remise en banque, et retire l'avoir ne de la retenue.
    -> le plan de `decider`, complete de {reglement, nouveau, certificat, avoir_retire} quand c'est fait."""
    import frappe

    from bank_retenue_sync.tej import paiements

    cert = _certificat_de_l_avoir(reglement)
    original = frappe.get_doc("Payment Entry", reglement)
    plan = decider(papier, prouvee, {"paid_amount": original.paid_amount, "unallocated_amount": original.unallocated_amount,
                                     "docstatus": original.docstatus, "en_encaissement": en_encaissement},
                   cert.avoir_retenue if cert else 0)
    plan.update(reglement=reglement, certificat=cert.name if cert else None)
    if plan["faire"] and not paiements._auto_submit():
        plan.update(faire=False, raison="la validation d’office des ajustements de retenue est décochée")
    if not plan["faire"] or not insert:
        return plan

    supprimer = paiements.suppression_permise(True, paiements._supprimer_annule())
    copie = frappe.copy_doc(original)
    if not supprimer:
        copie.amended_from = original.name
    copie.paid_amount = copie.received_amount = plan["apres"]
    paiements.fusionner_references(copie)
    copie.custom_remarks = 1               # sans quoi ERPNext recrit la remarque et la trace de la reprise disparait
    copie.remarks = ("%s\nRepris le %s : montant ramené de %s à %s, celui de la pièce remise en banque (bordereau %s). "
                     "Elle était nette de la retenue à la source (certificat TEJ %s) : l’avoir de %s est retiré."
                     % (original.remarks or "", frappe.utils.nowdate(), _dt(plan["avant"]), _dt(plan["apres"]), bordereau,
                        cert.name, _dt(cert.avoir_retenue)))
    original.flags.ignore_permissions = True
    original.cancel()
    copie.insert(ignore_permissions=True)
    copie.submit()
    suppression = paiements._supprimer_reglement(original.name) if supprimer else {"supprime": False, "raison": None}
    frappe.db.set_value(paiements.DOCTYPE, cert.name, {
        "avoir_retenue": 0, "avoir_reglement": copie.name,
        "match_raison": ((cert.match_raison or "") + " ; le %s : règlement %s repris en %s au montant de la pièce (%s, "
                         "bordereau %s) — elle était nette de la retenue, avoir de %s retiré"
                         % (frappe.utils.nowdate(), original.name, copie.name, _dt(plan["apres"]), bordereau,
                            _dt(cert.avoir_retenue)))[:1000],
    }, update_modified=False)
    plan.update(nouveau=copie.name, avoir_retire=_m(cert.avoir_retenue), original_supprime=suppression["supprime"],
                original_a_supprimer=original.name if supprimer and not suppression["supprime"] else None)
    return plan


def supprimer_originaux(plans: list):
    """Les reglements annules que la piece du bordereau citait encore au moment de la correction : une fois le
    bordereau enregistre avec le reglement repris, plus rien ne les retient. Ne leve jamais."""
    from bank_retenue_sync.tej import paiements

    for plan in plans or ():
        if plan.get("original_a_supprimer"):
            paiements._supprimer_reglement(plan["original_a_supprimer"])


def corriger_depuis_bordereau(bordereau: str, rapprochees: list, portefeuille: list, total) -> list:
    """SENS 1 — le bordereau arrive apres la regularisation. Pour chaque piece qui designe (« À vérifier ») un
    reglement portant une part non affectee, tente la correction. Ne leve jamais : une correction qui echoue laisse
    la piece « a verifier ». -> [plans des corrections faites, avec leur `phrase` a ecrire sur le bordereau]."""
    import frappe

    faits = []
    prouvee = lecture_prouvee(rapprochees, total)
    par_nom = {c["name"]: c for c in portefeuille or ()}
    for p in rapprochees or ():
        c = par_nom.get(p.get("paiement"))
        if p.get("etat") != "À vérifier" or not c or not c.get("non_affecte") or p.get("montant") in (None, ""):
            continue
        try:
            plan = corriger(c["name"], p["montant"], bordereau, prouvee, bool(c.get("en_encaissement")))
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title="Bordereau : règlement net de retenue non corrigé", message=frappe.get_traceback())
            continue
        if plan.get("nouveau"):
            plan["phrase"] = (f"Règlement {plan['reglement']} repris en {plan['nouveau']} : {_dt(plan['avant'])} → "
                              f"{_dt(plan['apres'])}, le montant de la pièce. Elle était nette de la retenue à la "
                              f"source : l’avoir de {_dt(plan['avoir_retire'])} du client est retiré.")
            faits.append(plan)
    return faits


def montant_du_papier(reglement: str, retenue: float):
    """SENS 2 — la retenue arrive apres le bordereau. Le montant de la piece d'un bordereau collecte, quand elle
    designe ce reglement (meme paiement, meme numero, ou meme client) et vaut « reglement − retenue ».
    -> {montant, bordereau} ou None. Une seule piece doit repondre : sinon on ne devine pas."""
    import frappe

    from bank_retenue_sync.api import bordereaux
    from bank_retenue_sync.encaissement import pending

    pe = frappe.db.get_value("Payment Entry", reglement, ["party", "paid_amount", "reference_no", "paid_to", "docstatus"],
                             as_dict=True)
    natures = {pending.CHEQUE_ACCOUNT: ("Chèques", pending._extract_number),
               pending.TRAITE_ACCOUNT: ("Traites", pending._extract_traite_number)}
    if not pe or pe.docstatus != 1 or pe.paid_to not in natures or reglement in pending.already_encaissed_refs():
        return None
    nature, extraire = natures[pe.paid_to]
    attendu = _m(_m(pe.paid_amount) - _m(retenue))
    numero = bordereaux._chiffres(extraire(pe.reference_no))
    trouvees = []
    for b in frappe.get_all("BRS Bordereau Remise", filters={"nature": nature, "lecture_statut": "Lue"},
                            fields=["name", "total_lu"], limit_page_length=0):
        pieces = frappe.get_all("BRS Bordereau Remise Piece", filters={"parent": b.name}, limit_page_length=0,
                                fields=["numero", "montant", "tireur", "paiement"])
        if not lecture_prouvee(pieces, b.total_lu):
            continue
        for p in pieces:
            designe = p.paiement == reglement or (numero and bordereaux._chiffres(p.numero) == numero) \
                or (not p.paiement and bordereaux.meme_tireur(p.tireur, pe.party))
            if designe and abs(_m(p.montant) - attendu) <= TOLERANCE + 0.0005 and _m(p.montant) < _m(pe.paid_amount):
                trouvees.append({"montant": _m(p.montant), "bordereau": b.name})
    return trouvees[0] if len(trouvees) == 1 else None
