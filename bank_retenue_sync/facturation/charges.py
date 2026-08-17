"""La liste des charges du mois : depenses indirectes, achats, retenues — et leurs justificatifs.

Portage du Server Script « Rapport mensuelle comptable » (`get_mensuel_situation`), dont seules
les trois dernieres sorties concernent le dossier : `indirect_charges`, `achat`, `retenue`.

⚠️ CE QUE LE SCRIPT D'ORIGINE FAISAIT ET QU'ON NE FAIT PLUS : relire les PDF avec un modele de
langage (`apply_llm_to_df_docs`) pour en extraire montants et references. Cette etape existait
parce que la donnee ERP etait incomplete. Elle ne l'est plus : le controle d'achat local exige un
justificatif PDF a la validation, et `Extraction Facture Achat` porte deja ce que le modele
allait rechercher dans le fichier. On lit la donnee, on ne la redevine pas.

⚠️ ET ON NE CHARGE PLUS UN DOCUMENT PAR LIGNE. Le script fait un `frappe.get_doc` par ecriture,
puis un autre par ligne d'ecriture : sur un mois charge, cela se compte en milliers. Ici tout est
lu par lots, ce qui change la nature de l'ecran — il s'ouvre au lieu de se lancer.
"""
from __future__ import annotations

import re
from collections import defaultdict

import frappe
from frappe.utils import flt

from bank_retenue_sync.facturation import periode

PRECISION = 3

# Les racines telles que le Server Script les nomme. Elles sont resolues au moment de l'appel :
# un compte absent est signale a l'ecran, il ne fait pas tomber le bloc.
RACINE_DEPENSES = "Charges Indirectes - A&S"
COMPTE_ACHATS = "Stock Existant - A&S"
COMPTE_RETENUES = "Avance  impôt société - A&S"   # le double espace est celui du plan comptable

# Taux de la retenue a la source sur les ventes, en pourcentage du TTC.
TAUX_RAS_VENTE = 1.0

# Comptes volontairement hors dossier : ils sont deja portes ailleurs (retenue a la source, TCL,
# timbre) ou n'ont pas de justificatif a produire (amortissement, arrondi).
COMPTES_IGNORES = {
    "Perte de non paiement - A&S", "Amortissement - A&S", "Arrondi - A&S",
    "Impot sur revenu + CNSS - A&S", "Retenue a la source achat - A&S", "T.C.L - A&S",
    "Taxe Loyer - A&S", "Timbre Fiscal - A&S", "Dépenses non déclarées - A&S",
}


# ------------------------------------------------------------------ justificatif exigible
#
# ⚠️ TOUTES LES CHARGES N'ONT PAS DE PIECE, ET C'EST NORMAL. Une echeance de leasing, une
# commission bancaire ou une depense reglee par la carte technologique n'ont pas de facture
# fournisseur a produire : la preuve, c'est le releve. Les compter comme manquantes noyait les
# VRAIS manques — quatre lignes exigibles perdues au milieu de quinze exemptes.
#
# Chaque exemption porte sa raison, affichee a l'ecran : une regle muette ne se verifie pas.

COMPTE_CARTE = "Carte technologique - A&S"

_RX_LEASING = re.compile(r"^\s*LD\d{6,}", re.I)
_RX_ECHEANCE_PRET = re.compile(r"Remboursement\s+\d+\s*/\s*\d+", re.I)

COMPTES_EXEMPTES = {
    "Frais bancaire - A&S": "frais bancaires : la preuve est le relevé",
    "Frais bancaire Emprunt - A&S": "intérêts d'emprunt : portés par l'échéancier",
    "Prêts garantis - A&S": "principal d'un prêt : porté par l'échéancier",
    "Charges remboursement véhicules - A&S": "échéance de leasing : portée par le contrat",
}


def exemption(ligne: dict) -> str:
    """La raison pour laquelle cette charge n'exige pas de justificatif, ou "" si elle en exige.

    L'ordre compte peu, les motifs ne se recouvrent pas — mais le test par la REFERENCE passe
    avant celui par le compte : deux echeances de leasing de juillet sont tombees dans « Frais
    de Deplacement » et non dans le compte des vehicules, seul leur `LD…` les trahissait.
    """
    ref = (ligne.get("ref") or "").strip()
    compte = (ligne.get("categorie") or "").strip()
    credite = (ligne.get("tiers") or "").strip()

    if credite == COMPTE_CARTE:
        return "réglé par la carte technologique"
    if _RX_LEASING.match(ref):
        return "échéance de leasing : portée par le contrat"
    if _RX_ECHEANCE_PRET.search(ref):
        return "échéance de prêt : portée par l'échéancier"
    return COMPTES_EXEMPTES.get(compte, "")


# ------------------------------------------------------------------ reference d'export


def _propre(valeur) -> str:
    """Une valeur sur UNE ligne, sans espaces doubles — un nom de piece comptable ne se plie pas."""
    return " ".join(str(valeur or "").split())


def contrats_par_reference() -> dict:
    """{reference LD… -> libelle du contrat}. Vide si les contrats ne sont pas declares."""
    try:
        from bank_retenue_sync.expenses import contrats

        return {(c.get("reference_bancaire") or "").strip().upper():
                (c.get("libelle") or c.get("cle") or "").strip()
                for c in contrats.load_contrats() if c.get("reference_bancaire")}
    except Exception:
        return {}


def nom_du_contrat(ref: str, contrats: dict) -> str:
    """« LD2503400140 » -> « Leasing CHERY Tiggo X3 ». "" si la reference n'est pas connue.

    ⚠️ UN `LD…` NE DIT PAS QUELLE VOITURE. Six echeances de leasing par mois, toutes affichees
    sous une suite de chiffres : impossible de savoir laquelle concerne le Cenntro et laquelle
    le Changan sans ouvrir le contrat. Le libelle est deja declare dans les Reglages, il n'y a
    qu'a s'en servir.
    """
    trouve = _RX_LEASING.match(_propre(ref) or "")
    return contrats.get(trouve.group(0).strip().upper(), "") if trouve else ""


def reference_export(ligne: dict) -> str:
    """Le nom sous lequel la piece part dans l'export comptable. Fonction pure.

    Deux regles, parce que les deux familles ne portent pas leur identite au meme endroit :

      · DEPENSE (ecriture de journal) : la reference de l'ecriture, puis le n° de facture LU
        dans le justificatif. L'ecriture ne connait pas ce numero — il n'existe que sur le PDF,
        et n'apparait donc qu'apres le controle. Tant que la piece n'a pas ete lue, la reference
        d'export est celle de l'ecriture, et rien de plus.
      · FACTURE D'ACHAT : le tiers, puis le n° de facture fournisseur. Les deux sont deja en
        base ; aucune lecture n'est necessaire.

    Un fragment deja contenu dans un autre n'est pas repete : « Facture Facebook 038151 » suivi
    de « 038151 » donnerait deux fois le meme numero dans le nom.
    """
    lu = _propre(((ligne.get("controle") or {}).get("extrait") or {}).get("reference"))
    if ligne.get("type") == "Purchase Invoice":
        morceaux = [_propre(ligne.get("tiers")), _propre(ligne.get("ref"))]
    else:
        # Une echeance de leasing s'annonce par son vehicule, pas par sa suite de chiffres.
        morceaux = [_propre(ligne.get("contrat")), _propre(ligne.get("ref")), lu]

    # ⚠️ LA DEDUPLICATION SE FAIT AU MOT, PAS A LA SOUS-CHAINE. « Leasing Changan New Star Van »
    # et « LD2613900139 Changan New Star Van » ne se contiennent pas l'un l'autre, et la
    # reference sortait avec le vehicule ecrit deux fois. On ne garde d'un fragment que les
    # mots que les precedents n'ont pas deja.
    out, vus = [], set()
    for morceau in morceaux:
        mots = [m for m in _propre(morceau).split() if m.lower() not in vus]
        if not mots:
            continue
        vus.update(m.lower() for m in mots)
        out.append(" ".join(mots))
    return " ".join(out)


def _resoudre(nom: str) -> str | None:
    """Le compte s'il existe, sinon None. Aucune approximation : un compte proche n'est pas ce
    compte-la, et sommer les charges du mauvais compte ne se verrait pas."""
    return nom if frappe.db.exists("Account", nom) else None


def _descendance(racine: str) -> list:
    """Les comptes feuilles sous `racine`, sur deux niveaux comme le script d'origine."""
    niveau1 = [a.name for a in frappe.get_all("Account", filters={"parent_account": racine},
                                              fields=["name"], limit_page_length=0)]
    comptes = list(niveau1)
    if niveau1:
        comptes += [a.name for a in frappe.get_all(
            "Account", filters={"parent_account": ["in", niveau1]},
            fields=["name"], limit_page_length=0)]
    return [c for c in comptes if c not in COMPTES_IGNORES]


def _ecritures(comptes: list, debut: str, fin: str) -> list:
    if not comptes:
        return []
    return frappe.get_all(
        "GL Entry",
        filters={"account": ["in", comptes], "posting_date": ["between", [debut, fin]],
                 "is_cancelled": 0},
        fields=["account", "voucher_type", "voucher_no", "debit", "credit", "posting_date"],
        order_by="posting_date asc", limit_page_length=0)


def _justificatifs(pieces: list) -> dict:
    """{(doctype, nom): [{file_name, file_url}]} — en une requete pour tout le mois."""
    if not pieces:
        return {}
    par_type = defaultdict(list)
    for doctype, nom in pieces:
        par_type[doctype].append(nom)
    out = defaultdict(list)
    for doctype, noms in par_type.items():
        for f in frappe.get_all("File",
                                filters={"attached_to_doctype": doctype,
                                         "attached_to_name": ["in", noms]},
                                fields=["attached_to_name", "file_name", "file_url"],
                                limit_page_length=0):
            out[(doctype, f.attached_to_name)].append(
                {"file_name": f.file_name, "file_url": f.file_url})
    return out


def _tva_du_journal(lignes: list) -> dict:
    """Ventile une ecriture de journal : TVA 7, TVA 19, HT, compte credite.

    Le taux se lit sur le NOM du compte (« TVA 19% - A&S »), exactement comme le script
    d'origine et comme `tej/emis.py` : c'est la seule lecture qui resiste a une ligne exoneree.
    """
    out = {"tva7": 0.0, "tva19": 0.0, "ht": 0.0, "categorie": "", "compte_credite": ""}
    for l in lignes:
        debit, credit = flt(l.get("debit"), PRECISION), flt(l.get("credit"), PRECISION)
        compte = l.get("account") or ""
        if credit > 0 and not out["compte_credite"]:
            out["compte_credite"] = compte
        if debit <= 0:
            continue
        if "7%" in compte:
            out["tva7"] = flt(out["tva7"] + debit, PRECISION)
        elif "19%" in compte:
            out["tva19"] = flt(out["tva19"] + debit, PRECISION)
        else:
            out["ht"] = flt(out["ht"] + debit, PRECISION)
            if not out["categorie"]:
                out["categorie"] = compte
    return out


def _bloc(cle: str, titre: str, comptes: list, debut: str, fin: str) -> dict:
    ecritures = _ecritures(comptes, debut, fin)
    pieces = sorted({(e.voucher_type, e.voucher_no) for e in ecritures})
    fichiers = _justificatifs(pieces)

    par_type = defaultdict(list)
    for doctype, nom in pieces:
        par_type[doctype].append(nom)

    lignes = []
    lignes += _lignes_journal(par_type.get("Journal Entry", []), fichiers)
    lignes += _lignes_achat(par_type.get("Purchase Invoice", []), fichiers)
    lignes += _lignes_retenue(par_type.get("Payment Entry", []), fichiers)
    lignes.sort(key=lambda r: (r["date"], r["ref"]))

    contrats = contrats_par_reference()
    for l in lignes:
        l["source"] = titre
        l["contrat"] = nom_du_contrat(l.get("ref"), contrats)
        l["exemption"] = exemption(l)
        l["justificatif_requis"] = not l["exemption"]
        l["manque"] = l["justificatif_requis"] and not l["justificatifs"]
        l["reference_export"] = reference_export(l)

    return {
        "cle": cle,
        "titre": titre,
        "comptes": comptes,
        "lignes": lignes,
        "totaux": {
            "nombre": len(lignes),
            "ht": flt(sum(l["ht"] for l in lignes), PRECISION),
            "tva": flt(sum(l["tva"] for l in lignes), PRECISION),
            "ttc": flt(sum(l["ttc"] for l in lignes), PRECISION),
            "retenue": flt(sum(l["retenue"] for l in lignes), PRECISION),
            # Ne compte QUE ce qui est exigible : un compteur qui melange les exemptions ne
            # se regarde plus au bout de deux mois.
            "sans_justificatif": sum(1 for l in lignes if l["manque"]),
            "exemptes": sum(1 for l in lignes if l["exemption"]),
            "avec_justificatif": sum(1 for l in lignes if l["justificatifs"]),
        },
    }


def _lignes_journal(noms: list, fichiers: dict) -> list:
    if not noms:
        return []
    entetes = {j.name: j for j in frappe.get_all(
        "Journal Entry", filters={"name": ["in", noms], "docstatus": 1},
        fields=["name", "posting_date", "cheque_no", "user_remark", "mode_of_payment",
                "total_credit"], limit_page_length=0)}
    detail = defaultdict(list)
    for l in frappe.get_all("Journal Entry Account", filters={"parent": ["in", list(entetes)]},
                            fields=["parent", "account", "debit_in_account_currency",
                                    "credit_in_account_currency"], limit_page_length=0):
        detail[l.parent].append({"account": l.account, "debit": l.debit_in_account_currency,
                                 "credit": l.credit_in_account_currency})
    out = []
    for nom, j in entetes.items():
        v = _tva_du_journal(detail.get(nom, []))
        out.append({
            "date": str(j.posting_date or ""),
            "ref": j.cheque_no or j.user_remark or nom,
            "type": "Journal Entry",
            "document_type": "Journal Entry", "document_name": nom,
            "tiers": v["compte_credite"],
            "categorie": v["categorie"],
            "mode": j.mode_of_payment or "",
            "ht": v["ht"], "tva7": v["tva7"], "tva19": v["tva19"],
            "tva": flt(v["tva7"] + v["tva19"], PRECISION),
            "ttc": flt(j.total_credit, PRECISION),
            "retenue": 0.0,
            "justificatifs": fichiers.get(("Journal Entry", nom), []),
        })
    return out


def _lignes_achat(noms: list, fichiers: dict) -> list:
    if not noms:
        return []
    out = []
    for f in frappe.get_all("Purchase Invoice", filters={"name": ["in", noms], "docstatus": 1},
                            fields=["name", "posting_date", "supplier", "supplier_name", "bill_no",
                                    "total", "base_taxes_and_charges_added",
                                    "taxes_and_charges_deducted", "grand_total"],
                            limit_page_length=0):
        retenue = flt(f.taxes_and_charges_deducted, PRECISION)
        out.append({
            "date": str(f.posting_date or ""),
            "ref": f.bill_no or f.name,
            "type": "Purchase Invoice",
            "document_type": "Purchase Invoice", "document_name": f.name,
            "tiers": f.supplier_name or f.supplier,
            "categorie": "Achat",
            "mode": "",
            "ht": flt(f.total, PRECISION),
            "tva7": 0.0, "tva19": 0.0,
            "tva": flt(f.base_taxes_and_charges_added, PRECISION),
            # Le TTC du dossier est celui d'AVANT retenue : c'est la dette envers le fournisseur,
            # pas ce qui lui a ete verse. La retenue est portee dans sa propre colonne.
            "ttc": flt(flt(f.grand_total) + retenue, PRECISION),
            "retenue": retenue,
            "justificatifs": fichiers.get(("Purchase Invoice", f.name), []),
        })
    return out


def _lignes_retenue(noms: list, fichiers: dict) -> list:
    """Les retenues subies sur nos ventes : un Payment Entry client, portant nos factures."""
    if not noms:
        return []
    entetes = {p.name: p for p in frappe.get_all(
        "Payment Entry", filters={"name": ["in", noms], "docstatus": 1},
        fields=["name", "posting_date", "party_name", "party", "paid_amount", "reference_no"],
        limit_page_length=0)}
    refs = defaultdict(list)
    for r in frappe.get_all("Payment Entry Reference",
                            filters={"parent": ["in", list(entetes)],
                                     "reference_doctype": "Sales Invoice"},
                            fields=["parent", "reference_name"], limit_page_length=0):
        refs[r.parent].append(r.reference_name)

    # ⚠️ CE QU'ON MONTRE, C'EST LA FACTURE — LA RETENUE EST UNE COLONNE A PART. Une retenue de
    # 15,68 DT ne dit rien seule ; ce qui se controle, c'est la facture de 1 568 DT sur laquelle
    # elle a ete prelevee. Le HT, la TVA et le TTC sont donc ceux de la FACTURE liee, et le
    # montant du paiement va dans « Retenue ».
    factures_liees = {}
    toutes = [n for liste_noms in refs.values() for n in liste_noms]
    if toutes:
        factures_liees = {f.name: f for f in frappe.get_all(
            "Sales Invoice", filters={"name": ["in", toutes]},
            fields=["name", "custom_numero_facture", "net_total",
                    "total_taxes_and_charges", "grand_total"],
            limit_page_length=0)}
    numeros = {n: f.custom_numero_facture for n, f in factures_liees.items()}

    # ⚠️ LE CERTIFICAT DE RETENUE EST ATTACHE A LA FACTURE, PAS AU PAIEMENT. `fichiers` ne
    # contient que les pieces des vouchers du grand livre — donc les Payment Entry. Chercher
    # ("Sales Invoice", …) dedans ne rendait JAMAIS rien : les sept retenues de juillet
    # paraissaient sans justificatif alors que quatre portaient leur `certificat_ras_*.pdf`.
    fichiers_factures = _justificatifs([("Sales Invoice", n) for n in toutes])

    out = []
    for nom, p in entetes.items():
        factures = refs.get(nom, [])
        pieces = list(fichiers.get(("Payment Entry", nom), []))
        for facture in factures:
            pieces += fichiers_factures.get(("Sales Invoice", facture), [])
        liees = [factures_liees[f] for f in factures if f in factures_liees]
        ttc = flt(sum(flt(f.grand_total) for f in liees), PRECISION)
        # ⚠️ LA RETENUE DE VENTE SE CONTROLE, ELLE NE SE CONSTATE PAS. Elle vaut 1 % du TTC de
        # la facture : un client qui retient trop, ou pas assez, laisse un ecart que personne
        # ne voit si l'ecran se contente d'afficher le montant recu. Verifie sur juillet :
        # 1 568 -> 15,68 ; 5 377 -> 53,77 ; 3 413,5 -> 34,14.
        attendue = flt(ttc * TAUX_RAS_VENTE / 100.0, PRECISION)
        out.append({
            "date": str(p.posting_date or ""),
            "ref": "Retenue à la source %s" % (p.party_name or p.party or ""),
            "type": "Payment Entry",
            "document_type": "Payment Entry", "document_name": nom,
            "tiers": p.party_name or p.party or "",
            "categorie": "Retenue à la source vente",
            "mode": "Retenue à la source vente",
            "ht": flt(sum(flt(f.net_total) for f in liees), PRECISION),
            "tva7": 0.0, "tva19": 0.0,
            "tva": flt(sum(flt(f.total_taxes_and_charges) for f in liees), PRECISION),
            "ttc": ttc,
            "retenue": flt(p.paid_amount, PRECISION),
            "retenue_attendue": attendue,
            "retenue_ecart": flt(flt(p.paid_amount, PRECISION) - attendue, PRECISION),
            "taux_ras": TAUX_RAS_VENTE,
            "factures": [numeros.get(f) or f for f in factures],
            "factures_noms": factures,
            "justificatifs": pieces,
        })
    return out


def liste(mois: str) -> dict:
    """Les trois blocs de charges du mois. -> dict pret pour l'ecran et pour l'export."""
    mois = periode.normaliser(mois)
    debut, fin = periode.bornes(mois)

    racine = _resoudre(RACINE_DEPENSES)
    achats = _resoudre(COMPTE_ACHATS)
    retenues = _resoudre(COMPTE_RETENUES)
    absents = [n for n, r in ((RACINE_DEPENSES, racine), (COMPTE_ACHATS, achats),
                              (COMPTE_RETENUES, retenues)) if not r]

    blocs = [
        _bloc("depenses", "Dépenses", _descendance(racine) if racine else [], debut, fin),
        _bloc("achats", "Achats", [achats] if achats else [], debut, fin),
        _bloc("retenues", "Retenues / Ventes", [retenues] if retenues else [], debut, fin),
    ]

    return {
        "mois": mois,
        "libelle": periode.libelle(mois),
        "periode": {"debut": debut, "fin": fin},
        "blocs": blocs,
        "comptes_absents": absents,
        "totaux": {
            "nombre": sum(b["totaux"]["nombre"] for b in blocs),
            "ht": flt(sum(b["totaux"]["ht"] for b in blocs), PRECISION),
            "tva": flt(sum(b["totaux"]["tva"] for b in blocs), PRECISION),
            "ttc": flt(sum(b["totaux"]["ttc"] for b in blocs), PRECISION),
            "retenue": flt(sum(b["totaux"]["retenue"] for b in blocs), PRECISION),
            "sans_justificatif": sum(b["totaux"]["sans_justificatif"] for b in blocs),
            "exemptes": sum(b["totaux"]["exemptes"] for b in blocs),
            "avec_justificatif": sum(b["totaux"]["avec_justificatif"] for b in blocs),
        },
    }
