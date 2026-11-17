"""Les factures qu'une MEME retenue couvre — et la trace qu'on en garde.

Un client qui retient 1 % sur un mois de factures les regle d'un seul paiement, net de la retenue. La
caisse laisse la part retenue en dette sur UNE des factures, et c'est la que la retenue se loge (cf.
`rapprochement.facture_par_reglement_net`, `facture_designee_par_dette`). Le certificat ne designe donc
qu'une facture alors qu'il en couvre plusieurs.

Demande du 17/11/2026 (COMOSACC, 4 factures d'aout, 37,621 de retenue) : « la retenue, a priori il l'a faite
sur plusieurs factures, comment je l'identifie ? », puis « est-ce que je peux avoir un truc sur mon UI pour
tracer ce cas ? ». La trace tient en trois endroits :
  - le CERTIFICAT : `factures_couvertes` (le groupe, pour l'ecran) et `factures_couvertes_resume` (lisible) ;
  - le TABLEAU des certificats : un repere « + N factures » qui ouvre la liste ;
  - chaque FACTURE couverte : une note dans son historique, qui dit quel certificat la couvre et quelle
    facture porte l'ecriture de retenue — posee quand la retenue est enregistree.

Rien ici ne touche a la comptabilite : la retenue reste logee sur une seule facture, on ne fait que le dire.
"""
from __future__ import annotations

import json

import frappe
from frappe.utils import flt

DOCTYPE = "Retenue Certificate"
CHAMP = "factures_couvertes"
CHAMP_RESUME = "factures_couvertes_resume"


def _nombre(valeur) -> str:
    return f"{round(float(valeur or 0), 3):,.3f}".replace(",", " ").replace(".", ",")


def _jour(valeur) -> str:
    texte = str(valeur or "")[:10]
    return "%s/%s/%s" % (texte[8:10], texte[5:7], texte[:4]) if len(texte) == 10 else texte


def resume(groupe: dict, facture_portee: str = None, retenue: float = None) -> str:
    """Le groupe, en une phrase puis une ligne par facture. Pur."""
    if not groupe or len(groupe.get("factures") or []) < 2:
        return ""
    tete = "%d factures réglées ensemble par %s (%s de %s, le %s)" % (
        len(groupe["factures"]), groupe.get("reglement") or "un paiement",
        (groupe.get("mode") or "paiement").lower(), _nombre(groupe.get("montant")), _jour(groupe.get("date")))
    if retenue:
        tete = "Retenue de %s sur %s" % (_nombre(retenue), tete)
    lignes = [tete + (" ; la retenue est enregistrée sur %s." % facture_portee if facture_portee else ".")]
    for f in groupe["factures"]:
        porte = bool(facture_portee) and f["name"] == facture_portee
        # (pas `flt(v, 3)` : hors site — dans les tests purs — il rend 0)
        partiel = (not porte) and round(float(f.get("impute") or 0), 3) < round(float(f.get("ttc") or 0), 3) - 0.005
        lignes.append("- %s%s du %s : %s%s%s" % (
            f["name"], (" (n° %s)" % f["numero"]) if f.get("numero") else "", _jour(f.get("date")), _nombre(f.get("ttc")),
            (" dont %s réglés par ce paiement" % _nombre(f.get("impute"))) if partiel else "",
            " — porte la retenue" if porte else ""))
    lignes.append("Total des factures : %s" % _nombre(groupe.get("total_ttc")))
    return "\n".join(lignes)


def note_facture(cert: dict, groupe: dict, facture_portee: str) -> str:
    """La note posee dans l'historique de chaque facture couverte. Pur. Elle finit par un marqueur
    « [RAS <reference>] » : c'est lui qui evite de la poser deux fois."""
    autres = ", ".join(f["name"] for f in groupe["factures"])
    return ("Retenue à la source — certificat TEJ%s du %s (%s) : retenue de %s sur %d factures (%s), réglées ensemble "
            "par %s. L’écriture de retenue est enregistrée sur %s. [RAS %s]" % (
                (" « %s »" % cert["numero_chez_declarant"]) if cert.get("numero_chez_declarant") else "",
                _jour(cert.get("date_paiement")), cert.get("customer") or cert.get("declarant") or "",
                _nombre(cert.get("montant_retenue")), len(groupe["factures"]), autres, groupe.get("reglement") or "un paiement",
                facture_portee or "l’une d’elles", cert.get("name") or cert.get("reference")))


def factures_du_reglement_net(cert: dict, ctx) -> dict | None:
    """Le reglement net qui a paye plusieurs factures d'un coup, et ces factures. Lecture seule.
    None si le reglement net ne porte qu'une facture, ou s'il n'est pas retrouve."""
    from bank_retenue_sync.tej import rapprochement

    try:
        nom = (rapprochement.facture_par_reglement_net(cert, ctx) or {}).get("reglement")
        if not nom:
            return None
        numero = ", si.custom_numero_facture as numero" if frappe.db.has_column("Sales Invoice", "custom_numero_facture") else ""
        factures = frappe.db.sql(
            """select per.reference_name as name, sum(per.allocated_amount) as impute, si.posting_date as date,
                      si.grand_total as ttc%s
               from `tabPayment Entry Reference` per
               join `tabSales Invoice` si on si.name = per.reference_name
               where per.parent = %%s and per.reference_doctype = 'Sales Invoice' and per.allocated_amount > 0
               group by per.reference_name order by si.posting_date, si.name""" % numero, (nom,), as_dict=True)
        if len(factures) < 2:
            return None
        pe = frappe.db.get_value("Payment Entry", nom, ["mode_of_payment", "paid_amount", "posting_date"], as_dict=True)
        return {"reglement": nom, "mode": pe.mode_of_payment, "montant": flt(pe.paid_amount, 3), "date": str(pe.posting_date),
                "total_ttc": flt(sum(flt(f.ttc) for f in factures), 3),
                "factures": [{"name": f.name, "numero": f.get("numero"), "date": str(f.date), "ttc": flt(f.ttc, 3),
                              "impute": flt(f.impute, 3)} for f in factures]}
    except Exception:
        frappe.log_error(title="RAS : factures du reglement net", message=frappe.get_traceback())
        return None


def pret() -> bool:
    try:
        return bool(frappe.db.has_column(DOCTYPE, CHAMP))
    except Exception:
        return False


def tracer(reference: str, groupe: dict = None, ctx=None, commenter: bool = False) -> dict | None:
    """Garde la trace du groupe sur le certificat ; avec `commenter`, pose aussi la note dans l'historique de
    chaque facture couverte (une seule fois par certificat et par facture). Ne leve jamais. -> le groupe."""
    try:
        if not pret():
            return None
        from bank_retenue_sync.tej import rapprochement

        champs = list(rapprochement.CHAMPS_CERTIFICAT) + ["sales_invoice", "sales_order", "total_brut", "numero_chez_declarant", CHAMP]
        cert = frappe.db.get_value(DOCTYPE, reference, list(dict.fromkeys(champs)), as_dict=True)
        if not cert:
            return None
        if groupe is None:
            groupe = factures_du_reglement_net(dict(cert), ctx if ctx is not None else rapprochement.charger_contexte())
        if not groupe:
            return None
        # LA FACTURE DU CERTIFICAT DOIT ETRE DU LOT. Le reglement net se retrouve par son MONTANT : s'il ne paie pas
        # la facture ou la retenue est logee, ce n'est pas (ou pas surement) le reglement de ce certificat — BHM,
        # 29/06/2026 : retenue sur ACC-SINV-2026-00826, reglement du meme montant sur deux autres factures. Mieux
        # vaut ne rien tracer que tracer un lien qui n'existe pas.
        portee = cert.get("sales_invoice")
        if portee and portee not in [f["name"] for f in groupe["factures"]]:
            return None
        texte = json.dumps(groupe, ensure_ascii=False, sort_keys=True)
        if (cert.get(CHAMP) or "") != texte:
            frappe.db.set_value(DOCTYPE, reference, {CHAMP: texte, CHAMP_RESUME: resume(groupe, portee, cert.get("montant_retenue"))},
                                update_modified=False)
        if commenter:
            marqueur = "[RAS %s]" % reference
            note = note_facture(dict(cert, name=reference), groupe, portee)
            for f in groupe["factures"]:
                deja = frappe.db.exists("Comment", {"reference_doctype": "Sales Invoice", "reference_name": f["name"],
                                                    "comment_type": "Info", "content": ["like", "%" + marqueur + "%"]})
                if not deja:
                    frappe.get_doc({"doctype": "Comment", "comment_type": "Info", "reference_doctype": "Sales Invoice",
                                    "reference_name": f["name"], "content": note}).insert(ignore_permissions=True)
        return groupe
    except Exception:
        frappe.log_error(title="RAS : trace des factures couvertes (%s)" % reference, message=frappe.get_traceback())
        return None


def tracer_manquants(limite: int = 200) -> dict:
    """Pose la trace sur les certificats du perimetre qui ne l'ont pas encore (ceux d'avant cette fonction, et
    ceux que le rapprochement vient de designer). La note des factures n'est posee que si la retenue est deja
    enregistree. -> {vus, traces: [reference]}"""
    out = {"vus": 0, "traces": []}
    if not pret():
        return out
    from bank_retenue_sync.tej import rapprochement

    lignes = frappe.db.get_all(
        DOCTYPE, filters={"hors_perimetre": 0, "anomalie": 0, "customer": ["is", "set"], CHAMP: ["is", "not set"],
                          "match_status": ["in", ["Auto Matched", "Manually Matched", "Sans piece"]]},
        fields=["name", "payment_entry"], order_by="date_paiement desc", limit_page_length=limite)
    if not lignes:
        return out
    ctx = rapprochement.charger_contexte()
    for l in lignes:
        out["vus"] += 1
        if tracer(l.name, ctx=ctx, commenter=bool(l.payment_entry)):
            out["traces"].append(l.name)
    return out
