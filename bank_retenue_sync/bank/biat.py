"""Mouvements de la BIAT : import au registre et RECONNAISSANCE des lignes.

Demande du 16/11/2026 : « faire pareil pour la BIAT », en commencant par les mouvements et les
cheques ; puis « le plus important maintenant : les transactions peuvent etre detectees ».

CE QUE CE MODULE FAIT — ET CE QU'IL NE FAIT PAS ENCORE
------------------------------------------------------
Il range les mouvements BIAT au registre (`BRS Bank Movement`, banque = « BIAT ») et dit de chaque
ligne ce qu'elle EST : remise de cheques (avec son numero de bordereau), commission de cette remise,
versement d'especes. Quand une piece d'ERPNext la porte deja sans ambiguite, il la designe.

Il ne cree AUCUNE ecriture et ne lance aucun encaissement. L'« Encaissement Paiement » de
customization_app deplace les cheques vers le compte Zitouna, ecrit en dur dans son server script :
tant qu'il ne sait pas viser la BIAT, une remise BIAT reste « a rapprocher » ici.

LES LIBELLES (compte ouvert le 02/11/2026 — trois natures de lignes vues au 16/11/2026)
------------------------------------------------------------------------------------
    Versement TND                                                    credit   reference TT…
    Credit Divers Chq Telec A Enc Cre Global/borde : 7473933         credit   reference FT…
    Debit Divers Commission CHQ A Enc/Bordereau : 7473932            debit    reference FT…

Le numero de BORDEREAU (7 chiffres) est la cle d'une remise : il relie le credit de la remise a sa
commission, et c'est lui qu'on cherche sur les Payment Entry (`BR:<numero>`, la convention deja
suivie pour Zitouna). Tout autre libelle sort « a verifier : libelle BIAT inconnu » — jamais un
mouvement passe sous silence.

D'OU VIENNENT LES MOUVEMENTS
----------------------------
De tej-bank-service (`POST /jobs/biat/mouvements/export`, `GET /biat/mouvements/export/latest`),
dont l'export reprend les colonnes de l'export Zitouna : le lecteur de `bank/movements.py` sert tel
quel. `importer_fichier` prend le meme fichier quand le service n'est pas joignable.
"""
from __future__ import annotations

import re

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

from bank_retenue_sync.bank import registry

BANQUE = "BIAT"
#: Fenetre de recherche d'une ecriture autour de la date d'un versement (jours).
FENETRE_VERSEMENT = 5
#: Tolerance sur le total d'une remise (frais preleves a la source, arrondis).
TOLERANCE_REMISE = 1.0

#: Reference de l'ecriture mensuelle des frais BIAT : « BIAT frais bancaires MM-YYYY ». Elle ne commence
#: PAS par « Frais bancaire » : le classement Zitouna indexe les ecritures dont la reference commence
#: ainsi (`fees.PREFIXE_MENSUEL`), et les frais des deux banques ne doivent jamais se croiser.
PREFIXE_FRAIS = "BIAT frais bancaires "
COMPTE_FRAIS = "Frais bancaire - A&S"

STATUT_IDENTIFIE = "Identifie"
STATUT_BROUILLON = "Identifie non comptabilise"
STATUT_ORPHELIN = "Orphelin"
STATUT_A_VERIFIER = "A verifier"

_BORDEREAU = r"BORDE\w*\s*:?\s*(\d{4,12})"
#: (cle de regle, sens, motif sur le libelle en majuscules sans accents, categorie, sous-categorie, libelle)
#: Les cles et categories sont celles des regles Zitouna de meme nature : filtres et rapports les
#: retrouvent sans rien apprendre de neuf.
REGLES = (
    ("enc_cheque", "Credit", re.compile(r"CHQ\s+TELEC\s+A\s+ENC.*?" + _BORDEREAU), "encaissement", "cheque",
     "Remise de chèques"),
    ("com_enc_cheque", "Debit", re.compile(r"COMMISSION\s+CHQ\s+A\s+ENC.*?" + _BORDEREAU), "frais_bancaires", "commission",
     "Commission de remise de chèques"),
    ("versement_especes", "Credit", re.compile(r"^VERSEMENT\b"), "caisse", "versement",
     "Versement d’espèces"),
)


# ---------------------------------------------------------------------------
# Reconnaissance d'un libelle — pur
# ---------------------------------------------------------------------------

def _m(valeur) -> float:
    """Un montant au millime. Pas `frappe.utils.flt(v, 3)` : hors site (tests purs), il rend 0."""
    try:
        return round(float(valeur or 0), 3)
    except (TypeError, ValueError):
        return 0.0


def _norme(texte: str) -> str:
    import unicodedata

    sans = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode()
    return " ".join(sans.upper().split())


def detecter(operation: str, sens: str) -> dict:
    """Ce qu'est une ligne BIAT d'apres son libelle et son sens.
    -> {regle, categorie, sous_categorie, libelle, numero} ; `regle` vide si le libelle est inconnu."""
    texte = _norme(operation)
    for cle, sens_regle, motif, categorie, sous_categorie, libelle in REGLES:
        if sens != sens_regle:
            continue
        trouve = motif.search(texte)
        if trouve:
            return {"regle": cle, "categorie": categorie, "sous_categorie": sous_categorie, "libelle": libelle,
                    "numero": trouve.group(1) if trouve.groups() else ""}
    return {"regle": "", "categorie": "", "sous_categorie": "", "libelle": "", "numero": ""}


def classer_ligne(ligne: dict, contexte: dict) -> dict:
    """Le classement d'UNE ligne du registre. `contexte` : ce qu'ERPNext porte deja, charge une fois
    (`charger_contexte`) — {"paiements_par_bordereau": {numero: [{name, montant, docstatus, compte}]},
    "ecritures_par_reference": {reference: [{name, docstatus}]}, "ecritures_versement": [{name,
    date, montant, docstatus}], "remises": {numero: montant du credit}}. Pur."""
    sens = ligne.get("sens") or ("Debit" if _m(ligne.get("debit")) else "Credit")
    montant = _m(ligne.get("debit") or ligne.get("credit") or ligne.get("montant"))
    vu = detecter(ligne.get("operation"), sens)
    out = {"cle": ligne.get("cle") or ligne.get("name"), "categorie": vu["categorie"], "regle": vu["regle"],
           "groupe": None, "statut": STATUT_A_VERIFIER, "raison": "", "document_type": None,
           "document_name": None, "montant_document": 0.0, "ecart": 0.0}
    if not vu["regle"]:
        out["raison"] = "libellé BIAT inconnu : à classer (aucune règle ne le reconnaît)"
        return out
    numero = vu["numero"]
    if numero:
        out["groupe"] = "bordereau:" + numero

    if vu["regle"] == "enc_cheque":
        paiements = contexte.get("paiements_par_bordereau", {}).get(numero, [])
        if not paiements:
            # Le bordereau papier est le seul document qui dit quels cheques composent une remise BIAT : on dit
            # s'il est deja collecte (`BRS Bordereau Remise`) ou s'il reste a recueillir a la caisse.
            collecte = contexte.get("bordereaux", {}).get(numero)
            lecture = contexte.get("lectures", {}).get(numero)
            suite = (f"bordereau collecté ({collecte}), " + (f"lu par l’IA — {lecture}" if lecture else "chèques à rapprocher")) \
                if collecte else "bordereau à collecter (caisse → Bordereaux de remise)"
            # Un bordereau collecte et bon est encaisse tout seul (`encaisser_remises`) : s'il ne l'est pas,
            # c'est qu'une chose l'empeche, et c'est elle qu'il faut lire ici.
            refus = contexte.get("refus", {}).get(numero)
            out.update(statut=STATUT_A_VERIFIER if (collecte and refus) else STATUT_ORPHELIN,
                       raison=f"remise de chèques, bordereau {numero} : aucun paiement ne porte « BR:{numero} » — {suite}"
                       + (f" ; pas encaissé : {refus}" if (collecte and refus) else ""))
            return out
        total = _m(sum(_m(p["montant"]) for p in paiements))
        brouillon = any(int(p.get("docstatus") or 0) == 0 for p in paiements)
        # La piece a ouvrir depuis la ligne : l'encaissement du bordereau quand il existe (il liste les cheques
        # et porte les ecarts), sinon le paiement s'il est seul.
        encaissement = contexte.get("encaissements", {}).get(numero)
        out.update(statut=STATUT_BROUILLON if brouillon else STATUT_IDENTIFIE,
                   document_type="Encaissement Paiement" if encaissement else "Payment Entry",
                   document_name=encaissement or (paiements[0]["name"] if len(paiements) == 1 else None),
                   montant_document=total, ecart=_m(montant - total))
        detail = f"remise de chèques, bordereau {numero} : {len(paiements)} paiement(s) portent « BR:{numero} »"
        if encaissement:
            detail += f" (encaissement {encaissement})"
        if 0.0005 < abs(out["ecart"]) <= TOLERANCE_REMISE:
            detail += f" — petit écart de {out['ecart']:.3f} avec le crédit de la banque"
        if abs(out["ecart"]) > TOLERANCE_REMISE:
            detail += f" pour {total:.3f} — écart de {out['ecart']:.3f} avec le crédit de la banque"
        out["raison"] = detail
        return out

    if vu["regle"] == "com_enc_cheque":
        ecritures = contexte.get("ecritures_par_reference", {}).get((ligne.get("reference") or "").upper(), [])
        du_mois = contexte.get("frais_mensuels", {}).get(periode_de(ligne.get("date")))
        if du_mois:
            # Les frais ne se comptabilisent jamais a l'unite : la commission rejoint l'ecriture de son mois.
            out.update(statut=STATUT_BROUILLON if int(du_mois.get("docstatus") or 0) == 0 else STATUT_IDENTIFIE,
                       document_type="Journal Entry", document_name=du_mois["name"], montant_document=montant,
                       raison=f"commission de la remise {numero} : dans l’écriture mensuelle des frais BIAT "
                              f"({cle_frais(periode_de(ligne.get('date')))})")
            return out
        if ecritures:
            brouillon = any(int(e.get("docstatus") or 0) == 0 for e in ecritures)
            out.update(statut=STATUT_BROUILLON if brouillon else STATUT_IDENTIFIE, document_type="Journal Entry",
                       document_name=ecritures[0]["name"], montant_document=montant,
                       raison=f"commission de la remise {numero} : écriture qui cite la référence bancaire")
        else:
            remise = contexte.get("remises", {}).get(numero)
            suite = f" (remise de {remise:.3f})" if remise else ""
            out.update(statut=STATUT_ORPHELIN, raison=f"commission de la remise de chèques, bordereau {numero}{suite} : "
                       "frais bancaires à comptabiliser")
        return out

    # versement d'especes : l'ecriture qui cite la reference bancaire, sinon celle du meme montant a quelques jours
    reference = (ligne.get("reference") or "").upper()
    ecritures = contexte.get("ecritures_par_reference", {}).get(reference, [])
    comment = "écriture qui cite la référence bancaire"
    if not ecritures:
        jour = getdate(ligne.get("date")) if ligne.get("date") else None
        ecritures = [e for e in contexte.get("ecritures_versement", [])
                     if abs(_m(e["montant"]) - montant) < 0.0005 and jour
                     and abs((getdate(e["date"]) - jour).days) <= FENETRE_VERSEMENT]
        comment = "écriture du même montant sur le compte BIAT"
    if len(ecritures) == 1:
        e = ecritures[0]
        out.update(statut=STATUT_BROUILLON if int(e.get("docstatus") or 0) == 0 else STATUT_IDENTIFIE,
                   document_type="Journal Entry", document_name=e["name"], montant_document=montant,
                   raison=f"versement d’espèces : {comment}")
    elif ecritures:
        out.update(statut=STATUT_A_VERIFIER, raison=f"versement d’espèces : {len(ecritures)} écritures possibles "
                   "(" + ", ".join(e["name"] for e in ecritures[:4]) + ") — à trancher")
    else:
        out.update(statut=STATUT_ORPHELIN, raison="versement d’espèces : aucune écriture caisse → BIAT du même montant")
    return out


# ---------------------------------------------------------------------------
# Frais bancaires : une ecriture par mois, separee de celle de Zitouna — pur
# ---------------------------------------------------------------------------
# Decision du 16/11/2026 : « pour le moment tout passe en frais bancaire pour la BIAT ; separe les frais
# de la BIAT de ceux de Zitouna ; le mecanisme est le meme ». Donc, comme `expenses/fees.py` : UNE
# ecriture par mois, refaite en entier depuis le registre a chaque frais nouveau (jamais incrementee :
# une ligne vue deux fois ne double rien), datee du dernier frais du mois. Pas de part TVA ni timbre :
# la BIAT ne les donne pas sur des lignes a part, et tout va en « Frais bancaire ».

def periode_de(jour) -> str:
    """« YYYY-MM » d'une date (ou de son texte ISO)."""
    return str(jour or "")[:7]


def cle_frais(periode: str) -> str:
    """Reference de l'ecriture du mois : « BIAT frais bancaires MM-YYYY »."""
    annee, mois = periode.split("-")
    return "%s%s-%s" % (PREFIXE_FRAIS, mois, annee)


def cumul_frais(lignes: list, periode: str) -> dict:
    """Le cumul des frais BIAT d'un mois : lignes de debit de categorie « frais_bancaires », hors
    lignes ignorees a la main. -> {periode, cle, total, nb, jour (dernier frais), references}"""
    retenues = [ligne for ligne in lignes
                if (ligne.get("categorie") == "frais_bancaires" and not ligne.get("ignore_manuel")
                    and periode_de(ligne.get("date")) == periode and _m(ligne.get("debit")) > 0)]
    return {"periode": periode, "cle": cle_frais(periode), "nb": len(retenues),
            "total": _m(sum(_m(ligne.get("debit")) for ligne in retenues)),
            "jour": max((str(ligne.get("date")) for ligne in retenues), default=None),
            "references": sorted({(ligne.get("reference") or "").strip() for ligne in retenues} - {""})}


# ---------------------------------------------------------------------------
# Avec le site
# ---------------------------------------------------------------------------

#: Ecart tolere PAR CHEQUE entre le credit de la banque et les paiements d'ERPNext — meme politique que les
#: encaissements Zitouna (`encaissement.matching.TOLERANCE_PAIEMENT`, 1 DT par paiement).
ECART_PAR_PIECE = 1.0
COMPTE_CHEQUES = "Chèques - A&S"


def decider_encaissement(ligne: dict, numero: str, bordereau: dict, paiements: dict) -> dict:
    """Peut-on encaisser cette remise de cheques a partir de son bordereau collecte ? Pur.

    LE BORDEREAU PAPIER TIENT LIEU DE PORTAIL. Zitouna publie le detail de chaque remise, que le robot
    telecharge ; la BIAT ne publie rien : le seul document qui dit quels cheques composent la remise est le
    bordereau depose a la caisse (`BRS Bordereau Remise`), lu par l'IA et rapproche des paiements. Le n° du
    libelle bancaire est le n° imprime sur le papier — c'est le lien.

    `ligne` : {date, credit, reference} du registre ; `bordereau` : None ou {name, nature, lecture_statut,
    a_signaler, pieces: [{numero, montant, paiement, client}]} ; `paiements` : {nom: {montant, compte,
    docstatus, en_encaissement}} des paiements cites par le bordereau.
    -> {faire, raison, lignes: [lignes de « Liste Cheque »], total, ecart, ecarts: [diagnostics « Toléré »]}

    Encaisse seulement ce qui est SUR : bordereau lu sans rien a signaler, chaque cheque encore en
    portefeuille, et un credit bancaire qui tombe sur le total des paiements a `ECART_PAR_PIECE` pres par
    cheque. Tout le reste est refuse AVEC SA RAISON, qui s'affiche sur la ligne de l'identification."""
    non = lambda raison: {"faire": False, "raison": raison, "lignes": [], "total": 0.0, "ecart": 0.0, "ecarts": []}  # noqa: E731
    if not bordereau:
        return non("bordereau à collecter (caisse → Bordereaux de remise)")
    if bordereau.get("nature") != "Chèques":
        return non(f"le bordereau collecté ({bordereau.get('name')}) n’est pas une remise de chèques")
    if bordereau.get("lecture_statut") != "Lue":
        return non("le bordereau n’est pas encore lu par l’IA" if bordereau.get("lecture_statut") != "Échec"
                   else "l’IA n’a pas pu lire le bordereau : relisez-le ou saisissez ses chèques sur sa fiche")
    signale = [l for l in (bordereau.get("a_signaler") or "").split("\n") if l.strip()]
    if signale:
        return non("à signaler sur le bordereau — " + signale[0] + (f" (+{len(signale) - 1})" if len(signale) > 1 else ""))
    pieces = bordereau.get("pieces") or []
    if not pieces:
        return non("aucun chèque sur le bordereau collecté")
    vus, lignes, total = set(), [], 0.0
    for p in pieces:
        nom = p.get("paiement")
        pe = (paiements or {}).get(nom) if nom else None
        if not pe:
            return non(f"chèque n° {p.get('numero') or '?'} : pas de paiement dans ERPNext")
        if nom in vus:
            return non(f"le paiement {nom} est cité deux fois par le bordereau")
        vus.add(nom)
        if int(pe.get("docstatus") or 0) != 1 or pe.get("compte") != COMPTE_CHEQUES:
            return non(f"chèque n° {p.get('numero') or '?'} : le paiement {nom} n’est plus dans le portefeuille "
                       f"(« {pe.get('compte') or 'annulé'} »)")
        if pe.get("en_encaissement"):
            return non(f"chèque n° {p.get('numero') or '?'} : le paiement {nom} est déjà sur un autre encaissement")
        montant = _m(pe.get("montant"))
        total = _m(total + montant)
        lignes.append({"ref_paiement": nom, "n_cheque": p.get("numero"), "valeur": montant, "emmeteur": p.get("client"),
                       "bon_remise": numero, "date": ligne.get("date"), "statut": "Versé"})
    credit = _m(ligne.get("credit") or ligne.get("montant"))
    ecart = _m(credit - total)
    if abs(ecart) > ECART_PAR_PIECE * len(lignes) + 0.0005:
        return non(f"le crédit de la banque ({credit:.3f}) et les chèques du bordereau dans ERPNext ({total:.3f}) "
                   f"diffèrent de {abs(ecart):.3f}")
    # Les petits ecarts, cheque par cheque : traces (« Toléré »), jamais bloquants. On ne les detaille que si la
    # lecture du bordereau est PROUVEE par le credit de la banque (somme des montants lus = credit) ; sinon un
    # montant mal lu passerait pour un ecart de paiement, et seul l'ecart global de la ligne fait foi.
    ecarts = []
    lus = [p.get("montant") for p in pieces]
    if all(v is not None for v in lus) and abs(_m(sum(_m(v) for v in lus)) - credit) <= 0.0005:
        for p, l in zip(pieces, lignes):
            delta = _m(l["valeur"] - _m(p["montant"]))
            if abs(delta) > 0.0005:
                ecarts.append({"type": "ecart", "flux": "cheque", "sous_type": "Toléré", "bloquant": 0,
                               "reason": "ecart <= tolerance", "reference": ligne.get("reference") or "",
                               "credit": credit, "bon": numero, "suivi": p.get("numero") or "",
                               "client": p.get("client"), "ref_paiement": l["ref_paiement"],
                               "montant_piece": l["valeur"], "montant_advice": _m(p["montant"]), "ecart": delta})
    return {"faire": True, "raison": "", "lignes": lignes, "total": total, "ecart": ecart, "ecarts": ecarts}


def compte() -> str:
    """Le compte comptable de la BIAT : celui du Bank Account de la societe, sinon « BIAT - A&S »."""
    return frappe.db.get_value("Bank Account", {"bank": BANQUE, "is_company_account": 1, "disabled": 0}, "account") \
        or "BIAT - A&S"


def charger_contexte(lignes: list) -> dict:
    """Ce qu'ERPNext porte deja pour ces lignes. Lecture seule."""
    numeros, references, remises = set(), set(), {}
    for ligne in lignes:
        sens = ligne.get("sens") or ("Debit" if flt(ligne.get("debit")) else "Credit")
        vu = detecter(ligne.get("operation"), sens)
        if vu["numero"]:
            numeros.add(vu["numero"])
            if vu["regle"] == "enc_cheque":
                remises[vu["numero"]] = flt(ligne.get("credit") or ligne.get("montant"), 3)
        if ligne.get("reference"):
            references.add(ligne["reference"].upper())

    paiements = {}
    for numero in numeros:
        # « BR:7473933 » dans le n° de reference d'un paiement, numero entier (pas « BR:74739331 »).
        lignes_pe = frappe.db.sql(
            """select name, paid_amount as montant, docstatus, paid_to as compte, reference_no
               from `tabPayment Entry` where docstatus < 2 and reference_no like %s""",
            ("%BR:" + numero + "%",), as_dict=True)
        exactes = [p for p in lignes_pe if re.search(r"BR:\s*" + re.escape(numero) + r"(?!\d)", p.reference_no or "")]
        if exactes:
            paiements[numero] = exactes

    le_compte = compte()
    par_reference = {}
    for reference in references:
        ecritures = frappe.db.sql(
            """select distinct je.name, je.docstatus from `tabJournal Entry` je
               join `tabJournal Entry Account` a on a.parent = je.name and a.account = %s
               where je.docstatus < 2 and (je.user_remark like %s or je.cheque_no like %s or je.remark like %s)""",
            (le_compte, f"%{reference}%", f"%{reference}%", f"%{reference}%"), as_dict=True)
        if ecritures:
            par_reference[reference] = ecritures
    versements = frappe.db.sql(
        """select je.name, je.posting_date as date, je.docstatus, a.debit_in_account_currency as montant
           from `tabJournal Entry` je join `tabJournal Entry Account` a on a.parent = je.name
           where je.docstatus < 2 and a.account = %s and a.debit_in_account_currency > 0""",
        (le_compte,), as_dict=True)
    mensuelles = {}
    for e in frappe.db.get_all("Journal Entry", filters={"cheque_no": ["like", PREFIXE_FRAIS + "%"], "docstatus": ["<", 2]},
                               fields=["name", "cheque_no", "docstatus"]):
        mois, annee = e.cheque_no[len(PREFIXE_FRAIS):].split("-")[:2]
        mensuelles[f"{annee}-{mois}"] = {"name": e.name, "docstatus": e.docstatus}
    bordereaux, lectures = {}, {}
    if numeros and frappe.db.exists("DocType", "BRS Bordereau Remise"):
        lu = frappe.db.has_column("BRS Bordereau Remise", "lecture_resume")
        for b in frappe.db.get_all("BRS Bordereau Remise", filters={"banque": BANQUE, "numero": ["in", list(numeros)]},
                                   fields=["name", "numero"] + (["lecture_resume"] if lu else [])):
            bordereaux[b.numero] = b.name
            if b.get("lecture_resume"):
                lectures[b.numero] = b.lecture_resume
    # L'encaissement soumis de chaque bordereau, pour l'ouvrir depuis la ligne.
    encaissements = {}
    if paiements and frappe.db.has_column("Encaissement Paiement", "custom_banque_depot"):
        for nom, bon in frappe.db.sql(
                """select c.parent, c.bon_remise from `tabListe Cheque` c
                   join `tabEncaissement Paiement` e on e.name = c.parent
                   where c.parenttype = 'Encaissement Paiement' and e.docstatus = 1
                     and ifnull(e.custom_banque_depot, '') = %s and c.bon_remise in %s""", (BANQUE, list(paiements))):
            encaissements[bon] = nom
    # Pourquoi une remise dont le bordereau est collecte n'est pas (encore) encaissee.
    refus = {}
    migre = bool(bordereaux) and frappe.db.has_column("Encaissement Paiement", "custom_banque_depot")
    for ligne in (lignes if migre else []):
        vu = detecter(ligne.get("operation"), ligne.get("sens") or ("Debit" if flt(ligne.get("debit")) else "Credit"))
        numero = vu["numero"]
        if vu["regle"] != "enc_cheque" or not numero or numero in paiements or numero not in bordereaux:
            continue
        brouillon = _encaissement_en_cours(numero)
        if brouillon:
            refus[numero] = f"l’encaissement {brouillon} est en brouillon (à soumettre)"
            continue
        b = _bordereau(numero)
        decision = decider_encaissement(ligne, numero, b, _etat_paiements([p.get("paiement") for p in (b or {}).get("pieces", [])]))
        if not decision["faire"]:
            refus[numero] = decision["raison"]
    return {"paiements_par_bordereau": paiements, "ecritures_par_reference": par_reference,
            "ecritures_versement": versements, "remises": remises, "frais_mensuels": mensuelles,
            "bordereaux": bordereaux, "lectures": lectures, "refus": refus, "encaissements": encaissements}


def classer_registre(persist: bool = True, _frais: bool = True) -> dict:
    """Reclasse toutes les lignes BIAT du registre. Un arbitrage humain (ignore, lien manuel) n'est
    jamais ecrase : `apply_classifications` saute les lignes ignorees, et une ligne rattachee a la
    main garde son rattachement. -> {mouvements, kpi, maj?}"""
    lignes = frappe.db.get_all(
        registry.DOCTYPE, filters={"banque": BANQUE}, limit_page_length=0, order_by="date asc, creation asc",
        fields=["name as cle", "date", "operation", "reference", "debit", "credit", "montant", "sens",
                "lien_manuel", "ignore_manuel"])
    contexte = charger_contexte(lignes)
    classements = [classer_ligne(ligne, contexte) for ligne in lignes if not ligne.lien_manuel]
    kpi = {}
    par_cle = {c["cle"]: c for c in classements}
    for ligne in lignes:
        c = par_cle.get(ligne.cle)
        if not c:
            continue
        case = kpi.setdefault(ligne.sens or "Credit", {}).setdefault(c["statut"], {"nb": 0, "montant": 0.0})
        case["nb"] += 1
        case["montant"] = flt(case["montant"] + flt(ligne.montant), 3)
    out = {"mouvements": len(lignes), "kpi": kpi}
    if persist:
        out.update(registry.apply_classifications(classements))
        if _frais:
            # Les frais reconnus rejoignent l'ecriture de leur mois ; on reclasse ensuite, une fois, pour que
            # chaque commission designe cette ecriture.
            out["frais"] = sync_frais()
            if any(f.get("statut") in ("cree", "remplacee", "soumise") for f in out["frais"]):
                suite = classer_registre(persist=True, _frais=False)
                out.update(kpi=suite["kpi"], maj=suite.get("maj"))
    return out


def sync_frais(periode: str = None, insert: bool = True) -> list:
    """Refait l'ecriture mensuelle des frais BIAT de chaque mois qui porte des frais (ou de `periode`).

        Dr  Frais bancaire - A&S     total du mois
        Cr  BIAT - A&S               total du mois

    Memes regles que les frais Zitouna (`expenses/fees.sync_ecriture_mensuelle`) : rien n'est cree tant
    que « Comptabiliser les frais bancaires » n'est pas coche ; une ecriture au bon montant n'est pas
    refaite ; une ecriture remplacee retrouve son etat (soumise si elle l'etait, ou si le site soumet
    automatiquement). -> [{periode, statut, total, je}]"""
    from bank_retenue_sync.encaissement.pending import COMPANY
    from bank_retenue_sync.expenses import fees, journal

    lignes = frappe.db.get_all(
        registry.DOCTYPE, filters={"banque": BANQUE, "categorie": "frais_bancaires"}, limit_page_length=0,
        fields=["name", "date", "reference", "debit", "categorie", "ignore_manuel"])
    periodes = [periode] if periode else sorted({periode_de(ligne.date) for ligne in lignes})
    if not fees.is_enabled():
        return [{"periode": p, "statut": "inactif"} for p in periodes]
    bilan = []
    for p in periodes:
        cumul = cumul_frais(lignes, p)
        existant = frappe.db.get_value("Journal Entry", {"cheque_no": cumul["cle"], "docstatus": ["<", 2]},
                                       ["name", "total_debit", "docstatus"], as_dict=True)
        if not cumul["total"]:
            bilan.append({"periode": p, "statut": "vide", "total": 0.0, "je": existant.name if existant else None})
            continue
        if existant and abs(_m(existant.total_debit) - cumul["total"]) < 0.005:
            statut = "inchangee"
            if insert and existant.docstatus == 0 and journal._auto_submit_enabled():
                try:
                    frappe.get_doc("Journal Entry", existant.name).submit()
                    statut = "soumise"
                except Exception:
                    frappe.log_error(frappe.get_traceback(), "frais BIAT : soumission %s" % existant.name)
            bilan.append({"periode": p, "statut": statut, "total": cumul["total"], "je": existant.name})
            continue
        etait_soumise = bool(existant and existant.docstatus == 1)
        remplacee = "%s (%s)" % (existant.name, fees._supprimer_ecriture(existant.name)) if existant and insert else None
        jour = fees.date_ecriture(cumul["jour"])
        centre = frappe.db.get_value("Company", COMPANY, "cost_center")
        je = journal.build_journal_entry(
            COMPANY, jour,
            [{"account": compte(), "credit": cumul["total"]},
             {"account": COMPTE_FRAIS, "debit": cumul["total"], "cost_center": centre}],
            remark="%s\n%d opérations bancaires cumulées\nRéf. banque %s" % (
                cumul["cle"], cumul["nb"], ", ".join(cumul["references"][:20])),
            cheque_no=cumul["cle"], cheque_date=jour)
        if insert:
            je.insert(ignore_permissions=True)
            if etait_soumise or journal._auto_submit_enabled():
                je.submit()
        bilan.append({"periode": p, "statut": "remplacee" if remplacee else "cree", "total": cumul["total"],
                      "je": je.name if insert else "(essai)", "remplacee": remplacee})
    return bilan


def _importer(mouvements: list, origine: str) -> dict:
    out = registry.upsert_movements(mouvements, origine=origine, banque=BANQUE)
    out.pop("cles", None)
    out["encaissements"] = encaisser_remises()
    out.update(classer_registre(persist=True))
    return out


def importer_fichier(contenu: bytes) -> dict:
    """Range au registre un export de mouvements BIAT (le fichier .xlsx produit par le service) puis
    reclasse. Idempotent : rejouer le meme fichier ne cree aucun doublon."""
    from bank_retenue_sync.bank.movements import _parse_movements_xlsx

    return _importer(_parse_movements_xlsx(contenu), "manuel")   # `origine` : export | bank_transaction | manuel


def rafraichir(lookback_days: int = None, timeout: int = 300) -> dict:
    """Demande au service un nouvel export BIAT, le range au registre, puis reclasse. Non fatal :
    si le service ne repond pas, ou si le portail redemande un code SMS, le registre reste tel quel
    et l'erreur est rapportee."""
    import requests

    from bank_retenue_sync.bank import movements as mv

    jours = int(lookback_days or 45)
    out = {"export": jours, "erreur": None}
    try:
        job = mv.start_job("/jobs/biat/mouvements/export",
                           {"date_from": str(add_days(nowdate(), -jours)), "date_to": nowdate()})
        mv.wait_job(job, timeout=timeout)
        r = requests.get(mv._base_url() + "/biat/mouvements/export/latest", headers=mv._headers(), timeout=60)
        r.raise_for_status()
        out.update(_importer(mv._parse_movements_xlsx(r.content), "export"))
    except Exception as e:
        out["erreur"] = str(e)[:300]
        frappe.log_error(f"rafraichissement BIAT echoue\n{e}", "brs page")
        out.update(classer_registre(persist=True))
    return out


# ---------------------------------------------------------------------------
# Encaissement des remises de cheques, a partir du bordereau collecte
# ---------------------------------------------------------------------------

def _bordereau(numero: str):
    """Le bordereau BIAT collecte sous ce numero, avec ses pieces — ou None."""
    name = frappe.db.get_value("BRS Bordereau Remise", {"banque": BANQUE, "numero": numero}, "name")
    if not name:
        return None
    doc = frappe.get_doc("BRS Bordereau Remise", name)
    return {"name": doc.name, "nature": doc.nature, "lecture_statut": doc.lecture_statut, "a_signaler": doc.a_signaler,
            "pieces": [{"numero": p.numero, "montant": p.montant, "paiement": p.paiement, "client": p.client}
                       for p in doc.pieces]}


def _etat_paiements(noms: list) -> dict:
    from bank_retenue_sync.encaissement import pending

    noms = [n for n in noms if n]
    if not noms:
        return {}
    deja = pending.already_encaissed_refs()
    return {pe.name: {"montant": pe.paid_amount, "compte": pe.paid_to, "docstatus": pe.docstatus,
                      "en_encaissement": pe.name in deja}
            for pe in frappe.get_all("Payment Entry", filters={"name": ["in", noms]},
                                     fields=["name", "paid_amount", "paid_to", "docstatus"])}


def _encaissement_en_cours(numero: str):
    """Un Encaissement Paiement en BROUILLON qui porte deja ce bordereau (soumission automatique coupee, ou
    soumission precedente en echec) — pour ne pas en creer un second."""
    lignes = frappe.db.sql(
        """select distinct c.parent from `tabListe Cheque` c
           join `tabEncaissement Paiement` e on e.name = c.parent
           where c.parenttype = 'Encaissement Paiement' and c.bon_remise = %s and e.docstatus = 0
             and ifnull(e.custom_banque_depot, '') = %s""", (numero, BANQUE))
    return lignes[0][0] if lignes else None


def _soumission_automatique() -> bool:
    return bool(frappe.utils.cint(frappe.db.get_single_value("Bank Retenue Sync Settings", "encaissement_auto_submit")))


def encaisser_remises(insert: bool = True) -> dict:
    """LE FLUX CONTINU (demande du 16/11/2026 : « je veux tout automatiser, pas de validation ; s'il y a des
    ecarts c'est facile a trouver »). Pour chaque remise de cheques BIAT creditee dont le bordereau est
    collecte et bon : un Encaissement Paiement est cree avec ses cheques, banque de depot BIAT, puis soumis
    (meme interrupteur `encaissement_auto_submit` que Zitouna). Le server script « Traitement des
    encaissement » fait le reste : chaque cheque quitte « Chèques - A&S » pour le compte BIAT et porte
    « BR:<n°> », ce que le classement reconnait.

    Idempotent : une remise deja encaissee (paiements « BR:<n°> ») ou deja en brouillon n'est pas refaite.
    Rien n'est force : ce qui n'est pas sur est laisse, avec sa raison sur la ligne de l'identification.
    -> {encaisses: [{bordereau, encaissement, soumis, total, ecart}], refus: {n°: raison}, erreurs: [...]}"""
    from bank_retenue_sync.encaissement import builder
    from bank_retenue_sync.encaissement import ecarts as _ecarts

    out = {"encaisses": [], "refus": {}, "erreurs": []}
    if not frappe.db.exists("DocType", "BRS Bordereau Remise") \
            or not frappe.db.has_column("Encaissement Paiement", "custom_banque_depot"):
        return out                                     # pas encore migre : rien a faire, rien a casser
    # ⚠️ LE SCRIPT COMPTABLE DOIT CONNAITRE LA BANQUE DE DEPOT. C'est lui (customization_app, « Traitement des
    # encaissement ») qui choisit le compte ; une version qui ne lit pas `custom_banque_depot` enverrait une
    # remise BIAT sur le compte Zitouna. Les deux apps se deploient separement : on verifie, on ne suppose pas.
    script = frappe.db.get_value("Server Script", {"name": "Traitement des encaissement", "disabled": 0}, "script") or ""
    if "custom_banque_depot" not in script:
        out["erreurs"].append({"erreur": "le script « Traitement des encaissement » ne lit pas encore la banque de "
                                         "dépôt (customization_app à déployer) : aucune remise BIAT n’est encaissée"})
        return out
    lignes = frappe.db.get_all(
        registry.DOCTYPE, filters={"banque": BANQUE, "sens": "Credit", "ignore_manuel": 0}, limit_page_length=0,
        order_by="date asc, creation asc",
        fields=["name as cle", "date", "operation", "reference", "credit", "montant", "sens", "lien_manuel"])
    auto = _soumission_automatique()
    utilisateur = frappe.session.user
    if insert:
        # UN SEUL PASSAGE A LA FOIS. Le depot d'un bordereau et le passage planifie peuvent tomber ensemble :
        # sans verrou, chacun verrait la remise « pas encore encaissee » et creerait le sien.
        from frappe.utils.file_lock import LockTimeoutError
        from frappe.utils.synchronization import filelock

        try:
            with filelock("brs_biat_encaissement", timeout=120):
                return _encaisser(lignes, auto, utilisateur, out, True, builder, _ecarts)
        except LockTimeoutError:
            out["erreurs"].append({"erreur": "un autre passage BIAT est en cours : remis au suivant"})
            return out
    return _encaisser(lignes, auto, utilisateur, out, False, builder, _ecarts)


def _encaisser(lignes, auto, utilisateur, out, insert, builder, _ecarts) -> dict:
    try:
        if insert:
            # Le server script annule et recree des paiements : il lui faut les droits de la comptabilite,
            # que n'a pas la personne dont le depot de bordereau declenche ce passage.
            frappe.set_user("Administrator")
        for ligne in lignes:
            if ligne.lien_manuel:
                continue
            vu = detecter(ligne.operation, "Credit")
            numero = vu["numero"]
            if vu["regle"] != "enc_cheque" or not numero:
                continue
            if frappe.db.sql("select name from `tabPayment Entry` where docstatus < 2 and reference_no regexp %s limit 1",
                             ("BR:[[:space:]]*" + numero + "([^0-9]|$)",)):
                continue                               # deja encaissee
            brouillon = _encaissement_en_cours(numero)
            if not brouillon:
                bordereau = _bordereau(numero)
                decision = decider_encaissement(
                    ligne, numero, bordereau, _etat_paiements([p.get("paiement") for p in (bordereau or {}).get("pieces", [])]))
                if not decision["faire"]:
                    out["refus"][numero] = decision["raison"]
                    continue
                if not insert:
                    out["encaisses"].append({"bordereau": numero, "encaissement": "(essai)", "soumis": False,
                                             "total": decision["total"], "ecart": decision["ecart"]})
                    continue
                doc = builder.build_encaissement(decision["lignes"], insert=False)
                doc.custom_banque_depot = BANQUE
                doc.insert(ignore_permissions=True)
                _ecarts.persister(doc.name, decision["ecarts"])
                frappe.db.commit()                     # le brouillon existe, quoi qu'il arrive a la soumission
                brouillon, total, ecart = doc.name, decision["total"], decision["ecart"]
            else:
                total = ecart = None
                if not insert:
                    continue
            soumis = False
            if auto:
                try:
                    frappe.get_doc("Encaissement Paiement", brouillon).submit()
                    frappe.db.commit()
                    soumis = True
                except Exception as e:
                    frappe.db.rollback()
                    frappe.log_error(title=f"BIAT : soumission de l'encaissement {brouillon}", message=frappe.get_traceback())
                    out["erreurs"].append({"bordereau": numero, "encaissement": brouillon, "erreur": str(e)[:300]})
            out["encaisses"].append({"bordereau": numero, "encaissement": brouillon, "soumis": soumis,
                                     "total": total, "ecart": ecart})
    finally:
        if insert and frappe.session.user != utilisateur:
            frappe.set_user(utilisateur)
    return out


def actif() -> bool:
    try:
        return bool(frappe.db.get_single_value("Bank Retenue Sync Settings", "enabled"))
    except Exception:
        return False


def passage(refresh: bool = None) -> dict:
    """Un passage BIAT complet : releve (si le robot est actif), encaissement des remises, classement.
    Appele par le planificateur apres chaque verification Zitouna, et par le depot d'un bordereau."""
    if not actif():
        return {}
    if refresh is None:
        try:
            refresh = bool(frappe.utils.cint(frappe.db.get_single_value("Bank Retenue Sync Settings", "biat_robot_actif")))
        except Exception:
            refresh = False                            # reglage pas encore migre : pas de releve
    if refresh:
        return rafraichir()                            # importe, encaisse et classe
    out = {"encaissements": encaisser_remises()}
    out.update(classer_registre(persist=True))
    return out


def passage_job():
    """Declenche par le depot (ou la reverification) d'un bordereau BIAT : la remise est peut-etre deja
    creditee, auquel cas elle s'encaisse tout de suite. Jamais de releve ici — le robot a ses heures."""
    try:
        out = passage(refresh=False)
        frappe.db.commit()
        return out
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title="BIAT : passage après dépôt d'un bordereau", message=frappe.get_traceback())
