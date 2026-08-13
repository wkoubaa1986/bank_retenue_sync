"""Prets et leasings : ecritures declenchees par la banque, a partir de l'echeancier.

FORME DE L'ECRITURE — reprise des saisies manuelles existantes (ACC-JV-2026-00461/00462) :
UNE ecriture par echeance, a QUATRE lignes, avec DEUX credits distincts sur le compte bancaire :

    Cr  STE430127B - Zitouna - A&S      interet
    Cr  STE430127B - Zitouna - A&S      principal
    Dr  Prets garantis - A&S            principal
    Dr  Frais bancaire Emprunt - A&S    interet

C'est bien deux lignes de credit et non une seule somme : c'est ainsi que les ecritures
historiques sont faites, et les conserver telles quelles rend la comparaison avec l'existant
immediate.

COMMENT ON RECONNAIT LE CONTRAT
-------------------------------
Les deux prets partagent le MEME libelle bancaire (« PAIEMENT PRINCIPAL/PROFIT TAMOUIL CHIRAET ») :
le texte ne les distingue pas. En revanche, le principal et le profit d'un meme jour se somment en
un TOTAL MENSUEL CONSTANT, propre a chaque contrat (17 705,228 pour le pret nantissement,
14 134,538 pour la ligne de credit). C'est ce total qui identifie le contrat.

Consequence : on n'ecrit RIEN tant que les deux mouvements du jour ne sont pas tous les deux
presents. Une echeance a moitie imputee serait pire que pas d'ecriture du tout.
"""
from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.bank import rules as R
from bank_retenue_sync.expenses import journal

SETTINGS = "Bank Retenue Sync Settings"
TABLE_FIELD = "contrats_financement"


def load_contrats(only=None) -> list:
    """Contrats actifs declares dans les Settings."""
    rows = []
    try:
        if frappe.db and frappe.db.exists("DocType", SETTINGS):
            doc = frappe.get_cached_doc(SETTINGS)
            rows = [r.as_dict() for r in (doc.get(TABLE_FIELD) or [])]
    except Exception:
        rows = []
    rows = [r for r in rows if r.get("actif")]
    if only:
        only = {only} if isinstance(only, str) else set(only)
        rows = [r for r in rows if r.get("cle") in only]
    return rows


def numero_echeance(contrat: dict, jour) -> int:
    """Rang de l'echeance depuis la date de debut (le « 6 » de « Remboursement 6/10 »)."""
    debut = contrat.get("date_debut")
    if not debut:
        return 0
    debut, jour = getdate(debut), getdate(jour)
    return (jour.year - debut.year) * 12 + (jour.month - debut.month) + 1


def build_reference(contrat: dict, jour) -> str:
    n = numero_echeance(contrat, jour)
    total = int(contrat.get("nb_echeances") or 0)
    jour = getdate(jour)
    modele = contrat.get("template_reference") or (contrat.get("libelle") or contrat["cle"])
    return modele.format(n=n, total=total, mm="%02d" % jour.month, yyyy="%04d" % jour.year)


def paires_du_releve(movements: list) -> list:
    """Regroupe par (jour, total) les mouvements principal + profit d'une meme echeance.

    Rend [{jour, principal, interet, assurance, total, references}].
    L'assurance (PRIME TAKAFUL) est rattachee au groupe du jour mais n'entre PAS dans le total
    servant a identifier le contrat : les ecritures manuelles ne la comptent pas dans l'echeance.
    """
    par_jour = defaultdict(lambda: {"principal": [], "profit": [], "assurance": []})
    for m in movements or []:
        if not flt(m.get("debit"), 3):
            continue
        rule = R.find_rule(m)
        if not rule or rule.categorie != "pret":
            continue
        # `.get` et non `[...]` : la categorie « pret » contient AUSSI `tva_bancaire` et
        # `droit_timbre` (ce sont des composants d'echeance de leasing, pas des frais bancaires).
        # L'indexation directe levait un KeyError des qu'un releve en contenait — c'est-a-dire
        # sur tout releve reel depuis que ces deux regles ont ete reclassees.
        bucket = {"pret_principal": "principal", "pret_profit": "profit",
                  "pret_assurance": "assurance"}.get(rule.key)
        if not bucket:
            continue
        par_jour[getdate(m["date"])][bucket].append(m)

    paires = []
    for jour, groupes in sorted(par_jour.items()):
        principaux = sorted(groupes["principal"], key=lambda m: -flt(m["debit"]))
        profits = sorted(groupes["profit"], key=lambda m: -flt(m["debit"]))
        # Appariement par rang decroissant : la plus grosse part de capital va avec le plus gros
        # interet du meme jour. Vrai des lors qu'il s'agit d'echeances de contrats distincts.
        for i in range(min(len(principaux), len(profits))):
            p, ii = principaux[i], profits[i]
            paires.append({
                "jour": jour,
                "principal": flt(p["debit"], 3),
                "interet": flt(ii["debit"], 3),
                "total": flt(flt(p["debit"], 3) + flt(ii["debit"], 3), 3),
                "references": [r for r in (p.get("reference"), ii.get("reference")) if r],
                "mouvements": [p, ii],
            })
        # Un mouvement sans jumeau : on le signale, on n'ecrit rien.
        for reste in principaux[len(profits):] + profits[len(principaux):]:
            paires.append({"jour": jour, "incomplet": reste})
    return paires


def contrat_de(paire: dict, contrats: list):
    """Contrat dont le total mensuel correspond a cette echeance."""
    for c in contrats:
        total = flt(c.get("total_mensuel"), 3)
        if not total:
            continue
        if abs(paire["total"] - total) <= flt(c.get("tolerance") or 0.01, 3):
            return c
    return None


def build_lines(contrat: dict, paire: dict) -> list:
    """Les 4 lignes de l'echeance, dans l'ordre des ecritures manuelles."""
    company = journal._company_of(contrat["compte_banque"])
    cc = frappe.db.get_value("Company", company, "cost_center") if frappe.db else None
    banque = contrat["compte_banque"]
    return [
        {"account": banque, "credit": paire["interet"]},
        {"account": banque, "credit": paire["principal"]},
        {"account": contrat.get("compte_principal"), "debit": paire["principal"], "cost_center": cc},
        {"account": contrat.get("compte_interet"), "debit": paire["interet"], "cost_center": cc},
    ]


# =========================== LEASINGS (echeances de vehicule) ===========================
#
# Un leasing ne se lit PAS comme un pret. La banque en eclate l'echeance en plusieurs debits du
# meme jour, tous porteurs de la MEME reference de contrat `LD…` :
#     principal + profit + assurance (takaful) + droit de timbre = HT
#     HT + TVA = TTC = ce qui sort du compte
# Verifie au centime sur ACC-JV-2026-00473 : 324,838 + 281,190 + 136,245 + 1 = 743,273,
# + 115,145 = 858,418.
#
# DEUX FORMES D'ECRITURE, relevees sur les saisies manuelles — c'est le contrat qui tranche :
#   a) simple (FIAT LD2227700127, CHERY LD2503400140) — 3 lignes :
#        Cr banque TTC / Dr TVA / Dr « Charges remboursement vehicules » HT
#   b) avec amortissement (Changan LD2613900139, Cenntro LD2614000071) — 5 lignes :
#        Cr banque TTC / Dr TVA / Dr charge HT / Cr amortissement / Dr charge dotation
#      La dotation est CONSTANTE par contrat (280,112 et 70,028, identiques en juin et juillet).
#
# /!\ ON IDENTIFIE PAR LA REFERENCE, PAS PAR LE TOTAL MENSUEL. Les prets CHIRAET partagent leur
# libelle et n'ont que leur total pour les distinguer, mais un leasing porte son `LD…` : c'est plus
# sur, et surtout cela reste vrai sur les echeances HORS NORME — le premier loyer de LD2613900139
# vaut 20 223,172 contre 680,232 ensuite, un critere de total l'aurait manque.

def echeances_leasing(movements: list) -> list:
    """Regroupe les debits par (reference de contrat, jour). -> [{reference, jour, ttc, tva, ht,
    composition, mouvements}]."""
    par_cle = defaultdict(list)
    for m in movements or []:
        if not flt(m.get("debit"), 3):
            continue
        rule = R.find_rule(m)
        if not rule or rule.groupe != "echeance" or rule.categorie != "pret":
            continue
        ref = (m.get("reference") or "").strip().upper()
        if not ref:
            continue
        par_cle[(ref, getdate(m["date"]))].append((rule.key, m))

    out = []
    for (ref, jour), lignes in sorted(par_cle.items(), key=lambda x: (x[0][1], x[0][0])):
        # Une ligne SEULE n'est pas une echeance de leasing : c'est une TVA sur commission
        # bancaire (references 'CHG…'). Meme garde-fou que dans bank/classify.
        if len(lignes) < 2:
            continue
        ttc = round(sum(flt(m["debit"], 3) for _, m in lignes), 3)
        tva = round(sum(flt(m["debit"], 3) for cle, m in lignes if cle == "tva_bancaire"), 3)
        out.append({
            "reference": ref, "jour": jour, "ttc": ttc, "tva": tva, "ht": round(ttc - tva, 3),
            "composition": {cle: flt(m["debit"], 3) for cle, m in lignes},
            "mouvements": [m for _, m in lignes],
        })
    return out


def contrat_par_reference(reference: str, contrats: list):
    """Contrat dont la reference au releve correspond. Insensible a la casse et aux espaces."""
    ref = (reference or "").strip().upper()
    for c in contrats:
        if (c.get("reference_bancaire") or "").strip().upper() == ref:
            return c
    return None


def build_lines_leasing(contrat: dict, ech: dict) -> list:
    """Lignes de l'echeance de leasing, dans l'ordre des saisies manuelles.

    L'amortissement n'est ajoute que si le contrat en declare un : FIAT et CHERY n'en ont pas
    (charge de location), Changan et Cenntro si (immobilisation amortie).
    """
    company = journal._company_of(contrat["compte_banque"])
    cc = frappe.db.get_value("Company", company, "cost_center") if frappe.db else None
    charge = contrat.get("compte_charge")
    lignes = [
        {"account": contrat["compte_banque"], "credit": ech["ttc"]},
        {"account": contrat.get("compte_tva"), "debit": ech["tva"], "cost_center": cc},
        {"account": charge, "debit": ech["ht"], "cost_center": cc},
    ]
    dotation = flt(contrat.get("montant_amortissement"), 3)
    if dotation and contrat.get("compte_amortissement"):
        lignes.append({"account": contrat["compte_amortissement"], "credit": dotation})
        lignes.append({"account": charge, "debit": dotation, "cost_center": cc})
    # Une TVA nulle ne doit pas produire une ligne a zero.
    return [l for l in lignes if flt(l.get("debit")) or flt(l.get("credit"))]


def process_leasings(movements: list, context=None, insert: bool = True, only=None) -> list:
    """Cree les ecritures d'echeance de leasing detectees au releve."""
    contrats = [c for c in load_contrats(only=only) if (c.get("type") or "") == "Leasing"]
    if not contrats:
        return []
    out = []
    for ech in echeances_leasing(movements):
        contrat = contrat_par_reference(ech["reference"], contrats)
        if not contrat:
            continue          # echeance d'un contrat non declare : silence volontaire, pas une erreur

        reference = build_reference(contrat, ech["jour"])
        # Idempotence : d'abord la reference periodisee, puis le rapprochement de GROUPE deja
        # calcule par l'identification bancaire — c'est lui qui reconnait les saisies manuelles,
        # dont le libelle ne ressemble a aucune cle generee.
        existant = None
        if context is not None:
            existant = (context.cheque_no_index or {}).get(reference)
            if not existant:
                from bank_retenue_sync.bank import classify as C

                ap = (context.echeances or {}).get(
                    C.cle_echeance({"reference": ech["reference"], "date": ech["jour"]}))
                existant = (ap or {}).get("voucher")
        elif frappe.db.exists("Journal Entry", {"cheque_no": reference}):
            existant = reference
        if existant:
            out.append({"flux": contrat["cle"], "ref": reference, "status": "skipped",
                        "je": existant, "date": str(ech["jour"])})
            continue

        try:
            lignes = build_lines_leasing(contrat, ech)
            manquants = [l["account"] for l in lignes if not l.get("account")]
            if manquants:
                out.append({"flux": contrat["cle"], "ref": reference, "status": "error",
                            "error": "comptes manquants dans le contrat (TVA/charge)"})
                continue
            remark = "%s\n%s" % (ech["reference"], contrat.get("libelle") or contrat["cle"])
            je = journal.build_journal_entry(
                journal._company_of(contrat["compte_banque"]), ech["jour"], lignes,
                remark=remark, cheque_no=reference, cheque_date=ech["jour"],
                mode_of_payment=contrat.get("mode_paiement") or None)
            if not insert:
                out.append({"flux": contrat["cle"], "ref": reference, "status": "created",
                            "je": "(dry-run)", "ttc": ech["ttc"], "tva": ech["tva"],
                            "ht": ech["ht"], "date": str(ech["jour"])})
                continue
            je.insert(ignore_permissions=True)
            # Meme reglage que les autres flux : une echeance constatee au releve est un fait,
            # pas une hypothese — si `auto_submit_journal_entries` est coche, elle est validee.
            if journal._auto_submit_enabled():
                je.submit()
            out.append({"flux": contrat["cle"], "ref": reference, "status": "created",
                        "je": je.name, "ttc": ech["ttc"], "date": str(ech["jour"])})
        except Exception as e:
            out.append({"flux": contrat["cle"], "ref": reference, "status": "error",
                        "error": str(e)[:160]})
    return out


def process_contrats(movements: list, context=None, insert: bool = True, only=None) -> list:
    """Cree les ecritures d'echeance detectees au releve. Contrat de retour habituel."""
    # Les leasings sont traites par `process_leasings` : les laisser ici les ferait identifier
    # par leur total mensuel, critere faux des la premiere echeance hors norme.
    contrats = [c for c in load_contrats(only=only) if (c.get("type") or "Pret") != "Leasing"]
    if not contrats:
        return []
    # References des leasings declares : leurs echeances ont leur propre traitement, et le
    # rapprochement par total mensuel n'a aucun sens pour elles.
    refs_leasing = {(c.get("reference_bancaire") or "").strip().upper()
                    for c in load_contrats() if (c.get("type") or "") == "Leasing"}
    out = []
    for paire in paires_du_releve(movements):
        refs_paire = {(r or "").strip().upper() for r in (paire.get("references") or [])}
        if paire.get("incomplet"):
            refs_paire.add((paire["incomplet"].get("reference") or "").strip().upper())
        if refs_leasing & refs_paire:
            continue
        if paire.get("incomplet"):
            m = paire["incomplet"]
            out.append({"flux": "pret", "ref": m.get("reference"), "status": "incomplet",
                        "date": str(paire["jour"]), "montant": flt(m.get("debit"), 3),
                        "raison": "principal et profit non apparies pour ce jour : "
                                  "aucune ecriture creee"})
            continue

        contrat = contrat_de(paire, contrats)
        if not contrat:
            out.append({"flux": "pret", "status": "contrat_inconnu",
                        "date": str(paire["jour"]), "total": paire["total"],
                        "raison": "aucun contrat dont le total mensuel vaut %s" % paire["total"]})
            continue

        reference = build_reference(contrat, paire["jour"])
        existant = None
        if context is not None:
            existant = (context.cheque_no_index or {}).get(reference)
            if not existant:
                # /!\ NE PAS retomber sur `je_par_reference` : une reference `LD…` est un numero
                # de CONTRAT que TOUTES les echeances precedentes citent. Ce repli declarait
                # « deja faite » l'echeance 7/10 en pointant l'ecriture de la 6/10 — donc les deux
                # echeances reellement manquantes n'auraient jamais ete creees.
                # L'appariement de GROUPE (reference, jour), lui, distingue les mois.
                from bank_retenue_sync.bank import classify as C

                for ref in paire["references"]:
                    ap = (context.echeances or {}).get(
                        C.cle_echeance({"reference": ref, "date": paire["jour"]}))
                    if ap and ap.get("voucher"):
                        existant = ap["voucher"]
                        break
        elif frappe.db.exists("Journal Entry", {"cheque_no": reference}):
            existant = reference
        if existant:
            out.append({"flux": contrat["cle"], "ref": reference, "status": "skipped",
                        "je": existant})
            continue

        try:
            lines = build_lines(contrat, paire)
            manquants = [l["account"] for l in lines if not l.get("account")]
            if manquants:
                out.append({"flux": contrat["cle"], "ref": reference, "status": "error",
                            "error": "comptes manquants dans le contrat (principal/interet)"})
                continue
            remark = "%s\nRéf. banque %s" % (reference, ", ".join(paire["references"]))
            je = journal.build_journal_entry(
                journal._company_of(contrat["compte_banque"]), paire["jour"], lines,
                remark=remark, cheque_no=reference, cheque_date=paire["jour"],
                mode_of_payment=contrat.get("mode_paiement") or None)
            if not insert:
                out.append({"flux": contrat["cle"], "ref": reference, "status": "created",
                            "je": "(dry-run)", "total": paire["total"]})
                continue
            je.insert(ignore_permissions=True)
            # Meme reglage que les autres flux : une echeance constatee au releve est un fait,
            # pas une hypothese — si `auto_submit_journal_entries` est coche, elle est validee.
            if journal._auto_submit_enabled():
                je.submit()
            out.append({"flux": contrat["cle"], "ref": reference, "status": "created",
                        "je": je.name, "total": paire["total"]})
        except Exception as e:
            out.append({"flux": contrat["cle"], "ref": reference, "status": "error",
                        "error": str(e)[:160]})
    return out


# ---------------------------------------------------------------------------------------
# Echeanciers par defaut, cales sur les ecritures manuelles existantes.
#
# Le compteur des libelles donne les dates de debut :
#   « Remboursement 5/10 Pret nantissement »  au 26/05/2026 -> echeance 1 le 26/01/2026
#   « Remboursement 1/6 Pret Ligne de credit » au 29/05/2026 -> echeance 1 le 29/05/2026
# et les totaux mensuels constants (17 705,228 / 14 134,538) sont exactement ce qui distingue
# les deux contrats au releve, ou ils portent le meme libelle « TAMOUIL CHIRAET ».
# ---------------------------------------------------------------------------------------
BANQUE = "STE430127B - Zitouna - A&S"

CONTRATS_DEFAUT = (
    {"cle": "pret_nantissement", "libelle": "Pret nantissement", "type": "Pret",
     "date_debut": "2026-01-26", "date_fin": "2026-10-26", "nb_echeances": 10,
     "total_mensuel": 17705.228, "tolerance": 0.01,
     "compte_banque": BANQUE, "compte_principal": "Prêts garantis - A&S",
     "compte_interet": "Frais bancaire Emprunt - A&S", "mode_paiement": "Virement",
     "template_reference": "Remboursement {n}/{total} Pret nantissement", "actif": 0,
     "notes": "Verifie : 17 099,432 + 605,796 = 17 705,228 au 26/06/2026 (ACC-JV-2026-00462)."},

    {"cle": "pret_ligne_credit", "libelle": "Pret Ligne de credit", "type": "Pret",
     "date_debut": "2026-05-29", "date_fin": "2026-10-29", "nb_echeances": 6,
     "total_mensuel": 14134.538, "tolerance": 0.01,
     "compte_banque": BANQUE, "compte_principal": "Prêts garantis - A&S",
     "compte_interet": "Frais bancaire Emprunt - A&S", "mode_paiement": "Virement",
     "template_reference": "Remboursement {n}/{total} Pret Ligne de credit", "actif": 0,
     "notes": "Verifie : 13 504,704 + 629,834 = 14 134,538 au 29/06/2026 (ACC-JV-2026-00461)."},

    # --- LEASINGS VEHICULES ---
    # Parametres releves sur les ecritures manuelles de l'utilisateur (ACC-JV-2026-00473/00472
    # pour la forme amortie, 00531/00532 pour la forme simple) et recoupes au centime avec les
    # groupes du releve. Les montants d'amortissement sont CONSTANTS (identiques en juin/juillet).
    {"cle": "leasing_fiat", "libelle": "Leasing FIAT", "type": "Leasing",
     "reference_bancaire": "LD2227700127", "total_mensuel": 1197.957, "tolerance": 0.01,
     "compte_banque": BANQUE, "compte_tva": "TVA 19% - A&S",
     "compte_charge": "Charges remboursement véhicules - A&S", "mode_paiement": "Virement",
     "template_reference": "Echeance Leasing FIAT {mm}-{yyyy}", "actif": 0,
     "notes": "Sans amortissement (charge de location). ATTENTION : les saisies manuelles "
              "portent 1 196,957 alors que la banque preleve 1 197,957 — le droit de timbre "
              "de 1 DT y est omis chaque mois. L'app comptabilise le montant reel."},

    {"cle": "leasing_chery", "libelle": "Leasing CHERY Tiggo X3", "type": "Leasing",
     "reference_bancaire": "LD2503400140", "total_mensuel": 1529.917, "tolerance": 0.01,
     "compte_banque": BANQUE, "compte_tva": "TVA 19% - A&S",
     "compte_charge": "Charges remboursement véhicules - A&S", "mode_paiement": "Virement",
     "template_reference": "Echeance Leasing CHERY {mm}-{yyyy}", "actif": 0,
     "notes": "Sans amortissement. Verifie : 197,352 + 1 332,565 = 1 529,917 (ACC-JV-2026-00532)."},

    {"cle": "leasing_changan", "libelle": "Leasing Changan New Star Van", "type": "Leasing",
     "reference_bancaire": "LD2613900139", "total_mensuel": 680.232, "tolerance": 0.01,
     "compte_banque": BANQUE, "compte_tva": "TVA 19% - A&S",
     "compte_charge": "Frais de Déplacement - A&S",
     "compte_amortissement": "Amortissement Cumulé - A&S", "montant_amortissement": 280.112,
     "mode_paiement": "Virement",
     "template_reference": "Echeance Leasing Changan {mm}-{yyyy}", "actif": 0,
     "notes": "Verifie : 72,976 + 607,256 = 680,232 et dotation 280,112 (ACC-JV-2026-00472)."},

    {"cle": "leasing_cenntro", "libelle": "Leasing Cenntro Logistar 100", "type": "Leasing",
     "reference_bancaire": "LD2614000071", "total_mensuel": 858.418, "tolerance": 0.01,
     "compte_banque": BANQUE, "compte_tva": "TVA 7% - A&S",
     "compte_charge": "Frais de Déplacement - A&S",
     "compte_amortissement": "Amortissement Cumulé - A&S", "montant_amortissement": 70.028,
     "mode_paiement": "Virement",
     "template_reference": "Echeance Leasing Cenntro {mm}-{yyyy}", "actif": 0,
     "notes": "Verifie : 115,145 + 743,273 = 858,418 et dotation 70,028 (ACC-JV-2026-00473)."},
)


def seed_defaults(overwrite: bool = False) -> int:
    """Amorce la table des contrats. Les lignes deja presentes (par `cle`) sont conservees."""
    doc = frappe.get_single(SETTINGS)
    existantes = {r.cle for r in (doc.get(TABLE_FIELD) or []) if r.cle}
    n = 0
    for row in CONTRATS_DEFAUT:
        if row["cle"] in existantes and not overwrite:
            continue
        doc.append(TABLE_FIELD, dict(row))
        n += 1
    if n:
        doc.save(ignore_permissions=True)
        frappe.db.commit()
    return n
