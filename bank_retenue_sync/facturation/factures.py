"""Les factures de vente du mois : liste, ventilation par taux, reglements, trous de numerotation.

Remplace le Server Script `Get_month_invoices` et le post-traitement pandas de
`get_invoices_pdf.py`. Trois differences qui comptent :

  1. LE MOIS EST UN PARAMETRE. Le Server Script est fige sur le mois precedent (`add_months(-1)`
     en dur) : impossible de reconstituer un dossier ancien sans modifier le script.
  2. LES MONTANTS SORTENT STRUCTURES. Le script concatene taxes et paiements en une chaine
     (« TVA 19%: 12.3, TVA 7%: 4.5 ») que l'outil re-decoupe ensuite a coups de `split`. Un
     libelle de compte qui change, et le decoupage rend un nombre faux sans rien signaler.
  3. LA VENTILATION EST EXACTE. Voir `tva.ventiler` — plus de base retrouvee par division.
"""
from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.facturation import periode, tva

PRECISION = 3


def _numero(valeur) -> int | None:
    """Le numero de facture est un texte en base ; seuls les numeriques entrent dans la serie."""
    texte = str(valeur or "").strip()
    return int(texte) if texte.isdigit() else None


def _lignes(noms: list) -> dict:
    if not noms:
        return {}
    rows = frappe.get_all("Sales Invoice Item", filters={"parent": ["in", noms]},
                          fields=["parent", "item_code", "item_name", "net_amount"],
                          limit_page_length=0)
    out = defaultdict(list)
    for r in rows:
        out[r.parent].append({"item_code": r.item_code, "item_name": r.item_name,
                              "net_amount": flt(r.net_amount, PRECISION)})
    return out


def _taxes(noms: list) -> dict:
    if not noms:
        return {}
    rows = frappe.get_all("Sales Taxes and Charges", filters={"parent": ["in", noms]},
                          fields=["parent", "rate", "tax_amount_after_discount_amount",
                                  "item_wise_tax_detail", "account_head", "description"],
                          limit_page_length=0)
    out = defaultdict(list)
    for r in rows:
        out[r.parent].append({
            "rate": r.rate,
            "tax_amount": flt(r.tax_amount_after_discount_amount, PRECISION),
            "item_wise_tax_detail": r.item_wise_tax_detail,
            "account_head": r.account_head,
            "description": r.description,
        })
    return out


# Seuls ces moyens de paiement portent un numero de piece. Pour les autres — especes, dette non
# payee, retenue a la source — `reference_no` contient un NOM DE DOCUMENT (« SAL-ORD-2026-02246 »,
# « ENC-02-07-2026-00002 »), qui n'est pas un numero de piece et qui, pris pour tel, empechait
# tout regroupement : dix versements en especes restaient dix lignes distinctes.
MOTS_AVEC_NUMERO = ("cheque", "chèque", "traite", "effet", "virement")

MODE_ESPECES = "Espèces"


def _arr(v) -> float:
    """Arrondi SANS `frappe.utils.flt`.

    ⚠️ `flt(x, precision)` REND 0.0 HORS D'UN SITE. Il passe par les reglages systeme pour
    connaitre la methode d'arrondi ; sans `frappe.local`, l'echec est avale et la valeur revient
    a zero. Une fonction censee etre pure et testable ne peut donc pas s'en servir — les tests
    la voyaient rendre zero partout, alors qu'elle marchait en production.
    """
    try:
        return round(float(v or 0), PRECISION)
    except (TypeError, ValueError):
        return 0.0


def _porte_un_numero(mode: str) -> bool:
    m = (mode or "").lower()
    return any(mot in m for mot in MOTS_AVEC_NUMERO)


def _est_especes(mode: str) -> bool:
    m = (mode or "").lower()
    return "espece" in m or "espèce" in m


def presumer_especes(paiements: list, reste_du: float) -> list:
    """Impute le reste du a un reglement en especes. Fonction pure.

    ⚠️ RIEN N'EST CREE EN COMPTABILITE ICI. C'est une PRESOMPTION d'affichage : le solde d'une
    facture de ce type part en especes sans qu'une piece soit saisie, et le recapitulatif doit
    equilibrer pour etre exploitable. La part presumee reste donc marquee comme telle, et la
    colonne « Reste dû » continue d'afficher le vrai reste comptable. Deux chiffres qui se
    contredisent seraient un bug ; deux chiffres dont l'un est signale comme presume sont une
    information.

    Si la facture porte deja des especes, la presomption s'y AJOUTE au lieu d'ouvrir une seconde
    ligne — meme regle de regroupement que pour les versements reels.
    """
    reste = _arr(reste_du)
    if reste <= 0.001:
        return paiements
    lignes = [dict(p) for p in paiements or []]
    for ligne in lignes:
        if _est_especes(ligne.get("mode")) and not ligne.get("piece"):
            ligne["montant"] = _arr(ligne["montant"] + reste)
            ligne["presume"] = _arr(_arr(ligne.get("presume")) + reste)
            return sorted(lignes, key=lambda l: (-l["montant"], l["mode"]))
    lignes.append({"mode": MODE_ESPECES, "piece": "", "banque": "", "montant": reste,
                   "presume": reste, "nombre": 0, "date": "", "payment_entry": None})
    return sorted(lignes, key=lambda l: (-l["montant"], l["mode"]))


def _reglements(noms: list) -> dict:
    """Les Payment Entry pointant sur ces factures, regroupees. -> {facture: [{...}]}

    ⚠️ UN REGLEMENT SE LIT SUR LA REFERENCE, PAS SUR LE MONTANT. On prend `allocated_amount`
    de la ligne de reference et non `paid_amount` de la piece : un meme encaissement solde
    souvent plusieurs factures, et `paid_amount` les compterait toutes au montant du tout.

    ⚠️ ET ON REGROUPE PAR MOYEN DE PAIEMENT. Une facture reglee en dix fois en especes affichait
    dix lignes identiques, illisibles ; un cheque, lui, doit rester distinct puisque c'est son
    NUMERO qu'on cherche. Le regroupement se fait donc sur (mode, n° de piece, reference
    bancaire) : les especes et les dettes non payees, qui n'ont ni l'un ni l'autre, se somment
    naturellement, et deux cheques differents restent deux lignes.
    """
    # Import tardif : `reglement` importe deja ce module pour nommer les factures d'un
    # encaissement. Au niveau du module, les deux se refermeraient l'un sur l'autre.
    from bank_retenue_sync.facturation import reglement

    if not noms:
        return {}
    refs = frappe.get_all("Payment Entry Reference",
                          filters={"reference_doctype": "Sales Invoice",
                                   "reference_name": ["in", noms], "docstatus": 1},
                          fields=["parent", "reference_name", "allocated_amount"],
                          limit_page_length=0)
    if not refs:
        return {}
    pieces = {p.name: p for p in frappe.get_all(
        "Payment Entry", filters={"name": ["in", list({r.parent for r in refs})], "docstatus": 1},
        fields=["name", "mode_of_payment", "reference_no", "reference_date", "posting_date"],
        limit_page_length=0)}

    groupes = defaultdict(dict)
    for r in refs:
        piece = pieces.get(r.parent)
        if not piece:
            continue
        mode = piece.mode_of_payment or "Non précisé"
        decompose = reglement.decomposer_reference(piece.reference_no) \
            if _porte_un_numero(mode) else {"piece": "", "banque": ""}
        cle = (mode, decompose["piece"], decompose["banque"])
        ligne = groupes[r.reference_name].setdefault(cle, {
            "mode": mode,
            "piece": decompose["piece"],
            "banque": decompose["banque"],
            "montant": 0.0,
            "nombre": 0,
            "date": str(piece.reference_date or piece.posting_date or ""),
            "payment_entry": r.parent,
        })
        ligne["montant"] = flt(ligne["montant"] + flt(r.allocated_amount), PRECISION)
        ligne["nombre"] += 1
        # Une seule piece derriere la ligne : on garde son lien. Plusieurs : le lien perdrait
        # son sens, on l'efface plutot que d'en designer une au hasard.
        if ligne["payment_entry"] != r.parent:
            ligne["payment_entry"] = None

    return {facture: sorted(lignes.values(), key=lambda l: (-l["montant"], l["mode"]))
            for facture, lignes in groupes.items()}


def liste(mois: str) -> dict:
    """Le recapitulatif des ventes du mois. -> dict pret pour l'ecran et pour l'export."""
    mois = periode.normaliser(mois)
    debut, fin = periode.bornes(mois)

    factures = frappe.get_all(
        "Sales Invoice",
        filters={"docstatus": 1, "is_opening": "No", "posting_date": ["between", [debut, fin]]},
        fields=["name", "custom_numero_facture", "customer", "customer_name", "posting_date",
                "net_total", "total_taxes_and_charges", "grand_total", "outstanding_amount"],
        order_by="posting_date asc, name asc", limit_page_length=0)

    noms = [f.name for f in factures]
    lignes, taxes, reglements = _lignes(noms), _taxes(noms), _reglements(noms)

    out, ventilations, numeros = [], [], []
    for f in factures:
        v = tva.ventiler(lignes.get(f.name, []), taxes.get(f.name, []),
                         net_total=f.net_total, total_taxes=f.total_taxes_and_charges)
        ventilations.append(v)
        numero = _numero(f.custom_numero_facture)
        if numero is not None:
            numeros.append(numero)
        reste_du = flt(f.outstanding_amount, PRECISION)
        reels = reglements.get(f.name, [])
        paiements = presumer_especes(reels, reste_du)
        out.append({
            "facture": f.name,
            "numero": f.custom_numero_facture or "",
            "numero_trie": numero if numero is not None else 10 ** 9,
            "nom_dossier": nom_de_piece(mois, f.custom_numero_facture),
            "date": str(f.posting_date or ""),
            "client": f.customer,
            "client_nom": f.customer_name or f.customer,
            "ht": flt(f.net_total, PRECISION),
            "tva": flt(f.total_taxes_and_charges, PRECISION),
            "ttc": flt(f.grand_total, PRECISION),
            "reste_du": reste_du,
            "ventilation": v,
            "paiements": paiements,
            # `regle` reste le montant REELLEMENT encaisse ; la presomption est comptee a part.
            "regle": flt(sum(p["montant"] for p in reels), PRECISION),
            "presume": flt(sum(flt(p.get("presume")) for p in paiements), PRECISION),
        })
    out.sort(key=lambda r: (r["numero_trie"], r["date"]))

    return {
        "mois": mois,
        "libelle": periode.libelle(mois),
        "periode": {"debut": debut, "fin": fin},
        "factures": out,
        "totaux": {
            "nombre": len(out),
            "ht": flt(sum(r["ht"] for r in out), PRECISION),
            "tva": flt(sum(r["tva"] for r in out), PRECISION),
            "ttc": flt(sum(r["ttc"] for r in out), PRECISION),
            "regle": flt(sum(r["regle"] for r in out), PRECISION),
            "reste_du": flt(sum(r["reste_du"] for r in out), PRECISION),
            "presume": flt(sum(r["presume"] for r in out), PRECISION),
            "factures_presumees": sum(1 for r in out if r["presume"]),
            **tva.cumuler(ventilations),
        },
        "numerotation": numerotation(numeros),
    }


def numerotation(numeros: list) -> dict:
    """Trous et doublons dans la serie du mois.

    Le meme controle que la page Facturation Auto fait avant de generer — refait ici APRES, sur
    ce qui existe reellement en base. Un dossier remis au comptable avec un numero manquant se
    discute ; le decouvrir a ce moment-la coute moins cher que de l'apprendre de lui.
    """
    if not numeros:
        return {"premier": None, "dernier": None, "trous": [], "doublons": []}
    vus, doublons = set(), set()
    for n in numeros:
        (doublons if n in vus else vus).add(n)
    premier, dernier = min(vus), max(vus)
    trous = [n for n in range(premier, dernier + 1) if n not in vus]
    return {"premier": premier, "dernier": dernier,
            "trous": trous[:200], "trous_total": len(trous),
            "doublons": sorted(doublons)}


def nom_de_piece(mois: str, numero) -> str:
    """« FAC-07-2026-01134 » — le nom d'usage d'une facture, mois sur deux chiffres.

    ⚠️ DEUX CONVENTIONS COEXISTAIENT, ET ELLES SE CROISAIENT SUR LE MEME ECRAN. Le script des
    PDF construisait « FAC-7-2026-… » (mois non complete) tandis que le rapport de caisse et
    `payment_details`, tous deux cote ERPNext, rendent « FAC-07-2026-… ». L'onglet Caisse
    affichait donc la premiere forme et l'onglet Factures la seconde, pour la meme facture.
    On retient celle des deux outils ERPNext : elle se trie correctement, et c'est celle que
    l'utilisateur voit deja partout ailleurs.
    """
    annee, numero_mois = periode.eclater(periode.normaliser(mois))
    return "FAC-%02d-%04d-%s" % (numero_mois, annee, str(numero or "").strip().zfill(5))


def nom_de_piece_depuis_date(date, numero) -> str:
    """Meme nom, mais deduit de la date de la facture — pour les lectures hors periode."""
    jour = getdate(date) if date else None
    if not jour:
        return str(numero or "")
    return "FAC-%02d-%04d-%s" % (jour.month, jour.year, str(numero or "").strip().zfill(5))
