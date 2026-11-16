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
            suite = f"bordereau collecté ({collecte}), chèques à rapprocher" if collecte \
                else "bordereau à collecter (caisse → Bordereaux de remise)"
            out.update(statut=STATUT_ORPHELIN, raison=f"remise de chèques, bordereau {numero} : aucun paiement "
                       f"ne porte « BR:{numero} » — {suite}")
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
    bordereaux = {}
    if numeros and frappe.db.exists("DocType", "BRS Bordereau Remise"):
        for b in frappe.db.get_all("BRS Bordereau Remise", filters={"banque": BANQUE, "numero": ["in", list(numeros)]},
                                   fields=["name", "numero"]):
            bordereaux[b.numero] = b.name
    return {"paiements_par_bordereau": paiements, "ecritures_par_reference": par_reference,
            "ecritures_versement": versements, "remises": remises, "frais_mensuels": mensuelles,
            "bordereaux": bordereaux}


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
