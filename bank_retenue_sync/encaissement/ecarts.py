"""Ecarts d'encaissement Aramex : persistance, blocage de soumission, resolutions humaines.

POLITIQUE (decisions utilisateur 2026-08-18, cas reel ENC-18-08-2026-00008 / FT26224F6S40) :
  - ecart <= 1 DT par paiement (TOLERANCE_PAIEMENT de matching.py) : ligne VALIDE, ecart trace
    (« Toléré ») et constate par l'ecart de paiement — jamais bloquant ;
  - frais Aramex (lignes negatives de l'advice, ex. TRFREVENUE −2,380 chaque semaine) : absorbes
    (« Frais »), non bloquants ;
  - paiement identifie avec un delta > 1 DT (ex. Baccari : PE 440, Aramex reverse 265 apres
    retour d'un article defectueux) : le brouillon est CREE avec la ligne, mais un ecart
    BLOQUANT empeche la soumission tant que l'humain n'a pas tranche ;
  - ligne d'advice sans PE : ecart BLOQUANT « Sans pièce ».

RESOLUTIONS — elles reprennent LES MECANISMES MAISON de customization_app, jamais les notres :
  Sur un « Delta paiement » (client connu via la PE), premier geste COMMUN : la PE Aramex est
  remplacee a l'identique au montant reellement verse (meme reference « Aramex N: <suivi> » —
  le rapprochement et la dedup en dependent), puis le DELTA est traite selon le choix :
    « Perte de non paiement »      : JE Dr Perte de non paiement / Cr Debiteurs (client,
                                     reference a la COMMANDE du paiement d'origine) ;
    « Ajustement livraison-dette » : le delta REDEVIENT UNE DETTE, patron du server script
                                     « Traitement des encaissement » (PE mode « Dette non
                                     payée », Débiteurs -> Dettes - A&S, reference_no = la
                                     commande) — le flux dettes l'encaissera plus tard ;
    « Paiement par avoir »         : SEULEMENT si le client a encore un AVOIR FLOTTANT
                                     (avoir_client.py, non encore applique) — on ajoute a
                                     l'echeancier de la commande une ligne mode « Avoir
                                     client » du delta et c'est le tandem « re-generate
                                     payment after sales order » qui cree la JE d'application
                                     (une JE fabriquee ici n'aurait pas de custom_source_row_uid).
  Sur un « Sans pièce » :
    « Ajustement »                 : reclasser une PE existante designee par l'utilisateur sur
                                     Livraison Aramex avec le suivi de l'advice ;
    « Ignoré »                     : montant <= 1 DT uniquement (cas Sahbi Chouchane, 1 DT dont
                                     l'avoir est deja consomme) — l'ecart est resolu sans piece,
                                     le montant sera constate par l'ecart de paiement mensuel.

TRACABILITE : chaque resolution accepte une note utilisateur, posee dans le « Numéro de
reference » (cheque_no) et la remarque des JE creees, les remarques des PE, et l'ecart.

/!\ La branche aramex du server script « Traitement des encaissement » copie la PE INTEGRALE
vers la banque puis la SUPPRIME : toute resolution met d'abord la PE au bon montant, puis
aligne la ligne du brouillon — jamais l'inverse.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

DOCTYPE = "BRS Ecart Encaissement"
ENCAISSEMENT_DOCTYPE = "Encaissement Paiement"
COMPTE_ARAMEX = "Livraison Aramex - A&S"
# compte d'attente cible d'un reclassement, par flux (cf. pending.get_pending_*)
_COMPTES_ATTENTE = {"aramex": "Livraison Aramex - A&S", "cheque": "Chèques - A&S",
                    "traite": "Traite Bancaire - A&S"}
COMPTE_DETTES = "Dettes - A&S"                   # patron dette du server script (l.794)
PERTE_ACCOUNT = "Perte de non paiement - A&S"    # meme compte que expenses/pertes.py
SEUIL_IGNORABLE = 1.0                            # « Sans pièce » ignorable jusqu'a 1 DT inclus


# ------------------------------------------------------------------ persistance & blocage

def persister(encaissement: str, diagnostics: list) -> list:
    """Enregistre les diagnostics {"type": "ecart"} produits par matching.match_aramex sur le
    brouillon `encaissement`. Idempotent : un ecart deja present (meme brouillon, meme suivi,
    meme type) n'est pas reecrit — l'arbitrage humain (statut, resolution) prime."""
    created = []
    for e in diagnostics or []:
        if e.get("type") != "ecart":
            continue
        key = {"encaissement": encaissement, "type_ecart": e.get("sous_type"),
               "suivi": e.get("suivi") or "", "reference_bancaire": e.get("reference") or ""}
        if frappe.db.exists(DOCTYPE, key):
            continue
        doc = frappe.get_doc({
            "doctype": DOCTYPE,
            "encaissement": encaissement,
            "reference_bancaire": e.get("reference") or "",
            "suivi": e.get("suivi") or "",
            "flux": e.get("flux") or "aramex",
            "bon": e.get("bon") or "",
            "type_ecart": e.get("sous_type"),
            "bloquant": 1 if e.get("bloquant") else 0,
            "statut": "À traiter" if e.get("bloquant") else "Auto",
            "client": e.get("client") or "",
            "ref_paiement": e.get("ref_paiement") or "",
            "montant_advice": flt(e.get("montant_advice"), 3),
            "montant_piece": flt(e.get("montant_piece"), 3),
            "ecart": flt(e.get("ecart"), 3),
            "note": e.get("note") or "",
        })
        doc.insert(ignore_permissions=True)
        created.append(doc.name)
    return created


def before_submit(doc, method=None):
    """Hook doc_events sur Encaissement Paiement : la soumission est interdite tant qu'un ecart
    bloquant du lot n'est pas resolu — c'est le point d'intervention humaine voulu."""
    pend = frappe.get_all(DOCTYPE,
                          filters={"encaissement": doc.name, "bloquant": 1, "statut": "À traiter"},
                          fields=["type_ecart", "suivi", "client", "montant_advice",
                                  "montant_piece", "ecart"])
    if not pend:
        return
    lignes = "".join(
        "<li>{0} — suivi {1} ({2}) : advice {3} / pièce {4} → écart {5}</li>".format(
            frappe.utils.escape_html(p.type_ecart or ""),
            frappe.utils.escape_html(p.suivi or "?"),
            frappe.utils.escape_html(p.client or "?"),
            frappe.format_value(p.montant_advice, {"fieldtype": "Currency"}),
            frappe.format_value(p.montant_piece, {"fieldtype": "Currency"}),
            frappe.format_value(p.ecart, {"fieldtype": "Currency"}))
        for p in pend)
    frappe.throw(
        _("Ce lot Aramex porte {0} écart(s) non résolu(s) :<ul>{1}</ul>"
          "Résolvez-les depuis le bouton « Écarts Aramex » du formulaire "
          "avant de soumettre.").format(len(pend), lignes),
        title=_("Écarts Aramex non résolus"))


# ------------------------------------------------------------------ avoir flottant (maison)

def avoir_flottant(customer: str) -> float:
    """Solde d'avoir flottant du client, au sens de customization_app :
      crees   : JE d'avoir_client.py — credit Debiteurs (party), SANS reference de commande,
                « Numéro de référence » commençant par « Avoir » ;
      appliques : JE du tandem (create_avoir_journal_entry_for_sales_order) — voucher
                « Credit Note », mode of payment « Avoir client » (ligne credit referencant
                la commande).
    Rend crees − appliques (>= 0). L'appariement maison se faisant par MONTANT, ce solde est
    un plafond de disponibilite, pas un lettrage."""
    receivable = "Débiteurs - A&S"
    crees = frappe.db.sql("""
        select coalesce(sum(jea.credit_in_account_currency), 0)
        from `tabJournal Entry Account` jea
        join `tabJournal Entry` je on je.name = jea.parent
        where je.docstatus = 1 and jea.account = %s
          and jea.party_type = 'Customer' and jea.party = %s
          and coalesce(jea.reference_type, '') = ''
          and jea.credit_in_account_currency > 0
          and je.cheque_no like 'Avoir%%'
          and coalesce(je.mode_of_payment, '') != 'Avoir client'
    """, (receivable, customer))[0][0]
    appliques = frappe.db.sql("""
        select coalesce(sum(jea.credit_in_account_currency), 0)
        from `tabJournal Entry Account` jea
        join `tabJournal Entry` je on je.name = jea.parent
        where je.docstatus = 1 and jea.account = %s
          and jea.party_type = 'Customer' and jea.party = %s
          and jea.credit_in_account_currency > 0
          and je.mode_of_payment = 'Avoir client'
    """, (receivable, customer))[0][0]
    return max(0.0, flt(flt(crees, 3) - flt(appliques, 3), 3))


@frappe.whitelist()
def liste(encaissement: str):
    """Les ecarts du brouillon, enrichis pour le formulaire :
      - `avoir_disponible` (Delta uniquement) : solde d'avoir flottant du client de la PE —
        le bouton « Avoir » n'est propose que s'il couvre le delta ;
      - `ignorable` : « Sans pièce » de montant <= 1 DT."""
    frappe.only_for(("System Manager", "Accounts Manager"))
    rows = frappe.get_all(DOCTYPE, filters={"encaissement": encaissement},
                          fields=["name", "type_ecart", "bloquant", "statut", "suivi", "client",
                                  "flux", "bon", "ref_paiement", "montant_advice",
                                  "montant_piece", "ecart", "resolution", "piece_resolution",
                                  "note"],
                          order_by="bloquant desc, creation")
    for r in rows:
        r["avoir_disponible"] = 0.0
        r["ignorable"] = 0
        if r.statut != "À traiter" or not r.bloquant:
            continue
        if r.type_ecart == "Delta paiement" and r.ref_paiement:
            party = frappe.db.get_value("Payment Entry", r.ref_paiement, "party")
            if party:
                r["avoir_disponible"] = avoir_flottant(party)
        elif r.type_ecart == "Sans pièce" and flt(r.montant_advice, 3) <= SEUIL_IGNORABLE:
            r["ignorable"] = 1
    return rows


# ------------------------------------------------------------------ outils communs

def _company_et_cc():
    company = (frappe.db.get_single_value("Bank Retenue Sync Settings", "company")
               or frappe.db.get_value("Account", COMPTE_ARAMEX, "company"))
    return company, frappe.db.get_value("Company", company, "cost_center")


def _receivable(company):
    return frappe.db.get_value("Company", company, "default_receivable_account")


def _ecart_a_traiter(name: str):
    e = frappe.get_doc(DOCTYPE, name)
    if e.statut != "À traiter":
        frappe.throw(_("L'écart {0} est déjà {1}.").format(e.name, e.statut))
    return e


def _commande_de(pe_doc) -> str:
    """La commande (ou facture) du paiement d'origine — cible des gestes perte/dette/avoir."""
    for r in pe_doc.get("references") or []:
        if r.reference_doctype in ("Sales Order", "Sales Invoice"):
            return r.reference_name
    frappe.throw(_("La Payment Entry {0} ne référence aucune commande.").format(pe_doc.name))


def _remplacer_pe(pe_name: str, montant: float, note: str = ""):
    """Annule la PE soumise et la recree a l'identique au nouveau montant (allocations reduites
    en premier-arrive). Retourne la nouvelle PE soumise. La branche aramex du script de
    soumission copiant la PE ENTIERE, c'est le seul moyen d'encaisser un partiel.
    /!\\ `reference_no` reste « Aramex N: <suivi> » : rapprochement et dedup en dependent —
    la note utilisateur va dans `remarks`."""
    src = frappe.get_doc("Payment Entry", pe_name)
    if src.docstatus != 1:
        frappe.throw(_("La Payment Entry {0} n'est pas soumise (docstatus {1}).")
                     .format(pe_name, src.docstatus))
    new = frappe.new_doc("Payment Entry")
    for f in ("payment_type", "posting_date", "company", "mode_of_payment", "party_type",
              "party", "party_name", "paid_from", "paid_from_account_currency", "paid_to",
              "paid_to_account_currency", "reference_no", "reference_date", "remarks",
              "source_exchange_rate", "target_exchange_rate", "cost_center", "project"):
        new.set(f, src.get(f))
    if note:
        new.remarks = ((new.remarks + "\n") if new.remarks else "") + note
    new.paid_amount = new.received_amount = flt(montant, 3)
    reste = flt(montant, 3)
    refs = []
    for r in src.get("references") or []:
        part = min(reste, flt(r.allocated_amount, 3))
        if part <= 0:
            continue
        refs.append({"reference_doctype": r.reference_doctype,
                     "reference_name": r.reference_name,
                     "total_amount": r.total_amount,
                     "outstanding_amount": r.outstanding_amount,
                     "allocated_amount": part})
        reste = flt(reste - part, 3)
    src.cancel()
    for r in refs:
        new.append("references", r)
    new.insert(ignore_permissions=True)
    new.submit()
    # Regle utilisateur (2026-08-18) : un paiement annule-remplace ne reste pas en base —
    # meme style que la branche aramex du server script (delete_doc force).
    frappe.delete_doc("Payment Entry", src.name, force=True, ignore_permissions=True)
    return new


# Table du brouillon et champ de total par flux. Le classement suit le COMPTE D'ATTENTE de la
# PE (les trois branches du server script ne se valent pas) — le flux vient de l'ecart, pose
# par le matcher qui a produit la ligne.
_TABLES = {
    "aramex": ("livraison_aramex_a_encaisser", "total_virement_a"),
    "cheque": ("chèques_a_encaisser", "total_des_chèques"),
    "traite": ("traite_bancaire_a_encaissées", "total_des_traites_bancaires_encaissés"),
}


def _table_de(flux: str):
    if flux not in _TABLES:
        frappe.throw(_("Flux inconnu pour cette résolution : {0}.").format(flux))
    return _TABLES[flux]


def _recalc_total(enc, flux: str):
    """Les totaux sont poses par le builder A LA CREATION : toute modification de ligne doit
    recalculer celui du flux, sinon il affiche l'ancien montant (constate en reel sur
    ENC-18-08-2026-00010 : 3 678,600 affiche pour 3 503,600 de lignes)."""
    table, champ = _table_de(flux)
    enc.set(champ, round(sum(flt(r.valeur) or 0 for r in (enc.get(table) or [])), 3))


def _maj_ligne_brouillon(encaissement: str, ancienne_pe: str, nouvelle_pe: str, montant: float,
                         flux: str = "aramex"):
    """Reoriente la ligne du brouillon (table du flux) vers la PE de remplacement."""
    enc = frappe.get_doc(ENCAISSEMENT_DOCTYPE, encaissement)
    if enc.docstatus != 0:
        frappe.throw(_("{0} n'est plus un brouillon.").format(encaissement))
    table, _champ = _table_de(flux)
    for row in enc.get(table) or []:
        if row.ref_paiement == ancienne_pe:
            row.ref_paiement = nouvelle_pe
            row.valeur = flt(montant, 3)
            break
    else:
        frappe.throw(_("Aucune ligne du brouillon {0} ne référence la PE {1}.")
                     .format(encaissement, ancienne_pe))
    _recalc_total(enc, flux)
    enc.save(ignore_permissions=True)


def _ajouter_ligne_brouillon(encaissement: str, pe, suivi: str, reference_bancaire: str,
                             flux: str = "aramex", bon: str = ""):
    enc = frappe.get_doc(ENCAISSEMENT_DOCTYPE, encaissement)
    if enc.docstatus != 0:
        frappe.throw(_("{0} n'est plus un brouillon.").format(encaissement))
    table, _champ = _table_de(flux)
    if flux == "aramex":
        ligne = {
            "ref_paiement": pe.name,
            "type": "Virement",
            "n_virement": reference_bancaire or "",
            "n_aramex": suivi or "",
            "valeur": flt(pe.paid_amount, 3),
            "emmeteur": pe.party_name or pe.party,
            "date": pe.posting_date,
        }
    else:
        # cheques et traites partagent la meme forme de ligne (cf. builder) ; pour une traite,
        # le bon de remise est la reference bancaire du credit.
        ligne = {
            "ref_paiement": pe.name,
            "n_cheque": suivi or "",
            "valeur": flt(pe.paid_amount, 3),
            "emmeteur": pe.party_name or pe.party,
            "bon_remise": (bon or reference_bancaire or suivi or ""),
            "date": pe.posting_date,
            "statut": "Versé",
        }
    enc.append(table, ligne)
    _recalc_total(enc, flux)
    enc.save(ignore_permissions=True)


def _resoudre(e, resolution: str, pieces: list, note: str = ""):
    e.db_set("statut", "Résolu")
    e.db_set("resolution", resolution)
    e.db_set("piece_resolution", ", ".join(pieces)[:140])
    if note:
        e.db_set("note", ((e.note + " | ") if e.note else "") + note)


def _delta_valide(e):
    """Controles communs des resolutions d'un « Delta paiement »."""
    if e.type_ecart != "Delta paiement" or not e.ref_paiement:
        frappe.throw(_("Cette résolution s'applique à un écart « Delta paiement » "
                       "porteur d'une Payment Entry."))
    montant_reel = flt(e.montant_advice, 3)
    delta = flt(e.ecart, 3)                      # piece - advice (> 0 : la PE est trop grosse)
    if montant_reel <= 0 or delta <= 0:
        frappe.throw(_("Écart incohérent : advice {0}, delta {1}.").format(montant_reel, delta))
    return montant_reel, delta


# ------------------------------------------------------------------ les resolutions

@frappe.whitelist()
def resoudre_perte(ecart: str, note: str = None):
    """« Perte de non paiement » — retour d'article defectueux (Baccari) : PE remplacee au
    montant reellement verse ; le delta part en JE Dr Perte de non paiement / Cr Debiteurs
    (client), la ligne de credit REFERENCANT LA COMMANDE du paiement d'origine (meme forme
    que la JE d'application d'avoir du tandem maison)."""
    frappe.only_for(("System Manager", "Accounts Manager"))
    from bank_retenue_sync.expenses import journal

    e = _ecart_a_traiter(ecart)
    montant_reel, delta = _delta_valide(e)
    note = (note or "").strip()

    src = frappe.get_doc("Payment Entry", e.ref_paiement)
    commande = _commande_de(src)
    party = src.party
    new_pe = _remplacer_pe(e.ref_paiement, montant_reel, note=note)
    company, cc = _company_et_cc()

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = new_pe.posting_date
    je.cheque_no = (note or _("Perte de non paiement {0} — suivi Aramex {1}")
                    .format(commande, e.suivi))[:140]
    je.cheque_date = new_pe.posting_date
    je.user_remark = _("Perte de non paiement (retour) — commande {0}, suivi Aramex {1} : "
                       "PE {2} ({3}) remplacée par {4} ({5}), advice {6}. {7}").format(
        commande, e.suivi, e.ref_paiement, flt(e.montant_piece, 3),
        new_pe.name, montant_reel, e.reference_bancaire, note)
    je.append("accounts", {
        "account": PERTE_ACCOUNT,
        "debit_in_account_currency": delta,
        "cost_center": cc,
    })
    ref_doctype = "Sales Order" if frappe.db.exists("Sales Order", commande) else "Sales Invoice"
    je.append("accounts", {
        "account": _receivable(company),
        "party_type": "Customer",
        "party": party,
        "credit_in_account_currency": delta,
        "cost_center": cc,
        "reference_type": ref_doctype,
        "reference_name": commande,
    })
    je.insert(ignore_permissions=True)
    je.submit()
    _maj_ligne_brouillon(e.encaissement, e.ref_paiement, new_pe.name, montant_reel,
                         flux=e.flux or "aramex")
    _resoudre(e, "Perte de non paiement", [new_pe.name, je.name], note=note)
    frappe.db.commit()
    return {"payment_entry": new_pe.name, "journal_entry": je.name}


@frappe.whitelist()
def resoudre_ajustement(ecart: str, pe_source: str = None, note: str = None):
    """« Ajustement livraison-dette ». Deux formes :
      - « Delta paiement » : PE remplacee au montant verse, et le delta REDEVIENT UNE DETTE
        au patron maison (PE mode « Dette non payée », Débiteurs -> Dettes - A&S,
        reference_no = la commande) — le flux dettes l'encaissera plus tard ;
      - « Sans pièce » (type Adem boubaker) : `pe_source` (erreur de saisie) est reclassee sur
        Livraison Aramex avec le suivi de l'advice, puis ajoutee au brouillon."""
    frappe.only_for(("System Manager", "Accounts Manager"))
    e = _ecart_a_traiter(ecart)
    note = (note or "").strip()

    if e.type_ecart == "Delta paiement":
        montant_reel, delta = _delta_valide(e)
        src = frappe.get_doc("Payment Entry", e.ref_paiement)
        commande = _commande_de(src)
        party = src.party
        new_pe = _remplacer_pe(e.ref_paiement, montant_reel, note=note)
        company, _cc = _company_et_cc()
        # patron dette du server script « Traitement des encaissement » (l.785-795)
        dette = frappe.get_doc({
            "doctype": "Payment Entry",
            "payment_type": "Receive",
            "company": company,
            "mode_of_payment": "Dette non payée",
            "party_type": "Customer",
            "party": party,
            "paid_from": _receivable(company),
            "paid_to": COMPTE_DETTES,
            "posting_date": frappe.utils.nowdate(),
            "reference_no": commande,
            "reference_date": frappe.utils.nowdate(),
            "paid_amount": delta,
            "received_amount": delta,
            "remarks": (_("Dette recréée depuis l'écart Aramex {0} (suivi {1}). {2}")
                        .format(e.reference_bancaire, e.suivi, note)).strip(),
            "references": [{
                "reference_doctype": ("Sales Order" if frappe.db.exists("Sales Order", commande)
                                      else "Sales Invoice"),
                "reference_name": commande,
                "allocated_amount": delta,
            }],
        })
        dette.insert(ignore_permissions=True)
        dette.submit()
        _maj_ligne_brouillon(e.encaissement, e.ref_paiement, new_pe.name, montant_reel,
                             flux=e.flux or "aramex")
        _resoudre(e, "Ajustement livraison-dette", [new_pe.name, dette.name], note=note)
        frappe.db.commit()
        return {"payment_entry": new_pe.name, "dette": dette.name}

    if e.type_ecart != "Sans pièce":
        frappe.throw(_("« Ajustement » ne s'applique qu'à un écart « Sans pièce » "
                       "ou « Delta paiement »."))
    if not pe_source:
        frappe.throw(_("Choisissez la Payment Entry à reclasser (erreur de saisie)."))
    src = frappe.get_doc("Payment Entry", pe_source)
    if abs(flt(src.paid_amount, 3) - flt(e.montant_advice, 3)) > 1.0:
        frappe.throw(_("La PE {0} ({1}) ne correspond pas au montant de l'advice ({2}) "
                       "au-delà de la tolérance de 1 DT.")
                     .format(pe_source, src.paid_amount, e.montant_advice))
    new = frappe.new_doc("Payment Entry")
    for f in ("payment_type", "posting_date", "company", "mode_of_payment", "party_type",
              "party", "party_name", "paid_from", "paid_from_account_currency",
              "paid_to_account_currency", "reference_date", "remarks",
              "source_exchange_rate", "target_exchange_rate", "cost_center", "project"):
        new.set(f, src.get(f))
    flux = e.flux or "aramex"
    new.paid_to = _COMPTES_ATTENTE.get(flux, COMPTE_ARAMEX)
    # la reference suit la convention maison du flux, celle que la dedup et l'audit relisent
    # (cf. pending._RX_BON / _RX_REF et l'extraction des numeros).
    if flux == "cheque":
        new.reference_no = "{0} / BR:{1}".format(e.suivi, e.bon or "")
    elif flux == "traite":
        new.reference_no = "{0} / RE:{1}".format(e.suivi, e.reference_bancaire or "")
    else:
        new.reference_no = "Aramex N: {0}".format(e.suivi)
    if note:
        new.remarks = ((new.remarks + "\n") if new.remarks else "") + note
    new.paid_amount = new.received_amount = flt(src.paid_amount, 3)
    refs = [{"reference_doctype": r.reference_doctype, "reference_name": r.reference_name,
             "total_amount": r.total_amount, "outstanding_amount": r.outstanding_amount,
             "allocated_amount": r.allocated_amount} for r in (src.get("references") or [])]
    src.cancel()
    for r in refs:
        new.append("references", r)
    new.insert(ignore_permissions=True)
    new.submit()
    # meme regle : la PE reclassee (annulee) est supprimee, pas laissee en docstatus 2
    frappe.delete_doc("Payment Entry", pe_source, force=True, ignore_permissions=True)
    _ajouter_ligne_brouillon(e.encaissement, new, e.suivi, e.reference_bancaire,
                             flux=flux, bon=e.bon or "")
    _resoudre(e, "Ajustement livraison-dette", [new.name],
              note=(_("reclassement de {0}").format(pe_source) + (" — " + note if note else "")))
    frappe.db.commit()
    return {"payment_entry": new.name}


@frappe.whitelist()
def resoudre_avoir(ecart: str, note: str = None):
    """« Paiement par avoir » — uniquement sur un « Delta paiement » dont le client possede
    encore un avoir flottant suffisant. PE remplacee au montant verse, puis une ligne
    d'echeancier mode « Avoir client » du delta est ajoutee a la commande : c'est le tandem
    maison « re-generate payment after sales order » qui cree la JE d'application (fabriquer
    la JE ici la laisserait sans custom_source_row_uid, orpheline pour la regeneration)."""
    frappe.only_for(("System Manager", "Accounts Manager"))
    e = _ecart_a_traiter(ecart)
    montant_reel, delta = _delta_valide(e)
    note = (note or "").strip()

    src = frappe.get_doc("Payment Entry", e.ref_paiement)
    commande = _commande_de(src)
    party = src.party
    dispo = avoir_flottant(party)
    if dispo < delta:
        frappe.throw(_("Le client {0} n'a pas d'avoir flottant suffisant : disponible {1}, "
                       "requis {2}. Créez d'abord l'avoir (bouton « Avoir » de la commande) "
                       "ou choisissez une autre résolution.").format(party, dispo, delta))
    if not frappe.db.exists("Sales Order", commande):
        frappe.throw(_("« Paiement par avoir » exige une commande (Sales Order) ; {0} n'en "
                       "est pas une.").format(commande))

    flux = e.flux or "aramex"
    new_pe = _remplacer_pe(e.ref_paiement, montant_reel, note=note)
    so = frappe.get_doc("Sales Order", commande)
    so.append("payment_schedule", {
        "due_date": frappe.utils.nowdate(),
        "mode_of_payment": "Avoir client",
        "payment_amount": delta,
        "custom__n_chèque__transaction": e.suivi,
        "description": (note or _("Avoir applique via écart Aramex {0}")
                        .format(e.reference_bancaire))[:140],
    })
    # le save d'une commande soumise declenche le tandem (After Save Submitted Document),
    # qui cree la JE d'application de l'avoir appariee a cette ligne.
    so.flags.ignore_validate_update_after_submit = True
    so.save(ignore_permissions=True)
    _maj_ligne_brouillon(e.encaissement, e.ref_paiement, new_pe.name, montant_reel, flux=flux)
    _resoudre(e, "Paiement par avoir", [new_pe.name, commande], note=note)
    frappe.db.commit()
    return {"payment_entry": new_pe.name, "commande": commande}


@frappe.whitelist()
def resoudre_ignorer(ecart: str, note: str = None):
    """« Ignoré » — reserve aux « Sans pièce » de montant <= 1 DT (cas Sahbi Chouchane : 1 DT
    dont l'avoir est deja consomme). Aucune piece n'est creee ; le montant sera constate par
    l'ecart de paiement mensuel."""
    frappe.only_for(("System Manager", "Accounts Manager"))
    e = _ecart_a_traiter(ecart)
    if e.type_ecart != "Sans pièce" or flt(e.montant_advice, 3) > SEUIL_IGNORABLE:
        frappe.throw(_("« Ignoré » est réservé aux écarts « Sans pièce » de {0} DT ou moins.")
                     .format(SEUIL_IGNORABLE))
    _resoudre(e, "Ignoré", [], note=(note or "").strip())
    frappe.db.commit()
    return {"ignore": e.name}
