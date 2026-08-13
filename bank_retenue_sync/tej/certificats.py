"""Certificats de retenue a la source RECUS (portail TEJ), lus chez tej-bank-service.

CE QUE C'EST, ET POURQUOI ON LE RECUPERE
----------------------------------------
Quand un client marche public nous paie, il retient 1 % du TTC et le verse au Tresor a notre nom.
Cette retenue est un CREDIT D'IMPOT : elle vaut argent, a condition de detenir le certificat qui
la prouve. Le certificat est declare par le CLIENT sur le portail TEJ ; nous n'avons aucune prise
dessus, seulement le droit de le lire. C'est la seule source qui existe — d'ou ce flux.

En face, ERPNext porte deja la retenue sous forme d'une Payment Entry au mode « Retenue a la
source vente ». Le present module ne remplace pas cette saisie : il la CONFRONTE au portail, pour
repondre a une question que personne ne pouvait poser jusqu'ici — « toutes les retenues declarees
par mes clients sont-elles dans mes comptes, et ai-je le justificatif de chacune ? »

⚠️ TROIS PIEGES DU FICHIER REEL, TOUS SILENCIEUX
------------------------------------------------
1. Les montants portent un ESPACE INSECABLE comme separateur de milliers (« 5 376.000 »). Le
   `_num` partage de `bank/movements.py` ne retire que l'espace ordinaire et rend 0.0 sur echec :
   92 certificats a zero, sans un message. D'ou `_num_tej`, qui LEVE plutot que de rendre zero.
2. Les dates sont en `JJ-MM-AAAA`. Le format a ete ajoute a `movements._to_date`, en dernier pour
   que l'ISO garde la priorite.
3. Les raisons sociales contiennent des espaces multiples (« SOCIETE FM WATER      PLUS »). Sans
   normalisation, deux libelles identiques a l'oeil donnent deux alias differents.

L'ETAT EST LA REGLE LA PLUS IMPORTANTE
--------------------------------------
Comme le releve de carte a ses refus, l'export TEJ a ses annulations : un certificat `ANNULE` a ete
RETIRE par le declarant. Aucune retenue n'a eu lieu, il n'y a rien a comptabiliser et rien a
justifier. Il est malgre tout ingere et affiche — c'est en le voyant ECARTE qu'on verifie qu'il
l'a bien ete.
"""
from __future__ import annotations

import hashlib
import io
import json

import frappe
import requests
from frappe.utils import flt, getdate

from bank_retenue_sync.bank.movements import _base_url, _col, _headers, _to_date

DOCTYPE = "Retenue Certificate"

# Route de pull, calquee sur les flux banque : le service scrape, nous lisons son export.
ROUTE_EXPORT = "/tej/certificats-recus/export/latest"
ROUTE_JOB_EXPORT = "/jobs/tej/certificats-recus/export"

# Annee a partir de laquelle un certificat est traite. Anterieur = ingere pour l'historique, mais
# jamais rapproche ni comptabilise : reprendre 2025 rouvrirait des exercices clos.
ANNEE_MINIMALE = 2026

ETAT_RECUE, ETAT_RECTIFIE, ETAT_ANNULE, ETAT_AUTRE = "Recue", "Rectifie", "Annule", "Autre"

TAUX_ATTENDU = 1.0          # 1 % du TTC — verifie sur 91 lignes de l'export reel sur 92
TOLERANCE_TAUX = 0.05

# Espaces vus dans les montants du portail : insecable, fine insecable, fine, chiffre, ordinaire.
_ESPACES = "\xa0    "


def _texte(v) -> str:
    """Texte propre : espaces insecables ramenes a l'espace, doublons ecrases."""
    if v is None:
        return ""
    return " ".join(str(v).replace("\xa0", " ").split())


def _num_tej(v) -> float:
    """Montant du portail. LEVE si illisible — un montant faux vaut mieux absent que zero.

    `bank/movements._num` rend 0.0 en cas d'echec, ce qui convient a une colonne bancaire vide.
    Ici, un zero silencieux ferait disparaitre la retenue tout en laissant le certificat valide :
    l'erreur deviendrait invisible. Une ligne illisible doit etre comptee en echec, pas lue.
    """
    if v in (None, ""):
        return 0.0
    if isinstance(v, (int, float)):
        return round(float(v), 3)
    s = str(v)
    for e in _ESPACES:
        s = s.replace(e, "")
    s = s.replace(",", ".")
    return round(float(s), 3)          # ValueError volontairement non rattrapee


def fetch_latest_certificats() -> tuple:
    """Dernier export de certificats recus. -> (contenu brut, lignes normalisees).

    Le contenu brut est rendu avec les lignes : son empreinte sert a reconnaitre un export
    inchange, et donc a ne rien refaire.
    """
    r = requests.get(_base_url() + ROUTE_EXPORT, headers=_headers(), timeout=120)
    r.raise_for_status()
    return r.content, parse_certificats_xlsx(r.content)


def empreinte(contenu: bytes) -> str:
    return hashlib.sha1(contenu or b"").hexdigest()


def parse_certificats_xlsx(content: bytes) -> list:
    """XLSX -> [{reference, declarant, declarant_matricule, ttc, retenue, ...}]. Sans base."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    if not rows:
        return []
    header = [_texte(c) for c in rows[0]]
    out = []
    for raw in rows[1:]:
        d = dict(zip(header, raw))
        ligne = _ligne(d)
        if ligne:
            out.append(ligne)
    return out


def _ligne(d: dict) -> dict:
    """Une ligne de l'export, normalisee. None si elle ne porte pas de reference (ligne vide)."""
    reference = _texte(_col(d, "Référence de certificat", "Reference de certificat"))
    if not reference:
        return None
    ht = _num_tej(_col(d, "totalMontantHT"))
    tva = _num_tej(_col(d, "totalMontantTVA"))
    retenue = _num_tej(_col(d, "totalMontantRS"))
    jour = _to_date(_texte(_col(d, "Date de paiement")))
    return {
        "reference": reference,
        "declarant": _texte(_col(d, "Raison social du déclarant", "Raison social du declarant")),
        "declarant_matricule": _texte(_col(d, "Déclarant", "Declarant")),
        "numero_chez_declarant": _texte(_col(d, "Numéro chez le déclarant",
                                             "Numero chez le declarant")),
        "date_paiement": jour,
        "etat_source": _texte(_col(d, "État", "Etat")),
        "type_generation": _texte(_col(d, "Type de génération", "Type de generation")),
        "total_ht": ht,
        "total_tva": tva,
        "total_brut": round(ht + tva, 3),          # assiette de la retenue
        "montant_retenue": retenue,
        "fichier_declare": _texte(_col(d, "Fichier déclaré", "Fichier declare")),
        "beneficiaire_matricule": _texte(_col(d, "Identifiant du bénéficiaire",
                                              "Identifiant du beneficiaire")),
        "beneficiaire": _texte(_col(d, "Raison social du bénéficiaire",
                                    "Raison social du beneficiaire")),
        "date_creation_certificat": _to_date(_texte(_col(d, "Date de création",
                                                         "Date de creation"))),
    }


def normaliser_etat(brut) -> str:
    """« REÇUE » -> Recue, « ANNULÉ » -> Annule. Insensible aux accents et a la casse.

    Un libelle inconnu devient `Autre` et n'est jamais devine : l'etat brut reste conserve a cote,
    lisible sans relancer un scraping.
    """
    s = _texte(brut).upper()
    s = (s.replace("Ç", "C").replace("É", "E").replace("È", "E").replace("Ê", "E")
          .replace("À", "A").replace("Û", "U"))
    if s.startswith("RECU"):
        return ETAT_RECUE
    if s.startswith("RECTIF"):
        return ETAT_RECTIFIE
    if s.startswith("ANNUL"):
        return ETAT_ANNULE
    return ETAT_AUTRE if s else ETAT_AUTRE


def taux(ligne: dict):
    """Taux reel, ou None si l'assiette est nulle (jamais 999,98 %)."""
    brut = flt(ligne.get("total_brut"), 3)
    return round(flt(ligne.get("montant_retenue"), 3) / brut * 100, 3) if brut else None


def anomalie(ligne: dict, matricule_beneficiaire: str = None) -> str:
    """Raison d'anomalie, ou None. Une anomalie n'empeche pas l'ingestion, elle interdit
    l'automatisme : on garde la trace, on ne comptabilise pas a l'aveugle."""
    from bank_retenue_sync.tej import matricule as MF

    brut = flt(ligne.get("total_brut"), 3)
    retenue = flt(ligne.get("montant_retenue"), 3)
    if retenue and retenue > brut and brut:
        return "retenue (%s) superieure a l'assiette (%s)" % (retenue, brut)
    if retenue and not brut:
        # Ligne reelle de l'export : HT et TVA a zero, retenue non nulle. Le taux vaudrait
        # 999,98 % — un nombre qui a l'air d'une donnee sans en etre une.
        return "assiette nulle pour une retenue de %s" % retenue
    if matricule_beneficiaire:
        recu = MF.normaliser(ligne.get("beneficiaire_matricule"))
        attendu = MF.normaliser(matricule_beneficiaire)
        if recu and attendu and recu != attendu:
            # Garde-fou : sans lui, un export mal filtre cote portail injecterait les certificats
            # d'un autre contribuable dans nos comptes.
            return "certificat d'un autre beneficiaire (%s)" % ligne.get("beneficiaire_matricule")
    t = taux(ligne)
    if t is not None and abs(t - TAUX_ATTENDU) > TOLERANCE_TAUX:
        return "taux inhabituel (%s %%)" % t
    return None


def hors_perimetre(ligne: dict, annee_min: int = None) -> bool:
    """Anterieur a l'annee suivie -> historique seulement."""
    annee_min = ANNEE_MINIMALE if annee_min is None else annee_min
    jour = ligne.get("date_paiement")
    if not jour:
        return True                     # sans date, on ne peut pas affirmer le perimetre
    return getdate(jour).year < annee_min


def est_exploitable(ligne: dict, etat: str = None, raison_anomalie: str = None,
                    annee_min: int = None) -> bool:
    """Rapprochable et comptabilisable : etat exploitable, sans anomalie, dans le perimetre."""
    etat = etat or normaliser_etat(ligne.get("etat_source"))
    if etat not in (ETAT_RECUE, ETAT_RECTIFIE):
        return False
    if raison_anomalie is None:
        raison_anomalie = anomalie(ligne)
    return not raison_anomalie and not hors_perimetre(ligne, annee_min)


# ------------------------------------------------------------------ persistance

# Champs poses par la machine a l'ingestion : ils suivent la source a chaque passage.
CHAMPS_SOURCE = ("declarant", "declarant_matricule", "numero_chez_declarant", "date_paiement",
                 "type_generation", "total_ht", "total_tva", "total_brut", "montant_retenue",
                 "fichier_declare", "beneficiaire", "beneficiaire_matricule",
                 "date_creation_certificat")

# Champs qu'un humain peut avoir arbitres : la machine n'y touche plus des lors que le certificat
# porte `Manually Matched`. C'est le point ou l'utilisateur perdrait confiance si on repassait
# derriere lui.
CHAMPS_HUMAINS = ("customer", "payment_entry", "sales_invoice", "sales_order", "match_status",
                  "match_method", "match_score", "match_raison")


def _matricule_societe() -> str:
    try:
        company = frappe.db.get_single_value("Bank Retenue Sync Settings", "company")
        return frappe.db.get_value("Company", company, "tax_id") if company else None
    except Exception:
        return None


class _StoreFrappe:
    """Acces base isole derriere trois methodes : les tests injectent un faux et tournent sans
    base, conformement a la convention de l'app."""

    def get(self, reference):
        return frappe.db.get_value(DOCTYPE, reference,
                                   ["name", "match_status", "customer", "etat_depot"], as_dict=1)

    def insert(self, valeurs):
        doc = frappe.new_doc(DOCTYPE)
        doc.update(valeurs)
        doc.flags.ignore_arbitrage = True
        doc.insert(ignore_permissions=True)
        return doc.name

    def update(self, reference, valeurs):
        frappe.db.set_value(DOCTYPE, reference, valeurs, update_modified=False)


def upsert_certificats(lignes: list, sync_run: str = None, store=None,
                       annee_min: int = None, matricule_beneficiaire=None) -> dict:
    """Insere les certificats inconnus, rafraichit les connus. IDEMPOTENT.

    Re-ingerer une periode chevauchante ne cree rien et ne perd aucune decision : c'est la
    propriete la plus importante du module, et celle que les tests verifient en premier.

    Sur un certificat deja arbitre a la main, seuls les champs SOURCE sont rafraichis. Le client,
    la piece et le statut poses par un humain survivent a toutes les synchronisations suivantes.
    """
    store = store or _StoreFrappe()
    if matricule_beneficiaire is None:
        matricule_beneficiaire = _matricule_societe()
    res = {"crees": 0, "revus": 0, "annules": 0, "anomalies": 0, "hors_perimetre": 0,
           "echecs": 0, "references": [], "signales": []}

    for ligne in lignes:
        reference = ligne.get("reference")
        if not reference:
            res["echecs"] += 1
            continue
        etat = normaliser_etat(ligne.get("etat_source"))
        raison = anomalie(ligne, matricule_beneficiaire)
        hors = hors_perimetre(ligne, annee_min)
        valeurs = {k: ligne.get(k) for k in CHAMPS_SOURCE}
        valeurs.update({
            "etat_depot": etat,
            "etat_source": ligne.get("etat_source"),
            "hors_perimetre": int(hors),
            "anomalie": int(bool(raison)),
            "anomalie_raison": raison,
            "sync_run": sync_run,
            "raw_payload": json.dumps(ligne, default=str, ensure_ascii=False),
        })
        if raison:
            res["anomalies"] += 1
        if hors:
            res["hors_perimetre"] += 1
        if etat == ETAT_ANNULE:
            res["annules"] += 1

        existant = store.get(reference)
        if not existant:
            valeurs["reference"] = reference
            valeurs["match_status"] = "Ignore" if etat == ETAT_ANNULE else "Unmatched"
            valeurs["match_raison"] = (
                "certificat annule au portail : aucune retenue a comptabiliser"
                if etat == ETAT_ANNULE else None)
            store.insert(valeurs)
            res["crees"] += 1
            res["references"].append(reference)
            continue

        # ⚠️ Un certificat rapproche puis ANNULE au portail : on signale, on ne defait rien.
        # Retirer un lien comptable ou une piece jointe automatiquement est irreversible pour
        # l'utilisateur ; le lui dire suffit, il tranchera.
        if etat == ETAT_ANNULE and (existant.get("match_status") or "") not in ("Ignore",
                                                                               "Unmatched"):
            valeurs["revue_requise"] = 1
            valeurs["match_raison"] = ("annule au portail APRES rapprochement : retirer la piece "
                                       "justificative et verifier l'ecriture")
            res["signales"].append(reference)
        elif etat == ETAT_RECTIFIE:
            valeurs["revue_requise"] = 1

        if (existant.get("match_status") or "") == "Manually Matched":
            for champ in CHAMPS_HUMAINS:
                valeurs.pop(champ, None)
        store.update(reference, valeurs)
        res["revus"] += 1
        res["references"].append(reference)
    return res


DOCTYPE_RUN = "BRS Sync Run"
KIND = "tej_certificats_recus"


def synchroniser(refresh: bool = False, insert: bool = True, annee_min: int = None) -> dict:
    """Export -> certificats. Rend {statut, run, ...compteurs}.

    L'empreinte du fichier fait office de garde : le portail n'est alimente que quelques fois
    par mois, alors que la tache tourne tous les jours. Un export identique a celui de la veille
    s'arrete AVANT tout parsing et tout ecrit — `payload_hash` etant unique, c'est la base qui
    tranche, pas une comparaison qu'on pourrait oublier de faire.
    """
    if refresh:
        try:
            refresh_certificats()
        except Exception as e:
            # Non fatal, comme partout ailleurs : le dernier export vaut mieux que rien.
            frappe.log_error("refresh certificats TEJ : %s" % str(e)[:120], "brs tej")

    contenu, lignes = fetch_latest_certificats()
    signature = empreinte(contenu)
    if not insert:
        # Essai a blanc : on decrit ce qui serait fait, on n'ecrit ni run ni certificat.
        return {"statut": "essai a blanc", "empreinte": signature, "lignes": len(lignes),
                "apercu": apercu(lignes, annee_min)}

    if frappe.db.exists(DOCTYPE_RUN, {"payload_hash": signature}):
        return {"statut": "export inchange", "empreinte": signature, "lignes": len(lignes)}

    run = frappe.new_doc(DOCTYPE_RUN)
    run.update({"kind": KIND, "status": "Running", "started_at": frappe.utils.now_datetime(),
                "payload_hash": signature, "rows_received": len(lignes)})
    run.insert(ignore_permissions=True)

    try:
        res = upsert_certificats(lignes, sync_run=run.name, annee_min=annee_min)
    except Exception as e:
        run.update({"status": "Failed", "finished_at": frappe.utils.now_datetime(),
                    "error_log": frappe.get_traceback()})
        run.save(ignore_permissions=True)
        raise e

    run.update({"status": "Partial" if res["echecs"] else "Success",
                "finished_at": frappe.utils.now_datetime(),
                "rows_created": res["crees"], "rows_duplicate": res["revus"],
                "rows_failed": res["echecs"]})
    run.save(ignore_permissions=True)
    return {"statut": "synchronise", "run": run.name, "empreinte": signature, **res}


def apercu(lignes: list, annee_min: int = None) -> dict:
    """Ce que l'export contient, sans rien ecrire — pour l'essai a blanc et le controle."""
    out = {"total": len(lignes), "perimetre": 0, "annules": 0, "anomalies": 0,
           "hors_perimetre": 0, "retenue_perimetre": 0.0}
    matricule = _matricule_societe()
    for l in lignes:
        etat = normaliser_etat(l.get("etat_source"))
        raison = anomalie(l, matricule)
        hors = hors_perimetre(l, annee_min)
        if etat == ETAT_ANNULE:
            out["annules"] += 1
        if raison:
            out["anomalies"] += 1
        if hors:
            out["hors_perimetre"] += 1
            continue
        out["perimetre"] += 1
        out["retenue_perimetre"] = round(out["retenue_perimetre"]
                                         + flt(l.get("montant_retenue"), 3), 3)
    return out


def refresh_certificats(timeout: int = 900) -> dict:
    """Demande au service un nouvel export, puis attend qu'il aboutisse.

    Non fatal par contrat, comme les autres flux : si le portail est indisponible, le dernier
    export exploitable vaut mieux qu'une synchronisation qui echoue.
    """
    from bank_retenue_sync.bank import movements

    job = movements.start_job(ROUTE_JOB_EXPORT)
    return movements.wait_job(job, timeout=timeout)
