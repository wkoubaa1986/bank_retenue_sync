"""La retenue a la source SUR ACHAT : 1 % du TTC hors timbre, des la barre des 1 000 DT.

OU ELLE VIT, ET POURQUOI PAS AILLEURS
--------------------------------------
Dans la TABLE DES TAXES de la facture, en ligne de deduction — comme le fait deja la comptabilite :

    TVA 19 %                      175,311   Ajouter   ->  1 099,011
    Retenue a la source achat      10,990   Deduire   ->  1 088,021

La premiere version de ce module creait une ecriture de paiement separee, sur le modele de la
retenue de VENTE. C'etait une erreur : la retenue d'achat est deja portee par la facture. Les deux
ensemble auraient retenu deux fois la meme somme au meme fournisseur, une fois dans son solde et
une fois dans une ecriture — et le total facture n'aurait plus rien voulu dire.

⚠️ ELLE NE SE POSE QU'EN BROUILLON. Apres validation, la table des taxes est figee : la seule
facon d'ajouter la ligne serait d'annuler la facture. D'ou le crochet sur `validate`, et le refus
sur `before_submit` quand elle manque encore.

⚠️ L'ASSIETTE EXCLUT LE TIMBRE FISCAL. Verifie sur les 17 factures locales de 2026 depassant le
seuil : « 1 % du TTC hors timbre » tombe au millime sur dix d'entre elles. C'est la meme regle que
le portail applique aux ventes, ou l'assiette declaree vaut notre TTC moins le timbre de 1 DT.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from bank_retenue_sync.achat import regles

DOCTYPE_EXTRACTION_ACHAT = "Extraction Facture Achat"

MODE_RETENUE_ACHAT = "Retenue a la source achat"
COMPTE_RETENUE_ACHAT = "Retenue a la source achat - A&S"
LIBELLE = "Retenue a la source achat"


def _reglage(champ, defaut=None):
    try:
        v = frappe.db.get_single_value("Bank Retenue Sync Settings", champ)
        return defaut if v in (None, "") else v
    except Exception:
        return defaut


def compte_retenue() -> str:
    return _reglage("ras_achat_compte", None) or COMPTE_RETENUE_ACHAT


def _seuil():
    # ⚠️ Un seuil a zero rendrait TOUTE facture passible de retenue ; un taux a zero n'en calculerait
    # aucune. Ni l'un ni l'autre n'est un reglage voulu : c'est `controle_achat_actif` qui coupe.
    return flt(_reglage("ras_achat_seuil", None) or regles.SEUIL_RETENUE, 3)


def _taux():
    return flt(_reglage("ras_achat_taux", None) or regles.TAUX_RETENUE, 3)


def centre_de_cout(doc):
    """Le centre de couts que doit porter la ligne de retenue. -> str | None.

    ⚠️ SANS LUI, LA FACTURE NE SE VALIDE PLUS. `Retenue a la source achat - A&S` est un compte de
    RESULTAT, et ERPNext refuse toute ecriture de resultat sans centre de couts — l'ecriture de
    taxe reprend telle quelle la colonne de la ligne, sans se rabattre sur le defaut de la societe.
    Le champ a pourtant `:Company` pour defaut : mais ce defaut n'est pose qu'a la saisie A L'ECRAN,
    jamais par un `append()` cote serveur. La ligne posee par la machine partait donc vide la ou les
    quatorze lignes saisies a la main avant elle portent toutes le centre par defaut de la societe.
    Cas reel : ACC-PINV-2026-00092, refusee a la validation apres avoir ete corrigee sans bruit.
    """
    return doc.get("cost_center") or frappe.db.get_value("Company", doc.company, "cost_center")


def _lignes(doc):
    return [{"account_head": t.account_head, "tax_amount": t.tax_amount,
             "add_deduct_tax": t.add_deduct_tax} for t in (doc.get("taxes") or [])]


def controle(doc) -> dict:
    """Ce qui est du, ce qui est saisi, et l'ecart. -> dict (cf. `regles.controle_retenue`)."""
    return regles.controle_retenue(doc.grand_total, _lignes(doc), _seuil(), _taux())


def poser_ligne(doc) -> dict:
    """Ajoute la ligne de retenue si elle manque. -> dict. Ne touche a rien d'autre.

    Une ligne DEJA saisie n'est pas touchee ici : c'est `corriger_ligne` qui la ramene au montant
    du. Les deux gestes restent separes parce qu'ils ne repondent pas de la meme chose — l'un pose
    ce qui manque, l'autre corrige ce qui est faux.
    """
    # ⚠️ RECALCULER AVANT DE DECIDER. En `before_validate`, `grand_total` porte encore la valeur du
    # dernier enregistrement : sur une facture dont on venait de retirer la ligne de retenue, il
    # valait 1 087,021 (net de l'ancienne retenue) et la nouvelle a ete calculee a 10,870 au lieu de
    # 10,990. La base doit venir des lignes du moment, pas du total d'avant.
    doc.calculate_taxes_and_totals()
    c = controle(doc)
    if not c["due"]:
        return {"statut": "sous le seuil", **c}
    if c["saisie"]:
        return {"statut": "deja saisie", **c}

    compte = compte_retenue()
    if not frappe.db.exists("Account", compte):
        return {"statut": "compte introuvable", "compte": compte, **c}

    doc.append("taxes", {
        "charge_type": "Actual",
        "account_head": compte,
        "description": LIBELLE,
        "add_deduct_tax": "Deduct",
        "category": "Total",
        "tax_amount": c["due"],
        "cost_center": centre_de_cout(doc),
    })
    # ERPNext recalcule les totaux a partir de la table : sans cet appel, `grand_total` resterait
    # celui d'avant la ligne et la facture se validerait avec un total faux.
    doc.calculate_taxes_and_totals()
    return {"statut": "posee", **c, "compte": compte}


def corriger_ligne(doc) -> dict:
    """Ramene la ligne de retenue au montant du. -> dict.

    ⚠️ REVIREMENT ASSUME. La version precedente refusait de corriger une retenue deja saisie, au
    motif qu'elle avait pu etre negociee. Demande explicitement : une retenue a la source ne se
    negocie pas, elle se calcule — 1 % de l'assiette, et le fournisseur n'a pas voix au chapitre.
    Une saisie fausse est donc une erreur a corriger, pas un choix a respecter. Sur 2026, sept
    factures sur dix-sept etaient dans ce cas.
    """
    c = controle(doc)
    if c["verdict"] != "montant faux":
        return {"statut": c["verdict"], **c}
    compte = compte_retenue()
    # ⚠️ TOUTES LES LIGNES DE RETENUE, PAS SEULEMENT LA PREMIERE. `saisie` est la SOMME des lignes
    # de deduction : n'en redresser qu'une laisse le total faux — la premiere porte le du, les
    # suivantes (doublons de saisie) sont ramenees a zero, et chacune est dite.
    lignes_retenue = [l for l in (doc.get("taxes") or [])
                      if l.add_deduct_tax == "Deduct"
                      and regles.MOT_RETENUE in (l.account_head or "")]
    if not lignes_retenue:
        return {"statut": "ligne introuvable", **c}
    avant = flt(c["saisie"], 3)
    annulees = 0
    for i, ligne in enumerate(lignes_retenue):
        # ⚠️ LA LIGNE DOIT DEVENIR « Actual ». Saisie en « On Net Total » a 1 %, son montant est
        # RECALCULE depuis le taux a chaque calculate_taxes_and_totals : poser tax_amount sans
        # changer le type etait aussitot ecrase, et la correction annoncee ne collait jamais.
        # C'est l'origine meme des montants faux — 16,523 = 1 % du HT (ACC-PINV-2026-00091),
        # quand la retenue se calcule sur le TTC hors timbre.
        ligne.charge_type = "Actual"
        ligne.rate = 0
        ligne.tax_amount = c["due"] if i == 0 else 0
        ligne.account_head = compte if i == 0 else ligne.account_head
        if i > 0:
            annulees += 1
    doc.calculate_taxes_and_totals()
    res = {"statut": "corrigee", "avant": avant, "apres": c["due"], **c}
    if annulees:
        res["doublons_annules"] = annulees
    return res


def completer_centre(doc) -> dict:
    """Pose le centre de couts manquant sur la ligne de retenue. -> dict.

    Geste separe de `poser_ligne` et de `corriger_ligne` parce qu'il repond d'un autre etat : une
    ligne posee AVANT ce correctif est en brouillon, au bon montant — `corriger_ligne` la declare
    conforme et la laisse passer — et pourtant invalidable. Rien dans le montant ne dit qu'elle est
    cassee ; seule la colonne vide le dit.

    Un centre deja saisi n'est jamais ecrase : sur une societe a plusieurs centres, celui que
    l'utilisateur a choisi vaut mieux que le defaut.
    """
    # ⚠️ LA LIGNE D'ABORD, LE CENTRE ENSUITE. Une societe sans centre par defaut n'est un probleme
    # que s'il y a une retenue a porter : annoncer le manque sur une facture sous le seuil ferait
    # crier au loup sur une facture que rien ne menace.
    for ligne in doc.get("taxes") or []:
        if ligne.add_deduct_tax == "Deduct" and regles.MOT_RETENUE in (ligne.account_head or ""):
            if ligne.cost_center:
                return {"statut": "deja pose", "cost_center": ligne.cost_center}
            centre = centre_de_cout(doc)
            if not centre:
                return {"statut": "aucun centre de couts"}
            ligne.cost_center = centre
            return {"statut": "pose", "cost_center": centre}
    return {"statut": "ligne introuvable"}


@frappe.whitelist()
def poser_maintenant(facture):
    """Bouton du formulaire : pose la ligne si elle manque, la corrige si elle est fausse.

    ⚠️ LE BOUTON DOIT FAIRE CE QUE FAIT L'ENREGISTREMENT, NI PLUS NI MOINS. Tant qu'il ne savait que
    poser, il repondait « rien a poser (montant faux) » sur une facture que le simple fait
    d'enregistrer aurait corrigee : deux reponses differentes pour un meme etat, c'est l'ecran qui
    devient un menteur.
    """
    frappe.only_for(["System Manager", "Accounts Manager", "Accounts User", "Purchase Manager",
                     "Purchase User"])
    doc = frappe.get_doc("Purchase Invoice", facture)
    if doc.docstatus != 0:
        frappe.throw(_("La facture est validée : sa table des taxes ne peut plus changer."))
    res = poser_ligne(doc)
    if res["statut"] != "posee":
        res = corriger_ligne(doc)
    centre = completer_centre(doc)
    res = {**res, "centre": centre}
    if res["statut"] in ("posee", "corrigee") or centre["statut"] == "pose":
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return res


def inventaire(annee=None, seuil=None) -> dict:
    """Les factures locales du periode qui ne retiennent pas ce qu'elles devraient.

    L'autre sens du controle : celui-ci ne regarde pas ce qu'on saisit aujourd'hui, mais ce qui est
    deja valide. Une retenue oubliee est une somme due au Tresor que personne ne reclamera avant
    un controle fiscal.
    """
    annee = annee or frappe.utils.getdate(frappe.utils.nowdate()).year
    seuil = flt(seuil or _seuil(), 3)
    # ⚠️ PLANCHER DU PERIMETRE : meme regle que les hooks (facture._plancher). Une annee demandee
    # entierement anterieure au plancher rend un inventaire vide — c'est voulu, l'exercice est clos.
    from bank_retenue_sync.achat.facture import _plancher as _plancher_achat
    plancher = _plancher_achat()
    rows = frappe.db.sql("""select p.name, p.supplier, p.posting_date, p.grand_total
                            from `tabPurchase Invoice` p
                            join `tabSupplier` s on s.name = p.supplier
                            where p.docstatus = 1 and s.country = %(pays)s
                              and year(p.posting_date) = %(annee)s
                              and p.posting_date >= %(plancher)s
                            order by p.posting_date""",
                         {"pays": regles.PAYS_LOCAL, "annee": annee, "plancher": plancher},
                         as_dict=1)
    out = {"annee": annee, "factures": 0, "conformes": 0, "manquantes": 0, "fausses": 0,
           "manque_total": 0.0, "detail": []}
    for r in rows:
        lignes = frappe.db.get_all("Purchase Taxes and Charges", filters={"parent": r.name},
                                   fields=["account_head", "tax_amount", "add_deduct_tax"],
                                   order_by="idx")
        c = regles.controle_retenue(r.grand_total, [dict(l) for l in lignes], seuil, _taux())
        if not c["due"]:
            continue
        out["factures"] += 1
        if c["verdict"] == "conforme":
            out["conformes"] += 1
            continue
        out["manquantes" if c["verdict"] == "manquante" else "fausses"] += 1
        out["manque_total"] = round(out["manque_total"] - c["ecart"], 3)
        out["detail"].append({"facture": r.name, "fournisseur": r.supplier,
                              "date": str(r.posting_date), **c})
    return out


@frappe.whitelist()
def inventaire_retenues(annee=None):
    frappe.only_for(["System Manager", "Accounts Manager"])
    return inventaire(annee)


@frappe.whitelist()
def recreer_avec_retenue(facture):
    """Supprime la facture NON PAYEE et la recree SOUS LE MEME NUMERO, retenue posee. -> dict.

    La table des taxes d'une facture validee est figee : la seule voie pour y poser la retenue
    manquante (ou corriger un montant faux) est de la refaire. Decision utilisateur du
    26/08/2026 : pas d'amendee « -1 » avec une annulee qui traine — l'ancienne est SUPPRIMEE et
    la facture renait a l'identique, meme numero, avec sa retenue posee par
    `a_l_enregistrement`. Pieces jointes et extraction restent au meme nom.

    ⚠️ SEULEMENT SI RIEN N'EST PAYE. Un paiement lie devrait etre desalloue puis reimpute ; ce
    geste-la ne se fait pas d'un clic. Un avoir lie non plus. En cas d'echec en cours de route,
    la transaction Frappe annule TOUT — la facture d'origine reste en place, rien a moitie fait.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    from bank_retenue_sync.achat.facture import _plancher as _plancher_achat

    doc = frappe.get_doc("Purchase Invoice", facture)
    if doc.docstatus != 1:
        frappe.throw(_("La facture n'est pas validée."))
    if doc.get("is_return"):
        frappe.throw(_("C'est un avoir : rien à recréer."))
    if not regles.dans_le_perimetre(doc.posting_date, _plancher_achat()):
        frappe.throw(_("Facture antérieure au plancher des contrôles : exercice clos."))
    c = controle(doc)
    if c["verdict"] == "conforme":
        frappe.throw(_("La retenue de cette facture est déjà conforme."))
    if flt(doc.outstanding_amount, 3) < flt(doc.grand_total, 3) - 0.005:
        frappe.throw(_("Un paiement est déjà imputé ({0} restant sur {1}) : désallouez-le "
                       "d'abord — ce bouton ne touche qu'aux factures où rien n'est payé.")
                     .format(doc.outstanding_amount, doc.grand_total))
    if frappe.get_all("Payment Entry Reference",
                      filters={"reference_doctype": "Purchase Invoice",
                               "reference_name": facture, "docstatus": 1}, limit=1):
        frappe.throw(_("Un paiement validé référence cette facture : désallouez-le d'abord."))
    if frappe.get_all("Purchase Invoice",
                      filters={"return_against": facture, "docstatus": ["<", 2]}, limit=1):
        frappe.throw(_("Un avoir existe contre cette facture : à traiter d'abord."))

    # Copie des donnees AVANT toute destruction — c'est elle qui renaitra.
    doc.cancel()
    donnees = frappe.copy_doc(doc)
    donnees.amended_from = None
    donnees.docstatus = 0
    # La date de comptabilisation d'origine est conservee : sans set_posting_time, ERPNext
    # ramenerait la copie a aujourd'hui et changerait l'exercice du montant.
    donnees.set_posting_time = 1

    # Les pieces jointes et l'extraction sont PARQUEES le temps de la suppression (delete_doc
    # efface les fichiers attaches au document supprime), puis rendues au MEME nom — puisque la
    # facture renait sous son numero, rien d'autre n'a a etre repointe.
    PARC = "__recree_en_cours"
    frappe.db.sql("""update tabFile set attached_to_name = %(parc)s
                     where attached_to_doctype = 'Purchase Invoice'
                       and attached_to_name = %(nom)s""", {"parc": PARC, "nom": facture})
    ext = frappe.db.get_value(DOCTYPE_EXTRACTION_ACHAT, {"purchase_invoice": facture}, "name")
    if ext:
        frappe.db.set_value(DOCTYPE_EXTRACTION_ACHAT, ext, "purchase_invoice", None,
                            update_modified=False)

    # L'annulation d'une facture a stock cree un « Repost Item Valuation » qui LIE la facture :
    # delete_doc refuse tant qu'il existe (constate sur ACC-PINV-2026-00091). La recreation
    # immediate au meme numero, memes articles, meme date le rend sans objet — la validation de
    # la copie declenchera son propre reposting.
    for nom_repost in frappe.get_all("Repost Item Valuation",
                                     filters={"voucher_type": "Purchase Invoice",
                                              "voucher_no": facture}, pluck="name"):
        # `before_cancel` du repost refuse d'annuler un Queued/In Progress (« réessayez dans une
        # heure ») : on le passe d'abord Skipped — l'etat prevu pour un repost sans objet, ce
        # qu'il est puisque la resoumission immediate en recreera un equivalent.
        frappe.db.set_value("Repost Item Valuation", nom_repost, "status", "Skipped",
                            update_modified=False)
        repost = frappe.get_doc("Repost Item Valuation", nom_repost)
        if repost.docstatus == 1:
            repost.flags.ignore_permissions = True
            repost.cancel()
        frappe.delete_doc("Repost Item Valuation", nom_repost, ignore_permissions=True,
                          force=True)

    frappe.delete_doc("Purchase Invoice", facture, ignore_permissions=True)

    # L'insert passe par before_validate : `a_l_enregistrement` pose (ou redresse) la retenue,
    # le stock, le centre de couts — exactement comme une saisie a la main.
    donnees.insert(set_name=facture)
    frappe.db.sql("""update tabFile set attached_to_name = %(nom)s
                     where attached_to_doctype = 'Purchase Invoice'
                       and attached_to_name = %(parc)s""", {"parc": PARC, "nom": facture})
    if ext:
        frappe.db.set_value(DOCTYPE_EXTRACTION_ACHAT, ext, "purchase_invoice", facture,
                            update_modified=False)

    donnees.submit()
    frappe.db.commit()
    apres = controle(donnees)
    return {"facture": donnees.name,
            "retenue_avant": c["saisie"], "retenue_apres": apres["saisie"],
            "verdict_apres": apres["verdict"]}


@frappe.whitelist()
def lire_scans_manquants():
    """LANCE EN TACHE DE FOND la lecture des scans des factures sans n° fournisseur. -> dict.

    ⚠️ EN TACHE DE FOND, ET C'EST OBLIGATOIRE : une quinzaine de scans a un appel OpenAI chacun
    depasse le timeout du proxy — en prod, le clic rendait « La Requête a Expirée » et la
    transaction annulait TOUTES les lectures (constate le 26/08/2026). La requete desk ne fait
    que compter et enqueuer ; l'ecran invite a actualiser dans quelques minutes.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    from bank_retenue_sync.achat.facture import _plancher as _plancher_achat

    noms = [r[0] for r in frappe.db.sql(
        """select p.name from `tabPurchase Invoice` p
           join `tabSupplier` s on s.name = p.supplier
           where p.docstatus = 1 and s.country = %(pays)s
             and p.posting_date >= %(depuis)s
             and ifnull(p.bill_no, '') = ''
           order by p.posting_date""",
        {"pays": regles.PAYS_LOCAL, "depuis": str(_plancher_achat())})]
    if not noms:
        return {"statut": "rien a lire", "factures": 0}
    frappe.enqueue("bank_retenue_sync.achat.retenue.executer_lecture_scans",
                   queue="long", timeout=3600, job_name="lecture scans achats", noms=noms)
    return {"statut": "lance", "factures": len(noms)}


def executer_lecture_scans(noms):
    """La lecture elle-meme, en worker. Un COMMIT PAR FACTURE : un scan illisible ou une panne
    OpenAI n'emporte pas les lectures deja faites — c'est tout l'interet de sortir du clic."""
    from bank_retenue_sync.achat import facture as M_facture

    for nom in noms or []:
        try:
            M_facture.extraire(nom, forcer=False)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title="Lecture scan %s" % nom, message=frappe.get_traceback())


def orphelins_tej(export, cles_locales, depuis, etats_vivants,
                  references_attachees=frozenset()) -> list:
    """Les certificats VIVANTS de l'export sans facture locale correspondante. Pure.

    L'AUTRE SENS du recapitulatif : le tableau principal part des factures et cherche leur
    certificat ; ici on part du PORTAIL et on cherche la facture. Un certificat emis sous un
    numero mal saisi, ou pour une facture qui n'existe pas dans ERPNext, n'apparaitrait dans
    aucun des deux ecrans simples — c'est pourtant une declaration au fisc sans comptabilite.

    `cles_locales` : {(numero, matricule)} de TOUTES les factures locales validees, sans plancher
    — un certificat 2026 d'une facture comptabilisee en 2025 n'est pas un orphelin. Le plancher ne
    filtre que la date du certificat (paiement, sinon creation) ; une date illisible GARDE le
    certificat : le doute se montre, il ne se cache pas.
    """
    from frappe.utils import getdate

    out = []
    for c in export or []:
        if (c.get("etat") or "") not in etats_vivants:
            continue
        cle = ((c.get("numero") or "").strip(), c.get("beneficiaire") or "")
        if not cle[0] or cle in cles_locales:
            continue
        # Un certificat dont le PDF est DEJA attache a une facture n'est pas orphelin, quel que
        # soit son numero : l'attachement est une identification faite par un humain (ou par le
        # bouton « Attacher ce certificat »). Par PREFIXE : Frappe suffixe le nom du fichier
        # quand un homonyme existe (certificat_ras_<ref>dce363.pdf) — c'est la raison d'etre de
        # pdf.motif_fichier, et une egalite stricte laissait l'orphelin affiche a jamais.
        ref = c.get("reference") or ""
        if ref and any(att.startswith(ref) for att in references_attachees):
            continue
        ref_date = c.get("date_paiement") or c.get("cree")
        if ref_date:
            try:
                if str(getdate(ref_date)) < str(depuis):
                    continue
            except Exception:
                pass
        out.append(c)
    return out


def _date_portail(v):
    """Une date du portail TEJ (JJ-MM-AAAA) ou d'ERPNext (AAAA-MM-JJ). -> date | None.

    ⚠️ EXPLICITE, PAS DEVINEE : « 05-02-2026 » est ambigu pour un parseur souple (5 fevrier ou
    2 mai) — et une suggestion fondee sur une date mal lue enverrait vers la mauvaise facture.
    """
    from datetime import datetime

    if not v:
        return None
    s = str(v).strip()[:10]
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def rapprochements_suggeres(orphelins, lignes, tolerance_jours: int = 3) -> list:
    """Les paires probables orphelin ↔ facture. Pure. SUGGESTIF : rien n'est lie.

    Meme matricule fiscal ET date de paiement du certificat proche de la comptabilisation
    (la soumission envoie posting_date comme date de paiement : sur les vrais cas, elles sont
    identiques). C'est le cran juste au-dessus de « rien » : l'ecran montre la paire, l'humain
    tranche — en general en corrigeant le « N° chez le declarant » ou le bill_no.
    """
    out = []
    for o in orphelins or []:
        mat = o.get("beneficiaire")
        numero = (o.get("numero") or "").strip()
        date_o = _date_portail(o.get("date_paiement") or o.get("cree"))
        for ligne in lignes or []:
            # Signal 1 — INCLUSION DES NUMEROS : le numero du portail contient le bill_no de la
            # facture ou l'inverse (cas reel : « 26FA01134_V2 », le suffixe ajoute pour passer le
            # refus de contenu identique apres annulation). Plus fort que la date : il joue seul.
            # L'IDENTITE VIENT DU DOCUMENT : si le champ officiel est vide, la valeur lue sur
            # le scan (Extraction Facture Achat) parle a sa place — pour SUGGERER seulement.
            bill_no = (ligne.get("bill_no") or ligne.get("bill_no_scan") or "").strip()
            # ⚠️ DES NUMEROS SEMBLABLES N'EXCUSENT PAS DES BENEFICIAIRES DIFFERENTS. Cas reel du
            # 26/08/2026 : « 4/2026 » (NIZAR BELGUITH, loyer 7,14 DT) suggere vers la facture
            # M.F.K au bill_no « 04/2026 » — attache A TORT en prod sur la foi des numeros.
            # Quand les deux matricules sont connus et different, le signal se tait.
            mat_connu = ligne.get("matricule") or ligne.get("matricule_scan")
            if (numero and bill_no and len(bill_no) >= 5
                    and (bill_no in numero or numero in bill_no) and numero != bill_no
                    and not (mat and mat_connu and mat != mat_connu)):
                out.append({"numero": numero, "reference": o.get("reference"),
                            "facture": ligne["facture"], "motif": "numero",
                            "ecart_jours": None,
                            "facture_tej": (ligne.get("tej") or {}).get("statut")})
                continue
            # Signal 2 — MATRICULE + DATE : meme beneficiaire, paiement proche de la
            # comptabilisation.
            mat_ligne = ligne.get("matricule") or ligne.get("matricule_scan")
            if not mat or not date_o or mat_ligne != mat:
                continue
            date_l = _date_portail(ligne.get("date"))
            if not date_l:
                continue
            ecart = abs((date_o - date_l).days)
            if ecart <= int(tolerance_jours):
                out.append({"numero": numero, "reference": o.get("reference"),
                            "facture": ligne["facture"], "motif": "matricule+date",
                            "ecart_jours": ecart,
                            "facture_tej": (ligne.get("tej") or {}).get("statut")})
    return out


@frappe.whitelist()
def recapitulatif_retenues(depuis=None):
    """Le tableau croise ERPNext ↔ TEJ des retenues d'achat depuis le plancher. -> dict.

    Une ligne par facture CONCERNEE (retenue due ou saisie), avec les deux verites cote a cote :
    ce que la COMPTABILITE retient (ligne de taxe) et ce que le FISC a recu (certificat attache —
    du module ou a la main —, depot en cours, ou certificat vivant dans l'export du portail).
    C'est la confrontation des deux qui fait le controle : une retenue comptabilisee sans
    certificat est une declaration en retard ; un certificat sans ecriture est une comptabilite
    fausse (cas reels ACC-PINV-2026-00042 et 00054).

    ⚠️ L'EXPORT PORTAIL EST UN COMPLEMENT, PAS UN PREREQUIS. Sa lecture passe par le service TEJ ;
    injoignable (dev, panne), le tableau sort quand meme — colonne TEJ fondee sur les seules
    preuves locales, et `export_disponible: False` pour que l'ecran le dise.
    """
    frappe.only_for(["System Manager", "Accounts Manager", "Accounts User", "Purchase Manager",
                     "Purchase User"])
    from bank_retenue_sync.tej import emis, matricule
    from bank_retenue_sync.tej import depot as M_depot

    from bank_retenue_sync.achat.facture import _plancher as _plancher_achat
    depuis = str(depuis or _plancher_achat())
    seuil, taux = _seuil(), _taux()

    export, export_disponible = [], True
    try:
        export = emis.certificats_emis()
    except Exception:
        export_disponible = False
        # ⚠️ AVALER L'EXCEPTION NE SUFFIT PAS : un frappe.throw en profondeur (ex. jeton
        # indeciffrable apres un restore — la clé de chiffrement du site a changé) a DEJA mis son
        # message dans la file d'affichage, et l'ecran montrerait un popup d'erreur par-dessus un
        # tableau pourtant rendu. Le bandeau « export indisponible » suffit.
        frappe.clear_last_message()

    rows = frappe.db.sql("""select p.name, p.supplier, s.supplier_name, s.tax_id,
                                   p.posting_date, p.bill_no, p.grand_total,
                                   p.status, p.outstanding_amount
                            from `tabPurchase Invoice` p
                            join `tabSupplier` s on s.name = p.supplier
                            where p.docstatus = 1 and s.country = %(pays)s
                              and p.posting_date >= %(depuis)s
                            order by p.posting_date""",
                         {"pays": regles.PAYS_LOCAL, "depuis": depuis}, as_dict=1)

    lignes, totaux = [], {"due": 0.0, "saisie": 0.0, "manque": 0.0, "restant": 0.0}
    compte = {"factures": 0, "conformes": 0, "manquantes": 0, "fausses": 0,
              "tej_emis": 0, "tej_en_cours": 0, "tej_manquants": 0, "impayees": 0}
    for r in rows:
        taxes = frappe.db.get_all("Purchase Taxes and Charges", filters={"parent": r.name},
                                  fields=["account_head", "tax_amount", "add_deduct_tax"],
                                  order_by="idx")
        c = regles.controle_retenue(r.grand_total, [dict(t) for t in taxes], seuil, taux)
        if not c["due"] and not c["saisie"]:
            continue

        # La verite TEJ, par force de preuve decroissante : le PDF attache (module ou manuel),
        # le depot en cours, l'export du portail. « manquant » = aucune des trois.
        tej = {"statut": "manquant", "detail": ""}
        cert = emis._certificat_attache(r.name)
        if cert:
            tej = {"statut": "emis",
                   "detail": _("certificat attaché à la main") if cert.get("manuel")
                   else cert.get("reference") or "",
                   "file_url": cert.get("file_url")}
        else:
            en_cours = M_depot.en_cours(r.name)
            if en_cours:
                vue = M_depot.vue(en_cours)
                tej = {"statut": vue.get("statut") or "en_analyse",
                       "detail": vue.get("numero") or ""}
            elif export:
                mat = matricule.normaliser(r.tax_id)
                vivant = next((x for x in export
                               if x["numero"] == (r.bill_no or "").strip()
                               and x["beneficiaire"] == mat
                               and x["etat"] in emis.ETATS_VIVANTS), None)
                if vivant:
                    # La reference voyage avec la ligne : c'est elle qui permet au bouton 📎
                    # de rapatrier le PDF du portail sur la facture.
                    tej = {"statut": "emis",
                           "reference": vivant.get("reference"),
                           "detail": _("portail : {0} (PDF non attaché)").format(
                               vivant.get("reference") or vivant.get("etat"))}

        compte["factures"] += 1
        compte["conformes" if c["verdict"] == "conforme"
               else "manquantes" if c["verdict"] == "manquante" else "fausses"] += 1
        if tej["statut"] == "emis":
            compte["tej_emis"] += 1
        elif tej["statut"] == "manquant":
            compte["tej_manquants"] += 1
        else:
            compte["tej_en_cours"] += 1
        totaux["due"] = round(totaux["due"] + c["due"], 3)
        totaux["saisie"] = round(totaux["saisie"] + c["saisie"], 3)
        if c["verdict"] != "conforme":
            totaux["manque"] = round(totaux["manque"] - c["ecart"], 3)

        # L'etat du paiement, tel qu'ERPNext le tient : le statut pour le mot, le restant
        # pour le chiffre — un « Partiellement paye » sans montant ne dit rien d'utile.
        restant = flt(r.outstanding_amount, 3)
        if restant > 0.005:
            compte["impayees"] += 1
            totaux["restant"] = round(totaux["restant"] + restant, 3)

        lignes.append({"facture": r.name, "fournisseur": r.supplier_name or r.supplier,
                       "matricule": matricule.normaliser(r.tax_id),
                       "date": str(r.posting_date), "bill_no": r.bill_no or "",
                       "ttc": c["ttc_avant_retenue"], "due": c["due"], "saisie": c["saisie"],
                       "verdict": c["verdict"], "tej": tej,
                       "paiement": {"statut": r.status or "", "restant": restant},
                       # Recreable = retenue non conforme ET rien de paye : le seul cas ou la
                       # facture peut etre refaite d'un clic (les garde-fous complets sont
                       # re-verifies cote serveur au moment du geste).
                       "recreable": c["verdict"] != "conforme"
                       and restant >= flt(r.grand_total, 3) - 0.005})

    # L'IDENTITE DOCUMENTAIRE en secours : quand bill_no ou matricule manquent sur les champs
    # officiels, la lecture du scan (Extraction Facture Achat) parle a leur place — pour les
    # suggestions uniquement, jamais pour l'appariement strict.
    if lignes:
        extractions = {e.purchase_invoice: e for e in frappe.get_all(
            "Extraction Facture Achat",
            filters={"purchase_invoice": ["in", [l["facture"] for l in lignes]]},
            fields=["purchase_invoice", "invoice_no", "supplier_tax_id"])}
        for l in lignes:
            e = extractions.get(l["facture"])
            if not e:
                continue
            if not l["bill_no"] and (e.invoice_no or "").strip():
                l["bill_no_scan"] = e.invoice_no.strip()
            if not l["matricule"]:
                m = matricule.normaliser(e.supplier_tax_id)
                if m:
                    l["matricule_scan"] = m

    # L'AUTRE SENS : les certificats du portail sans facture correspondante. Les cles locales
    # couvrent TOUTES les factures validees, sans plancher — un certificat 2026 d'une facture
    # 2025 n'est pas un orphelin ; le plancher ne filtre que la date du certificat.
    orphelins = []
    if export:
        cles_locales = set()
        fournisseurs_par_matricule = {}
        for f in frappe.db.sql("""select p.bill_no, s.tax_id, s.supplier_name
                                  from `tabPurchase Invoice` p
                                  join `tabSupplier` s on s.name = p.supplier
                                  where p.docstatus = 1 and s.country = %(pays)s
                                    and ifnull(p.bill_no, '') != ''""",
                               {"pays": regles.PAYS_LOCAL}, as_dict=1):
            mat = matricule.normaliser(f.tax_id)
            cles_locales.add((f.bill_no.strip(), mat))
            if mat:
                fournisseurs_par_matricule[mat] = f.supplier_name
        # Le nom du fournisseur se retrouve par matricule meme sans facture a son numero.
        for s in frappe.db.get_all("Supplier", filters={"country": regles.PAYS_LOCAL},
                                   fields=["supplier_name", "tax_id"]):
            mat = matricule.normaliser(s.tax_id)
            if mat and mat not in fournisseurs_par_matricule:
                fournisseurs_par_matricule[mat] = s.supplier_name
        refs_attachees = set()
        for f in frappe.get_all("File", filters={"attached_to_doctype": "Purchase Invoice",
                                                 "file_name": ["like", "certificat_ras_%"]},
                                fields=["file_name"]):
            refs_attachees.add(
                (f.file_name or "").replace("certificat_ras_", "").rsplit(".pdf", 1)[0])
        for c in orphelins_tej(export, cles_locales, depuis, emis.ETATS_VIVANTS,
                               frozenset(refs_attachees)):
            # Le nom : la fiche fournisseur locale d'abord, sinon la raison sociale que
            # l'export du portail porte deja — plus d'orphelin anonyme.
            orphelins.append({**c, "fournisseur": fournisseurs_par_matricule.get(
                c.get("beneficiaire") or "", "") or (c.get("beneficiaire_nom") or "")})
    compte["tej_orphelins"] = len(orphelins)
    suggestions = rapprochements_suggeres(orphelins, lignes)

    # QUAND l'export a ete genere : c'est la reponse a « pourquoi je ne vois pas le certificat
    # que je viens de creer sur le portail » — le tableau lit un fichier, pas le portail.
    export_genere_le = ""
    if export_disponible:
        try:
            export_genere_le = str(emis.date_export() or "")
        except Exception:
            pass

    return {"depuis": depuis, "seuil": seuil, "export_disponible": export_disponible,
            "export_genere_le": export_genere_le,
            "lignes": lignes, "totaux": totaux, "compte": compte, "orphelins": orphelins,
            "suggestions": suggestions}


@frappe.whitelist()
def rafraichir_export():
    """Regenere l'export des certificats emis sur le portail TEJ. -> {certificats}.

    Le recap lit le DERNIER EXPORT que le service detient : un certificat cree a la main sur le
    portail n'y figure qu'apres regeneration — qui n'arrivait qu'a la prochaine soumission
    reelle. Ce bouton la demande explicitement : UN job Playwright (~1 min), le meme cout que le
    📜 d'un orphelin, sur le worker unique du service — c'est pour cela qu'elle reste un geste
    et non le comportement par defaut de l'ecran.
    """
    frappe.only_for(["System Manager", "Accounts Manager", "Accounts User", "Purchase Manager",
                     "Purchase User"])
    from bank_retenue_sync.tej import emis
    return {"certificats": len(emis.certificats_emis(rafraichir=True))}
