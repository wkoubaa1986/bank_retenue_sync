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
            out.update(statut=STATUT_ORPHELIN, raison=f"remise de chèques, bordereau {numero} : aucun paiement "
                       f"ne porte « BR:{numero} » — chèques à rapprocher")
            return out
        total = _m(sum(_m(p["montant"]) for p in paiements))
        brouillon = any(int(p.get("docstatus") or 0) == 0 for p in paiements)
        out.update(statut=STATUT_BROUILLON if brouillon else STATUT_IDENTIFIE, document_type="Payment Entry",
                   document_name=paiements[0]["name"] if len(paiements) == 1 else None,
                   montant_document=total, ecart=_m(montant - total))
        detail = f"remise de chèques, bordereau {numero} : {len(paiements)} paiement(s) portent « BR:{numero} »"
        if abs(out["ecart"]) > TOLERANCE_REMISE:
            detail += f" pour {total:.3f} — écart de {out['ecart']:.3f} avec le crédit de la banque"
        out["raison"] = detail
        return out

    if vu["regle"] == "com_enc_cheque":
        ecritures = contexte.get("ecritures_par_reference", {}).get((ligne.get("reference") or "").upper(), [])
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
# Avec le site
# ---------------------------------------------------------------------------

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
    return {"paiements_par_bordereau": paiements, "ecritures_par_reference": par_reference,
            "ecritures_versement": versements, "remises": remises}


def classer_registre(persist: bool = True) -> dict:
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
    return out


def _importer(mouvements: list, origine: str) -> dict:
    out = registry.upsert_movements(mouvements, origine=origine, banque=BANQUE)
    out.pop("cles", None)
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
