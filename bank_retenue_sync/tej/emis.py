"""Emettre sur TEJ le certificat de la retenue prelevee sur une facture d'achat.

L'AUTRE SENS DU FLUX, ET IL N'EXISTAIT PAS
------------------------------------------
Tout le module `tej` lit des certificats RECUS : ceux que nos clients emettent quand ils nous
retiennent 1 %. Ici c'est l'inverse — nous retenons 1 % a un fournisseur local, et c'est A NOUS
de le declarer sur le portail et de lui remettre son certificat. Sans ce geste, le fournisseur ne
peut pas imputer la retenue, et la somme retenue reste due au Tresor sans preuve de versement.

Le service sait le faire (`POST /jobs/tej/certificats-emis`), l'app ne le lui avait jamais demande.

⚠️ LA REPETITION D'ABORD, LA SOUMISSION ENSUITE — ET C'EST LE SERVICE QUI L'IMPOSE. `dry_run` vaut
`true` par defaut cote TEJ : le job remplit tout le formulaire, RELEVE LES MONTANTS QUE LE PORTAIL
CALCULE, et s'arrete avant « Valider ». On s'en sert comme d'un controle : si la retenue calculee
par TEJ ne tombe pas sur celle que porte la facture, il y a desaccord entre notre comptabilite et
l'administration, et personne ne doit soumettre avant de savoir pourquoi.

⚠️ CE QUI EST ENVOYE EST DECLARATIF ET IRREVERSIBLE. Un certificat soumis se lit chez le
fournisseur et chez l'administration ; l'annuler laisse une trace (l'export en montre deux a
l'etat « ANNULE »). Rien ne part donc sans un `dry_run=False` explicite, demande a l'ecran.

⚠️ LE PDF EST LA MEMOIRE — POUR UN CERTIFICAT, ET SEULEMENT POUR LUI. On ne stocke la reference du
certificat nulle part ailleurs que dans le NOM du PDF attache a la facture
(`certificat_ras_<reference>.pdf`, cf. `tej/pdf.py`) : pas de champ a migrer, et le justificatif et
sa trace ne peuvent pas diverger.

⚠️ MAIS UN DEPOT N'A NI REFERENCE NI PDF, DONC RIEN NE PEUT LE PORTER. « Valider » n'emet pas un
certificat : TEJ enregistre un DEPOT qu'il analyse ensuite, et le certificat n'existe qu'apres.
Entre les deux, la declaration EST PARTIE et cette doctrine ne sait rien en dire — c'est pour ce
seul cas qu'existe `BRS Depot TEJ` (cf. `tej/depot.py`).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate

from bank_retenue_sync.achat import regles
from bank_retenue_sync.tej import depot as M_depot
from bank_retenue_sync.tej import matricule, pdf

ROUTE_CREER = "/jobs/tej/certificats-emis"
ROUTE_JOB_PDF_EMIS = "/jobs/tej/certificats-emis/pdf"
ROUTE_JOB_EXPORT = "/jobs/tej/certificats-emis/export"
ROUTE_EXPORT = "/tej/certificats-emis/export/latest"
#: Liste des exports, avec leur date de GENERATION — la seule mesure de ce que le controle voit.
ROUTE_LISTE_EXPORT = "/tej/certificats-emis/export"

# Un certificat ANNULE ne bloque pas une nouvelle emission : c'est justement pour cela qu'on
# l'annule. Seul un certificat vivant interdit d'en refaire un.
ETATS_VIVANTS = ("REÇUE", "RECUE", "EN COURS", "VALIDÉE", "VALIDEE")

# Delai large : le job pilote un navigateur sur le portail, formulaire compris.
DELAI_JOB = 900


def _reglage(champ, defaut=None):
    try:
        v = frappe.db.get_single_value("Bank Retenue Sync Settings", champ)
        return defaut if v in (None, "") else v
    except Exception:
        return defaut


def type_operation() -> str:
    return (_reglage("tej_emis_type_operation", "") or "").strip()


def operation() -> str:
    return (_reglage("tej_emis_operation", "") or "").strip()


def _taux_tva(doc) -> float | None:
    """Le taux de TVA de la facture, ou None si elle en porte plusieurs.

    TEJ ne prend qu'UN taux par operation. Une facture a 19 % et 7 % ne se declare donc pas d'un
    bloc — mieux vaut refuser que d'en choisir un au hasard et sous-declarer.
    """
    lignes = [t for t in (doc.get("taxes") or [])
              if (t.add_deduct_tax or "Add") == "Add" and regles.MOT_TVA in (t.account_head or "")]
    if not lignes:
        return None
    taux = set()
    for t in lignes:
        # Le taux se lit sur le compte (« TVA 19% - A&S »), pas sur un ratio calcule : le ratio
        # derape des que la facture porte une ligne exoneree.
        chiffres = "".join(c for c in (t.account_head or "") if c.isdigit())
        if chiffres:
            taux.add(int(chiffres))
    return float(next(iter(taux))) if len(taux) == 1 else None


def contexte(facture: str) -> dict:
    """Tout ce qu'il faut savoir avant d'emettre, et ce qui manque encore. -> dict.

    Ne parle a personne : ni au portail, ni au service. Sert au bouton pour dire d'avance ce qui
    bloquerait, comme le fait `achat.facture.diagnostic` avant une validation.
    """
    doc = frappe.get_doc("Purchase Invoice", facture)
    lignes = [{"account_head": t.account_head, "tax_amount": t.tax_amount,
               "add_deduct_tax": t.add_deduct_tax} for t in (doc.get("taxes") or [])]
    ras = regles.retenue_saisie(lignes)
    tva = _taux_tva(doc)
    tax_id = frappe.db.get_value("Supplier", doc.supplier, "tax_id")
    deja = _certificat_attache(facture)

    manques = []
    if doc.docstatus != 1:
        manques.append(_("la facture n'est pas validée"))
    if not regles.est_local(frappe.db.get_value("Supplier", doc.supplier, "country")):
        manques.append(_("le fournisseur n'est pas tunisien : aucune retenue à déclarer"))
    if ras <= 0:
        manques.append(_("aucune retenue à la source sur cette facture"))
    if not matricule.normaliser(tax_id):
        manques.append(_("le fournisseur {0} n'a pas de matricule fiscal exploitable — "
                         "renseignez-le sur sa fiche").format(doc.supplier))
    if not doc.bill_no:
        manques.append(_("le n° de facture fournisseur est vide : c'est lui que le portail "
                         "attend comme « numéro chez le déclarant »"))
    if tva is None:
        manques.append(_("le taux de TVA n'est pas unique sur cette facture : TEJ ne prend "
                         "qu'un taux par opération"))
    if not type_operation() or not operation():
        manques.append(_("le type et le libellé d'opération TEJ ne sont pas réglés "
                         "(Réglages Bank Retenue Sync)"))

    return {
        "facture": facture,
        "fournisseur": doc.supplier,
        "fournisseur_nom": doc.supplier_name,
        "matricule": matricule.normaliser(tax_id),
        "matricule_saisi": tax_id or "",
        "bill_no": doc.bill_no or "",
        "date_paiement": str(doc.posting_date or ""),
        "montant_ht": flt(doc.net_total, 3),
        "taux_tva": tva,
        "retenue_facture": ras,
        "exercice": getdate(doc.posting_date).year if doc.posting_date else None,
        "deja_emis": deja,
        "manques": manques,
    }


def nom_de_certificat_manuel(nom) -> bool:
    """Ce nom de fichier annonce-t-il un certificat de retenue attache A LA MAIN ? Pure.

    ⚠️ LA BARRIERE NE VOYAIT QUE SA PROPRE CONVENTION (`certificat_ras_*`), ET C'EST UN ANGLE
    MORT REEL : au 26/08/2026, TREIZE factures 2026 portent un certificat attache a la main
    (« Retenue à la source -JEGHAM... ») — l'ere papier, d'avant le portail obligatoire (04/2026).
    Ces certificats-la ne figurent NI dans le nom attendu, NI dans l'export du portail : rien ne
    barrait une seconde declaration de la meme retenue. Meme doctrine que le sens vente : le scan
    attache a la facture prouve la retenue.

    « retenue » + « source » dans un .pdf : couvre les deux graphies (avec et sans accents) sans
    dependre de l'ordre des mots. Un scan de facture nomme ainsi par erreur bloquerait l'emission
    — se corrige en renommant la piece, quand une double declaration ne se corrige pas.
    """
    n = (nom or "").lower().strip()
    return n.endswith(".pdf") and "retenue" in n and "source" in n


def _certificat_manuel(facture: str):
    """Le certificat attache A LA MAIN a cette facture, ou None."""
    for f in frappe.db.get_all("File", filters={"attached_to_doctype": "Purchase Invoice",
                                                "attached_to_name": facture},
                               fields=["file_name", "file_url"], order_by="creation"):
        if nom_de_certificat_manuel(f.file_name):
            return {"reference": None, "file_url": f.file_url, "file_name": f.file_name,
                    "manuel": True}
    return None


def _certificat_attache(facture: str):
    """Le certificat deja attache a cette facture, ou None. La reference vit dans le nom.

    Deux provenances : le PDF telecharge par ce module (`certificat_ras_<reference>.pdf`), et le
    certificat attache a la main (reference inconnue — `manuel: True`). Les deux prouvent la meme
    chose : la retenue est deja certifiee, il n'y a rien a soumettre."""
    f = frappe.db.get_value("File", {"attached_to_doctype": "Purchase Invoice",
                                     "attached_to_name": facture,
                                     "file_name": ["like", "certificat_ras_%"]},
                            ["file_name", "file_url"], as_dict=1)
    if not f:
        return _certificat_manuel(facture)
    ref = (f.file_name or "").replace("certificat_ras_", "").rsplit(".pdf", 1)[0]
    return {"reference": ref, "file_url": f.file_url, "file_name": f.file_name}


def certificats_emis(rafraichir: bool = False) -> list:
    """Les certificats DEJA EMIS, lus chez TEJ. -> [{numero, beneficiaire, etat, reference}].

    ⚠️ L'EXPORT VIEILLIT, ET UN EXPORT PERIME NE PROUVE RIEN. Celui que le service detient datait
    de trois semaines : un certificat emis entre-temps n'y figure pas, et le controle laisserait
    passer un doublon en jurant qu'il n'y en a pas. `rafraichir` relance le scraping avant de lire
    — c'est lent (une session sur le portail), donc reserve au moment qui compte : juste avant une
    soumission reelle.
    """
    import io

    import openpyxl
    import requests

    from bank_retenue_sync.bank import movements
    from bank_retenue_sync.bank.movements import _base_url, _headers

    if rafraichir:
        movements.wait_job(movements.start_job(ROUTE_JOB_EXPORT), timeout=DELAI_JOB)
    r = requests.get(_base_url() + ROUTE_EXPORT, headers=_headers(), timeout=120)
    r.raise_for_status()
    ws = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True).active
    lignes = list(ws.iter_rows(values_only=True))
    if not lignes:
        return []
    entete = [str(c or "").strip() for c in lignes[0]]

    def col(*noms):
        for n in noms:
            if n in entete:
                return entete.index(n)
        return None

    i_num, i_ben = col("Numéro chez le déclarant"), col("Identifiant du bénéficiaire")
    i_etat, i_ref = col("État", "Etat"), col("Référence de certificat")
    i_cree, i_dp = col("Date de création", "Date de creation"), col("Date de paiement")
    out = []
    for l in lignes[1:]:
        out.append({"numero": str(l[i_num] or "").strip() if i_num is not None else "",
                    "beneficiaire": matricule.normaliser(l[i_ben]) if i_ben is not None else "",
                    "etat": str(l[i_etat] or "").strip().upper() if i_etat is not None else "",
                    "reference": str(l[i_ref] or "").strip() if i_ref is not None else "",
                    "cree": str(l[i_cree] or "").strip() if i_cree is not None else "",
                    "date_paiement": str(l[i_dp] or "").strip() if i_dp is not None else ""})
    return out


#: Au-dela de ce delai, un export ne prouve plus rien : un certificat emis entre-temps n'y
#: figurerait pas. Une soumission reelle regenere l'export juste avant de controler, il a donc
#: quelques secondes ; une repetition lit celui qui traine, souvent vieux de plusieurs jours.
FRAICHEUR_EXPORT_MIN = 15


def derniere_emission(certs=None):
    """La date du certificat le plus recent que porte l'export. -> date | None.

    ⚠️ CE N'EST PAS UNE MESURE DE COUVERTURE, ET LA CONFONDRE AVEC UNE MESURE DE COUVERTURE FAIT
    CRIER LE GARDE-FOU EN PERMANENCE. Cette date dit quand on a emis pour la derniere fois, rien
    de plus. L'export du portail est TOUJOURS complet — son contrat le precise, et un certificat
    genere a l'instant y figure — donc une date ancienne signifie « rien n'a ete emis depuis »,
    pas « l'export s'arrete la ». Le 15/08/2026, cette lecture faisait alerter sur toute facture
    posterieure au 20/07, c'est-a-dire sur toutes.
    """
    from datetime import datetime

    dates = []
    for c in (certs if certs is not None else certificats_emis()):
        for v in (c.get("cree"), c.get("date_paiement")):
            try:
                dates.append(datetime.strptime(str(v).strip(), "%d-%m-%Y").date())
            except Exception:
                continue
    return max(dates) if dates else None


def date_export():
    """Quand le fichier d'export a ete GENERE. -> datetime | None.

    C'est la seule mesure honnete de ce que le controle anti-doublon peut voir : tout certificat
    emis apres cette date est invisible, quelle que soit la periode que couvrent les lignes.
    """
    from datetime import datetime

    import requests

    from bank_retenue_sync.bank.movements import _base_url, _headers

    try:
        r = requests.get(_base_url() + ROUTE_LISTE_EXPORT, headers=_headers(), timeout=60)
        r.raise_for_status()
        fichiers = r.json() or []
    except Exception:
        return None
    dates = []
    for f in fichiers:
        brut = (f or {}).get("modified")
        if not brut:
            continue
        try:
            dates.append(datetime.fromisoformat(str(brut)))
        except ValueError:
            continue
    return max(dates) if dates else None


def est_aveugle(genere_le, maintenant, marge_minutes: int = FRAICHEUR_EXPORT_MIN) -> bool:
    """Le controle anti-doublon a-t-il un angle mort ? Fonction pure.

    Sans date de generation, on ne sait pas : on repond oui, parce qu'un garde-fou qui ne sait pas
    doit le dire plutot que rassurer.
    """
    if not genere_le or not maintenant:
        return True
    return (maintenant - genere_le).total_seconds() > marge_minutes * 60


def deja_chez_tej(numero: str, mat: str, rafraichir: bool = False):
    """Le certificat vivant qui porte deja ce numero pour ce beneficiaire, ou None.

    ⚠️ LA CLE EST LE COUPLE, PAS LE NUMERO SEUL. « 108 » ou « 30/2026 » sont des numeros de
    facture fournisseur : deux fournisseurs differents en emettent chaque annee. Croiser le
    numero avec le matricule du beneficiaire est ce qui distingue un vrai doublon d'une
    homonymie de numerotation.
    """
    cible_num = (numero or "").strip()
    cible_mat = matricule.normaliser(mat)
    if not cible_num or not cible_mat:
        return None
    for c in certificats_emis(rafraichir=rafraichir):
        if c["numero"] == cible_num and c["beneficiaire"] == cible_mat \
                and c["etat"] in ETATS_VIVANTS:
            return c
    return None


def charge_utile(ctx: dict, date_paiement=None) -> dict:
    """Le corps envoye au service. Pure : c'est elle qu'on relit avant de soumettre."""
    return {
        "beneficiaire": {"type_identifiant": "Matricule fiscal",
                         "identifiant": ctx["matricule"]},
        "date_paiement": str(date_paiement or ctx["date_paiement"]),
        "numero_chez_declarant": ctx["bill_no"],
        "operations": [{
            "exercice": ctx["exercice"],
            "type_operation": type_operation(),
            "operation": operation(),
            "prise_en_charge": False,
            "convention": False,
            "montant_ht": ctx["montant_ht"],
            "taux_tva": int(ctx["taux_tva"]),
        }],
    }


def cle_idempotence(ctx: dict, dry_run: bool):
    """La cle d'idempotence — pour la SOUMISSION seulement. -> str | None.

    ⚠️ LA REPETITION NE DOIT PAS PORTER LA MEME CLE QUE LA SOUMISSION, ET CE FUT UNE PANNE REELLE.
    Le service honore la cle : meme cle, meme job, aucun second passage — verifie. Avec une cle
    commune aux deux, la sequence normale (repeter, puis soumettre) faisait rendre au « soumettre »
    le job de la repetition, deja termine en `dry_run` : reponse `submitted: false`, aucune
    reference, et RIEN de declare — alors que l'ecran annonçait un certificat emis. Le
    13/08/2026 a 13:51, c'est exactement ce qui s'est produit sur ACC-PINV-2026-00093.

    ⚠️ ET UNE REPETITION N'A PAS A ETRE IDEMPOTENTE. On repete justement pour reessayer apres
    avoir change quelque chose ; une cle la figerait sur son premier resultat. Elle part donc sans
    cle. Seule la soumission — le geste qu'on ne veut pas voir se produire deux fois — en porte une.
    """
    return None if dry_run else "PINV-%s" % ctx["facture"]


def emettre(facture: str, dry_run: bool = True, date_paiement=None,
            depot_reserve: str = None) -> dict:
    """Repete (dry_run) ou soumet (dry_run=False) le certificat sur TEJ. -> dict.

    `depot_reserve` : le nom de la ligne `BRS Depot TEJ` que la tache de fond a posee pour
    elle-meme avant de lancer l'appel. Elle est ignoree par la barriere anti-doublon — sans quoi
    la soumission se bloquerait sur sa propre reservation — et c'est elle qui sera complétée au
    retour, plutot que d'en creer une seconde.
    """
    from bank_retenue_sync.bank import movements

    ctx = contexte(facture)
    if ctx["manques"]:
        return {"statut": "impossible", **ctx}
    if ctx["deja_emis"]:
        return {"statut": "deja emis", **ctx}

    # ⚠️ QUATRIEME BARRIERE, ET LA SEULE QUI VOIE L'ANGLE MORT DES TROIS AUTRES. Un depot en
    # analyse n'a pas de PDF (donc la premiere ne le voit pas), il est passe par une cle
    # d'idempotence dont la fenetre est expiree (la deuxieme non plus), et il ne figure pas dans
    # l'export des certificats EMIS puisque aucun certificat n'existe encore (la troisieme non
    # plus). Il est pourtant deja chez l'administration fiscale : le rejouer, c'est declarer deux
    # fois. Meme une repetition est refusee — remplir le formulaire d'un certificat en cours de
    # depot n'apprend rien et invite au geste de trop.
    en_cours = M_depot.en_cours(facture, sauf=depot_reserve)
    if en_cours:
        return {"statut": "depot en analyse", "depot": M_depot.vue(en_cours), **ctx}

    # ⚠️ TROISIEME BARRIERE, ET LA SEULE QUI FASSE FOI. Les deux premieres sont locales — le PDF
    # attache a la facture, la cle d'idempotence du service — et toutes deux ont un angle mort :
    # si la soumission reussit mais que le PDF ne se telecharge pas, la facture ne porte RIEN
    # alors que le certificat existe chez TEJ, et le bouton reproposerait de l'emettre. Ici on
    # demande au portail lui-meme, et on RAFRAICHIT avant une soumission reelle : un export vieux
    # de trois semaines ne prouve rien sur ce qui a ete emis hier.
    certs = certificats_emis(rafraichir=not dry_run)
    doublon = next((c for c in certs
                    if c["numero"] == (ctx["bill_no"] or "").strip()
                    and c["beneficiaire"] == ctx["matricule"]
                    and c["etat"] in ETATS_VIVANTS), None)
    if doublon:
        return {"statut": "deja chez tej", "doublon": doublon, **ctx}
    # ⚠️ « PAS TROUVE » N'EST PAS « PAS DE DOUBLON » — MAIS LA QUESTION EST QUAND L'EXPORT A ETE
    # GENERE, PAS CE QU'IL CONTIENT. L'export du portail est toujours complet ; ce qu'il ignore,
    # c'est ce qui a ete emis DEPUIS sa generation. Comparer la date de ses lignes a celle de la
    # facture faisait alerter des qu'aucune emission n'etait recente, c'est-a-dire presque
    # toujours — un garde-fou qui crie sans cesse ne garde plus rien.
    ctx["derniere_emission"] = str(derniere_emission(certs) or "")
    genere = date_export()
    ctx["export_genere_le"] = str(genere or "")
    ctx["controle_aveugle"] = est_aveugle(genere, frappe.utils.now_datetime())

    corps = dict(charge_utile(ctx, date_paiement), dry_run=bool(dry_run),
                 idempotency_key=cle_idempotence(ctx, dry_run))
    job = movements.start_job(ROUTE_CREER, corps)
    # ⚠️ L'IDENTIFIANT DU JOB EN BASE TOUT DE SUITE, PAS AU RETOUR. C'est lui qui donne acces a la
    # progression du service (`progress.pct` / `progress.step`) ET au corps du job en cas
    # d'echec. L'enregistrer seulement au succes, c'est le perdre exactement quand il sert :
    # devant un job mort a 95 %, sans son identifiant, on ne peut plus rien savoir.
    if depot_reserve:
        frappe.db.set_value(M_depot.DOCTYPE, depot_reserve, "job_creation", job,
                            update_modified=False)
        frappe.db.commit()
    resultat = movements.wait_job(job, timeout=DELAI_JOB) or {}
    lu = resultat.get("result") or resultat.get("data") or resultat
    calcul = montants_calcules(lu)
    reference = _reference_creee(lu)
    commun = {"envoye": corps, "job": job, "reponse": lu, "calcule": calcul,
              "ecart": (round(calcul["retenue"] - ctx["retenue_facture"], 3)
                        if calcul.get("retenue") is not None else None),
              "reference": reference, **ctx}

    if dry_run:
        return {"statut": "repetition", **commun}

    # ⚠️ « LE JOB A REUSSI » NE VEUT PAS DIRE « LE CERTIFICAT EXISTE » — MAIS PAS NON PLUS
    # « RIEN N'EST PARTI ». Le service distingue trois issues dans `cert_create.statut`, et c'est
    # LUI qui les nomme ; les deduire de la seule presence d'une `reference` faisait conclure
    # « non soumis » sur `en_analyse`, qui est le cas NOMINAL — l'ecran annonçait alors « aucun
    # certificat n'a ete cree » alors que le depot existait deja chez le fisc, et invitait au
    # second clic qui declare en double.
    lecture = M_depot.lire_creation(lu)
    commun["creation"] = lecture
    commun["reference"] = lecture["reference"] or reference

    if lecture["statut"] == M_depot.EN_ANALYSE:
        # Le numero de depot est le fait le plus couteux a perdre : en base AVANT tout le reste.
        nom = _persister(ctx, lecture, job, depot_reserve)
        return {"statut": "depot en analyse",
                "depot": M_depot.vue(frappe.get_doc(M_depot.DOCTYPE, nom)), **commun}

    if lecture["statut"] == M_depot.GENERE and lecture["reference"]:
        # Trace du depot meme quand TEJ a analyse tout de suite : l'historique de ce qui est parti
        # au fisc ne doit pas dependre de la vitesse de son analyse.
        _persister(ctx, lecture, job, depot_reserve)
        return {"statut": "soumis", **commun}

    # Ni reference, ni depot, ni statut connu : on ne sait pas. C'est different de « rien n'est
    # parti » — et le dire ainsi est la seule reponse honnete. L'export tranchera.
    if lecture["submitted"] is False and not lecture["depot_numero"]:
        return {"statut": "non soumis", **commun}
    if not commun["reference"]:
        return {"statut": "incertain", **commun}
    return {"statut": "soumis", **commun}


#: Ce que dit le service quand le PORTAIL a refuse la saisie. Un refus n'est pas une panne : TEJ
#: a tranche, rien n'est parti, et la facture n'a pas a rester bloquee sur un doute inexistant.
MARQUEURS_REFUS = ("soumission refusee par tej", "soumission refusée par tej")


def est_un_refus(message) -> bool:
    """Le portail a-t-il explicitement refuse ? Fonction pure.

    ⚠️ ON NE DEVINE PAS UN REFUS, ON LE RECONNAIT A CE QUE LE SERVICE DIT. Toute autre erreur —
    reseau coupe, worker tue, timeout — laisse planer le doute : le clic « Valider » a pu aboutir
    avant la panne. Seul un refus nomme autorise a conclure que rien n'a ete declare.
    """
    texte = str(message or "").lower()
    return any(m in texte for m in MARQUEURS_REFUS)


def progression(depot_ligne) -> dict:
    """Ou en est le job de creation, cote service. -> {pct, step, statut, erreur}.

    Le service expose `progress.step` et `progress.pct` sur `GET /jobs/{id}` : c'est la seule
    reponse honnete a « ou ça en est ? » pendant les minutes ou le navigateur pilote le portail.
    """
    import requests

    from bank_retenue_sync.bank.movements import _base_url, _headers

    job = (depot_ligne.get("job_creation") if isinstance(depot_ligne, dict)
           else depot_ligne.job_creation)
    if not job:
        return {"job": None, "pct": None, "step": "", "statut": "", "erreur": ""}
    try:
        r = requests.get(_base_url() + "/jobs/%s" % job, headers=_headers(), timeout=30)
        r.raise_for_status()
        j = r.json() or {}
    except Exception as e:
        return {"job": job, "pct": None, "step": "", "statut": "",
                "erreur": "progression illisible : %s" % str(e)[:150]}
    prog = j.get("progress") or {}
    return {"job": job, "pct": prog.get("pct"), "step": prog.get("step") or "",
            "statut": j.get("status") or "", "erreur": str(j.get("error") or "")[:300]}


def _persister(ctx: dict, lecture: dict, job: str, depot_reserve: str = None) -> str:
    """Complete la ligne reservee, ou en cree une. -> nom du document."""
    if depot_reserve:
        M_depot.completer(depot_reserve, lecture, job)
        return depot_reserve
    return M_depot.enregistrer(ctx, lecture, job)


def _nombre_tej(valeur):
    """« 1 099.203 » ou « 1 500,000 » -> float. None si illisible. Fonction pure.

    ⚠️ LE PORTAIL REND DES CHAINES, PAS DES NOMBRES, et il separe les milliers par une ESPACE
    INSECABLE. Un `float()` direct leve, un `or` avale l'erreur, et le controle qui devait
    confronter les deux montants passe alors pour « le portail n'a rien renvoye » — c'est
    exactement ce qui s'est produit devant l'utilisateur, avec le montant affiche juste en dessous.
    """
    if valeur is None:
        return None
    texte = str(valeur).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return round(float(texte), 3)
    except (TypeError, ValueError):
        return None


def montants_calcules(reponse) -> dict:
    """Ce que TEJ a calcule, extrait de la reponse du job. -> {taux, tva, ttc, retenue, net}.

    Le chemin est `result.cert_create.operations[0].computed` : le service rend le job entier, et
    les montants du portail vivent au fond. Une seule operation par certificat ici — s'il y en
    avait plusieurs, on sommerait les retenues.
    """
    ops = (((reponse or {}).get("cert_create") or {}).get("operations")) or []
    calculs = [(o or {}).get("computed") or {} for o in ops]
    if not calculs:
        return {}
    retenues = [_nombre_tej(c.get("mantantRs")) for c in calculs]
    retenues = [r for r in retenues if r is not None]
    premier = calculs[0]
    return {
        "taux": _nombre_tej(premier.get("tauxRs")),
        "tva": _nombre_tej(premier.get("mantantTva")),
        "ttc": _nombre_tej(premier.get("mantantTTC")),
        "net": _nombre_tej(premier.get("mantantNet")),
        "retenue": round(sum(retenues), 3) if retenues else None,
    }


def _reference_creee(reponse):
    """La reference du certificat cree, quand la soumission est reelle."""
    cc = (reponse or {}).get("cert_create") or {}
    return cc.get("reference") or (reponse or {}).get("reference")


def attacher_pdf(facture: str, reference: str) -> dict:
    """Telecharge le certificat emis et le pose sur la facture d'achat. -> dict.

    Meme mecanique que pour les certificats recus (`tej/pdf.py`) : nom stable, attachement
    idempotent — un justificatif en double au controle fiscal est un probleme, pas un detail.
    """
    from bank_retenue_sync.bank import movements

    existant = _certificat_attache(facture)
    if existant:
        return {"statut": "deja attache", **existant}

    job = movements.start_job(ROUTE_JOB_PDF_EMIS, {"reference": reference})
    movements.wait_job(job, timeout=DELAI_JOB)
    contenu = _telecharger(reference)
    res = pdf.attacher({"reference": reference}, contenu, "Purchase Invoice", facture)
    return {"reference": reference, **res}


def _texte_bytes(contenu: bytes):
    """Le texte d'un PDF recu en memoire, ou None s'il n'en rend aucun."""
    import io

    try:
        from pypdf import PdfReader

        lecteur = PdfReader(io.BytesIO(contenu))
        return "\n".join((p.extract_text() or "") for p in lecteur.pages)
    except Exception:
        return None


@frappe.whitelist()
def verifier_concordance(facture, reference):
    """Le certificat ATTACHE a la facture et celui du PORTAIL (reference) sont-ils le MEME ? -> dict.

    Le cas qu'on verifie : un certificat du portail suggere vers une facture qui porte DEJA un
    certificat. Si c'est le meme document (numero divergent, rien de grave), tout va bien ; si
    c'en est un AUTRE, la meme retenue a ete declaree deux fois au fisc.

    Trois niveaux de preuve :
    - reference connue des deux cotes -> egalite des references, imparable ;
    - certificat manuel -> comparaison du TEXTE des deux PDF (les octets ne servent a rien : le
      portail regenere le fichier a chaque demande, cf. pdf.textes_concordent) ;
    - un scan sans couche texte ne prouve rien -> « inverifiable », avec les deux liens pour
      trancher a l'oeil.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    from bank_retenue_sync.bank import movements

    atteste = _certificat_attache(facture)
    if not atteste:
        return {"verdict": "aucun", "message": _("Aucun certificat n'est attaché à {0} : rien à "
                                                 "comparer.").format(facture)}
    if atteste.get("reference"):
        # Par PREFIXE, pas par egalite : Frappe suffixe le nom de fichier en cas d'homonyme
        # (certificat_ras_<ref>dce363.pdf) et la reference relue porte ce suffixe — une egalite
        # stricte declarerait « different » un certificat qu'on vient soi-meme d'attacher.
        if atteste["reference"].startswith(reference) or reference.startswith(atteste["reference"]):
            return {"verdict": "meme", "message": _("Même certificat : la référence attachée est "
                                                    "identique ({0}).").format(reference)}
        return {"verdict": "different",
                "message": _("DEUX certificats différents : la facture porte {0}, le portail "
                             "propose {1} — double déclaration probable, à trancher sur le "
                             "portail.").format(atteste["reference"], reference)}

    texte_local = pdf.texte_pdf(atteste.get("file_url"))
    if texte_local is None:
        return {"verdict": "inverifiable", "file_url": atteste.get("file_url"),
                "message": _("Le PDF attaché ne rend aucun texte (scan) : comparez à l'œil — "
                             "certificat attaché : {0}.").format(atteste.get("file_name"))}

    job = movements.start_job(ROUTE_JOB_PDF_EMIS, {"reference": reference})
    movements.wait_job(job, timeout=DELAI_JOB)
    contenu = _telecharger(reference)
    texte_portail = _texte_bytes(contenu)
    if texte_portail is None:
        return {"verdict": "inverifiable",
                "message": _("Le PDF du portail ({0}) ne rend aucun texte : comparez à "
                             "l'œil.").format(reference)}
    if pdf.textes_concordent(texte_local, texte_portail):
        # ⚠️ LE VERDICT SE MEMORISE, ET LE PDF EST LA MEMOIRE. Prouver deux fois la meme paire
        # coute un job de generation a chaque fois, et l'orphelin resterait affiche a jamais.
        # Le PDF du portail — deja telecharge pour la comparaison — est attache sous son nom
        # officiel : la reference attachee sort le certificat des orphelins, et toute
        # verification future se tranche par simple egalite de references.
        pdf.attacher({"reference": reference}, contenu, "Purchase Invoice", facture)
        frappe.db.commit()
        return {"verdict": "meme",
                "message": _("Même document : texte identique au caractère près. Le certificat "
                             "du portail a été attaché sous son nom officiel ({0}) — cette paire "
                             "ne s'affichera plus.").format(reference)}
    return {"verdict": "different",
            "message": _("Documents DIFFÉRENTS ({0} contre {1} caractères) : double déclaration "
                         "probable, à trancher sur le portail.").format(
                             len(texte_local), len(texte_portail))}


@frappe.whitelist()
def verifier_concordances(paires):
    """Le bouton « Tout verifier » : chaque paire suggeree, en sequence. -> [dict].

    Sequentiel a dessein : chaque verification genere un PDF sur le portail via le worker unique
    du service — paralleliser reviendrait a se faire la queue a soi-meme. Une paire en erreur
    n'arrete pas les suivantes.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    paires = frappe.parse_json(paires) if isinstance(paires, str) else paires
    out = []
    for paire in paires or []:
        try:
            res = verifier_concordance(paire.get("facture"), paire.get("reference"))
            out.append({"facture": paire.get("facture"), "reference": paire.get("reference"),
                        **res})
        except Exception as e:
            frappe.clear_last_message()
            out.append({"facture": paire.get("facture"), "reference": paire.get("reference"),
                        "verdict": "erreur", "message": str(e)[:200]})
    return out


@frappe.whitelist()
def voir_certificat_orphelin(reference):
    """Genere et rend le PDF d'un certificat du portail SANS l'attacher a une facture. -> dict.

    Pour un certificat SANS facture correspondante, voir le document est le seul moyen de
    comprendre a quoi il correspond (fournisseur, montants, periode) — la ligne du recap n'a
    ni facture ni piece ou cliquer. Le PDF est range en File prive non attache ; s'il a deja
    ete telecharge (meme nom, suffixe d'homonymie compris), on le rend tel quel plutot que de
    refaire un job de generation sur le worker unique."""
    frappe.only_for(["System Manager", "Accounts Manager"])
    from frappe.utils.file_manager import save_file

    from bank_retenue_sync.bank import movements

    existant = frappe.db.get_value("File", {"file_name": ["like", pdf.motif_fichier(reference)]},
                                   "file_url")
    if existant:
        return {"file_url": existant, "statut": "deja telecharge"}

    job = movements.start_job(ROUTE_JOB_PDF_EMIS, {"reference": reference})
    movements.wait_job(job, timeout=DELAI_JOB)
    contenu = _telecharger(reference)
    f = save_file(pdf.nom_fichier(reference), contenu, None, None, is_private=1)
    frappe.db.commit()
    return {"file_url": f.file_url, "statut": "telecharge"}


@frappe.whitelist()
def attacher_certificat(facture, reference):
    """Bouton « Attacher ce certificat » du recap : pose le PDF du portail sur la facture.

    L'attachement EST l'identification : une fois le PDF sur la facture, elle passe
    « Certificat émis ✓ » et le certificat sort de la liste des orphelins."""
    frappe.only_for(["System Manager", "Accounts Manager"])
    res = attacher_pdf(facture, reference)
    frappe.db.commit()
    return res


def _telecharger(reference: str) -> bytes:
    """Le PDF du certificat, une fois le job de generation passe.

    La route de telechargement est la MEME pour les certificats emis et recus
    (`/tej/certificats/{reference}/pdf`) ; seul le job qui les fabrique differe.
    """
    import requests

    from bank_retenue_sync.bank.movements import _base_url, _headers

    r = requests.get(_base_url() + pdf.ROUTE_PDF % reference, headers=_headers(), timeout=120)
    r.raise_for_status()
    if not r.content or not r.content.startswith(b"%PDF"):
        raise ValueError("le service n'a pas rendu un PDF pour %s" % reference)
    return r.content


# ------------------------------------------------------------------ boutons


@frappe.whitelist()
def preparer(facture):
    """Ce que le bouton affiche avant tout appel : la charge utile, et ce qui bloquerait.

    ⚠️ MEMES ROLES QUE `soumettre`, ET C'EST DELIBERE. `Accounts User` etait autorise ici et
    refuse a la soumission : il voyait donc le bouton rouge, preparait tout, et se prenait un
    refus au clic. Un ecran qui propose un geste interdit est pire qu'un ecran qui ne le propose
    pas — ce n'est pas de la lecture, c'est la preparation d'une declaration fiscale.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])
    ctx = contexte(facture)
    ctx["charge_utile"] = charge_utile(ctx) if not ctx["manques"] else None
    # Sans rafraichir : l'ecran doit s'ouvrir tout de suite. Ce qu'on trouve ici est donc un
    # doublon CERTAIN ; ne rien trouver ne prouve rien, et c'est la soumission qui revérifiera
    # sur un export frais.
    if not ctx["manques"]:
        try:
            ctx["deja_chez_tej"] = deja_chez_tej(ctx["bill_no"], ctx["matricule"])
        except Exception:
            ctx["deja_chez_tej"] = None
            # Un frappe.throw en profondeur (jeton indeciffrable apres restore, service coupe) a
            # deja empile son message : sans ce retrait, chaque ouverture de facture montrerait un
            # popup d'erreur pour une simple decoration.
            frappe.clear_last_message()
        # ⚠️ FAIT OBSERVE LE 15/08/2026 (ACC-PINV-2026-00093) : TEJ refuse un contenu identique
        # MEME quand le certificat precedent est ANNULE — l'hypothese « un annule ne bloque pas »
        # est contredite par le portail. On ne bloque pas ici (TEJ tranche proprement, avant tout
        # depot), mais on PREVIENT : cliquer sans changer la date de paiement, c'est un refus
        # assure.
        if not ctx.get("deja_chez_tej"):
            try:
                cible = (ctx["bill_no"] or "").strip()
                ctx["doublon_annule"] = next(
                    (c for c in certificats_emis()
                     if c["numero"] == cible and c["beneficiaire"] == ctx["matricule"]
                     and c["etat"] not in ETATS_VIVANTS), None)
            except Exception:
                ctx["doublon_annule"] = None
                frappe.clear_last_message()
    # Un depot en analyse doit se voir AVANT le premier clic, pas au refus du serveur.
    en_cours = M_depot.en_cours(facture)
    ctx["depot_en_cours"] = M_depot.vue(en_cours) if en_cours else None
    return ctx


@frappe.whitelist()
def etats(factures):
    """L'etat d'emission de plusieurs factures d'un coup. -> {facture: vue}. Pour la vue liste.

    ⚠️ DEUX REQUETES, PAS UNE PAR LIGNE. La liste en affiche vingt a la fois et se rafraichit a
    chaque filtre : tout ce qui coute par ligne s'y multiplie par vingt. Rien n'est demande au
    service TEJ ici — c'est de la lecture locale, et l'ecran doit s'afficher tout de suite.

    Ne dit rien des factures sans depot ni certificat, et c'est deliberé : un badge « rien a
    signaler » sur chaque ligne serait du bruit, et savoir s'il y AURAIT une retenue a declarer
    demanderait de lire la table des taxes de chaque facture.
    """
    # Decoration en lecture seule : on garde le silence plutot que de faire crier la liste a
    # chaque rafraichissement pour un utilisateur qui n'a pas le droit d'en voir le detail.
    if not frappe.has_permission("Purchase Invoice", "read"):
        return {}
    noms = frappe.parse_json(factures) if isinstance(factures, str) else factures
    noms = [n for n in (noms or []) if n]
    if not noms:
        return {}

    out = {}
    # Du plus ANCIEN au plus recent, et le dernier ecrase : une facture refusee puis resoumise
    # porte deux lignes, et seule la derniere dit ou on en est.
    for ligne in frappe.get_all(M_depot.DOCTYPE, filters={"facture": ["in", noms]},
                                fields=["name", "facture", "statut", "numero_depot", "reference",
                                        "soumis_le", "derniere_verification", "verifications",
                                        "message"],
                                order_by="creation asc"):
        out[ligne["facture"]] = M_depot.vue(ligne)

    # ⚠️ LE PDF PRIME SUR TOUTE LIGNE DE DEPOT. Il est la memoire du certificat : s'il est la, le
    # certificat existe, quel qu'ait ete le sort des depots qui l'ont precede. Le certificat
    # attache A LA MAIN (ere papier) vaut preuve au meme titre ; celui du module, nomme
    # `certificat_ras_*`, passe en dernier et l'emporte s'il coexiste avec un manuel.
    for f in frappe.get_all("File", filters={"attached_to_doctype": "Purchase Invoice",
                                             "attached_to_name": ["in", noms]},
                            fields=["attached_to_name", "file_name", "file_url"],
                            order_by="creation"):
        if nom_de_certificat_manuel(f["file_name"]):
            out[f["attached_to_name"]] = {"statut": "emis", "reference": None,
                                          "file_url": f["file_url"],
                                          "message": _("certificat attaché à la main")}
    for f in frappe.get_all("File", filters={"attached_to_doctype": "Purchase Invoice",
                                             "attached_to_name": ["in", noms],
                                             "file_name": ["like", "certificat_ras_%"]},
                            fields=["attached_to_name", "file_name", "file_url"]):
        ref = (f["file_name"] or "").replace("certificat_ras_", "").rsplit(".pdf", 1)[0]
        out[f["attached_to_name"]] = {"statut": "emis", "reference": ref,
                                      "file_url": f["file_url"], "message": ""}
    return out


@frappe.whitelist()
def repeter(facture):
    """Remplit le formulaire TEJ sans le valider, et rend ce que le portail a calculé."""
    frappe.only_for(["System Manager", "Accounts Manager"])
    return emettre(facture, dry_run=True)


@frappe.whitelist()
def soumettre(facture, date_paiement=None):
    """⚠️ LANCE la soumission en tâche de fond et rend la main tout de suite.

    ⚠️ RIEN N'EST DÉCLARÉ DANS CETTE REQUÊTE, ET C'EST DÉLIBÉRÉ. La création pilote un navigateur
    sur le portail — régénération de l'export, puis remplissage du formulaire — sur un service à
    worker unique. Attendre les deux dans la requête desk, c'était bloquer l'écran plusieurs
    minutes et, le plus souvent, se faire couper par le proxy avant toute réponse : la
    déclaration partait, et personne ne le savait.

    La ligne de dépôt est posée AVANT la mise en file. Elle sert de barrière dès le clic, sans
    attendre de savoir ce que le portail répondra — c'est justement entre l'envoi et la réponse
    qu'un second clic coûte le plus cher.
    """
    frappe.only_for(["System Manager", "Accounts Manager"])

    # Les contrôles locaux restent synchrones : ils sont instantanés, et un refus doit se voir
    # tout de suite plutôt que d'arriver par une notification trois minutes plus tard.
    ctx = contexte(facture)
    if ctx["manques"]:
        return {"statut": "impossible", **ctx}
    if ctx["deja_emis"]:
        return {"statut": "deja emis", **ctx}
    en_cours = M_depot.en_cours(facture)
    if en_cours:
        return {"statut": "depot en analyse", "depot": M_depot.vue(en_cours), **ctx}

    nom = M_depot.reserver(ctx)
    frappe.db.commit()
    frappe.enqueue("bank_retenue_sync.tej.emis.executer_soumission",
                   queue="long", timeout=2400, facture=facture,
                   date_paiement=date_paiement, depot_nom=nom)
    return {"statut": "en file",
            "depot": M_depot.vue(frappe.get_doc(M_depot.DOCTYPE, nom)), **ctx}


def executer_soumission(facture, date_paiement=None, depot_nom=None) -> dict:
    """La soumission réelle, hors requête desk. Appelée par `frappe.enqueue`.

    ⚠️ TOUT ÉCHEC LAISSE LA LIGNE `incertain`, JAMAIS LIBRE. Une erreur ici ne prouve pas que
    rien n'est parti : le clic « Valider » a pu aboutir avant que la panne survienne. Rendre la
    facture réémettable serait exactement le geste qui déclare en double — c'est au portail de
    trancher, et l'export le dira.
    """
    try:
        res = emettre(facture, dry_run=False, date_paiement=date_paiement,
                      depot_reserve=depot_nom)
    except Exception as e:
        if depot_nom:
            # ⚠️ UN REFUS DU PORTAIL N'EST PAS UNE PANNE. TEJ a examiné la saisie et l'a rejetée :
            # rien n'est parti, et laisser la facture bloquée sur un `incertain` ferait courir
            # l'utilisateur au portail pour y constater qu'il n'y a rien. Toute AUTRE erreur reste
            # `incertain` — le clic a pu aboutir avant la panne.
            if est_un_refus(e):
                M_depot.marquer(depot_nom, M_depot.REFUSE,
                                "le portail a refusé la saisie : %s" % str(e)[:400])
            else:
                M_depot.marquer(depot_nom, M_depot.INCERTAIN,
                                "la soumission a échoué : %s — vérifier sur le portail avant tout "
                                "nouveau geste" % str(e)[:300])
            frappe.db.commit()
        frappe.log_error(title="Soumission TEJ %s" % facture, message=frappe.get_traceback())
        raise

    # Un refus des barrières locales n'a rien envoyé : la réservation n'a plus lieu d'être.
    if res.get("statut") in ("impossible", "deja emis", "deja chez tej") and depot_nom:
        M_depot.marquer(depot_nom, M_depot.ECHEC,
                        "rien n'a été soumis (%s)" % res.get("statut"))
    elif res.get("statut") == "non soumis" and depot_nom:
        M_depot.marquer(depot_nom, M_depot.ECHEC,
                        "le portail n'a rien enregistré : ni référence, ni dépôt")
    elif res.get("statut") == "incertain" and depot_nom:
        M_depot.marquer(depot_nom, M_depot.INCERTAIN,
                        "réponse inexploitable du service — vérifier sur le portail")
    elif res.get("statut") == "soumis" and res.get("reference"):
        try:
            res["pdf"] = attacher_pdf(facture, res["reference"])
        except Exception as e:
            # Le certificat EXISTE deja chez TEJ : echouer ici ne doit pas laisser croire le
            # contraire. La reference est en base, le PDF se reprendra.
            res["pdf"] = {"statut": "echec", "erreur": str(e)[:200]}
            frappe.log_error(title="PDF du certificat TEJ %s" % facture,
                             message=frappe.get_traceback())
    frappe.db.commit()
    return res


@frappe.whitelist()
def reprendre_pdf(facture, reference):
    """Rattrape un PDF qui n'a pas pu etre telecharge juste apres la soumission."""
    frappe.only_for(["System Manager", "Accounts Manager"])
    res = attacher_pdf(facture, reference)
    frappe.db.commit()
    return res


def suivre_depot(ligne) -> dict:
    """Demande a TEJ ou en est UN depot, et conclut s'il est genere. -> dict.

    ⚠️ RIEN N'EST RESOUMIS ICI. La route de statut est en lecture seule au contrat ; c'est
    precisement ce qui permet de la rappeler autant que necessaire. TEJ analyse ses depots quand
    il veut, et rien de notre cote ne peut l'accelerer.
    """
    nom = ligne["name"] if isinstance(ligne, dict) else ligne.name
    facture = ligne["facture"] if isinstance(ligne, dict) else ligne.facture
    try:
        vu = M_depot.interroger(ligne)
    except Exception as e:
        M_depot.toucher(nom, "suivi impossible : %s" % str(e)[:200])
        return {"depot": nom, "statut": "erreur", "erreur": str(e)[:200]}

    if vu["statut"] == M_depot.GENERE and vu["reference"]:
        M_depot.conclure(nom, M_depot.GENERE, vu["reference"], vu.get("message") or "")
        resultat = {"depot": nom, "statut": M_depot.GENERE, "reference": vu["reference"]}
        # Le PDF suit le certificat, pas le depot : il n'existait pas avant cet instant.
        try:
            resultat["pdf"] = attacher_pdf(facture, vu["reference"])
        except Exception as e:
            resultat["pdf"] = {"statut": "echec", "erreur": str(e)[:200]}
            frappe.log_error(title="PDF du certificat TEJ %s" % facture,
                             message=frappe.get_traceback())
        return resultat

    if vu["statut"] and vu["statut"] not in (M_depot.EN_ANALYSE, M_depot.GENERE):
        M_depot.conclure(nom, vu["statut"], "", vu.get("message") or "")
        return {"depot": nom, "statut": vu["statut"]}

    M_depot.toucher(nom, vu.get("message") or "")
    # Le message du service dit s'il faut attendre ou surtout pas resoumettre : il remonte jusqu'a
    # l'ecran, au lieu de finir dans un champ que personne n'ouvre.
    return {"depot": nom, "statut": M_depot.EN_ANALYSE, "message": vu.get("message") or ""}


def verifier_depots(limite: int = 20) -> dict:
    """Passe en revue les depots que TEJ n'a pas encore analyses. Point d'entree du cron."""
    # ⚠️ D'ABORD LES TACHES DE FOND PERDUES. Un worker tue au mauvais moment laisse une ligne
    # `en_envoi` eternelle, qui bloque la facture sans rien expliquer. On la marque `incertain`
    # SANS la liberer : le portail a peut-etre validé avant la panne, et rendre la facture
    # reemettable sur ce doute serait la pire des reponses.
    perdus = M_depot.perdus()
    for ligne in perdus:
        M_depot.marquer(ligne["name"], M_depot.INCERTAIN,
                        "la tâche de soumission ne s'est jamais conclue — vérifier sur le "
                        "portail TEJ si le dépôt existe avant tout nouveau geste")
        frappe.db.commit()

    ouverts = M_depot.ouverts(limite)
    out = []
    for ligne in ouverts:
        out.append(suivre_depot(ligne))
        frappe.db.commit()
    return {"perdus": len(perdus), "examines": len(ouverts),
            "generes": len([r for r in out if r.get("statut") == M_depot.GENERE]),
            "details": out}


@frappe.whitelist()
def suivre(facture):
    """Le bouton : ou en est le depot de cette facture, maintenant."""
    frappe.only_for(["System Manager", "Accounts Manager"])
    ligne = M_depot.en_cours(facture)
    if not ligne:
        return {"statut": "aucun depot en analyse"}
    # Rien a demander a TEJ tant que la tache de fond n'a pas fini : il n'y a pas encore de depot
    # a suivre, et l'interroger sur un numero qui n'existe pas rendrait un « introuvable » qu'on
    # prendrait pour un refus.
    if ligne.statut == M_depot.EN_ENVOI:
        return {"depot": ligne.name, "statut": M_depot.EN_ENVOI,
                "soumis_le": str(ligne.soumis_le or ""),
                "progression": progression(ligne)}
    res = suivre_depot(ligne)
    frappe.db.commit()
    return res
