"""Le depot TEJ : ce qui existe entre « Valider » et le certificat.

⚠️ « VALIDER » NE CREE PAS UN CERTIFICAT. Le service le dit noir sur blanc dans son contrat :
TEJ enregistre un DEPOT, qu'il analyse ensuite ; le certificat et sa reference n'existent que
lorsque ce depot passe a « Certificats generes ». Entre les deux, la declaration est PARTIE.

⚠️ ET `en_analyse` EST UNE REUSSITE, PAS UN ECHEC. Le job de creation rend `succeeded` avec
`cert_create.statut = "en_analyse"` et `cert_create.depot.numero`. Le code qui deduisait le succes
de la seule presence d'une `reference` concluait donc « non soumis » sur le cas NOMINAL, et
l'ecran annonçait « aucun certificat n'a ete cree » alors que le depot existait chez le fisc.
C'est le mensonge qui produit les doubles declarations.

Le suivi se fait par `POST /jobs/tej/certificats-emis/statut`, appel court et EN LECTURE SEULE —
le contrat precise qu'il ne soumet rien et que le rappeler est sans risque. Le corps a poster est
rendu tout pret par le job de creation dans `cert_create.suivi` : on le conserve verbatim plutot
que de le reconstruire, parce qu'un corps reconstruit pourrait suivre un autre depot que celui
qui vient d'etre cree.
"""
from __future__ import annotations

import json

import frappe

DOCTYPE = "BRS Depot TEJ"

ROUTE_STATUT = "/jobs/tej/certificats-emis/statut"

#: Le suivi est un appel court par construction (le contrat insiste : le worker est unique, une
#: longue attente bloque tous les autres jobs). Inutile de lui laisser les 900 s de la creation.
DELAI_STATUT = 180

EN_ENVOI = "en_envoi"
EN_ANALYSE = "en_analyse"
GENERE = "genere"
INCERTAIN = "incertain"
ECHEC = "echec"
REFUSE = "refuse"
DRY_RUN = "dry_run"

#: Les etats qui interdisent tout nouveau geste sur la facture. `en_envoi` en fait partie AVANT
#: meme de savoir si le portail a repondu : entre le clic et la reponse, une soumission peut avoir
#: abouti sans que rien ne soit encore revenu. Attendre de savoir pour bloquer, c'est laisser
#: grande ouverte la fenetre du double clic.
EN_COURS = (EN_ENVOI, EN_ANALYSE)

#: Au-dela, une tache de fond `en_envoi` est consideree comme perdue. Elle n'est pas liberee pour
#: autant — voir `perdus`.
DELAI_ENVOI_MIN = 45


def lire_creation(reponse) -> dict:
    """Ce que le job de creation a vraiment produit. Fonction pure.

    -> {statut, reference, depot_numero, suivi, submitted}

    ⚠️ ON LIT `cert_create.statut`, ON NE LE DEDUIT PAS. Trois valeurs au contrat — `dry_run`,
    `genere`, `en_analyse` — et elles ne se devinent pas depuis `reference` : un depot en analyse
    n'en a pas encore, ce qui ne veut surtout pas dire que rien n'est parti.

    Repli quand le service ne rend pas `statut` (version anterieure) : une reference vaut
    `genere`, un numero de depot vaut `en_analyse`. Sans l'un ni l'autre, on ne conclut rien et
    on rend `statut = None` — l'appelant traitera l'inconnu comme incertain, jamais comme un echec.
    """
    cc = ((reponse or {}).get("cert_create") or {})
    depot = (cc.get("depot") or {})
    statut = (cc.get("statut") or "").strip() or None
    reference = cc.get("reference") or (reponse or {}).get("reference") or ""
    numero = depot.get("numero") or depot.get("numero_depot") or ""
    if not statut:
        if reference:
            statut = GENERE
        elif numero:
            statut = EN_ANALYSE
    return {
        "statut": statut,
        "reference": str(reference or ""),
        "depot_numero": str(numero or ""),
        "suivi": cc.get("suivi") or None,
        "submitted": cc.get("submitted"),
    }


def lire_statut(reponse) -> dict:
    """Idem pour le job de SUIVI, dont le resultat vit sous `cert_statut`. Fonction pure."""
    cs = ((reponse or {}).get("cert_statut") or {})
    depot = (cs.get("depot") or {})
    statut = (cs.get("statut") or "").strip() or None
    reference = cs.get("reference") or ""
    if not statut and reference:
        statut = GENERE
    return {
        "statut": statut,
        "reference": str(reference or ""),
        "depot_numero": str(depot.get("numero") or depot.get("numero_depot") or ""),
        "message": (cs.get("message") or cs.get("etat") or ""),
    }


def en_cours(facture: str, sauf: str = None, piece_type: str = None):
    """Le depot non conclu de cette piece, ou None. C'est la quatrieme barriere anti-doublon.

    `sauf` : le nom d'une ligne a ignorer — celle que la tache de fond vient de reserver pour
    elle-meme. Sans ce parametre, la soumission se bloquerait sur sa propre reservation.

    `piece_type` : « Purchase Invoice » ou « Journal Entry ». Facultatif — deux pieces de nature
    differente ne portent jamais le meme nom ici — mais le passer rend le filtre exact.
    """
    filtres = {"facture": facture, "statut": ["in", EN_COURS]}
    if piece_type:
        filtres["piece_type"] = piece_type
    if sauf:
        filtres["name"] = ["!=", sauf]
    nom = frappe.db.get_value(DOCTYPE, filtres, "name")
    return frappe.get_doc(DOCTYPE, nom) if nom else None


def vue(ligne) -> dict:
    """Ce qu'un ecran doit savoir d'un depot. -> dict. Accepte `dict` comme `Document`.

    ⚠️ LE MESSAGE DU SERVICE EN FAIT PARTIE, ET IL DORMAIT EN BASE. Le contrat le destine
    explicitement a un operateur humain — « Depot IN260052 encore "Envoye a l'analyse" [...] NE PAS
    resoumettre la creation » — et personne ne le lisait. Le statut et la derniere verification
    aussi : sans eux, l'ecran ne peut pas distinguer un envoi en cours d'un depot en analyse, ni
    dire depuis quand rien n'a bouge.
    """
    lire = ligne.get
    return {
        "nom": lire("name"),
        "numero": lire("numero_depot") or "",
        "statut": lire("statut") or "",
        "reference": lire("reference") or "",
        "soumis_le": str(lire("soumis_le") or ""),
        "derniere_verification": str(lire("derniere_verification") or ""),
        "verifications": lire("verifications") or 0,
        "message": lire("message") or "",
    }


def ouverts(limite: int = 50) -> list:
    """Les depots que TEJ n'a pas encore analyses, du plus ancien au plus recent.

    Seulement `en_analyse` : une ligne `en_envoi` n'a pas encore de depot a suivre.
    """
    return frappe.get_all(DOCTYPE, filters={"statut": EN_ANALYSE},
                          fields=["name", "facture", "statut", "numero_depot",
                                  "numero_declarant", "suivi", "verifications"],
                          order_by="soumis_le asc", limit_page_length=limite)


def incertains(limite: int = 20) -> list:
    """Les depots conclus `incertain` — qu'on REINTERROGE quand meme a chaque passage.

    ⚠️ `incertain` N'EST PAS UN ETAT FINAL POUR LE SUIVI, seulement pour la facture (qui reste
    bloquee). La route de statut est en lecture seule au contrat : la rappeler ne coute rien et
    peut conclure toute seule. Constate en prod le 26/08/2026 (DEP-2026-00323) : la confirmation
    post-Valider du service avait rendu une erreur alors que le certificat etait GENERE — et le
    cron, qui ne relisait que `en_analyse`, laissait la facture « a verifier sur le portail »
    pour toujours.
    """
    return frappe.get_all(DOCTYPE, filters={"statut": INCERTAIN},
                          fields=["name", "facture", "statut", "numero_depot",
                                  "numero_declarant", "suivi", "verifications"],
                          order_by="soumis_le asc", limit_page_length=limite)


def perdus(limite: int = 20) -> list:
    """Les taches de fond qui ne se sont jamais conclues. -> lignes `en_envoi` trop vieilles.

    ⚠️ ON NE LES LIBERE PAS, ON LES SIGNALE. Un worker tue au mauvais moment laisse une ligne
    `en_envoi` alors que le portail a peut-etre validé : rendre la facture reemettable serait
    exactement le geste qui declare en double. La ligne passe `incertain` et le dit — c'est au
    portail de trancher, pas a nous.
    """
    limite_datetime = frappe.utils.add_to_date(frappe.utils.now_datetime(),
                                               minutes=-DELAI_ENVOI_MIN)
    return frappe.get_all(DOCTYPE,
                          filters={"statut": EN_ENVOI, "soumis_le": ["<", limite_datetime]},
                          fields=["name", "facture", "numero_declarant"],
                          order_by="soumis_le asc", limit_page_length=limite)


def reserver(ctx: dict) -> str:
    """Pose la ligne AVANT de lancer la soumission en tache de fond. -> nom du document.

    ⚠️ LA RESERVATION EST LA BARRIERE. Elle existe des le clic, donc un second clic — ou un
    rechargement de page suivi d'un nouveau clic — se heurte a elle sans avoir rien appele. La
    fenetre entre l'envoi et la reponse du portail est justement celle ou un doublon coute le
    plus cher.
    """
    doc = frappe.new_doc(DOCTYPE)
    # ⚠️ LA NATURE DE LA PIECE SE DECLARE. `facture` est un lien DYNAMIQUE depuis le 05/09/2026 :
    # une retenue se preleve sur une facture d'achat, mais aussi sur une ECRITURE quand la
    # depense est passee par la caisse. Sans ce champ, la reservation levait
    # « LinkValidationError : Impossible de trouver Facture d'achat : ACC-JV-2026-00698 » — et la
    # soumission depuis une ecriture ne pouvait meme pas commencer.
    doc.piece_type = ctx.get("piece_type") or "Purchase Invoice"
    doc.facture = ctx.get("facture")
    doc.fournisseur = ctx.get("fournisseur")
    doc.beneficiaire = ctx.get("matricule")
    doc.numero_declarant = ctx.get("bill_no")
    doc.date_paiement = ctx.get("date_paiement") or None
    doc.exercice = ctx.get("exercice")
    doc.statut = EN_ENVOI
    doc.soumis_le = frappe.utils.now_datetime()
    doc.message = "soumission lancée en tâche de fond"
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc.name


def completer(nom: str, creation: dict, job: str = None) -> None:
    """Remplit la ligne reservee avec ce que le portail a rendu."""
    doc = frappe.get_doc(DOCTYPE, nom)
    doc.statut = creation.get("statut") or EN_ANALYSE
    doc.numero_depot = creation.get("depot_numero") or ""
    doc.reference = creation.get("reference") or ""
    if creation.get("suivi"):
        doc.suivi = json.dumps(creation["suivi"], ensure_ascii=False, indent=1)
    if job:
        doc.job_creation = job
    doc.message = ""
    doc.flags.ignore_permissions = True
    doc.save()


def marquer(nom: str, statut: str, message: str = "") -> None:
    """Conclut une ligne sur un etat qui n'est ni un depot ni un certificat."""
    doc = frappe.get_doc(DOCTYPE, nom)
    doc.statut = statut
    doc.message = (message or "")[:500]
    doc.derniere_verification = frappe.utils.now_datetime()
    doc.flags.ignore_permissions = True
    doc.save()


def enregistrer(ctx: dict, creation: dict, job: str = None) -> str:
    """Pose la ligne de depot AVANT toute autre decision. -> nom du document.

    ⚠️ APPELEE AVANT L'ATTACHEMENT DU PDF ET AVANT TOUT RETOUR A L'ECRAN. Le numero de depot est
    le fait le plus couteux a perdre : il est la seule preuve que la declaration est partie, et
    il n'existe que dans la reponse du job. Tout ce qui peut echouer ensuite doit echouer APRES
    qu'il soit en base.
    """
    doc = frappe.new_doc(DOCTYPE)
    # ⚠️ LA NATURE DE LA PIECE SE DECLARE. `facture` est un lien DYNAMIQUE depuis le 05/09/2026 :
    # une retenue se preleve sur une facture d'achat, mais aussi sur une ECRITURE quand la
    # depense est passee par la caisse. Sans ce champ, la reservation levait
    # « LinkValidationError : Impossible de trouver Facture d'achat : ACC-JV-2026-00698 » — et la
    # soumission depuis une ecriture ne pouvait meme pas commencer.
    doc.piece_type = ctx.get("piece_type") or "Purchase Invoice"
    doc.facture = ctx.get("facture")
    doc.fournisseur = ctx.get("fournisseur")
    doc.beneficiaire = ctx.get("matricule")
    doc.numero_declarant = ctx.get("bill_no")
    doc.date_paiement = ctx.get("date_paiement") or None
    doc.exercice = ctx.get("exercice")
    doc.statut = creation.get("statut") or EN_ANALYSE
    doc.numero_depot = creation.get("depot_numero") or ""
    doc.reference = creation.get("reference") or ""
    doc.suivi = json.dumps(creation.get("suivi"), ensure_ascii=False, indent=1) \
        if creation.get("suivi") else ""
    doc.job_creation = job or ""
    doc.soumis_le = frappe.utils.now_datetime()
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc.name


def _corps_verbatim(brut):
    """Le corps rendu par le service, DEBALLE de son enveloppe. -> dict | None si inexploitable.

    ⚠️ LE SERVICE REND UNE ENVELOPPE, PAS UN CORPS. Le contrat est explicite — « poster
    `cert_create.suivi.body` » — et `CertSuivi` vaut `{endpoint, body}`. Postee entiere, elle n'a
    pas de `numero_chez_declarant` au premier niveau : `interroger` la refuse sans appeler
    personne, le depot reste eternellement « en analyse », et l'ecran repond « c'est normal » a un
    suivi qui n'a jamais eu lieu. Constate sur DEP-2026-00235 (depot IN260052), que rien n'aurait
    jamais conclu.

    Un corps nu reste accepte : les versions anterieures du service en rendaient un, et le
    deballage ne doit pas les rendre illisibles.
    """
    if not brut:
        return None
    try:
        lu = json.loads(brut)
    except (TypeError, ValueError):
        return None
    if not isinstance(lu, dict):
        return None
    corps = lu.get("body") if isinstance(lu.get("body"), dict) else lu
    # Sans numero chez le declarant, ce corps n'est pas suivable : mieux vaut reconstruire que
    # poster une requete que le service refusera.
    return corps if corps.get("numero_chez_declarant") else None


def corps_de_suivi(depot) -> dict:
    """Le corps a poster sur la route de statut. -> dict.

    Le corps verbatim rendu par le service quand il existe ; sinon on le reconstruit a partir de
    ce que la ligne porte — `numero_chez_declarant` suffit au contrat, le reste affine.

    ⚠️ ET LE VERBATIM PRIME PARCE QUE LUI SEUL PORTE LE NUMERO REELLEMENT SOUMIS. Quand le service
    incremente le numero (`26FA01134` annule -> `26FA01134_V2`), la ligne ERPNext garde le numero
    DEMANDE tandis que `suivi.body` porte celui que TEJ connait. Une reconstruction suivrait donc
    un numero qui n'existe pas chez l'administration.
    """
    brut = depot.get("suivi") if isinstance(depot, dict) else depot.suivi
    corps = _corps_verbatim(brut)
    if corps:
        return corps
    # `dict` comme `Document` repondent tous deux a `.get` : le suivi se lance aussi bien depuis
    # une ligne complete que depuis les champs allegés rendus par `ouverts()`.
    lire = depot.get
    corps = {"numero_chez_declarant": lire("numero_declarant") or ""}
    for champ, cle in (("beneficiaire", "beneficiaire"), ("date_paiement", "date_paiement"),
                       ("numero_depot", "numero_depot"), ("exercice", "exercice")):
        valeur = lire(champ)
        if valeur:
            corps[cle] = str(valeur) if champ != "exercice" else int(valeur)
    # ⚠️ LE PORTAIL COMPARE LE BENEFICIAIRE A CE QU'IL AFFICHE : « 1827548K », jamais
    # « 1827548K/P/M/000 ». Avec la forme longue, le service refuse la lecture (« le filtre n'a
    # pas ete applique ») et le depot reste eternellement en analyse — constate en prod le
    # 05/09/2026 sur une ligne reconstituee a la main. Une ligne posee par `reserver` porte deja
    # la forme courte ; on la garantit ici pour toutes les autres.
    if corps.get("beneficiaire"):
        from bank_retenue_sync.tej import matricule as M

        corps["beneficiaire"] = M.normaliser(corps["beneficiaire"]) or corps["beneficiaire"]
    return corps


def interroger(depot) -> dict:
    """Demande au service ou en est ce depot. Lecture seule cote TEJ. -> dict de `lire_statut`."""
    from bank_retenue_sync.bank import movements

    corps = corps_de_suivi(depot)
    if not corps.get("numero_chez_declarant"):
        return {"statut": None, "reference": "", "depot_numero": "",
                "message": "numero chez le declarant absent : le depot n'est pas suivable"}
    job = movements.start_job(ROUTE_STATUT, corps)
    resultat = movements.wait_job(job, timeout=DELAI_STATUT) or {}
    return lire_statut(resultat.get("result") or resultat.get("data") or resultat)


def conclure(nom: str, statut: str, reference: str = "", message: str = "",
             numero: str = "") -> None:
    """Met la ligne de depot a jour apres un suivi.

    `numero` : le numero de depot appris par le suivi — une ligne passee `incertain` avant la
    reponse du portail n'en a pas, et c'est la seule occasion de le recuperer. On ne remplace
    jamais un numero deja en base : celui de la creation fait foi.
    """
    doc = frappe.get_doc(DOCTYPE, nom)
    doc.statut = statut or doc.statut
    if reference:
        doc.reference = reference
    if numero and not doc.numero_depot:
        doc.numero_depot = numero
    if message:
        doc.message = message[:500]
    doc.derniere_verification = frappe.utils.now_datetime()
    doc.verifications = (doc.verifications or 0) + 1
    doc.flags.ignore_permissions = True
    doc.save()


def toucher(nom: str, message: str = "") -> None:
    """Note qu'une verification a eu lieu sans rien conclure."""
    doc = frappe.get_doc(DOCTYPE, nom)
    doc.derniere_verification = frappe.utils.now_datetime()
    doc.verifications = (doc.verifications or 0) + 1
    if message:
        doc.message = message[:500]
    doc.flags.ignore_permissions = True
    doc.save()
