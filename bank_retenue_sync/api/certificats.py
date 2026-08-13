"""API du suivi des certificats de retenue a la source, pour l'onglet « Certificats ».

Toute la logique vit ici ; la page ne fait que du rendu. C'est la meme separation que pour
« Identification bancaire » : un ecran qui calculerait ses propres regles finirait par diverger de
la tache planifiee qui, elle, fait foi.
"""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import flt, getdate

DOCTYPE = "Retenue Certificate"

ROLES = ("System Manager", "Accounts Manager", "Accounts User")

CHAMPS = ["name", "reference", "date_paiement", "declarant", "declarant_matricule", "customer",
          "total_ht", "total_tva", "total_brut", "montant_retenue", "taux", "etat_depot",
          "etat_source", "match_status", "match_method", "match_score", "match_raison",
          "match_candidates", "payment_entry", "sales_invoice", "sales_order", "ecart_piece",
          "revue_requise", "anomalie", "anomalie_raison", "hors_perimetre", "pdf_file",
          "pdf_attached_to_pe", "pdf_attache_a", "numero_chez_declarant"]


def _lecture():
    frappe.only_for(list(ROLES))


@frappe.whitelist()
def get_data(from_date=None, to_date=None, statut=None, inclure_hors_perimetre=0):
    """Certificats + synthese. Lecture seule.

    Les tuiles portent sur TOUT le filtre, jamais sur la page affichee : un compteur qui ne
    compterait que ce qu'on voit ne servirait a rien pour decider quoi traiter.
    """
    _lecture()
    filtres = {}
    if not frappe.utils.cint(inclure_hors_perimetre):
        filtres["hors_perimetre"] = 0
    if from_date:
        filtres["date_paiement"] = [">=", getdate(from_date)]
    if to_date:
        filtres["date_paiement"] = ([("between", [getdate(from_date), getdate(to_date)])][0]
                                    if from_date else ["<=", getdate(to_date)])
    if statut:
        filtres["match_status"] = statut

    lignes = frappe.db.get_all(DOCTYPE, filters=filtres, fields=CHAMPS,
                               order_by="date_paiement desc", limit_page_length=0)
    for l in lignes:
        l["candidats"] = json.loads(l.get("match_candidates") or "[]") if l.get(
            "match_candidates") else []
    return {"certificats": lignes, "kpis": _kpis(lignes), "etat": etat_synchronisation()}


def _kpis(lignes: list) -> dict:
    def somme(seq):
        return round(sum(flt(l["montant_retenue"], 3) for l in seq), 3)

    # ⚠️ « RAPPROCHE » EXIGE UNE ECRITURE, PAS UN STATUT. Poser la facture a la main fait passer le
    # certificat en « Manually Matched » alors que RIEN n'est encore comptabilise : compte comme
    # rapproche, il affichait une pastille verte sur un travail non fait, et le compteur « sans
    # ecriture » — celui qui appelle a l'action — l'oubliait.
    rapproche = [l for l in lignes
                 if l["match_status"] in ("Auto Matched", "Manually Matched")
                 and l.get("payment_entry")]
    auto = rapproche
    sans_piece = [l for l in lignes
                  if l["match_status"] == "Sans piece"
                  or (l["match_status"] in ("Auto Matched", "Manually Matched")
                      and not l.get("payment_entry"))]
    ambigus = [l for l in lignes if l["match_status"] == "Ambiguous"]
    inconnus = [l for l in lignes if l["match_status"] == "Unmatched"]
    ecarts = [l for l in lignes if flt(l["ecart_piece"], 3)]
    return {
        "total": len(lignes), "retenue_totale": somme(lignes),
        "rapproches": len(auto), "retenue_rapprochee": somme(auto),
        "sans_piece": len(sans_piece), "retenue_sans_piece": somme(sans_piece),
        "ambigus": len(ambigus), "non_identifies": len(inconnus),
        "ecarts": len(ecarts), "montant_ecarts": round(sum(flt(l["ecart_piece"], 3)
                                                           for l in ecarts), 3),
        "pdf": len([l for l in lignes if l["pdf_attached_to_pe"]]),
        "anomalies": len([l for l in lignes if l["anomalie"]]),
        "revue": len([l for l in lignes if l["revue_requise"]]),
        # « A jour » = plus rien a trancher NI a justifier. Deux conditions, comme partout dans
        # cette app : un tableau vert alors qu'il manque des justificatifs serait un mensonge.
        "a_jour": not (sans_piece or ambigus or inconnus)
                  and len(auto) == len([l for l in auto if l["pdf_attached_to_pe"]]),
    }


@frappe.whitelist()
def etat_synchronisation():
    """Fraicheur de l'export : un tableau lu sur un export d'il y a trois semaines induit en
    erreur, meme quand il est parfaitement rapproche."""
    _lecture()
    run = frappe.db.get_all("BRS Sync Run", filters={"kind": "tej_certificats_recus"},
                            fields=["name", "status", "started_at", "rows_received",
                                    "rows_created"],
                            order_by="creation desc", limit_page_length=1)
    return {"dernier_run": run[0] if run else None,
            "last_tej_sync": frappe.db.get_single_value("Bank Retenue Sync Settings",
                                                        "last_tej_sync"),
            "actif": bool(frappe.db.get_single_value("Bank Retenue Sync Settings",
                                                     "enable_tej_sync"))}


@frappe.whitelist()
def synchroniser(refresh=0, insert=0, pdf=0):
    """Lance la synchronisation. `insert=0` = essai a blanc, rien n'est ecrit."""
    frappe.only_for("System Manager")
    from bank_retenue_sync import orchestrator

    return orchestrator.run_certificats_ras(refresh=refresh, insert=insert, pdf=pdf)


def _demander_pdf(reference, decision=None) -> str:
    """Reclame le certificat PDF des qu'une piece est designee. -> statut, jamais d'exception.

    Un justificatif indisponible ne remet pas en cause le rapprochement qui vient d'etre fait : il
    ne doit donc jamais faire echouer l'appel qui l'a produit.
    """
    if decision is not None and not (decision.get("payment_entry")
                                     or decision.get("sales_invoice")):
        return "sans piece"
    try:
        from bank_retenue_sync.tej import pdf

        return pdf.demander(reference).get("statut")
    except Exception:
        return "erreur"


@frappe.whitelist()
def trancher(reference, customer):
    """Pose le client a la main. C'EST L'ALIAS DE DEMAIN.

    Le certificat passe en « Manually Matched » : la machine ne repassera plus dessus, et le
    prochain certificat du meme declarant sera rapproche sans fuzzy ni IA.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    if not frappe.db.exists("Customer", customer):
        frappe.throw(_("Client inconnu : {0}").format(customer))
    doc = frappe.get_doc(DOCTYPE, reference)
    doc.customer = customer
    doc.save(ignore_permissions=True)          # `on_update` pose le statut et la methode
    from bank_retenue_sync.tej import rapprochement

    # Rejouer aussitot pour lui trouver sa piece : l'utilisateur vient de donner l'information qui
    # manquait, il doit en voir l'effet tout de suite.
    cert = frappe.db.get_all(DOCTYPE, filters={"name": reference},
                             fields=list(rapprochement.CHAMPS_CERTIFICAT))[0]
    ctx = rapprochement.charger_contexte()
    decision = rapprochement.rapparier_manuel(cert, ctx)
    frappe.db.commit()
    # Le certificat vient de trouver sa piece : son justificatif se range dans la foulee.
    pdf_statut = _demander_pdf(reference, decision)
    return {"statut": "tranche", "customer": customer, "pdf": pdf_statut, **decision}


@frappe.whitelist()
def poser_facture(reference, sales_invoice):
    """Pose la facture a la main quand aucune regle ne la retrouve.

    Trois certificats 2026 sont dans ce cas : leur assiette ne correspond a aucune facture (facture
    partielle, avoir, regroupement) et aucun reglement net ne les designe. Sans ce geste, ils
    restent bloques a « sans ecriture » pour toujours — la retenue est declaree, elle existe, mais
    l'outil ne sait pas quelle creance elle eteint.

    ⚠️ LE CERTIFICAT PASSE EN « Manually Matched », comme apres `trancher` : sinon la prochaine
    passe de rapprochement, qui recalcule `sales_invoice`, effacerait le choix de l'utilisateur.
    La facture doit appartenir au client du certificat — c'est le seul controle, mais il est le bon.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    from bank_retenue_sync.tej import rapprochement

    doc = frappe.get_doc(DOCTYPE, reference)
    if not doc.customer:
        frappe.throw(_("Identifiez d'abord le client de ce certificat."))
    facture = frappe.db.get_value("Sales Invoice", sales_invoice,
                                  ["customer", "docstatus", "grand_total", "outstanding_amount"],
                                  as_dict=1)
    if not facture:
        frappe.throw(_("Facture inconnue : {0}").format(sales_invoice))
    if facture.docstatus != 1:
        frappe.throw(_("La facture {0} n'est pas validée.").format(sales_invoice))
    if facture.customer != doc.customer:
        frappe.throw(_("La facture {0} appartient à {1}, pas à {2}.").format(
            sales_invoice, facture.customer, doc.customer))

    doc.sales_invoice = sales_invoice
    doc.match_status = "Manually Matched"
    doc.match_raison = _("Facture posée par {0}").format(frappe.session.user)
    doc.save(ignore_permissions=True)

    # Meme reflexe qu'apres `trancher` : l'utilisateur vient de donner l'information qui manquait,
    # il doit voir tout de suite si une ecriture de retenue est deja imputee a cette facture.
    ctx = rapprochement.charger_contexte()
    cert = frappe.db.get_all(DOCTYPE, filters={"name": reference},
                             fields=list(rapprochement.CHAMPS_CERTIFICAT) + ["sales_invoice"])[0]
    piece = rapprochement.apparier_par_facture(cert, ctx, sales_invoice)
    if piece.get("payment_entry"):
        frappe.db.set_value(DOCTYPE, reference,
                            {"payment_entry": piece["payment_entry"],
                             "ecart_piece": flt(piece.get("ecart") or 0, 3),
                             "match_raison": "%s ; %s" % (doc.match_raison, piece["raison"])},
                            update_modified=False)
    frappe.db.commit()
    # La facture est connue : c'est elle qui recevra le certificat PDF (cf. `tej/pdf.cible`).
    return {"statut": "facture posee", "sales_invoice": sales_invoice,
            "payment_entry": piece.get("payment_entry"),
            "reste_du": flt(facture.outstanding_amount, 3), "ttc": flt(facture.grand_total, 3),
            "raison": piece.get("raison"), "pdf": _demander_pdf(reference)}


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def factures_client(doctype, txt, searchfield, start, page_len, filters):
    """Les factures du client, LA PLUS RECENTE D'ABORD, avec leur montant et leur reste du.

    ⚠️ POURQUOI NE PAS LAISSER LE CHAMP LINK STANDARD FAIRE. Il trie par NOM et n'affiche que dix
    lignes : sur FM WATER PLUS (40 factures), « ACC-SINV-2025-… » precede « ACC-SINV-2026-… » dans
    l'alphabet, si bien que la liste ne contenait QUE des factures de 2025 et que celles de l'annee
    en cours semblaient absentes. Un tri par date remet l'annee en cours en tete.

    Le libelle porte la date, le TTC et le reste du : c'est sur ces trois nombres que l'utilisateur
    reconnait la facture d'un certificat, jamais sur son nom. La recherche accepte donc aussi un
    MONTANT — « 3862 » retrouve la facture sans qu'on ait a connaitre son numero.

    ⚠️ LIMITE AU PERIMETRE (`ANNEE_MINIMALE`, 2026) : une retenue du portail ne se loge pas dans une
    facture d'un exercice qu'on ne suit pas. Demande explicitement apres avoir vu remonter des
    factures 2025. Si un jour une facture anterieure doit etre posee, ce filtre est le seul a lever.

    ⚠️ LES FACTURES DEJA PRISES PAR UN AUTRE CERTIFICAT SONT MASQUEES. Deux certificats sur une meme
    facture, c'est le chemin le plus court vers un credit d'impot compte deux fois — et l'erreur est
    invisible une fois faite. Le filtre l'empeche a la saisie, la ou elle se voit encore.
    Les certificats ecartes ne retiennent rien : leur facture reste proposable.
    """
    from bank_retenue_sync.tej import certificats as C

    customer = (filters or {}).get("customer")
    if not customer:
        return []
    return frappe.db.sql("""select si.name,
                                   concat(si.posting_date, '  ·  ', round(si.grand_total, 3),
                                          ' TND  ·  reste ', round(si.outstanding_amount, 3))
                            from `tabSales Invoice` si
                            where si.docstatus = 1 and si.customer = %(customer)s
                              and si.posting_date >= %(depuis)s
                              and not exists (select 1 from `tabRetenue Certificate` rc
                                              where rc.sales_invoice = si.name
                                                and rc.name != %(courant)s
                                                and ifnull(rc.match_status, '')
                                                    not in ('Ignore', 'Rejected'))
                              and (si.name like %(txt)s
                                   or cast(si.grand_total as char) like %(txt)s
                                   or cast(si.posting_date as char) like %(txt)s)
                            order by si.posting_date desc
                            limit %(start)s, %(page_len)s""",
                         {"customer": customer, "txt": "%%%s%%" % (txt or ""),
                          "depuis": "%s-01-01" % C.ANNEE_MINIMALE,
                          "courant": (filters or {}).get("certificat") or "",
                          "start": frappe.utils.cint(start),
                          "page_len": frappe.utils.cint(page_len) or 20})


@frappe.whitelist()
def ignorer(reference, motif):
    """Ecarte definitivement un certificat. Motif obligatoire : un rejet sans raison ne se relit
    pas six mois plus tard."""
    frappe.only_for(["System Manager", "Accounts Manager"])
    if not (motif or "").strip():
        frappe.throw(_("Le motif est obligatoire."))
    frappe.db.set_value(DOCTYPE, reference, {
        "match_status": "Ignore", "revue_requise": 0,
        "match_raison": _("Ecarte par {0} : {1}").format(frappe.session.user, motif)})
    frappe.db.commit()
    return {"statut": "ignore"}


@frappe.whitelist()
def creer_paiements(references=None, insert=0):
    """Cree les ecritures manquantes (brouillons). `insert=0` = essai a blanc."""
    frappe.only_for("System Manager")
    from bank_retenue_sync.tej import paiements

    if isinstance(references, str):
        references = json.loads(references) if references.strip().startswith("[") else [references]
    res = paiements.creer(references=references, insert=frappe.utils.cint(insert))
    if frappe.utils.cint(insert):
        frappe.db.commit()
    return res


@frappe.whitelist()
def plan_regularisation(reference):
    """Comment loger cette retenue ? -> {voie: creation|ajustement|impossible, ...}.

    Deux situations, deux gestes. La facture a encore du reste a payer : une simple ecriture de
    retenue suffit. La facture est deja soldee : c'est que le reglement a ete encaisse pour le TTC
    entier alors que le client retenait 1 % — il faut alors le reprendre a la baisse ET creer la
    retenue, sans quoi la facture serait payee deux fois pour la meme part.
    """
    _lecture()
    from bank_retenue_sync.tej import paiements, rapprochement

    cert = frappe.db.get_all(DOCTYPE, filters={"name": reference},
                             fields=list(rapprochement.CHAMPS_CERTIFICAT)
                             + ["sales_invoice", "total_brut"])
    if not cert:
        return {"voie": "impossible", "raison": _("Certificat inconnu.")}
    # Un seul contexte pour les deux questions : les charger separement doublait, a chaque clic,
    # l'inventaire complet des clients, des ecritures et des factures.
    ctx = rapprochement.charger_contexte()
    verdict = paiements.verifier(cert[0], ctx=ctx)
    if verdict["ok"]:
        return {"voie": "creation", "certificat": cert[0], "montant": cert[0]["montant_retenue"],
                "facture": cert[0]["sales_invoice"]}
    if "deja soldee" in (verdict["raison"] or ""):
        plan = paiements.ajuster(reference, insert=0, ctx=ctx)
        if plan.get("ok"):
            return {"voie": "ajustement", "plan": plan, "raison": verdict["raison"]}
        # Plusieurs reglements peuvent porter la retenue et aucune regle ne les departage : ce
        # n'est pas un echec, c'est une question. La liste part a l'ecran, l'utilisateur tranche.
        if plan.get("candidats"):
            return {"voie": "choix", "raison": plan.get("raison"),
                    "candidats": plan["candidats"], "certificat": cert[0],
                    "montant": flt(cert[0]["montant_retenue"], 3),
                    "facture": cert[0]["sales_invoice"]}
        return {"voie": "impossible", "raison": plan.get("raison") or verdict["raison"]}
    return {"voie": "impossible", "raison": verdict["raison"]}


@frappe.whitelist()
def ajuster(reference, insert=0, reglement=None):
    """Reprend le reglement a la baisse et cree la retenue. Brouillons, sauf reglage contraire.

    `reglement` : le choix de l'utilisateur quand la facture en compte plusieurs (voie « choix »).
    """
    frappe.only_for("System Manager")
    from bank_retenue_sync.tej import paiements

    res = paiements.ajuster(reference, insert=frappe.utils.cint(insert),
                            reglement=reglement or None)
    if frappe.utils.cint(insert):
        frappe.db.commit()
    return res


@frappe.whitelist()
def get_retenues_orphelines(from_date=None, to_date=None):
    """L'AUTRE SENS : les retenues comptabilisees qu'aucun certificat ne justifie.

    Le tableau des certificats dit ce que les clients ont declare ; celui-ci dit ce que nous avons
    deduit. Un credit d'impot sans certificat n'est pas opposable au fisc — c'est la seule liste
    qui le montre.
    """
    _lecture()
    from bank_retenue_sync.tej import orphelines

    return orphelines.inventaire(depuis=from_date or None, jusqu_a=to_date or None)


@frappe.whitelist()
def doublons_justificatifs(insert=0, garder="portail"):
    """Les pieces portant a la fois le PDF du portail et un certificat depose a la main.

    Chaque couple est confronte par son TEXTE : rien n'est supprime tant que les deux documents ne
    se sont pas reveles identiques. `insert=0` ne fait que montrer.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    from bank_retenue_sync.tej import pdf

    res = pdf.doublons(insert=frappe.utils.cint(insert), garder=garder)
    if frappe.utils.cint(insert):
        frappe.db.commit()
    return res


@frappe.whitelist()
def telecharger_pdf(limite=10, insert=1):
    """Telecharge et range les PDF manquants."""
    frappe.only_for("System Manager")
    from bank_retenue_sync.tej import pdf

    res = pdf.traiter(limite=frappe.utils.cint(limite), insert=frappe.utils.cint(insert))
    if frappe.utils.cint(insert):
        frappe.db.commit()
    return res
