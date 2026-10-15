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

⚠️ PAS DE CERTIFICAT, PAS DE RETENUE (regle de l'utilisateur, 15/10/2026) : une dette de 1 % n'est tenue
pour versee au fisc QUE lorsque le client l'a DECLAREE au portail — le certificat en est la preuve.
Tant qu'il n'existe pas dans l'export (rafraichi chaque nuit a 04:00), la dette reste une dette, la
ligne est « non declaree » et c'est le client qu'il faut relancer. Des que le certificat est la,
« Loger » passe par la regularisation du certificat (`paiements.ajuster`, regle « la dette d'abord »).
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


_REQUETE = """
        select '%(doctype)s' as piece_doctype, p.name as piece, p.name as sales_invoice, p.customer, c.customer_name, c.tax_id,
               p.%(date)s as posting_date, p.grand_total, p.status, pe.name as dette_pe, pe.posting_date as dette_date,
               pe.paid_amount as dette_total, per.allocated_amount as dette
        from `tab%(doctype)s` p
        join tabCustomer c on c.name = p.customer
        join `tabPayment Entry Reference` per on per.reference_name = p.name and per.reference_doctype = '%(doctype)s'
        join `tabPayment Entry` pe on pe.name = per.parent and pe.docstatus = 1 and pe.mode_of_payment = %%(mode)s
        where p.docstatus = 1 %(retour)s and ifnull(c.tax_id, '') <> '' and p.grand_total >= %%(seuil)s
          and not exists (select 1 from `tabPayment Entry Reference` p2 join `tabPayment Entry` e2 on e2.name = p2.parent
                           where p2.reference_name = p.name and e2.docstatus = 1 and e2.mode_of_payment = %%(ras)s)
          %(conditions)s
        order by p.%(date)s desc"""

PIECES = (("Sales Invoice", "posting_date", "and p.is_return = 0"),
          # Les reglements se font aussi sur les COMMANDES avant facturation (15/10/2026) ; a la
          # facturation ERPNext reporte les imputations sur la facture, qui prend alors le relais.
          ("Sales Order", "transaction_date", ""))


def dettes_de_la_retenue(ttc: float, lignes: list) -> list:
    """Parmi les lignes de dette d'une piece [{dette_pe, dette}], celles qui FORMENT la retenue : une
    seule qui vaut 1 % du TTC, sinon toutes ensemble quand leur somme le vaut — le cas des commandes
    regroupees dans une facture : chaque commande a laisse sa dette de 1 %, le client retient 1 % de
    la facture. [] si rien ne colle. PURE."""
    lignes = [l for l in lignes if flt(l.get("dette"), 3) > 0]
    seules = [l for l in lignes if dette_vaut_retenue(ttc, l["dette"])]
    if len(seules) == 1:
        return seules
    if len(lignes) > 1 and dette_vaut_retenue(ttc, sum(flt(l["dette"], 3) for l in lignes)):
        return lignes
    return []


def detecter(depuis=None, jusqu_a=None, sales_invoice: str = None, doctype: str = None) -> list:
    """Factures ET commandes de clients a matricule fiscal, soldees pour partie par une ligne de dette
    valant 1 % du TTC, sans aucune ecriture de retenue. → [{piece_doctype, piece, customer, …, dette_pe}]."""
    out = []
    for dt, date, retour in PIECES:
        if doctype and dt != doctype:
            continue
        conditions, params = [], {"mode": P.mode_dette(), "ras": R.mode_ras(), "seuil": SEUIL_TTC}
        if sales_invoice:
            conditions.append("p.name = %(si)s")
            params["si"] = sales_invoice
        if depuis:
            conditions.append("p.%s >= %%(de)s" % date)
            params["de"] = getdate(depuis)
        if jusqu_a:
            conditions.append("p.%s <= %%(a)s" % date)
            params["a"] = getdate(jusqu_a)
        sql = _REQUETE % {"doctype": dt, "date": date, "retour": retour,
                          "conditions": (" and " + " and ".join(conditions)) if conditions else ""}
        par_piece = {}
        for r in frappe.db.sql(sql, params, as_dict=1):
            par_piece.setdefault(r.piece, []).append(r)
        for piece, lignes in par_piece.items():
            retenues = dettes_de_la_retenue(lignes[0].grand_total, lignes)
            if not retenues:
                continue
            r = dict(retenues[0])
            r["dettes"] = [{"dette_pe": l.dette_pe, "montant": flt(l.dette, 3), "dette_total": flt(l.dette_total, 3),
                            "dette_date": l.dette_date} for l in retenues]
            r["retenue"] = round(sum(d["montant"] for d in r["dettes"]), 3)
            r["taux"] = round(r["retenue"] / flt(r["grand_total"], 3) * 100, 2) if r["grand_total"] else 0
            r["explication"] = ("la dette de %s DT vaut %s %% du TTC (%s) : c'est la part que le client a retenue"
                                % (r["retenue"], r["taux"], flt(r["grand_total"], 3)) if len(retenues) == 1 else
                                "%d lignes de dette (commandes regroupees) totalisent %s DT = %s %% du TTC (%s) : la part retenue"
                                % (len(retenues), r["retenue"], r["taux"], flt(r["grand_total"], 3)))
            cert = certificat_declare(r["customer"], piece, r["retenue"], dt)
            r["certificat"] = cert
            r["declaree"] = bool(cert)
            out.append(r)
    out.sort(key=lambda r: str(r.get("posting_date") or ""), reverse=True)
    return out


def certificat_declare(customer: str, piece: str, montant: float, doctype: str = "Sales Invoice"):
    """Le certificat du portail qui prouve cette retenue : meme client, meme montant au timbre pres (ou
    deja pose sur la piece), recu, dans le perimetre, pas encore porte par une ecriture. None sinon."""
    champ = "sales_invoice" if doctype == "Sales Invoice" else "sales_order"
    for c in frappe.get_all("Retenue Certificate",
                            filters=[["hors_perimetre", "=", 0], ["etat_depot", "in", ["Recue", "Rectifie"]],
                                     ["payment_entry", "is", "not set"]],
                            or_filters=[["customer", "=", customer], [champ, "=", piece]],
                            fields=["name", "numero_chez_declarant", "date_paiement", "montant_retenue", "customer",
                                    "sales_invoice", "sales_order", "match_status"], order_by="date_paiement desc"):
        if c.get(champ) == piece or abs(flt(c.montant_retenue, 3) - flt(montant, 3)) <= TOLERANCE:
            return c
    return None


def loger(sales_invoice: str, dette_pe: str, insert: bool = False, submit=None, doctype: str = "Sales Invoice") -> dict:
    """Loge la retenue d'une dette de 1 % — SEULEMENT si le client l'a declaree (certificat au portail).
    Passe alors par la regularisation du certificat : la dette baisse de la retenue (ou disparait si elle
    ne portait que ca), l'ecriture de retenue prend la difference sur la piece. Essai a blanc sans `insert`."""
    lignes = [l for l in detecter(sales_invoice=sales_invoice, doctype=doctype)
              if dette_pe in [d["dette_pe"] for d in l["dettes"]]]
    if not lignes:
        return {"ok": False, "raison": "cette piece ne porte pas (ou plus) de dette valant la retenue"}
    ligne = lignes[0]
    deja = R.pieces_ras_de_la_facture(sales_invoice, ligne["customer"], R.charger_contexte(), None, doctype=doctype)
    if deja:
        return {"ok": False, "raison": "la facture porte deja une retenue (%s)" % ", ".join(p["name"] for p in deja)}
    montant = flt(ligne["retenue"], 3)
    cert = ligne.get("certificat")
    if not cert:
        # La regle : rien n'est verse au fisc tant que le client n'a pas declare. La dette reste.
        return {"ok": False, "non_declaree": True,
                "raison": "aucun certificat du portail ne prouve cette retenue (dernier export : %s) : le client ne l'a pas "
                          "encore declaree — la dette reste, c'est lui qu'il faut relancer" % _date_export()}
    # Le certificat existe : on pose la piece dessus s'il ne la porte pas, puis la regularisation du
    # certificat fait le reste (« la dette d'abord », cf. paiements.ajuster).
    champ = "sales_invoice" if doctype == "Sales Invoice" else "sales_order"
    if cert.get(champ) != sales_invoice:
        frappe.db.set_value("Retenue Certificate", cert["name"], {champ: sales_invoice, "customer": ligne["customer"],
                                                                 "match_status": "Manually Matched"}, update_modified=False)
    plan = P.ajuster(cert["name"], insert=insert, submit=submit)
    plan["certificat_numero"] = cert.get("numero_chez_declarant")
    return plan


def _date_export() -> str:
    run = frappe.get_all("BRS Sync Run", filters={"kind": "tej_certificats_recus", "status": "Success"},
                         fields=["started_at"], order_by="started_at desc", limit_page_length=1)
    return getdate(run[0].started_at).strftime("%d/%m/%Y") if run else "jamais"
