"""Creation des ecritures de retenue manquantes, a partir d'un certificat TEJ.

CE QUE CE MODULE NE FAIT PAS
----------------------------
Il ne corrige jamais une ecriture existante. Un certificat qui differe de la saisie de quelques
millimes signale un ecart — il ne le rattrape pas : l'ecriture est deja passee, souvent sur un
exercice avance, et une annulation-reprise automatique serait une reprise de comptabilite decidee
par une machine. L'ecart est rendu, l'arbitrage reste humain.

CE QU'IL FAIT
-------------
Quand un certificat prouve une retenue que RIEN ne porte dans les comptes, il propose l'ecriture
qui manque, calquee trait pour trait sur celles deja saisies :

    Dr « Avance  impôt société - A&S »   (le credit d'impot que la retenue nous ouvre)
    Cr « Débiteurs - A&S »               (la creance client s'eteint d'autant)

⚠️ TOUJOURS EN BROUILLON, ET SEULEMENT SI LA FACTURE EST IDENTIFIEE
-------------------------------------------------------------------
Sans facture, l'ecriture serait un acompte flottant qui fausserait l'age des creances. Et sans
brouillon, une erreur de rapprochement deviendrait une ecriture a annuler. Les deux conditions
tiennent au meme principe : ce module PROPOSE, il ne decide pas.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_days, flt, getdate

from bank_retenue_sync.tej import rapprochement as R

DOCTYPE = "Retenue Certificate"

# ⚠️ DOUBLE ESPACE apres « Avance » : c'est le libelle reel du compte dans le plan comptable, et
# toute recherche avec un seul espace echoue. Verifie sur les 141 ecritures existantes.
COMPTE_CREDIT_IMPOT = "Avance  impôt société - A&S"
COMPTE_CLIENT = "Débiteurs - A&S"

# Fenetre d'anti-doublon : une retenue du meme client au meme montant a moins de deux mois est
# tres probablement la meme, saisie autrement (sur une commande, a une autre date).
FENETRE_DOUBLON = 60


def _company() -> str:
    return (frappe.db.get_single_value("Bank Retenue Sync Settings", "company")
            or frappe.defaults.get_global_default("company"))


def _charger_facture(nom: str):
    return frappe.db.get_value("Sales Invoice", nom,
                               ["customer", "outstanding_amount", "docstatus", "company"],
                               as_dict=1)


def verifier(cert: dict, ctx: R.Contexte = None, charger_facture=None, cause=None) -> dict:
    """Les garde-fous, tous bloquants. -> {ok, raison}.

    Chacun repond a une facon precise de se tromper ; aucun n'est decoratif. `charger_facture` et
    `cause` sont injectables : c'est ce qui rend ces regles testables sans base.
    """
    ctx = ctx if ctx is not None else R.charger_contexte()
    charger_facture = charger_facture or _charger_facture
    cause = cause or _cause_facture_soldee
    if cert.get("hors_perimetre"):
        return {"ok": False, "raison": "hors perimetre"}
    if cert.get("anomalie"):
        return {"ok": False, "raison": "anomalie : %s" % (cert.get("anomalie_raison") or "")}
    if (cert.get("etat_depot") or "") not in ("Recue", "Rectifie"):
        return {"ok": False, "raison": "etat non exploitable (%s)" % cert.get("etat_depot")}
    if cert.get("payment_entry"):
        return {"ok": False, "raison": "une ecriture porte deja cette retenue (%s)"
                % cert["payment_entry"]}
    if not cert.get("customer"):
        return {"ok": False, "raison": "client non identifie"}
    if not cert.get("sales_invoice"):
        # Le point le plus important : sans facture, on ne sait pas quelle creance s'eteint.
        return {"ok": False, "raison": "facture non identifiee"}
    deja = R.pieces_ras_de_la_facture(cert["sales_invoice"], cert["customer"], ctx,
                                      cert.get("name"))
    if deja:
        # ⚠️ LE GARDE-FOU QUI EMPECHE UNE DOUBLE COMPTABILISATION. Une retenue peut etre saisie des
        # mois avant que le client ne la declare : le certificat sort alors « sans ecriture » alors
        # que la facture en porte deja une. Creer ou ajuster ici doublerait le credit d'impot.
        return {"ok": False, "raison": "la facture porte deja une retenue (%s) : c'est un "
                                       "rapprochement a faire, pas une regularisation"
                % ", ".join(p["name"] for p in deja)}
    montant = flt(cert.get("montant_retenue"), 3)
    if montant <= 0:
        return {"ok": False, "raison": "montant nul"}

    facture = charger_facture(cert["sales_invoice"])
    if not facture or facture.docstatus != 1:
        return {"ok": False, "raison": "facture introuvable ou non validee"}
    if facture.customer != cert["customer"]:
        return {"ok": False, "raison": "la facture appartient a %s" % facture.customer}
    if flt(facture.outstanding_amount, 3) < montant:
        # Imputer plus que le reste du a la facture creerait un avoir fantome. Mais la cause
        # merite d'etre nommee : dans les cas reels observes, la facture est soldee parce que le
        # reglement a ete encaisse pour le TTC ENTIER alors que le client avait retenu 1 %. La
        # retenue existe donc bien — c'est le reglement qui est surevalue de son montant.
        return {"ok": False, "raison": "facture deja soldee%s"
                % cause(cert["sales_invoice"], montant, cert.get("date_paiement"))}

    jour = getdate(cert.get("date_paiement"))
    doublons = [p for p in ctx.pes_ras.get(cert["customer"], [])
                if abs(p["paid_amount"] - montant) < 0.005
                and p["posting_date"]
                and getdate(add_days(jour, -FENETRE_DOUBLON)) <= getdate(p["posting_date"])
                <= getdate(add_days(jour, FENETRE_DOUBLON))]
    if doublons:
        return {"ok": False, "raison": "ecriture semblable deja saisie (%s)" % doublons[0]["name"]}
    return {"ok": True, "raison": "", "facture": facture}


def _cause_facture_soldee(facture: str, montant: float, jour=None) -> str:
    """Nomme le reglement qui a solde la facture, et l'ajustement qu'il faudrait pour y loger la
    retenue. Sans cette precision, l'utilisateur lit « facture soldee » et ne sait pas quoi faire.

    ⚠️ Le reglement nomme ici doit etre CELUI QUE L'AJUSTEMENT REPRENDRAIT, sinon le message
    annonce une correction et l'outil en propose une autre. D'ou l'appel a la meme regle, plutot
    qu'un « le plus gros » recalcule au fil de la phrase.
    """
    lignes = frappe.db.sql("""select pe.name, pe.posting_date, pe.mode_of_payment,
                                     pe.paid_amount, per.allocated_amount
                              from `tabPayment Entry Reference` per
                              join `tabPayment Entry` pe on pe.name = per.parent
                              where per.reference_name = %s and pe.docstatus = 1
                                and pe.mode_of_payment != %s
                              order by per.allocated_amount desc""",
                           (facture, R.mode_ras()), as_dict=1)
    if not lignes:
        return ""
    ttc = frappe.db.get_value("Sales Invoice", facture, "grand_total")
    trouve = reglement_a_reprendre({"montant_retenue": montant, "sales_invoice": facture,
                                    "date_paiement": jour},
                                   lignes=lignes, ttc=flt(ttc, 3) if ttc else None)
    if not trouve.get("ok"):
        return " par %s reglements — %s" % (len(lignes), trouve.get("raison") or "")
    p = trouve["reglement"]
    # Le geste annonce doit etre CELUI QU'ON FERA : de l'argent compte ne se reduit pas, seule son
    # imputation bouge (cf. `argent_compte`).
    if argent_compte(p.get("mode_of_payment"), mode_dette()):
        return (" par %s (%s, %s DT du %s) : ce reglement porte la part retenue alors que le client "
                "a retenu %s DT — son montant ne bouge pas, c'est son imputation a la facture qu'il "
                "faudrait ramener a %s DT, la difference allant a une autre creance du client"
                % (p["name"], p["mode_of_payment"], flt(p["allocated_amount"], 3),
                   p["posting_date"], montant,
                   round(flt(p["allocated_amount"], 3) - montant, 3)))
    return (" par %s (%s, %s DT du %s) : ce reglement porte la part retenue alors que le client a "
            "retenu %s DT — c'est lui qu'il faudrait ramener a %s DT pour loger la retenue"
            % (p["name"], p["mode_of_payment"], flt(p["allocated_amount"], 3), p["posting_date"],
               montant, round(flt(p["paid_amount"], 3) - montant, 3)))


def construire(cert: dict, insert: bool = False, submit: bool = False):
    """Construit l'ecriture EN MEMOIRE. `insert=True` l'enregistre, `submit=True` la valide.

    ⚠️ `submit` n'est jamais decide ici : il vient du reglage « Valider automatiquement les
    ecritures de retenue » (cf. `_auto_submit`). Une ecriture validee ne se retire plus d'un clic —
    c'est une politique, pas un detail d'implementation.
    """
    montant = flt(cert["montant_retenue"], 3)
    company = _company()
    pe = frappe.new_doc("Payment Entry")
    pe.update({
        "payment_type": "Receive",
        "company": company,
        "posting_date": getdate(cert["date_paiement"]),
        "party_type": "Customer",
        "party": cert["customer"],
        "paid_from": COMPTE_CLIENT,
        "paid_to": COMPTE_CREDIT_IMPOT,
        "paid_amount": montant,
        "received_amount": montant,
        "source_exchange_rate": 1,
        "target_exchange_rate": 1,
        "mode_of_payment": R.mode_ras(),
        # La reference porte le certificat : c'est la tracabilite inverse, celle qui permet de
        # retrouver d'ou vient l'ecriture des mois plus tard.
        "reference_no": cert["reference"],
        "reference_date": getdate(cert["date_paiement"]),
        "remarks": ("Retenue a la source declaree au portail TEJ\nCertificat %s du %s\n"
                    "Declarant %s (%s)\nAssiette %s DT, retenue %s DT"
                    % (cert["reference"], cert["date_paiement"], cert.get("declarant"),
                       cert.get("declarant_matricule"), flt(cert.get("total_brut"), 3), montant)),
    })
    pe.append("references", {
        "reference_doctype": "Sales Invoice",
        "reference_name": cert["sales_invoice"],
        "allocated_amount": montant,
    })
    if insert:
        pe.insert(ignore_permissions=True)
        if submit:
            pe.submit()
    return pe


# ------------------------------------------------------------------ ajustement

MODE_DETTE_DEFAUT = "Dette non payée"


def mode_dette() -> str:
    """Le mode de paiement qui ne represente AUCUN argent compte."""
    try:
        return (frappe.db.get_single_value("Bank Retenue Sync Settings", "ras_mode_dette")
                or MODE_DETTE_DEFAUT)
    except Exception:
        return MODE_DETTE_DEFAUT


def argent_compte(mode: str, mode_de_dette: str = None) -> bool:
    """⚠️ LA DISTINCTION QUI EMPECHE DE FAIRE DISPARAITRE DE L'ARGENT RECU.

    Reprendre un reglement a la baisse revient a dire « nous avons encaisse moins que ce qui est
    ecrit ». C'est vrai d'une ligne « dette » : aucune caisse n'a ete ouverte, le montant a ete
    DEDUIT de la facture, et s'il incluait la part retenue par le client, il est simplement faux.

    C'est faux de tout le reste. Des especes, un cheque, un virement ont ete COMPTES par quelqu'un :
    le montant est un fait. Le diminuer de la retenue effacerait des comptes de l'argent
    physiquement recu. Dans ce cas la retenue est un credit EN PLUS, et c'est l'affectation du
    reglement qui doit bouger, jamais son montant.
    """
    return (mode or "") != (mode_de_dette or MODE_DETTE_DEFAUT)


def repartir(montant: float, dettes: list) -> tuple:
    """Ou loger la part du reglement que la retenue libere. -> (affectations, reste).

    `dettes` : [{doctype, name, reste, date}] deja triees, la plus ancienne d'abord — une somme
    disponible eteint d'abord la creance la plus vieille, c'est la regle comptable la moins
    discutable et la seule qui ne demande aucun arbitrage.

    Le reste non loge n'est pas perdu : il demeure sur le reglement en montant non affecte, donc
    en avance au credit du client. Fonction pure : c'est elle qu'on teste.
    """
    restant = round(flt(montant, 3), 3)
    affectations = []
    for d in dettes:
        if restant <= 0.001:
            break
        part = min(restant, round(flt(d.get("reste"), 3), 3))
        if part <= 0.001:
            continue
        affectations.append({"doctype": d["doctype"], "name": d["name"], "montant": round(part, 3),
                             "dette_pe": d.get("dette_pe"),
                             "dette_reste": round(flt(d.get("reste"), 3) - part, 3)})
        restant = round(restant - part, 3)
    return affectations, restant


def dettes_non_payees(customer: str) -> list:
    """Les ecritures « Dette non payee » du client, la plus ancienne d'abord.

    ⚠️ « DETTE » A UN SENS PRECIS ICI, ET CE N'EST PAS « RESTE DU ». Une facture au reste du non nul
    n'est pas une dette reconnue : c'est une facture qu'on attend. La DETTE, c'est l'ecriture de mode
    « Dette non payee » qui a solde une piece contre le compte de dettes — le client a recu la
    marchandise, la piece est comptablement eteinte, et ce qu'il doit vit desormais la.

    Premiere version : la part liberee allait sur les factures et commandes au reste du non nul.
    Constate en reel sur FM WATER PLUS — elle est partie en AVANCE sur une commande a facturer
    (SAL-ORD-2026-00287), qui ne doit rien a personne, pendant que la seule vraie dette du client
    (851,20 sur SAL-ORD-2026-02742) restait intacte. « Je vois pas la dette » : elle n'y etait pas.

    Une ligne par IMPUTATION, pas par ecriture : c'est l'imputation qui designe la piece a creancer,
    et une ecriture de dette peut en porter plusieurs.
    """
    return [{"dette_pe": r.dette_pe, "doctype": r.reference_doctype, "name": r.reference_name,
             "date": r.posting_date, "reste": flt(r.allocated_amount, 3)}
            for r in frappe.db.sql("""select pe.name as dette_pe, pe.posting_date,
                                             per.reference_doctype, per.reference_name,
                                             per.allocated_amount
                                      from `tabPayment Entry` pe
                                      join `tabPayment Entry Reference` per on per.parent = pe.name
                                      where pe.party = %(c)s and pe.party_type = 'Customer'
                                        and pe.docstatus = 1 and pe.mode_of_payment = %(mode)s
                                        and per.allocated_amount > 0.001
                                      order by pe.posting_date, per.idx""",
                                   {"c": customer, "mode": mode_dette()}, as_dict=1)]


def _auto_submit() -> bool:
    """Valider d'office les ecritures de retenue (creation comme ajustement) ?"""
    return bool(frappe.db.get_single_value("Bank Retenue Sync Settings",
                                           "auto_submit_ras_ajustement"))


def _supprimer_annule() -> bool:
    return bool(frappe.db.get_single_value("Bank Retenue Sync Settings",
                                           "supprimer_reglement_annule"))


def suppression_permise(valide: bool, demandee: bool) -> bool:
    """LA REGLE QUI PROTEGE L'ARGENT DEJA RECU, et la seule chose a retenir de ce fichier.

    Reprendre un reglement, c'est l'annuler puis le refaire. Supprimer l'original alors que sa
    copie est encore EN BROUILLON effacerait la seule piece qui atteste l'encaissement : la facture
    redeviendrait due, et plus rien n'expliquerait pourquoi. On ne supprime donc jamais avant que
    le remplacant ne soit valide — quel que soit le reglage.
    """
    return bool(valide and demandee)


def _supprimer_reglement(nom: str) -> dict:
    """Supprime le reglement annule. -> {supprime, raison}.

    ⚠️ JAMAIS `force=1`. Frappe refuse de supprimer un document encore reference (rapprochement
    bancaire, avoir, ecriture de journal) : ce refus est une protection, pas un obstacle. Quand il
    tombe, on garde le document annule et on dit pourquoi — l'ajustement comptable, lui, est deja
    fait et reste valable.
    """
    try:
        frappe.delete_doc("Payment Entry", nom, ignore_permissions=True)
        return {"supprime": True, "raison": None}
    except Exception as e:
        return {"supprime": False,
                "raison": "reglement annule conserve : %s" % str(e)[:200]}


def _demander_pdf(reference: str) -> dict:
    """Reclame le certificat PDF au portail. Ne leve JAMAIS : un justificatif indisponible ne doit
    pas faire echouer — ni pire, laisser a moitie faite — une regularisation comptable reussie."""
    try:
        from bank_retenue_sync.tej import pdf

        return pdf.demander(reference)
    except Exception as e:
        return {"statut": "erreur", "detail": str(e)[:160]}


def _reduire_dette(affectation: dict, valider: bool, supprimer: bool) -> dict:
    """Diminue l'ecriture de dette du montant que l'argent compte vient de couvrir.

    LE PRINCIPE : la piece garde exactement le meme total. Ce qui etait porte par une dette
    (fictive, non encaissee) l'est desormais par de l'argent reel — 851,20 de dette deviennent
    837,389 de dette + 13,811 d'especes. Le client doit moins, et ce qu'il doit encore est juste.

    Une dette entierement couverte n'est pas recreee : elle n'a plus d'objet.
    """
    nom, part = affectation["dette_pe"], flt(affectation["montant"], 3)
    original = frappe.get_doc("Payment Entry", nom)
    nouveau = round(flt(original.paid_amount, 3) - part, 3)
    copie = None
    if nouveau > 0.001:
        copie = frappe.copy_doc(original)
        if not supprimer:
            copie.amended_from = original.name
        copie.paid_amount = nouveau
        copie.received_amount = nouveau
        for ligne in copie.references:
            if ligne.reference_name == affectation["name"]:
                ligne.allocated_amount = round(flt(ligne.allocated_amount, 3) - part, 3)
        copie.remarks = ("%s\nRamenee a %s DT le %s : %s DT desormais couverts par un reglement "
                         "reel (retenue a la source)"
                         % (original.remarks or "", nouveau, frappe.utils.nowdate(), part))
    original.cancel()
    if copie:
        copie.insert(ignore_permissions=True)
        if valider:
            copie.submit()
    # Meme regle que pour le reglement : on n'efface l'original qu'une fois son remplacant valide,
    # et une dette soldee n'a pas de remplacant a attendre.
    suppression = ({"supprime": False, "raison": None} if not supprimer or (copie and not valider)
                   else _supprimer_reglement(original.name))
    return {"dette": nom, "avant": flt(original.paid_amount, 3), "apres": nouveau,
            "part": part, "piece": affectation["name"], "dette_reprise": copie.name if copie else None,
            "soldee": copie is None, "supprimee": suppression["supprime"],
            "suppression_raison": suppression["raison"]}


def _decrire(l: dict) -> dict:
    """Un candidat, tel que l'utilisateur doit le lire pour choisir."""
    return {"name": l["name"], "date": str(l.get("posting_date")),
            "mode": l.get("mode_of_payment"), "montant": flt(l.get("paid_amount"), 3),
            "alloue": flt(l.get("allocated_amount"), 3)}


def reglement_a_reprendre(cert: dict, lignes=None, choisi: str = None, ttc: float = None) -> dict:
    """QUEL reglement portait la retenue ? — la question que les donnees reelles ont imposee.

    La premiere version prenait le plus gros reglement qui couvre la retenue. Sur une facture
    reglee EN UNE FOIS, c'est evidemment le bon ; sur les factures reglees par dizaines
    d'encaissements en especes (SOCIETE FM WATER PLUS : 19 reglements de 5 a 1 540 DT), « le plus
    gros » designe un encaissement qui n'a rien a voir avec la retenue — et le reduire ferait
    disparaitre de l'argent reellement recu.

    Les quatre regles ci-dessous ne retiennent que les situations ou le reglement se DESIGNE :

    1. le choix explicite de l'utilisateur, quand il a tranche dans la liste des candidats ;
    2. un seul reglement couvre la retenue — il n'y a rien a arbitrer ;
    3. un reglement porte le TTC ENTIER de la facture : c'est le cas nominal, celui ou le client a
       paye net et ou nous avons enregistre le brut ;
    4. un reglement tombe A LA DATE EXACTE declaree au portail — le portail date le jour ou le
       client a paye net, et cette date-la ne ment pas.

    Hors de ces quatre cas, on rend la LISTE DES CANDIDATS et on laisse l'humain trancher : mieux
    vaut une question qu'une ecriture reprise au hasard.
    """
    montant = flt(cert.get("montant_retenue"), 3)
    if lignes is None:
        lignes = frappe.db.sql("""select pe.name, pe.posting_date, pe.mode_of_payment,
                                         pe.paid_amount, pe.docstatus, per.allocated_amount,
                                         per.idx
                                  from `tabPayment Entry Reference` per
                                  join `tabPayment Entry` pe on pe.name = per.parent
                                  where per.reference_name = %s and pe.docstatus = 1
                                    and pe.mode_of_payment != %s
                                  order by per.allocated_amount desc""",
                               (cert.get("sales_invoice"), R.mode_ras()), as_dict=1)
    # Un reglement ne peut porter la retenue que s'il peut la ceder : son imputation a la facture
    # ET son montant encaisse doivent rester positifs apres la reprise.
    candidats = [l for l in lignes if flt(l["allocated_amount"], 3) >= montant
                 and flt(l["paid_amount"], 3) > montant]
    if not candidats:
        return {"ok": False, "raison": "aucun reglement de la facture ne couvre la retenue"}

    if choisi:
        retenu = [c for c in candidats if c["name"] == choisi]
        if not retenu:
            return {"ok": False, "raison": "le reglement %s ne peut pas porter cette retenue"
                    % choisi, "candidats": [_decrire(c) for c in candidats]}
        return {"ok": True, "reglement": retenu[0], "regle": "choix explicite"}

    if len(candidats) == 1:
        return {"ok": True, "reglement": candidats[0], "regle": "seul reglement qui la couvre"}

    if ttc:
        entiers = [c for c in candidats if abs(flt(c["allocated_amount"], 3) - flt(ttc, 3)) < 0.005]
        if len(entiers) == 1:
            return {"ok": True, "reglement": entiers[0], "regle": "reglement du TTC entier"}

    jour = cert.get("date_paiement")
    if jour:
        du_jour = [c for c in candidats
                   if c.get("posting_date") and getdate(c["posting_date"]) == getdate(jour)]
        if len(du_jour) == 1:
            return {"ok": True, "reglement": du_jour[0],
                    "regle": "reglement du jour declare au portail"}

    return {"ok": False,
            "raison": "%s reglements peuvent porter la retenue : lequel corriger ?"
                      % len(candidats),
            "candidats": [_decrire(c) for c in candidats]}


def ajuster(reference: str, insert: bool = False, submit=None, reglement: str = None,
            ctx: R.Contexte = None) -> dict:
    """Loge la retenue dans une facture deja soldee, sans changer son total encaisse.

    LE PRINCIPE, tel que demande : le reglement est REPRIS a l'identique (meme mode, meme date,
    meme reference) mais alloue de son montant MOINS la retenue, et l'ecriture de retenue prend
    la difference. La facture reste soldee pour le meme TTC ; c'est sa composition qui devient
    juste — 1 143,46 encaisses + 11,54 retenus au lieu de 1 155 encaisses.

    ⚠️ REPRENDRE UN REGLEMENT VALIDE, C'EST L'ANNULER PUIS LE REFAIRE. Entre les deux, la facture
    redevient due. Tant que `auto_submit_ras_ajustement` est decoche, les deux ecritures restent
    en brouillon : c'est voulu (voir avant d'engager), mais il faut alors les valider pour que les
    comptes retombent juste.

    `reglement` : le choix de l'utilisateur quand la facture en compte plusieurs et qu'aucune
    regle ne les departage (cf. `reglement_a_reprendre`). Il est verifie comme les autres — un
    reglement qui ne couvre pas la retenue est refuse, meme demande explicitement.
    """
    cert = frappe.db.get_all(DOCTYPE, filters={"name": reference},
                             fields=list(R.CHAMPS_CERTIFICAT) + ["sales_invoice", "total_brut"])
    if not cert:
        return {"ok": False, "raison": "certificat inconnu"}
    cert = cert[0]
    montant = flt(cert.get("montant_retenue"), 3)

    if cert.get("payment_entry"):
        return {"ok": False, "raison": "une ecriture porte deja cette retenue"}
    if not cert.get("customer") or not cert.get("sales_invoice"):
        return {"ok": False, "raison": "client ou facture non identifie"}
    if cert.get("hors_perimetre") or cert.get("anomalie"):
        return {"ok": False, "raison": "certificat hors perimetre ou en anomalie"}

    facture = frappe.db.get_value("Sales Invoice", cert["sales_invoice"],
                                  ["grand_total", "customer"], as_dict=1)
    deja = R.pieces_ras_de_la_facture(cert["sales_invoice"], cert["customer"],
                                      ctx if ctx is not None else R.charger_contexte(),
                                      cert.get("name"))
    if deja:
        # Meme garde-fou que dans `verifier`, et pour la meme raison : l'ajustement est le seul
        # geste de cette app qui peut comptabiliser deux fois la meme retenue.
        return {"ok": False, "raison": "la facture porte deja une retenue (%s) : c'est un "
                                       "rapprochement a faire, pas une regularisation"
                % ", ".join(p["name"] for p in deja)}

    trouve = reglement_a_reprendre(cert, choisi=reglement,
                                   ttc=flt(facture.grand_total, 3) if facture else None)
    if not trouve.get("ok"):
        return {"ok": False, **trouve}
    pris = trouve["reglement"]
    nouvelle_allocation = round(flt(pris["allocated_amount"], 3) - montant, 3)

    # DEUX GESTES SELON LA NATURE DU REGLEMENT (cf. `argent_compte`). De l'argent compte ne se
    # reduit pas : c'est son affectation qui bouge, et la part liberee va eteindre une autre dette
    # du client — ou reste en avance a son credit si elle ne doit plus rien.
    reel = argent_compte(pris["mode_of_payment"], mode_dette())
    if reel:
        nouveau_montant = flt(pris["paid_amount"], 3)
        affectations, non_affecte = repartir(montant, dettes_non_payees(cert["customer"]))
    else:
        nouveau_montant = round(flt(pris["paid_amount"], 3) - montant, 3)
        affectations, non_affecte = [], 0.0

    plan = {
        "ok": True,
        "certificat": reference,
        "facture": cert["sales_invoice"],
        "retenue": montant,
        "reglement": pris["name"],
        "reglement_mode": pris["mode_of_payment"],
        "reglement_avant": flt(pris["paid_amount"], 3),
        "reglement_apres": nouveau_montant,
        "allocation_avant": flt(pris["allocated_amount"], 3),
        "allocation_apres": nouvelle_allocation,
        "argent_compte": reel,
        "reaffectations": affectations,
        "non_affecte": non_affecte,
        "regle": trouve.get("regle"),
        "brouillon": not (_auto_submit() if submit is None else submit),
    }
    if nouveau_montant <= 0:
        return {"ok": False, "raison": "le reglement (%s) ne peut pas descendre a %s"
                % (pris["name"], nouveau_montant)}
    if nouvelle_allocation < 0:
        return {"ok": False, "raison": "l'imputation de %s a la facture (%s) ne couvre pas la "
                "retenue de %s" % (pris["name"], plan["allocation_avant"], montant)}
    if not insert:
        return {**plan, "statut": "a faire (essai a blanc)"}

    valider = _auto_submit() if submit is None else bool(submit)
    supprimer = suppression_permise(valider, _supprimer_annule())
    original = frappe.get_doc("Payment Entry", pris["name"])
    # 1. Reprise : la copie AVANT l'annulation, pour garder tous les details sous la main.
    copie = frappe.copy_doc(original)
    if not supprimer:
        # `amended_from` relie la copie a l'original annule — utile tant qu'il existe. Si l'original
        # doit disparaitre, ce lien empecherait sa suppression et laisserait a la copie un nom
        # d'amendement (« …-1 ») sans parent.
        copie.amended_from = original.name
    copie.paid_amount = nouveau_montant
    copie.received_amount = nouveau_montant
    for ligne in copie.references:
        if ligne.reference_name == cert["sales_invoice"]:
            ligne.allocated_amount = nouvelle_allocation
    # La part liberee va sur les autres creances du client. Ce qui ne trouve pas preneur reste en
    # montant NON AFFECTE : ERPNext le porte alors en avance au credit du client — l'argent est
    # toujours la, visible, en attente d'une facture.
    for a in affectations:
        copie.append("references", {"reference_doctype": a["doctype"],
                                    "reference_name": a["name"],
                                    "allocated_amount": a["montant"]})
    copie.remarks = ("%s\nRepris le %s : %s DT retenus a la source par le client "
                     "(certificat TEJ %s)%s"
                     % (original.remarks or "", frappe.utils.nowdate(), montant, reference,
                        ("\nMontant inchange (argent compte) ; %s DT reaffectes a %s"
                         % (montant, ", ".join(a["name"] for a in affectations))) if affectations
                        else ("\nMontant inchange (argent compte) ; %s DT en avance non affectee"
                              % non_affecte) if reel else ""))
    original.cancel()

    # 1 bis. LES DETTES D'ABORD, ET C'EST UN ORDRE OBLIGATOIRE. La piece que porte une dette est
    #        souvent avancee a 100 % (SAL-ORD-2026-02742 : 1 101,20 sur 1 101,20) : lui imputer un
    #        millime de plus avant d'avoir reduit la dette, et ERPNext refuse « allocated amount
    #        greater than outstanding ». Reduire d'abord libere exactement la place qu'il faut.
    reprises = [_reduire_dette(a, valider, supprimer) for a in affectations]

    copie.insert(ignore_permissions=True)

    # 2. L'ecriture de retenue prend exactement la difference.
    retenue = construire(cert, insert=True)

    if valider:
        copie.submit()
        retenue.submit()

    # 3. L'original annule n'a plus de role : la copie porte le meme encaissement, corrige. On ne
    #    l'efface qu'APRES la validation des deux ecritures (cf. `suppression_permise`).
    suppression = _supprimer_reglement(original.name) if supprimer else {"supprime": False,
                                                                         "raison": None}
    frappe.db.set_value(DOCTYPE, reference, {
        "payment_entry": retenue.name,
        "match_status": "Auto Matched",
        "revue_requise": 0 if valider else 1,
        "match_raison": ("retenue logee dans la facture %s : reglement %s repris en %s (%s)%s%s"
                         % (cert["sales_invoice"], original.name, copie.name,
                            ("montant inchange %s, imputation %s -> %s%s"
                             % (nouveau_montant, plan["allocation_avant"], nouvelle_allocation,
                                (", %s DT vers %s" % (montant,
                                                      ", ".join(a["name"] for a in affectations)))
                                if affectations else ", %s DT en avance" % non_affecte))
                            if reel else "%s -> %s" % (plan["reglement_avant"], nouveau_montant),
                            "" if valider else " — BROUILLONS A VALIDER",
                            " ; original supprime" if suppression["supprime"]
                            else (" ; %s" % suppression["raison"] if suppression["raison"] else ""))),
    }, update_modified=False)

    # 4. Le justificatif : la retenue est logee, il lui manque sa preuve. Demande au portail en
    #    tache de fond — une session de scraping dure des minutes, l'ecran ne doit pas attendre.
    pdf = _demander_pdf(reference)
    return {**plan, "statut": "ajuste", "reglement_repris": copie.name,
            "payment_entry": retenue.name, "valide": valider, "dettes_reprises": reprises,
            "reglement_supprime": suppression["supprime"],
            "suppression_raison": suppression["raison"], "pdf": pdf.get("statut")}


def creer(references: list = None, insert: bool = False, limite: int = None,
          submit=None) -> dict:
    """Cree les ecritures manquantes des certificats indiques (ou de tous ceux « Sans piece »).

    `insert=0` est l'essai a blanc : il rend exactement ce qui serait cree, sans rien ecrire.
    `submit` suit le reglage « Valider automatiquement les ecritures de retenue » sauf ordre
    contraire — ici la validation ne detruit rien : elle passe un brouillon en ecriture.
    """
    ctx = R.charger_contexte()
    valider = _auto_submit() if submit is None else bool(submit)
    filtres = {"match_status": "Sans piece", "hors_perimetre": 0}
    if references:
        filtres = {"name": ["in", references]}
    certificats = frappe.db.get_all(
        DOCTYPE, filters=filtres, fields=list(R.CHAMPS_CERTIFICAT) + ["sales_invoice", "total_brut"],
        order_by="date_paiement", limit_page_length=0)

    out = {"creables": 0, "crees": 0, "refuses": 0, "valider": valider, "detail": []}
    for cert in certificats:
        if limite and out["creables"] >= limite:
            break
        verdict = verifier(cert, ctx)
        ligne = {"reference": cert["reference"], "declarant": cert.get("declarant"),
                 "customer": cert.get("customer"), "montant": flt(cert.get("montant_retenue"), 3),
                 "date": str(cert.get("date_paiement")), "sales_invoice": cert.get("sales_invoice")}
        if not verdict["ok"]:
            out["refuses"] += 1
            out["detail"].append({**ligne, "statut": "refuse", "raison": verdict["raison"]})
            continue
        out["creables"] += 1
        if not insert:
            out["detail"].append({**ligne, "statut": "a creer", "payment_entry": "(essai a blanc)"})
            continue
        try:
            pe = construire(cert, insert=True, submit=valider)
            frappe.db.set_value(DOCTYPE, cert["name"], {
                "payment_entry": pe.name,
                "match_status": "Auto Matched",
                # Validee, l'ecriture n'attend plus personne ; en brouillon, si.
                "revue_requise": 0 if valider else 1,
                "match_raison": ("ecriture creee depuis le certificat (%s)"
                                 % ("validee" if valider else "brouillon a valider")),
            }, update_modified=False)
            ctx.reserver(cert["name"], piece=pe.name, facture=cert.get("sales_invoice"))
            out["crees"] += 1
            out["detail"].append({**ligne, "statut": "cree", "payment_entry": pe.name,
                                  "valide": valider, "pdf": _demander_pdf(cert["name"]).get("statut")})
        except Exception as e:
            out["refuses"] += 1
            out["creables"] -= 1
            out["detail"].append({**ligne, "statut": "erreur", "raison": str(e)[:200]})
    return out
