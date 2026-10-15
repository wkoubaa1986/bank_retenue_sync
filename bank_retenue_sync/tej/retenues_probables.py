"""LA RETENUE DEGUISEE EN DETTE (15/10/2026).

Un client assujetti paie TTC moins 1 % ; la caisse enregistre le reglement reel et solde la facture
avec une ligne « Dette non payee » du 1 % manquant. Comptablement, le client « doit » 1 % qu'il ne
paiera jamais — il l'a verse au fisc pour notre compte — et le credit d'impot n'existe nulle part
chez nous tant que son certificat n'est pas arrive au portail (parfois trois mois, parfois jamais
si personne ne le relance). Constate le 15/10/2026 : WWF 130,50 DT sur ACC-SINV-2026-01225.

LA REGLE, donnee par l'utilisateur : la retenue doit etre ENTIEREMENT allouee a la facture. Si la
facture porte une dette, la retenue en est deduite ; sinon (argent compte : especes, cheque,
virement), l'argent reel va eteindre d'autres dettes du client, et ce qui reste devient une avance
a son credit. Ce module traite le premier cas AVANT le certificat ; `paiements.ajuster` traite les
deux a l'arrivee du certificat, qui retrouve alors l'ecriture deja logee (`apparier_par_facture`).

CE QUE CE MODULE NE DECIDE PAS : il PROPOSE (detection + plan) ; l'ecriture n'est passee qu'au clic,
sous le meme reglage de validation que les regularisations de certificat.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.tej import paiements as P, rapprochement as R

TAUX = 1.0           # 1 % du TTC — le taux de toutes les retenues vente observees (91/92)
TOLERANCE = 0.03     # le timbre (0,01) et les arrondis de saisie, jamais plus
SEUIL_TTC = 1000.0   # en dessous, pas de retenue a la source (regle fiscale, cf. ras_achat_seuil)


def dette_vaut_retenue(ttc: float, dette: float, taux: float = TAUX, tolerance: float = TOLERANCE,
                       seuil: float = SEUIL_TTC) -> bool:
    """La ligne de dette vaut-elle exactement la retenue que le client a du operer ? PURE."""
    ttc, dette = flt(ttc, 3), flt(dette, 3)
    if ttc < seuil or dette <= 0:
        return False
    return abs(dette - round(ttc * taux / 100.0, 3)) <= tolerance


def detecter(depuis=None, jusqu_a=None, sales_invoice: str = None) -> list:
    """Factures de clients a matricule fiscal, soldees, dont une ligne de dette vaut 1 % du TTC et
    qui ne portent encore aucune ecriture de retenue. → [{sales_invoice, customer, …, dette_pe}]."""
    conditions, params = [], {"mode": P.mode_dette(), "ras": R.mode_ras()}
    if sales_invoice:
        conditions.append("si.name = %(si)s")
        params["si"] = sales_invoice
    if depuis:
        conditions.append("si.posting_date >= %(de)s")
        params["de"] = getdate(depuis)
    if jusqu_a:
        conditions.append("si.posting_date <= %(a)s")
        params["a"] = getdate(jusqu_a)
    rows = frappe.db.sql("""
        select si.name as sales_invoice, si.customer, c.customer_name, c.tax_id, si.posting_date, si.grand_total,
               si.status, pe.name as dette_pe, pe.posting_date as dette_date, pe.paid_amount as dette_total,
               per.allocated_amount as dette
        from `tabSales Invoice` si
        join tabCustomer c on c.name = si.customer
        join `tabPayment Entry Reference` per on per.reference_name = si.name and per.reference_doctype = 'Sales Invoice'
        join `tabPayment Entry` pe on pe.name = per.parent and pe.docstatus = 1 and pe.mode_of_payment = %(mode)s
        where si.docstatus = 1 and si.is_return = 0 and ifnull(c.tax_id, '') <> '' and si.grand_total >= %(seuil)s
          and not exists (select 1 from `tabPayment Entry Reference` p2 join `tabPayment Entry` e2 on e2.name = p2.parent
                           where p2.reference_name = si.name and e2.docstatus = 1 and e2.mode_of_payment = %(ras)s)
          %(conditions)s
        order by si.posting_date desc""".replace("%(conditions)s", (" and " + " and ".join(conditions)) if conditions else ""),
        {**params, "seuil": SEUIL_TTC}, as_dict=1)
    out = []
    for r in rows:
        if not dette_vaut_retenue(r.grand_total, r.dette):
            continue
        r["retenue"] = flt(r.dette, 3)
        r["taux"] = round(flt(r.dette, 3) / flt(r.grand_total, 3) * 100, 2) if r.grand_total else 0
        r["explication"] = ("la dette de %s DT vaut %s %% du TTC (%s) : c'est la part que le client a retenue"
                            % (r["retenue"], r["taux"], flt(r.grand_total, 3)))
        out.append(r)
    return out


def _construire(ligne: dict, montant: float):
    """L'ecriture de retenue, calquee sur celles d'un certificat, mais qui dit d'ou elle vient."""
    pe = P.construire({"montant_retenue": montant, "date_paiement": ligne["dette_date"], "customer": ligne["customer"],
                       "sales_invoice": ligne["sales_invoice"], "reference": ligne["sales_invoice"],
                       "declarant": ligne.get("customer_name"), "declarant_matricule": ligne.get("tax_id"),
                       "total_brut": flt(ligne["grand_total"], 3)}, insert=False)
    pe.reference_no = ligne["sales_invoice"]
    pe.remarks = ("Retenue a la source PROBABLE : la dette %s de %s DT valait %s %% du TTC de %s (%s DT).\n"
                  "Logee le %s en attendant le certificat TEJ du client %s (%s)."
                  % (ligne["dette_pe"], montant, ligne.get("taux"), ligne["sales_invoice"], flt(ligne["grand_total"], 3),
                     frappe.utils.nowdate(), ligne.get("customer_name") or ligne["customer"], ligne.get("tax_id") or "—"))
    return pe


def loger(sales_invoice: str, dette_pe: str, insert: bool = False, submit=None) -> dict:
    """Deduit la retenue de la dette et la loge sur la facture. Le total impute a la facture ne bouge
    pas : la dette baisse de la retenue (ou disparait si elle ne portait que ca), l'ecriture de
    retenue prend la difference. Essai a blanc sans `insert`."""
    lignes = [l for l in detecter(sales_invoice=sales_invoice) if l["dette_pe"] == dette_pe]
    if not lignes:
        return {"ok": False, "raison": "cette facture ne porte pas (ou plus) de dette valant la retenue"}
    ligne = lignes[0]
    deja = R.pieces_ras_de_la_facture(sales_invoice, ligne["customer"], R.charger_contexte(), None)
    if deja:
        return {"ok": False, "raison": "la facture porte deja une retenue (%s)" % ", ".join(p["name"] for p in deja)}
    montant = flt(ligne["retenue"], 3)
    valider = P._auto_submit() if submit is None else bool(submit)
    plan = {"ok": True, "facture": sales_invoice, "customer": ligne["customer"], "retenue": montant,
            "dette": dette_pe, "dette_avant": flt(ligne["dette_total"], 3),
            "dette_apres": round(flt(ligne["dette_total"], 3) - montant, 3), "brouillon": not valider}
    if not insert:
        return {**plan, "statut": "a faire (essai a blanc)"}
    supprimer = P.suppression_permise(valider, P._supprimer_annule())
    # La dette d'abord : elle libere exactement la place que la retenue va prendre sur la facture.
    reprise = P._reduire_dette({"dette_pe": dette_pe, "name": sales_invoice, "montant": montant}, valider, supprimer)
    pe = _construire(ligne, montant)
    pe.insert(ignore_permissions=True)
    if valider:
        pe.submit()
    try:
        frappe.get_doc("Sales Invoice", sales_invoice).add_comment(
            "Info", "💡 Retenue a la source probable logee : %s DT deduits de la dette %s, ecriture %s%s. "
                    "Le certificat TEJ, quand il arrivera, s'y rapprochera." % (montant, dette_pe, pe.name, "" if valider else " (brouillon)"))
    except Exception:
        pass
    return {**plan, "statut": "logee", "payment_entry": pe.name, "valide": valider, "dette_reprise": reprise}
