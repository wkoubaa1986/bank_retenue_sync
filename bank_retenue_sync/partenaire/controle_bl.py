"""Controle : toute vente realisee PAR Economiq porte un BL de main d'oeuvre seulement.

POURQUOI
--------
Les produits poses chez le client par le partenaire ne sortent pas de NOTRE stock : il les tient
de son cote (il nous les achete a part, sur ses propres commandes). Sur la vente au client final,
le BL ne doit donc porter que la main d'oeuvre. Un article de stock sur ce BL ferait sortir du
magasin un produit qui n'en est jamais parti — un ecart de stock silencieux, et un produit
compte deux fois.

Constat sur la base de prod du 07/10/2026 : les 13 ventes Economiq depuis juillet ont chacune UN
BL d'une seule ligne « Main d’œuvre » (M-I-OD, M-E-OD, M-R-OD...) dont le montant reprend la
commande, alors que la commande, elle, detaille les produits. C'est cette regle qu'on surveille.

⚠️ LE PERIMETRE EST CELUI DU BILAN. Les « ventes Economiq » sont la section `economiq` de
`customization_app.bilan_vente.get_data` (commandes partagees avec le compte partenaire dont la
tache est affectee a son employe), avec la REGLE ENREGISTREE. On ne la redefinit pas ici : un
controle qui ne parle pas des memes commandes que le bilan ne controlerait rien.

⚠️ « LIVRAISON » N'EST PAS DE LA MAIN D'OEUVRE. Le groupe est traite comme un passage (PA = PV)
par le bilan, mais une partie de ses articles est geree en stock : seul « Main d’œuvre » (et ses
sous-groupes) est admis.

LECTURE SEULE : rien n'est ecrit.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt, getdate, nowdate

from bank_retenue_sync.facturation import periode

PRECISION = 3

# Premier mois controle (demande utilisateur du 07/10/2026).
DEBUT = "2026-07"

# U+2019 (apostrophe typographique) : c'est le nom exact du groupe en base.
GROUPE_MAIN_D_OEUVRE = "Main d’œuvre"

SANS_BL = "sans_bl"
HORS_MAIN_D_OEUVRE = "hors_main_d_oeuvre"


# ------------------------------------------------------------------ regle (fonction pure)

def verifier(commande: dict, bons: list, groupes_admis: set) -> dict | None:
    """Anomalie d'une vente, ou None si elle est conforme. Fonction pure.

    `bons` : les BL VALIDES de la commande, retours exclus, chacun
    {"name", "posting_date", "lignes": [{"item_code", "item_name", "qty", "amount", "item_group"}]}.
    `commande` peut porter `brouillons` (noms des BL non valides) : dit pourquoi il n'y a pas de BL.
    """
    base = {"sales_order": commande.get("name"), "customer": commande.get("customer"),
            "mois": commande.get("mois"), "vente": flt(commande.get("vente"), PRECISION)}
    if not bons:
        brouillons = commande.get("brouillons") or []
        return {**base, "type": SANS_BL, "bons": [],
                "detail": ("BL non validé : " + ", ".join(brouillons)) if brouillons
                else "aucun BL validé"}

    fautives = []
    for b in bons:
        for l in b.get("lignes") or []:
            if (l.get("item_group") or "") not in groupes_admis:
                fautives.append({**l, "bon": b.get("name")})
    if not fautives:
        return None
    return {**base, "type": HORS_MAIN_D_OEUVRE, "bons": [b.get("name") for b in bons],
            "lignes": fautives,
            "detail": ", ".join("%s × %s (%s)" % (
                ("%g" % flt(l.get("qty"))), l.get("item_code"), l.get("item_group") or "sans groupe")
                for l in fautives)}


def mois_controles(depuis: str = DEBUT, jusqua: str | None = None) -> list:
    """`depuis` -> `jusqua` (defaut : le mois COURANT, en cours compris). Fonction pure."""
    fin = jusqua or getdate(nowdate()).strftime("%Y-%m")
    annee, numero = periode.eclater(depuis)
    out = []
    while periode.cle(annee, numero) <= fin:
        out.append(periode.cle(annee, numero))
        annee, numero = periode.suivant(annee, numero)
    return out


# ------------------------------------------------------------------ lecture de la base

def _groupes_admis() -> set:
    """« Main d’œuvre » et ses eventuels sous-groupes (arbre lft/rgt)."""
    bornes = frappe.db.get_value("Item Group", GROUPE_MAIN_D_OEUVRE, ["lft", "rgt"], as_dict=True)
    if not bornes:
        return {GROUPE_MAIN_D_OEUVRE}
    return set(frappe.get_all("Item Group",
                              filters={"lft": [">=", bornes.lft], "rgt": ["<=", bornes.rgt]},
                              pluck="name"))


def _bons(so_names: list) -> tuple[dict, dict]:
    """-> ({commande: [BL valides, retours exclus]}, {commande: [BL non valides]}).

    Le groupe est celui de l'ARTICLE aujourd'hui (comme le bilan), pas celui recopie sur la ligne
    a la creation du BL.
    """
    if not so_names:
        return {}, {}
    ph = ",".join(["%s"] * len(so_names))
    rows = frappe.db.sql(
        f"""SELECT dni.against_sales_order AS commande, dn.name, dn.docstatus, dn.posting_date,
                   dni.item_code, dni.item_name, dni.qty, dni.amount,
                   COALESCE(i.item_group, dni.item_group) AS item_group
            FROM `tabDelivery Note Item` dni
            INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
            LEFT JOIN `tabItem` i ON i.name = dni.item_code
            WHERE dni.against_sales_order IN ({ph})
              AND IFNULL(dn.is_return, 0) = 0
            ORDER BY dn.posting_date, dn.name, dni.idx""",
        tuple(so_names), as_dict=True)
    valides, brouillons = {}, {}
    for r in rows:
        if r.docstatus == 1:
            bons = valides.setdefault(r.commande, {})
            b = bons.setdefault(r.name, {"name": r.name, "posting_date": str(r.posting_date or ""),
                                         "lignes": []})
            b["lignes"].append({"item_code": r.item_code, "item_name": r.item_name or r.item_code,
                                "qty": flt(r.qty, PRECISION), "amount": flt(r.amount, PRECISION),
                                "item_group": r.item_group})
        elif r.docstatus == 0 and r.name not in brouillons.setdefault(r.commande, []):
            brouillons[r.commande].append(r.name)
    return {so: list(b.values()) for so, b in valides.items()}, brouillons


def _ventes_economiq(mois: str) -> list | None:
    """Les commandes de la section `economiq` du bilan, ou None si le bilan est indisponible."""
    from bank_retenue_sync.partenaire.economiq import _bilan

    bilan = _bilan(mois)
    if bilan is None:
        return None
    for s in bilan.get("sections") or []:
        if s.get("key") == "economiq":
            return [{"name": o["name"], "customer": o.get("customer"), "mois": mois,
                     "vente": (o.get("totals") or {}).get("vente")}
                    for o in s.get("orders") or []]
    return []


def controle(depuis: str = DEBUT, jusqua: str | None = None) -> dict:
    """Toutes les ventes Economiq depuis `depuis`, et celles qui sortent de la regle."""
    groupes = _groupes_admis()
    par_mois, anomalies, total = [], [], 0
    for mois in mois_controles(depuis, jusqua):
        ventes = _ventes_economiq(mois)
        if ventes is None:
            return {"disponible": False, "depuis": depuis,
                    "message": "Le bilan (customization_app.bilan_vente) n’est pas disponible."}
        valides, brouillons = _bons([v["name"] for v in ventes])
        fautes = []
        for v in ventes:
            a = verifier({**v, "brouillons": brouillons.get(v["name"])},
                         valides.get(v["name"]) or [], groupes)
            if a:
                fautes.append(a)
        total += len(ventes)
        anomalies += fautes
        par_mois.append({"mois": mois, "libelle": periode.libelle(mois),
                         "ventes": len(ventes), "conformes": len(ventes) - len(fautes)})
    return {
        "disponible": True,
        "depuis": depuis,
        "depuis_libelle": periode.libelle(depuis),
        "groupe": GROUPE_MAIN_D_OEUVRE,
        "ventes": total,
        "conformes": total - len(anomalies),
        "anomalies": anomalies,
        "mois": par_mois,
    }
