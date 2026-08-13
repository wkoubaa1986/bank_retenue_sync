"""Index de rapprochement banque -> Journal Entry, charges en une fois.

POURQUOI
--------
`orchestrator._exists(cheque_no)` fait UNE requete par reference testee. Sur un export de plusieurs
mois croise avec une table de depenses recurrentes, cela fait des centaines d'allers-retours pour
une information qui tient en une requete. Ces index sont construits une fois par run et passes au
moteur (meme principe que `pending.consumed_bank_keys()` deja calcule une seule fois dans
`orchestrator.process_encaissements`).

L'IDEMPOTENCE PAR REFERENCE BANCAIRE
------------------------------------
La seule cle d'idempotence existante est `Journal Entry.cheque_no`, un libelle metier periodise
(« Declaration comptable 06-2026 »). Elle echoue pour tout flux SANS periode — typiquement la
recharge de carte Total, qui peut survenir plusieurs fois le meme mois sans identifiant propre.
`journal_entries_by_bank_reference` comble ce trou : la reference bancaire (FT...) est unique par
operation et figure dans le libelle des ecritures produites par l'app.
"""
from __future__ import annotations

import re
from datetime import timedelta

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.encaissement.pending import BANK_ACCOUNT, COMPANY

# Longueur minimale d'une reference bancaire testee en sous-chaine. En deca, le risque de faux
# positif est reel : une reference de 3 caracteres se retrouve par hasard dans n'importe quel texte.
MIN_REF_LEN = 6

_TEXT_FIELDS = ("cheque_no", "user_remark", "remark")


def _window(date_from=None, date_to=None) -> dict:
    filters = {"company": COMPANY, "docstatus": ["!=", 2]}
    if date_from and date_to:
        filters["posting_date"] = ["between", [date_from, date_to]]
    elif date_from:
        filters["posting_date"] = [">=", date_from]
    elif date_to:
        filters["posting_date"] = ["<=", date_to]
    return filters


def cheque_no_index(date_from=None, date_to=None) -> dict:
    """{cheque_no -> nom de Journal Entry}. Remplace les appels repetes a `_exists`."""
    rows = frappe.db.get_all("Journal Entry", filters=_window(date_from, date_to),
                             fields=["name", "cheque_no"], limit_page_length=0)
    return {r.cheque_no.strip(): r.name for r in rows if (r.cheque_no or "").strip()}


def journal_entries_by_bank_reference(references, date_from=None, date_to=None) -> dict:
    """{reference bancaire -> [noms de Journal Entry]} pour les references reellement citees.

    Une seule requete bornee par la fenetre, puis test de sous-chaine sur les champs textuels.
    Les references trop courtes sont ecartees (cf. MIN_REF_LEN)."""
    refs = {str(r).strip().upper() for r in (references or [])
            if len(str(r or "").strip()) >= MIN_REF_LEN}
    if not refs:
        return {}
    rows = frappe.db.get_all("Journal Entry", filters=_window(date_from, date_to),
                            fields=["name", *_TEXT_FIELDS], limit_page_length=0)
    index: dict = {}
    for r in rows:
        blob = " ".join(str(r.get(f) or "") for f in _TEXT_FIELDS).upper()
        if not blob:
            continue
        for ref in refs:
            if ref in blob:
                index.setdefault(ref, []).append(r.name)
    return index


def find_journal_entries_by_bank_line(date, montant: float, sens: str = "credit",
                                      account: str = BANK_ACCOUNT, contrepartie: str = None,
                                      company: str = COMPANY, fenetre: int = 3) -> list:
    """Ecritures touchant le compte bancaire du meme montant, a `fenetre` jours pres.

    Generalisation de `especes.find_journal_entries` : le sens du compte bancaire est parametrable
    (`credit` = sortie d'argent, donc une depense ; `debit` = entree) et la contrepartie devient
    optionnelle. Rapprocher sur le seul montant confondrait deux operations du meme jour ; c'est
    pourquoi le compte bancaire fait partie du critere.
    """
    if not date or not montant:
        return []
    colonne = "credit" if sens == "credit" else "debit"
    params = {
        "banque": account, "company": company, "montant": flt(montant, 3),
        "debut": date - timedelta(days=fenetre), "fin": date + timedelta(days=fenetre),
    }
    join_cp = ""
    if contrepartie:
        join_cp = """join `tabJournal Entry Account` cp
                          on cp.parent = je.name and cp.account = %(contrepartie)s"""
        params["contrepartie"] = contrepartie
    return frappe.db.sql(
        f"""
        select distinct je.name, je.posting_date, je.docstatus, je.cheque_no,
               je.user_remark, je.remark, je.total_debit
        from `tabJournal Entry` je
        join `tabJournal Entry Account` banque
             on banque.parent = je.name and banque.account = %(banque)s
            and banque.{colonne} > 0
        {join_cp}
        where je.docstatus != 2
          and je.company = %(company)s
          and je.posting_date between %(debut)s and %(fin)s
          and abs(banque.{colonne} - %(montant)s) < 0.005
        """,
        params, as_dict=1)


def payment_entries_bancaires(date_from=None, date_to=None, compte: str = BANK_ACCOUNT) -> list:
    """Payment Entry soumises touchant le compte bancaire, dans LES DEUX SENS.

    Les reglements fournisseurs sont des Payment Entry `Pay` (154 sur Zitouna, 561 168 DT), pas des
    ecritures de journal. Un debit bancaire qui n'est cherche que parmi les Journal Entry ressort
    donc « orphelin » alors qu'il est parfaitement saisi — c'etait le cas des virements JEGHAM.
    """
    filters = {"docstatus": 1, "company": COMPANY}
    if date_from and date_to:
        filters["posting_date"] = ["between", [date_from, date_to]]
    rows = frappe.db.get_all(
        "Payment Entry",
        filters=dict(filters, paid_from=compte), limit_page_length=0,
        fields=["name", "posting_date", "paid_amount", "party", "reference_no", "payment_type"])
    recues = frappe.db.get_all(
        "Payment Entry",
        filters=dict(filters, paid_to=compte), limit_page_length=0,
        fields=["name", "posting_date", "paid_amount", "party", "reference_no", "payment_type"])
    for r in rows:
        r["sens"] = "debit"
    for r in recues:
        r["sens"] = "credit"
    return list(rows) + list(recues)


def ecritures_bancaires_cumulees(date_from=None, date_to=None, sens: str = "credit",
                                 compte: str = BANK_ACCOUNT, marge: int = 4) -> list:
    """Ecritures de journal touchant le compte bancaire, avec le CUMUL de leurs lignes.

    POURQUOI UN CUMUL, ET NON LA LIGNE
    ----------------------------------
    Une echeance de pret credite la banque DEUX fois — principal et profit sur deux lignes
    distinctes, a l'identique des saisies manuelles (cf. ACC-JV-2026-00412, « Remboursement
    5/10 Pret nantissement » : 16 951,250 + 753,978). Aucune ligne isolee n'egale donc le
    decaissement du releve : seul leur total le fait. Chercher ligne a ligne declarait ces
    echeances non comptabilisees alors qu'elles l'etaient.

    `sens='credit'` = sortie d'argent (la banque est creditee), donc un debit du releve.
    La fenetre est elargie de `marge` jours de chaque cote : la date de comptabilisation
    devance ou suit celle du releve de un a trois jours (cas reel ACC-JV-2026-00506, saisie
    le 18/07 pour un prelevement du 20/07).
    """
    colonne = "credit" if sens == "credit" else "debit"
    params = {"compte": compte, "company": COMPANY}
    borne = ""
    if date_from and date_to:
        borne = "and gl.posting_date between %(debut)s and %(fin)s"
        params["debut"] = getdate(date_from) - timedelta(days=marge)
        params["fin"] = getdate(date_to) + timedelta(days=marge)
    rows = frappe.db.sql(
        f"""
        select gl.voucher_no, min(gl.posting_date) as posting_date,
               sum(gl.{colonne}) as montant
        from `tabGL Entry` gl
        where gl.account = %(compte)s and gl.company = %(company)s
          and gl.voucher_type = 'Journal Entry' and gl.is_cancelled = 0
          and gl.{colonne} > 0
          {borne}
        group by gl.voucher_no
        """, params, as_dict=1)
    for r in rows:
        r["montant"] = flt(r["montant"], 3)
    return rows


def pieces_bancaires(date_from=None, date_to=None, compte: str = BANK_ACCOUNT,
                     marge: int = 4) -> list:
    """TOUTES les pieces ERPNext touchant le compte bancaire, quel que soit leur type.

    C'est l'inventaire du COTE ERPNEXT, celui qui manquait : `ecritures_bancaires_cumulees` ne
    voit que les Journal Entry et `payment_entries_bancaires` que les Payment Entry, chacune pour
    les besoins d'un appariement precis. Ici on veut la liste exhaustive, pour pouvoir repondre a
    « quelle piece n'a AUCUN mouvement bancaire en face ».

    Le cumul par piece est indispensable (cf. `ecritures_bancaires_cumulees`) : une echeance de
    pret credite la banque deux fois, aucune ligne isolee n'egale le prelevement.

    CONVENTION DE SENS — celle du RELEVE, pas celle du grand livre :
    un debit du compte bancaire cote ERPNext est de l'argent qui ENTRE, donc un CREDIT au releve.
    Les deux cotes se comparent ainsi sans retournement mental a chaque lecture.

    Rend : voucher_type, voucher_no, posting_date, entree, sortie, montant (absolu),
    sens ('Debit'|'Credit' au sens du releve), texte (tous les champs porteurs de reference),
    party.
    """
    params = {"compte": compte, "company": COMPANY}
    borne = ""
    if date_from and date_to:
        borne = "and gl.posting_date between %(debut)s and %(fin)s"
        params["debut"] = getdate(date_from) - timedelta(days=marge)
        params["fin"] = getdate(date_to) + timedelta(days=marge)
    rows = frappe.db.sql(
        f"""
        select gl.voucher_type, gl.voucher_no, min(gl.posting_date) as posting_date,
               sum(gl.debit) as entree, sum(gl.credit) as sortie
        from `tabGL Entry` gl
        where gl.account = %(compte)s and gl.company = %(company)s and gl.is_cancelled = 0
          {borne}
        group by gl.voucher_type, gl.voucher_no
        """, params, as_dict=1)

    textes = _textes_des_pieces(rows)
    out = []
    for r in rows:
        entree, sortie = flt(r["entree"], 3), flt(r["sortie"], 3)
        net = round(entree - sortie, 3)
        if not net:
            # Piece qui entre et sort le meme montant du compte : rien a rapprocher au releve.
            continue
        t = textes.get((r["voucher_type"], r["voucher_no"]), {})
        out.append({
            "voucher_type": r["voucher_type"], "voucher_no": r["voucher_no"],
            "posting_date": r["posting_date"], "entree": entree, "sortie": sortie,
            "montant": abs(net), "sens": "Credit" if net > 0 else "Debit",
            "texte": t.get("texte", ""), "party": t.get("party"),
        })
    return out


def _textes_des_pieces(rows) -> dict:
    """{(voucher_type, voucher_no) -> {texte, party}} : tout ce qui peut porter une reference.

    Une piece se relie au releve par ce que l'utilisateur y a ecrit — reference bancaire, n° de
    cheque, n° de bon de remise. Ces champs different par type de document, d'ou la lecture par
    lots plutot qu'une jointure : deux requetes suffisent pour l'ensemble d'une periode.
    """
    par_type: dict = {}
    for r in rows:
        par_type.setdefault(r["voucher_type"], []).append(r["voucher_no"])

    champs = {
        "Journal Entry": ["name", "cheque_no", "user_remark", "remark"],
        "Payment Entry": ["name", "reference_no", "remarks", "party"],
    }
    out: dict = {}
    for vtype, noms in par_type.items():
        fields = champs.get(vtype)
        if not fields:
            continue
        for i in range(0, len(noms), 500):
            for d in frappe.db.get_all(vtype, filters={"name": ["in", noms[i:i + 500]]},
                                       fields=fields, limit_page_length=0):
                texte = " ".join(str(d.get(f) or "") for f in fields if f not in ("name", "party"))
                out[(vtype, d["name"])] = {"texte": texte.strip(), "party": d.get("party")}
    return out


def _norm_ref(v) -> str:
    """Reference comparable : majuscules, sans espaces ni ponctuation.

    Indispensable ici — les references saisies a la main portent des fautes reelles :
    une espace en tete (' FT26191M0NN9') ou un caractere manquant ('FT26195KVMB' pour
    'FT261915KVMB'). La normalisation rattrape la premiere, pas la seconde : d'ou le repli
    sur le montant et la date.
    """
    return "".join(c for c in str(v or "").upper() if c.isalnum())


_RUN_CHIFFRES = re.compile(r"\d{6,}")


def numeros_de(reference_no) -> set:
    """Suites d'au moins 6 chiffres d'un `reference_no` — typiquement un n° de cheque.

    Les references de reglement fournisseur sont saisies sous des formes variees :
    '4000962', '400966-Bq Zitouna', '4001004'. On en extrait les suites de chiffres pour
    pouvoir les comparer au numero que le releve porte dans son libelle.
    """
    return set(_RUN_CHIFFRES.findall(str(reference_no or "")))


def apparier_payment_entry(m: dict, pes: list, fenetre: int = 7, tolerance: float = 1.5,
                           numero: str = None):
    """Payment Entry correspondant a un mouvement bancaire. -> (pe, mode, ecart) | (None, None, 0).

    Quatre passes, de la plus sure a la plus permissive :
      1. `reference`  : la reference bancaire figure dans le reference_no (compare normalise) ;
      2. `numero`     : le n° de CHEQUE porte par le libelle du releve ;
      3. `montant`    : meme montant a 5 millimes pres, dans la fenetre de dates ;
      4. `approchant` : montant proche a `tolerance` pres — signale, jamais tenu pour acquis.

    POURQUOI LA PASSE PAR NUMERO EXISTE
    -----------------------------------
    Le releve ecrit « REGLEMENT CHEQUE 4000968 LE CHAUFFAGE PAR LE SOL » : le numero y est en
    clair, et la Payment Entry le porte dans sa reference. Sans cette passe, 6 reglements
    fournisseurs sur 21 restaient non rapproches, pour deux raisons opposees :
      - un ecart de quelques millimes entre la piece et le releve (−0,016, +0,500, −1,122) faisait
        echouer le repli sur le montant ;
      - a l'inverse, le cheque 4000962 valait 400,000 des DEUX cotes, mais deux Payment Entry
        partageaient ce montant, donc le code refusait de trancher.
    Un numero de cheque est unique par construction : il tranche les deux cas. L'ecart de montant
    est alors RENDU, pour etre signale au lieu de disparaitre.
    """
    from datetime import timedelta

    sens = "debit" if flt(m.get("debit"), 3) else "credit"
    montant = flt(m.get("debit") or m.get("credit"), 3)
    ref = _norm_ref(m.get("reference"))
    jour = getdate(m["date"]) if m.get("date") else None
    candidats = [p for p in pes if p.get("sens") == sens]

    if ref:
        for p in candidats:
            pr = _norm_ref(p.get("reference_no"))
            if pr and (ref in pr or pr in ref):
                return p, "reference", flt(flt(p["paid_amount"], 3) - montant, 3)

    numero = str(numero or "").strip()
    if numero:
        # Egalite STRICTE sur la suite de chiffres : un prefixe commun ne prouve rien, et une
        # reference fautive ('400966' pour un cheque 4000966) doit rester non appariee.
        porteurs = [p for p in candidats if numero in numeros_de(p.get("reference_no"))]
        if len(porteurs) == 1:
            p = porteurs[0]
            return p, "numero", flt(flt(p["paid_amount"], 3) - montant, 3)

    if jour:
        proches = [p for p in candidats
                   if abs((getdate(p["posting_date"]) - jour).days) <= fenetre]
        exacts = [p for p in proches if abs(flt(p["paid_amount"], 3) - montant) < 0.005]
        if len(exacts) == 1:
            return exacts[0], "montant", 0.0
        approchants = [p for p in proches
                       if abs(flt(p["paid_amount"], 3) - montant) <= tolerance]
        if len(approchants) == 1:
            p = approchants[0]
            return p, "approchant", flt(flt(p["paid_amount"], 3) - montant, 3)
    return None, None, 0.0
