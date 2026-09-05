"""La file des certificats de retenue à émettre pour les DÉPENSES DE CAISSE facturées.

POURQUOI UNE FILE, ET PAS UNE ÉMISSION AUTOMATIQUE
---------------------------------------------------
⚠️ CE QUI PART SUR TEJ EST DÉCLARATIF ET IRRÉVERSIBLE. Un certificat soumis se lit chez le
fournisseur et chez l'administration ; l'annuler laisse une trace. La retenue, elle, se pose à la
saisie — on ne va pas arrêter la caisse pour une fiche fournisseur incomplète. Les deux gestes
sont donc séparés : la caisse retient, la file déclare, et personne ne déclare sans le vouloir
(décision utilisateur 04/09/2026).

CE QUI BLOQUE UNE ÉMISSION, ET POURQUOI ON L'ACCEPTE QUAND MÊME
---------------------------------------------------------------
Le portail exige un MATRICULE FISCAL. Or une dépense de caisse ne porte qu'un nom de fournisseur
en texte libre — « MS TECHAUTOMATION sarl » n'est rattaché à aucune fiche. La retenue est posée
malgré tout et la ligne entre ici à l'état « Incomplet », avec ce qui lui manque écrit noir sur
blanc. Refuser la retenue aurait fait perdre 1 % au Trésor ; l'émettre sans matricule est
impossible. Il reste la file.

LE PÉRIMÈTRE
------------
⚠️ RIEN AVANT LE 01/09/2026 (décision utilisateur). Les écritures antérieures n'ont pas été
saisies sous cette règle : les rattraper produirait des déclarations que personne n'a préparées.
Et les écritures créées AUTOMATIQUEMENT — Aramex, Total — sont hors sujet : elles n'entrent pas
par la caisse et leurs fournisseurs ne sont pas locaux.
"""
from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import flt, getdate

from bank_retenue_sync.achat import retenue_depense as R
from bank_retenue_sync.tej import depot as M_depot

DOCTYPE = "BRS Retenue Achat A Emettre"
DEPUIS = "2026-09-01"

#: Ce que la remarque de la dépense écrit — c'est la seule mémoire de la retenue sur l'écriture.
_RETENUE = re.compile(r"Retenue à la source achat\s*:\s*([\d\s.,]+)\s*sur\s*([\d\s.,]+)")
_FOURNISSEUR = re.compile(r"Fournisseur\s*:\s*(.+)")
_FACTURE = re.compile(r"Facture n°\s*(\S+)")

#: Les écritures créées par les flux automatiques : jamais de retenue à déclarer dessus.
PREFIXES_EXCLUS = ("Facture Total", "Facture Aramex", "Frais bancaire", "Dépense à payer —")


def _nombre(txt):
    return flt((txt or "").replace(" ", "").replace(" ", "").replace(",", "."), 3)


def lire_piece(remarque) -> dict:
    """Ce que la remarque d'une dépense dit de sa retenue. Fonction pure.

    La remarque est la seule mémoire : pas de champ dédié sur l'écriture, donc pas de champ à
    migrer et pas de risque que le justificatif et sa trace divergent — même convention que le
    « Chq N° » que l'identification bancaire lit déjà.
    """
    texte = remarque or ""
    m = _RETENUE.search(texte)
    f = _FOURNISSEUR.search(texte)
    n = _FACTURE.search(texte)
    return {
        "retenue": _nombre(m.group(1)) if m else 0.0,
        "ttc": _nombre(m.group(2)) if m else 0.0,
        "fournisseur": (f.group(1).strip().split("\n")[0] if f else ""),
        "numero_facture": (n.group(1).strip() if n else ""),
    }


def exclue(cheque_no) -> bool:
    """Une écriture des flux automatiques n'a rien à déclarer."""
    libelle = cheque_no or ""
    return any(libelle.startswith(p) for p in PREFIXES_EXCLUS)


def candidates(depuis=None) -> list:
    """Les écritures qui portent une retenue d'achat et devraient avoir un certificat."""
    depuis = depuis or DEPUIS
    lignes = frappe.db.sql(
        """SELECT je.name, je.posting_date, je.cheque_no, je.user_remark, je.total_debit,
                  SUM(jea.credit) AS retenue
           FROM `tabJournal Entry Account` jea
           INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
           WHERE je.docstatus = 1 AND je.posting_date >= %s
             AND jea.account = %s AND jea.credit > 0
           GROUP BY je.name""", (getdate(depuis), R.compte()), as_dict=True)
    return [l for l in lignes if not exclue(l.cheque_no)]


def rapatrier_certificats() -> int:
    """Reporte sur la file les certificats que le cron des depots a fait generer. -> nb.

    ⚠️ LA FILE NE SAIT RIEN DU CRON, ET LE CRON NE SAIT RIEN DE LA FILE. `tej/emis.verifier_depots`
    tourne cinq fois par jour, interroge le portail et conclut les depots — mais il travaille sur
    `BRS Depot TEJ`, pas ici. Sans ce report, la ligne resterait « À émettre » alors que le
    certificat existe, et quelqu'un finirait par le redemander : deux fois la meme declaration.
    """
    if not frappe.db.exists("DocType", M_depot.DOCTYPE):
        return 0
    faits = 0
    for d in frappe.get_all(M_depot.DOCTYPE,
                            filters={"piece_type": "Journal Entry",
                                     "statut": M_depot.GENERE},
                            fields=["facture", "reference", "modified"]):
        if not d.reference or not frappe.db.exists(DOCTYPE, d.facture):
            continue
        ligne = frappe.get_doc(DOCTYPE, d.facture)
        if ligne.statut == "Émis" and ligne.certificat == d.reference:
            continue
        ligne.statut = "Émis"
        ligne.certificat = d.reference
        ligne.emis_le = ligne.emis_le or d.modified
        ligne.note = ""
        ligne.flags.ignore_permissions = True
        ligne.save()
        faits += 1
    if faits:
        frappe.db.commit()
    return faits


def synchroniser(depuis=None) -> dict:
    """Remplit la file depuis les écritures. Idempotent : une ligne par écriture, jamais deux.

    Ne touche JAMAIS une ligne déjà émise : la référence du certificat est la preuve d'une
    déclaration partie, et rien ici ne doit pouvoir l'effacer.
    """
    # Ce que le cron a conclu depuis le dernier passage arrive AVANT le reste : une ligne deja
    # certifiee ne doit pas etre reproposee a l'emission le temps d'une boucle.
    rapatries = rapatrier_certificats()
    crees, revus = 0, 0
    for c in candidates(depuis):
        lu = lire_piece(c.user_remark)
        supplier, matricule, manque = _fournisseur(lu["fournisseur"])
        if frappe.db.exists(DOCTYPE, c.name):
            doc = frappe.get_doc(DOCTYPE, c.name)
            if doc.statut == "Émis":
                continue
            revus += 1
        else:
            doc = frappe.new_doc(DOCTYPE)
            doc.journal_entry = c.name
            crees += 1
        doc.date_piece = c.posting_date
        # ⚠️ LES ÉCRITURES D'AVANT CETTE RÈGLE N'ONT PAS LE TTC DANS LEUR REMARQUE. La retenue,
        # elle, se lit toujours sur la ligne comptable. Le total débit de l'écriture EST le TTC
        # pour une dépense de caisse (charge + TVA) : il sert de repli plutôt que d'afficher un
        # zéro qui ferait douter du reste (cas ACC-JV-2026-00698, saisie à la main le 04/09).
        doc.montant_ttc = lu["ttc"] or flt(c.total_debit, 3)
        doc.retenue = flt(c.retenue, 3)
        doc.fournisseur_lu = lu["fournisseur"]
        doc.numero_facture = doc.numero_facture or lu["numero_facture"]
        if not doc.supplier:
            doc.supplier = supplier
        doc.matricule = _matricule(doc.supplier)
        manques = _manques(doc)
        doc.note = " · ".join(manques)
        if doc.statut != "Ignoré":
            doc.statut = "Incomplet" if manques else "À émettre"
        doc.flags.ignore_permissions = True
        doc.save()
    frappe.db.commit()
    return {"crees": crees, "revus": revus, "certificats_rapatries": rapatries}


def _fournisseur(nom):
    """(supplier, matricule, manque) pour un nom lu sur la pièce."""
    if not (nom or "").strip():
        return None, "", _("aucun fournisseur nommé sur la pièce")
    exact = frappe.db.get_value("Supplier", {"supplier_name": nom.strip()}, "name")
    return exact, _matricule(exact), ""


def _matricule(supplier):
    return (frappe.db.get_value("Supplier", supplier, "tax_id") or "") if supplier else ""


def _reetat(doc):
    """Remet matricule, manques et statut d'accord avec la réalité. -> la liste des manques."""
    doc.matricule = _matricule(doc.supplier)
    manques = _manques(doc)
    note = " · ".join(manques)
    statut = doc.statut if doc.statut in ("Émis", "Ignoré") else (
        "Incomplet" if manques else "À émettre")
    if (doc.matricule or "") != (doc.get_db_value("matricule") or "") \
            or note != (doc.note or "") or statut != doc.statut:
        doc.note, doc.statut = note, statut
        doc.flags.ignore_permissions = True
        doc.save()
        frappe.db.commit()
    return manques


def _manques(doc) -> list:
    """Ce qui empêche encore d'émettre. C'est cette liste que l'écran affiche."""
    manques = []
    if not doc.supplier:
        manques.append(_("fournisseur non rattaché (« {0} »)")
                       .format(doc.fournisseur_lu or "?"))
    elif not (doc.matricule or "").strip():
        manques.append(_("le fournisseur {0} n'a pas de matricule fiscal").format(doc.supplier))
    if not (doc.numero_facture or "").strip():
        manques.append(_("n° de facture fournisseur manquant"))
    if flt(doc.retenue) <= 0:
        manques.append(_("aucune retenue sur cette pièce"))
    return manques


# ------------------------------------------------------------------ l'emission


def contexte(ligne: str) -> dict:
    """Le contexte d'emission d'une ligne de la file, dans la forme que `tej.emis` attend.

    ⚠️ C'EST UN ADAPTATEUR, PAS UNE SECONDE IMPLEMENTATION. `tej/emis.py` sait deja tout faire —
    repetition a blanc, controle du montant calcule par le portail, cle d'idempotence, PDF
    attache. Il ne sait pas lire une ecriture de journal : on lui fournit donc les memes cles
    depuis une autre source. Dupliquer l'emission aurait fait diverger les deux chemins au
    premier changement du portail.

    ⚠️ LE HT SE DEDUIT, IL NE SE LIT PAS. Une ecriture de caisse ne porte pas de « net_total » :
    elle porte le TTC et la ligne de TVA. Le HT est leur difference, et le taux se retrouve a
    partir des deux — ce que TEJ exige, un taux unique par operation.
    """
    from bank_retenue_sync.tej import matricule as M

    doc = frappe.get_doc(DOCTYPE, ligne)
    je = frappe.get_doc("Journal Entry", doc.journal_entry)
    ht, taux = _ht_et_taux(je, flt(doc.montant_ttc), flt(doc.retenue))
    mat = M.normaliser(doc.matricule or "")

    manques = _manques(doc)
    if doc.statut == "Émis":
        manques.append(_("un certificat a déjà été émis pour cette pièce"))
    if not mat:
        manques.append(_("le matricule fiscal {0} n'est pas exploitable")
                       .format(doc.matricule or "?"))
    if taux is None:
        manques.append(_("le taux de TVA n'est pas déterminable sur cette écriture : TEJ ne "
                         "prend qu'un taux par opération"))

    return {
        # `facture` est le nom que `tej.emis` donne a la piece d'origine : ici c'est l'ecriture.
        "facture": doc.journal_entry,
        "fournisseur": doc.supplier,
        "fournisseur_nom": frappe.db.get_value("Supplier", doc.supplier, "supplier_name")
                           if doc.supplier else "",
        "matricule": mat,
        "matricule_saisi": doc.matricule or "",
        "bill_no": doc.numero_facture or "",
        "date_paiement": str(doc.date_piece or ""),
        "montant_ht": ht,
        "taux_tva": taux,
        "retenue_facture": flt(doc.retenue, 3),
        # La nature de la piece : c'est elle qui permet au depot de pointer une ECRITURE.
        "piece_type": "Journal Entry",
        "exercice": getdate(doc.date_piece).year if doc.date_piece else None,
        "deja_emis": doc.certificat or None,
        "manques": manques,
    }


#: Les comptes de TVA deductible de la caisse. Le taux se lit dans leur nom, pas dans un champ.
_TVA = re.compile(r"TVA\s*(\d+)\s*%", re.IGNORECASE)


def _ht_et_taux(je, ttc, retenue):
    """(HT, taux de TVA) d'une ecriture de depense. -> (float, int|None).

    Le TTC est celui de la piece — retenue comprise, puisqu'elle en est deduite et non ajoutee.
    """
    tva, taux = 0.0, None
    for a in je.accounts:
        m = _TVA.search(a.account or "")
        if m and flt(a.debit) > 0:
            tva += flt(a.debit)
            t = int(m.group(1))
            # Deux taux differents sur la meme piece : TEJ n'en prend qu'un, on rend None.
            taux = t if taux in (None, t) else -1
    if taux == -1:
        return flt(ttc - tva, 3), None
    return flt(ttc - tva, 3), taux


@frappe.whitelist()
def emettre(ligne: str, dry_run: bool = True) -> dict:
    """Repete (synchrone) ou LANCE la soumission en tache de fond. -> dict.

    ⚠️ LA SOUMISSION NE SE FAIT PAS DANS LA REQUETE DESK, ET C'EST LE POINT LE PLUS IMPORTANT
    DE CETTE FONCTION. La creation pilote un NAVIGATEUR sur le portail — regeneration de
    l'export, puis remplissage du formulaire — sur un service a worker unique. Attendre dans la
    requete, c'est bloquer l'ecran plusieurs minutes puis, le plus souvent, se faire couper par
    le proxy AVANT toute reponse : la declaration part, et personne ne le sait. C'est exactement
    ce que faisait cette fonction jusqu'au 04/09/2026, et l'utilisateur l'a vu tourner dans le
    vide sur ACC-JV-2026-00698.

    `tej/emis.soumettre` reglait deja le probleme pour les factures d'achat ; on applique ici la
    meme mecanique — reservation d'un `BRS Depot TEJ`, mise en file, et suivi par `suivre()`.

    La REPETITION reste synchrone : elle ne declare rien, et son resultat n'a d'interet que
    tout de suite, sous les yeux de celui qui compare les montants.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    from bank_retenue_sync.tej import emis as E

    doc = frappe.get_doc(DOCTYPE, ligne)
    ctx = contexte(ligne)
    if ctx["manques"]:
        return {"statut": "impossible", **ctx}

    if frappe.utils.cint(dry_run):
        return E.emettre(doc.journal_entry, dry_run=True, ctx=ctx)

    # Les controles locaux restent synchrones : ils sont instantanes, et un refus doit se voir
    # tout de suite plutot que d'arriver par une notification trois minutes plus tard.
    en_cours = M_depot.en_cours(doc.journal_entry, piece_type="Journal Entry")
    if en_cours:
        return {"statut": "depot en analyse", "depot": M_depot.vue(en_cours), **ctx}

    nom = M_depot.reserver(ctx)
    frappe.db.commit()
    frappe.enqueue("bank_retenue_sync.tej.emis_journal.executer_soumission",
                   queue="long", timeout=2400, ligne=ligne, depot_nom=nom)
    return {"statut": "en file",
            "depot": M_depot.vue(frappe.get_doc(M_depot.DOCTYPE, nom)), **ctx}


def executer_soumission(ligne: str, depot_nom: str = None) -> dict:
    """La soumission reelle, hors requete desk. Appelee par `frappe.enqueue`.

    ⚠️ TOUT ECHEC LAISSE LE DEPOT `incertain`, JAMAIS LIBRE. Une erreur ici ne prouve pas que
    rien n'est parti : le clic « Valider » a pu aboutir avant la panne. Rendre la piece
    reemettable serait exactement le geste qui declare en double.
    """
    from bank_retenue_sync.tej import emis as E

    doc = frappe.get_doc(DOCTYPE, ligne)
    try:
        res = E.emettre(doc.journal_entry, dry_run=False, ctx=contexte(ligne),
                        depot_reserve=depot_nom)
    except Exception as e:
        if depot_nom:
            if E.est_un_refus(e):
                M_depot.marquer(depot_nom, M_depot.REFUSE,
                                "le portail a refusé la saisie : %s" % str(e)[:400])
            else:
                M_depot.marquer(depot_nom, M_depot.INCERTAIN, str(e)[:400])
        frappe.db.commit()
        raise

    reference = res.get("reference") or res.get("certificat")
    if reference:
        # L'etat ne bascule QU'AVEC une reference : sans elle, rien ne prouve qu'une declaration
        # est partie, et marquer « Émis » ferait perdre la ligne de vue pour toujours.
        doc.statut = "Émis"
        doc.certificat = reference
        doc.emis_le = frappe.utils.now_datetime()
        doc.flags.ignore_permissions = True
        doc.save()
        frappe.db.commit()
    if res.get("statut") == "soumis" and reference:
        # ⚠️ LE PDF NE VIENT PAS TOUT SEUL QUAND TEJ GENERE SUR-LE-CHAMP. Le cron des depots ne
        # relit que les lignes `en_analyse` et `incertain` : un depot conclu `genere` a la
        # soumission n'est jamais reexamine, et le certificat resterait chez TEJ — l'ecriture sans
        # justificatif. `tej/emis.executer_soumission` le fait pour les factures ; on le fait ici
        # pour l'ECRITURE, en le disant. Un echec ici ne remet rien en cause : la reference est en
        # base, le PDF se reprend.
        try:
            res["pdf"] = E.attacher_pdf(doc.journal_entry, reference, "Journal Entry")
        except Exception:
            res["pdf"] = {"statut": "echec"}
            frappe.log_error(title="PDF du certificat TEJ %s" % doc.journal_entry,
                             message=frappe.get_traceback())
        frappe.db.commit()
    return res


def reconstituer_depot(journal_entry: str, numero_depot: str, soumis_le=None,
                        suivre: bool = True) -> dict:
    """Pose la ligne `BRS Depot TEJ` qu'une soumission coupee n'a jamais enregistree. -> dict.

    ⚠️ LE CAS REEL : ACC-JV-2026-00698, le 04/09/2026. La soumission synchrone d'alors a ete
    coupee par le proxy APRES le clic « Valider » sur le portail : le depot IN260054 existait
    chez TEJ, rien ne le disait ici. Le correctif manuel a note le numero sur la ligne de file —
    mais le cron ne lit que `BRS Depot TEJ`, et la file ne sait rien des depots : le certificat,
    pourtant genere, n'est jamais revenu sur l'ecriture.

    Cette fonction fait ce que la reservation aurait fait, depuis ce que la ligne de file sait
    deja, puis laisse le circuit nominal conclure (suivi en lecture seule, PDF, rapatriement).
    Elle ne SOUMET rien. Une ligne de depot existante la rend inutile : on la rend telle quelle.

        bench --site <site> execute bank_retenue_sync.tej.emis_journal.reconstituer_depot \\
            --kwargs '{"journal_entry": "ACC-JV-2026-00698", "numero_depot": "IN260054"}'
    """
    from bank_retenue_sync.tej import emis as E
    from bank_retenue_sync.tej import matricule as M

    doc = frappe.get_doc(DOCTYPE, journal_entry)
    existants = frappe.get_all(M_depot.DOCTYPE,
                               filters={"facture": journal_entry, "piece_type": "Journal Entry"},
                               pluck="name")
    if existants:
        nom, cree = existants[0], False
    else:
        depot = frappe.new_doc(M_depot.DOCTYPE)
        depot.piece_type = "Journal Entry"
        depot.facture = journal_entry
        depot.fournisseur = doc.supplier
        depot.beneficiaire = M.normaliser(doc.matricule or "") or ""
        depot.numero_declarant = doc.numero_facture or ""
        depot.date_paiement = doc.date_piece or None
        depot.exercice = getdate(doc.date_piece).year if doc.date_piece else None
        depot.numero_depot = numero_depot
        depot.statut = M_depot.EN_ANALYSE
        depot.soumis_le = soumis_le or frappe.utils.now_datetime()
        depot.message = ("ligne reconstituée : la soumission avait été coupée avant "
                         "d'enregistrer le dépôt %s, constaté sur le portail" % numero_depot)
        depot.flags.ignore_permissions = True
        depot.insert()
        frappe.db.commit()
        nom, cree = depot.name, True

    out = {"depot": nom, "cree": cree}
    if suivre:
        out["suivi"] = E.suivre_depot(frappe.get_doc(M_depot.DOCTYPE, nom))
        frappe.db.commit()
        out["rapatries"] = rapatrier_certificats()
        out["ligne"] = frappe.db.get_value(DOCTYPE, journal_entry, ["statut", "certificat"],
                                           as_dict=True)
    return out


@frappe.whitelist()
def suivre(ligne: str) -> dict:
    """Ou en est la soumission lancee en file. -> dict. L'ecran interroge, il n'attend pas."""
    frappe.only_for(["System Manager", "Accounts Manager", "Accounts User"])
    # Le cron a pu conclure entre deux interrogations de l'ecran : on regarde d'abord ce qu'il a
    # rapporte, sinon la fenetre tournerait indefiniment sur un certificat deja genere.
    rapatrier_certificats()
    doc = frappe.get_doc(DOCTYPE, ligne)
    depot = M_depot.en_cours(doc.journal_entry, piece_type="Journal Entry")
    return {
        "statut": doc.statut,
        "certificat": doc.certificat,
        "emis_le": str(doc.emis_le or ""),
        "depot": M_depot.vue(depot) if depot else None,
        "progression": E_progression(depot) if depot else None,
    }


def E_progression(depot):
    from bank_retenue_sync.tej import emis as E

    try:
        return E.progression(depot)
    except Exception:
        return None


@frappe.whitelist()
def rafraichir(depuis=None) -> dict:
    """Bouton « Actualiser la file »."""
    frappe.only_for(["System Manager", "Accounts Manager"])
    return synchroniser(depuis)


@frappe.whitelist()
def etat(journal_entry: str) -> dict:
    """L'etat de la retenue d'une ecriture, pour le bouton pose sur sa fiche. -> dict.

    Cree la ligne de file si elle manque : l'utilisateur qui ouvre l'ecriture ne devrait pas
    avoir a lancer une synchronisation pour voir ou il en est.
    """
    frappe.only_for(["System Manager", "Accounts Manager", "Accounts User"])
    je = frappe.db.get_value("Journal Entry", journal_entry,
                             ["posting_date", "cheque_no", "docstatus"], as_dict=True)
    if not je:
        return {"concernee": False, "raison": _("écriture introuvable")}
    if je.docstatus != 1:
        return {"concernee": False, "raison": _("l'écriture n'est pas validée")}
    if exclue(je.cheque_no):
        return {"concernee": False, "raison": _("écriture d'un flux automatique")}
    if str(je.posting_date) < DEPUIS:
        return {"concernee": False,
                "raison": _("écriture antérieure au {0} : hors du périmètre").format(DEPUIS)}
    if not any(c.name == journal_entry for c in candidates()):
        return {"concernee": False, "raison": _("aucune retenue à la source sur cette écriture")}

    if not frappe.db.exists(DOCTYPE, journal_entry):
        synchroniser()
    if not frappe.db.exists(DOCTYPE, journal_entry):
        return {"concernee": False, "raison": _("la file n'a pas pu être alimentée")}

    doc = frappe.get_doc(DOCTYPE, journal_entry)
    # ⚠️ L'ÉTAT SE RECALCULE À LA LECTURE. Le matricule vit sur la fiche du fournisseur et le
    # rattachement peut avoir été défait ailleurs : afficher un statut mémorisé ferait annoncer
    # « À émettre » sur une ligne à laquelle il manque son fournisseur — vu en test.
    _reetat(doc)
    ctx = contexte(journal_entry)
    return {
        "concernee": True, "ligne": doc.name, "statut": doc.statut,
        "retenue": flt(doc.retenue, 3), "montant_ttc": flt(doc.montant_ttc, 3),
        "fournisseur_lu": doc.fournisseur_lu, "supplier": doc.supplier,
        "matricule": doc.matricule, "numero_facture": doc.numero_facture,
        "certificat": doc.certificat, "emis_le": str(doc.emis_le or ""),
        "montant_ht": ctx["montant_ht"], "taux_tva": ctx["taux_tva"],
        "manques": ctx["manques"],
        "peut_emettre": not ctx["manques"] and doc.statut != "Émis",
    }


@frappe.whitelist()
def completer(journal_entry: str, supplier=None, numero_facture=None) -> dict:
    """Rattache le fournisseur et le n° de facture depuis la fiche de l'ecriture.

    C'est le seul geste que l'ecran peut faire pour debloquer une emission : le matricule, lui,
    se corrige sur la fiche du fournisseur — on ne le recopie pas ici, sinon deux verites.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    if not frappe.db.exists(DOCTYPE, journal_entry):
        synchroniser()
    doc = frappe.get_doc(DOCTYPE, journal_entry)
    if doc.statut == "Émis":
        frappe.throw(_("Un certificat a déjà été émis pour cette écriture."))
    if supplier:
        doc.supplier = supplier
        doc.matricule = _matricule(supplier)
    if numero_facture is not None:
        doc.numero_facture = (numero_facture or "").strip()
    manques = _manques(doc)
    doc.note = " · ".join(manques)
    doc.statut = "Incomplet" if manques else "À émettre"
    doc.flags.ignore_permissions = True
    doc.save()
    frappe.db.commit()
    return etat(journal_entry)


# ------------------------------------------------------------------ lire la facture


#: Les extensions que le modele sait lire. Un .docx n'est ni une image ni un PDF : le lui
#: envoyer ne rend rien, et il y en a dans les pieces jointes reelles (« DETAIL DE VIREMENT.docx »).
_LISIBLES = (".pdf", ".png", ".jpg", ".jpeg", ".webp")

#: Ce qui DESIGNE une facture, et ce qui designe autre chose. Mesure sur les 16 ecritures de
#: depense a pieces multiples : on y trouve des pages d'une meme facture (« -p1 », « -p2 »), mais
#: aussi des documents de PAIEMENT — « DETAIL DE VIREMENT.docx », « Notification de paiement.pdf »,
#: « Bon de paiement.pdf ». Prendre la premiere piece venue lisait l'avis de virement au lieu de
#: la facture (04/09/2026).
_MOTS_FACTURE = ("fac", "facture", "invoice")
_MOTS_AUTRES = ("paiement", "virement", "notification", "bon de", "chq", "cheque", "chèque",
                "recu", "reçu", "bordereau")


def score_piece(nom) -> int:
    """A quel point ce nom de fichier ressemble a une FACTURE. Fonction pure, plus haut = mieux.

    Rend -1 pour ce qu'on ne sait pas lire : autant l'ecarter que d'envoyer un .docx au modele.
    """
    n = (nom or "").lower()
    if not n.endswith(_LISIBLES):
        return -1
    score = 0
    if any(n.startswith(m) or (" " + m) in n or n.startswith("facture-") for m in _MOTS_FACTURE):
        score += 10
    if any(m in n for m in _MOTS_AUTRES):
        score -= 8
    # ⚠️ LA PAGE 1 PORTE L'EN-TETE, donc le matricule fiscal. Sur une facture de trois pages,
    # lire la page 2 ne rend ni le nom ni le matricule.
    if "-p1" in n or " p1" in n:
        score += 4
    elif any(("-p%d" % i) in n or (" p%d" % i) in n for i in range(2, 10)):
        score -= 4
    return score


def pieces_jointes(journal_entry: str) -> list:
    """Les pieces jointes de l'ecriture, la plus probable en premier. -> [{nom, score, lisible}]."""
    fichiers = frappe.get_all(
        "File", filters={"attached_to_doctype": "Journal Entry",
                         "attached_to_name": journal_entry},
        fields=["name", "file_name"], order_by="creation")
    out = [{"fichier": f.name, "nom": f.file_name or "",
            "score": score_piece(f.file_name), "lisible": score_piece(f.file_name) >= 0}
           for f in fichiers]
    return sorted(out, key=lambda x: -x["score"])


def _scan_de(journal_entry: str, fichier: str = None):
    """La photo de facture attachee a l'ecriture. -> (contenu, mimetype, nom) | None.

    ⚠️ « LA PREMIERE PIECE JOINTE » N'EST PAS « LA FACTURE ». Seize ecritures de depense en
    portent plusieurs : des pages d'une meme facture, mais aussi des avis de virement, des
    notifications de paiement, des bons de paiement — et des .docx que le modele ne sait pas
    lire. On classe donc par ressemblance a une facture (`score_piece`) au lieu de prendre la
    premiere venue, et `fichier` permet a l'utilisateur de trancher lui-meme.
    """
    classees = pieces_jointes(journal_entry)
    if fichier:
        classees = [p for p in classees if p["fichier"] == fichier]
    else:
        classees = [p for p in classees if p["lisible"]]
    absents = []
    for p in classees:
        f = frappe._dict({"name": p["fichier"], "file_name": p["nom"]})
        try:
            doc = frappe.get_doc("File", f.name)
            contenu = doc.get_content()
        except Exception as e:
            # ⚠️ « PIÈCE JOINTE ABSENTE » N'EST PAS « AUCUNE PIÈCE JOINTE ». Un bench restauré
            # depuis une sauvegarde de BASE SEULE connaît les fichiers sans les avoir sur disque
            # (constaté le 04/09/2026 en dev). Dire « aucune pièce jointe » enverrait chercher
            # une photo qui existe pourtant.
            absents.append("%s (%s)" % (f.file_name, type(e).__name__))
            continue
        if not contenu:
            absents.append("%s (vide)" % f.file_name)
            continue
        nom = (f.file_name or "").lower()
        mime = ("application/pdf" if nom.endswith(".pdf")
                else "image/png" if nom.endswith(".png") else "image/jpeg")
        return contenu, mime, f.file_name
    return {"absents": absents} if absents else None


@frappe.whitelist()
def lire_facture(journal_entry: str, fichier: str = None) -> dict:
    """Relit le scan de la facture pour en tirer le fournisseur et son MATRICULE FISCAL.

    ⚠️ ON NE CREE RIEN ICI. La lecture propose ; la creation d'une fiche fournisseur est un
    second geste (`creer_fournisseur`), parce qu'un doublon de fournisseur se paie longtemps :
    les factures se repartissent alors sur deux fiches et aucun solde ne veut plus rien dire.

    ⚠️ ET LE MATRICULE DU FOURNISSEUR, JAMAIS LE NOTRE. Une facture porte les deux ; la consigne
    envoyee au modele l'exclut explicitement (cf. `caisse_depenses._consigne_matricule`).
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    pieces = pieces_jointes(journal_entry)
    scan = _scan_de(journal_entry, fichier)
    if not scan:
        return {"lu": False, "pieces": pieces,
                "raison": _("aucune pièce jointe sur cette écriture")}
    if isinstance(scan, dict):
        return {"lu": False, "pieces": pieces,
                "raison": _("la pièce jointe est référencée mais son fichier est introuvable "
                            "sur ce serveur : {0}").format(", ".join(scan["absents"]))}
    contenu, mime, nom_fichier = scan
    try:
        from customization_app.caisse_depenses import _decrire
    except Exception:
        return {"lu": False, "raison": _("le module de lecture (customization_app) est absent")}

    try:
        lu = _decrire(contenu, mime)
    except Exception as e:
        return {"lu": False, "raison": str(e)[:200]}
    # `_decrire` a rendu selon les versions (description, matricule) ou un tuple plus long :
    # on ne prend que ce dont on a besoin, sans supposer sa longueur.
    description = lu[0] if len(lu) > 0 else ""
    mat = (lu[1] if len(lu) > 1 else "") or ""

    fournisseur = frappe.db.get_value(DOCTYPE, journal_entry, "fournisseur_lu") or ""
    candidats = []
    try:
        from customization_app.caisse_depenses import _rapprocher_fournisseur

        r = _rapprocher_fournisseur(fournisseur, mat)
        candidats = r.get("candidats") or []
        certain = r.get("certain")
    except Exception:
        certain = None

    return {"lu": True, "fichier": nom_fichier, "pieces": pieces, "description": description,
            "fournisseur": fournisseur, "matricule": mat.strip(),
            "candidat_certain": certain, "candidats": candidats}


@frappe.whitelist()
def creer_fournisseur(journal_entry: str, nom=None, matricule=None) -> dict:
    """Cree (ou retrouve) la fiche fournisseur et la rattache a la ligne.

    ⚠️ EN CAS DE DOUTE ON REFUSE, ON NE CREE PAS. `caisse_depenses._supplier` porte deja cette
    regle : si des fiches proches existent, il leve et l'ecran demande de choisir. On ne la
    contourne pas — un doublon de fournisseur eparpille ses factures sur deux soldes.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    from customization_app.caisse_depenses import _supplier

    doc = frappe.get_doc(DOCTYPE, journal_entry)
    if doc.statut == "Émis":
        frappe.throw(_("Un certificat a déjà été émis pour cette écriture."))
    nom = (nom or doc.fournisseur_lu or "").strip()
    if not nom:
        frappe.throw(_("Aucun nom de fournisseur à créer."))
    supplier = _supplier(nom, matricule=matricule)
    if not supplier:
        frappe.throw(_("La fiche fournisseur n'a pas pu être créée."))
    frappe.db.commit()
    return completer(journal_entry, supplier=supplier)
