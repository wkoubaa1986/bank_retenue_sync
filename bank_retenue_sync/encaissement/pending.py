"""Payment Entry EN ATTENTE de depot en banque (comptes d'attente A&S) et non encore
encaissees dans un Encaissement Paiement.

Modele metier (verifie en base) : a la vente/livraison, le reglement recu (cheque client,
COD Aramex, traite) est deja enregistre en Payment Entry 'Receive' sur un compte d'attente :

    Cheques -> 'Chèques - A&S'          reference_no = '<n° cheque 7ch>-<banque>'
    Traites -> 'Traite Bancaire - A&S'  reference_no = '<n° traite>'
    Aramex  -> 'Livraison Aramex - A&S' reference_no = 'Aramex N: <tracking>'

Quand le depot arrive en banque, l'Encaissement Paiement (soumis) deplace le paid_to de ces
PE vers 'STE430127B - Zitouna - A&S'. Ce module liste les PE encore en attente et ecarte
celles deja referencees dans un Encaissement Paiement (brouillon ou soumis, hors annule).
"""
from __future__ import annotations

import re

import frappe
from frappe.utils import flt

COMPANY = "Aquaworld & Servicing"
CHEQUE_ACCOUNT = "Chèques - A&S"
TRAITE_ACCOUNT = "Traite Bancaire - A&S"
ARAMEX_ACCOUNT = "Livraison Aramex - A&S"
# Compte d'attente des commandes livrees NON PAYEES : a la livraison, le solde du client bascule
# de 'Débiteurs' vers ce compte, et il y reste jusqu'a reception du virement.
DETTE_ACCOUNT = "Dettes - A&S"
BANK_ACCOUNT = "STE430127B - Zitouna - A&S"

# Table enfant (fieldname du Table) -> DocType de la ligne : sert a reper les ref_paiement deja pris.
_ENCAISSEMENT_CHILD_TABLES = ("Liste Cheque", "Liste Aramex", "Liste Traite Bancaire", "Liste Dettes")

_LEADING_NUM_RE = re.compile(r"(\d{5,})")
_ARAMEX_NUM_RE = re.compile(r"ARAMEX\s*N\s*:?\s*(\d{6,})", re.I)


def _extract_number(reference_no: str):
    """Extrait le n° identifiant (cheque/traite) du reference_no d'une PE.
    Prend la premiere suite d'au moins 5 chiffres : '4001710-Banque Zitouna' -> '4001710',
    '011644584802' -> '011644584802'."""
    m = _LEADING_NUM_RE.search(reference_no or "")
    return m.group(1) if m else None


def _extract_aramex_number(reference_no: str):
    """N° de suivi Aramex, extrait UNIQUEMENT du motif 'Aramex N: <suivi>'.

    Le compte d'attente Aramex contient aussi des reglements d'une autre origine, dont le
    reference_no est du type 'WEB1-007803' (n° de commande web). Le regex generique en tirerait
    '007803', qui pourrait s'aligner par hasard sur un numero de l'advice Aramex et provoquer un
    appariement faux. Sans motif explicite -> None : la PE est ignoree (et diagnostiquee)."""
    m = _ARAMEX_NUM_RE.search(reference_no or "")
    return m.group(1) if m else None


def already_encaissed_refs() -> set:
    """Ensemble des `ref_paiement` deja presents dans un Encaissement Paiement non annule
    (brouillon docstatus=0 OU soumis docstatus=1). Empeche de re-encaisser une meme PE."""
    consumed: set = set()
    for child in _ENCAISSEMENT_CHILD_TABLES:
        if not frappe.db.exists("DocType", child):
            continue
        rows = frappe.get_all(
            child,
            filters={"ref_paiement": ["is", "set"], "parenttype": "Encaissement Paiement"},
            fields=["ref_paiement", "parent"],
        )
        if not rows:
            continue
        parents = {r["parent"] for r in rows}
        alive = {
            d["name"]
            for d in frappe.get_all(
                "Encaissement Paiement",
                filters={"name": ["in", list(parents)], "docstatus": ["!=", 2]},
                fields=["name"],
            )
        }
        for r in rows:
            if r["ref_paiement"] and r["parent"] in alive:
                consumed.add(r["ref_paiement"])
    return consumed


def consumed_bank_keys() -> dict:
    """Cles bancaires DEJA encaissees, par flux, lues sur les Encaissement Paiement non annules :

        cheque   -> `bon_remise` de Liste Cheque              = n° de bon de remise ('90028160')
        traite   -> `bon_remise` de Liste Traite Bancaire     = reference bancaire ('FT26198BPZR0')
        aramex   -> `n_virement` de Liste Aramex              = reference bancaire ('FT26209ZZCTH')
        virement -> `n_chèque` de Liste des Dettes client     = reference bancaire ('FT26211D5NMC')

    (Conventions verifiees sur les saisies manuelles historiques : le code produit les memes cles,
    la dedup couvre donc aussi tout l'existant.)

    Sert de garde-fou AMONT dans `matching` : `already_encaissed_refs` ecarte les Payment Entry
    deja consommees, mais pas le MOUVEMENT bancaire. Sans ca, chaque run reprend tous les depots
    de la fenetre d'export (55 jours), y compris ceux encaisses depuis longtemps — pour les
    cheques cela relance une extraction OpenAI PAYANTE par bordereau, et cela noie les
    diagnostics sous des lignes 'aucune PE en attente' sans objet."""
    keys = {"cheque": set(), "traite": set(), "aramex": set(), "virement": set()}
    for doctype, field, flux in (("Liste Cheque", "bon_remise", "cheque"),
                                 ("Liste Traite Bancaire", "bon_remise", "traite"),
                                 ("Liste Aramex", "n_virement", "aramex"),
                                 ("Liste des Dettes client", "n_chèque", "virement")):
        if not frappe.db.exists("DocType", doctype):
            continue
        rows = frappe.get_all(doctype, filters={field: ["is", "set"],
                                                "parenttype": "Encaissement Paiement"},
                              fields=[field, "parent"])
        if not rows:
            continue
        alive = {d["name"] for d in frappe.get_all(
            "Encaissement Paiement",
            filters={"name": ["in", list({r["parent"] for r in rows})], "docstatus": ["!=", 2]},
            fields=["name"])}
        for r in rows:
            if r.get(field) and r["parent"] in alive:
                keys[flux].add(str(r[field]).strip())
    return keys


def documents_par_cle_bancaire() -> dict:
    """Meme balayage que `consumed_bank_keys`, mais on GARDE le document. -> {flux: {cle: nom}}

    ⚠️ LE REGISTRE NE RETIENT PAS QUEL ENCAISSEMENT A CONSOMME LA CLE. `classify._resoudre_flux`
    pose `document_type = "Encaissement Paiement"` et laisse `document_name` vide : il lui suffit
    de savoir que la cle est consommee. A l'ecran, cela donne un mouvement identifie qui ne dit
    pas PAR QUOI — 23 encaissements de juillet dans ce cas. Cette fonction refait le lien a la
    lecture, sans rien ecrire.
    """
    par_flux = {"cheque": {}, "traite": {}, "aramex": {}, "virement": {}}
    for doctype, field, flux in (("Liste Cheque", "bon_remise", "cheque"),
                                 ("Liste Traite Bancaire", "bon_remise", "traite"),
                                 ("Liste Aramex", "n_virement", "aramex"),
                                 ("Liste des Dettes client", "n_chèque", "virement")):
        if not frappe.db.exists("DocType", doctype):
            continue
        rows = frappe.get_all(doctype, filters={field: ["is", "set"],
                                                "parenttype": "Encaissement Paiement"},
                              fields=[field, "parent"])
        if not rows:
            continue
        alive = {d["name"] for d in frappe.get_all(
            "Encaissement Paiement",
            filters={"name": ["in", list({r["parent"] for r in rows})], "docstatus": ["!=", 2]},
            fields=["name"])}
        for r in rows:
            if r.get(field) and r["parent"] in alive:
                par_flux[flux].setdefault(str(r[field]).strip(), r["parent"])
    return par_flux


# Cles bancaires citees par la `reference_no` d'une Payment Entry.
#   cheques  -> « 0000528-BIAT / BR:90028245 »            (BR: + n° de bon de remise)
#   traites  -> « 11376605424- / RE:FT26198BPZR0 »        (RE: + reference bancaire)
#   aramex   -> « Aramex N: 5133... Virement recu N: FT26216X2T8Z »
#   virements-> « FT26211D5NMC - Banque Zitouna »
_RX_BON = re.compile(r"BR:\s*(\d+)")
_RX_REF = re.compile(r"\b([A-Z]{2}\d{2,}[A-Z0-9]{4,})\b")


def montants_par_cle_bancaire() -> dict:
    """{cle bancaire -> montant total COMPTABILISE sur le compte bancaire}.

    Sert a mesurer l'ECART DE PAIEMENT : ce que la banque a credite pour une operation, face a ce
    qui a ete porte au compte bancaire pour cette meme operation.

    /!\\ On somme les PAYMENT ENTRY postees sur le compte, PAS les lignes d'`Encaissement Paiement`.
    Les lignes d'encaissement sont l'ENTREE du traitement, pas son resultat : re-saisir un cheque
    deja encaisse y laisse deux lignes, alors que le server script ANNULE puis REMPLACE la Payment
    Entry d'origine — la comptabilite, elle, n'a jamais double. Mesurer sur les lignes produisait
    donc de faux ecarts (verifie : 4 « doublons » sur 5 n'en etaient pas).
    """
    rows = frappe.db.sql("""
        select reference_no, paid_amount
        from `tabPayment Entry`
        where docstatus = 1 and ifnull(reference_no, '') != ''
          and (paid_to = %(c)s or paid_from = %(c)s)
    """, {"c": BANK_ACCOUNT}, as_dict=True)

    totaux: dict = {}
    for r in rows:
        ref = r.reference_no or ""
        cles = set(_RX_BON.findall(ref)) | set(_RX_REF.findall(ref))
        montant = flt(r.paid_amount, 3)
        for cle in cles:
            totaux[cle] = round(totaux.get(cle, 0.0) + montant, 3)
    return totaux


def paiements_par_cle_bancaire() -> dict:
    """{cle bancaire -> [noms de Payment Entry]}. Le pendant nomme de `montants_par_cle_bancaire`.

    ⚠️ CE SONT LES PAYMENT ENTRY DE REMPLACEMENT, PAS CELLES DU BORDEREAU. Le `ref_paiement` des
    lignes d'`Encaissement Paiement` pointe vers la piece d'ORIGINE, que le server script annule
    et remplace a la soumission : sur ce site, 1 894 lignes sur 1 894 designent une Payment Entry
    disparue. Les pieces vivantes sont les nouvelles, reconnaissables a la cle bancaire citee dans
    leur `reference_no` (« … / BR:90027975 »). C'est par la qu'un credit retrouve les factures
    qu'il solde.
    """
    rows = frappe.db.sql("""
        select name, reference_no
        from `tabPayment Entry`
        where docstatus = 1 and ifnull(reference_no, '') != ''
          and (paid_to = %(c)s or paid_from = %(c)s)
    """, {"c": BANK_ACCOUNT}, as_dict=True)

    par_cle: dict = {}
    for r in rows:
        ref = r.reference_no or ""
        for cle in set(_RX_BON.findall(ref)) | set(_RX_REF.findall(ref)):
            par_cle.setdefault(cle, []).append(r.name)
    return par_cle


def _pending_pes(paid_to: str, exclude: set, extractor=None) -> list:
    """PE 'Receive' soumises sur le compte d'attente `paid_to`, non deja encaissees.
    `extractor(reference_no)` derive le n° servant a l'appariement (defaut : suite de chiffres)."""
    extractor = extractor or _extract_number
    pes = frappe.get_all(
        "Payment Entry",
        filters={
            "company": COMPANY,
            "payment_type": "Receive",
            "paid_to": paid_to,
            "docstatus": 1,
        },
        fields=["name", "party", "reference_no", "reference_date", "paid_amount"],
        order_by="reference_date asc",
    )
    out = []
    for pe in pes:
        if pe["name"] in exclude:
            continue
        pe["numero"] = extractor(pe.get("reference_no"))
        out.append(pe)
    return out


def get_pending_cheques(exclude: set = None) -> list:
    """PE cheque en attente (paid_to = 'Chèques - A&S')."""
    return _pending_pes(CHEQUE_ACCOUNT, exclude if exclude is not None else already_encaissed_refs())


def get_pending_traites(exclude: set = None) -> list:
    """PE traite bancaire en attente (paid_to = 'Traite Bancaire - A&S')."""
    return _pending_pes(TRAITE_ACCOUNT, exclude if exclude is not None else already_encaissed_refs())


def get_pending_aramex(exclude: set = None) -> list:
    """PE COD Aramex en attente (paid_to = 'Livraison Aramex - A&S')."""
    return _pending_pes(ARAMEX_ACCOUNT, exclude if exclude is not None else already_encaissed_refs(),
                        extractor=_extract_aramex_number)


def get_pending_dettes(exclude: set = None) -> list:
    """Dettes clients en attente de reglement (paid_to = 'Dettes - A&S').

    Ici `reference_no` n'est pas un numero de cheque mais le SALES ORDER d'origine
    ('SAL-ORD-2026-02413') — c'est ce que la table `Liste Dettes` attend dans son champ `bl`.
    Quelques lignes historiques portent un libelle libre ('Dette a comprendre', 'Timbre fiscal
    06-2025') : `bl` est alors laisse vide, et le server script retrouve la piece via la premiere
    reference de la Payment Entry."""
    exclude = exclude if exclude is not None else already_encaissed_refs()
    pes = frappe.get_all(
        "Payment Entry",
        filters={"company": COMPANY, "payment_type": "Receive",
                 "paid_to": DETTE_ACCOUNT, "docstatus": 1},
        fields=["name", "party", "reference_no", "reference_date", "posting_date", "paid_amount"],
        order_by="posting_date asc, name asc",     # anciennete : base de l'imputation FIFO
    )
    out = []
    for pe in pes:
        if pe["name"] in exclude:
            continue
        ref = (pe.get("reference_no") or "").strip()
        pe["sales_order"] = ref if frappe.db.exists("Sales Order", ref) else None
        out.append(pe)
    return out


def get_pending_dettes_aramex(exclude: set = None) -> list:
    """Reliquats de dette CLIENT restes sur le compte Aramex, sans numero de suivi.

    POURQUOI CE VIVIER EXISTE
    -------------------------
    Quand une commande livree en COD par Aramex n'est reglee que PARTIELLEMENT, le solde impaye
    reste sur le compte d'attente D'ORIGINE — 'Livraison Aramex - A&S' — et non sur 'Dettes - A&S'.
    Cas reel : commande WEB1-007803 de 46 DT, 6 DT encaisses (ENC-22-07-2026-00003), les 40 DT
    restants demeurent sur la PE ACC-PAY-2026-04820. Le client a ensuite vire ces 40 DT lui-meme.

    Ce reliquat etait INATTEIGNABLE par les deux flux a la fois :
      - `match_aramex` apparie par numero d'advice, et ce reliquat n'en a aucun ;
      - `match_virements` ne regardait que 'Dettes - A&S'.

    LE DISCRIMINANT EST L'ABSENCE DE NUMERO DE SUIVI. Une PE du compte Aramex qui en porte un
    (33 sur 36 en aout 2026) attend une remise d'Aramex et ne doit PAS entrer ici.

    DESTINATION : LA TABLE DETTES, comme n'importe quelle dette
    ----------------------------------------------------------
    C'est un virement client recu qui solde une dette : le compte d'attente d'origine ne change
    pas sa nature. La branche dettes du server script convient d'ailleurs telle quelle — elle
    supprime la Payment Entry qu'on lui designe SANS regarder son `paid_to` (donc le compte
    Aramex est solde de la meme facon) puis cree une PE 'Débiteurs -> Zitouna' au prorata. Elle
    accepte en plus le paiement PARTIEL et le virement GROUPE, que la branche aramex ne sait pas
    faire (celle-ci deplace la PE en entier).

    /!\ `sales_order` est volontairement laisse a None. Le server script ne met a jour le
    `payment_schedule` du Sales Order que s'il y trouve une echeance « Dette non payée » d'un
    montant STRICTEMENT egal — or l'encaissement Aramex anterieur a REMPLACE ce payment_schedule
    par une ligne unique du montant remis (6 DT pour WEB1-007803). Fournir `bl` ferait donc
    chercher une echeance de 40 DT qui n'existe plus, et le script n'ecrirait rien en silence.
    Sans `bl`, il retrouve la piece via la premiere reference de la Payment Entry — le chemin
    deja prevu pour les dettes a libelle libre.
    """
    exclude = exclude if exclude is not None else already_encaissed_refs()
    out = []
    for pe in _pending_pes(ARAMEX_ACCOUNT, exclude, extractor=_extract_aramex_number):
        if pe.get("numero"):
            continue
        pe["sales_order"] = None
        pe["origine_compte"] = ARAMEX_ACCOUNT
        out.append(pe)
    return out


def dette_matches_schedule(sales_order: str, valeur: float) -> bool:
    """Le Sales Order porte-t-il bien une echeance 'Dette non payée' du montant `valeur` ?

    Le server script apparie la ligne de detail a l'echeance par EGALITE STRICTE
    (`iterm.payment_amount == ipay.valeur`) et, s'il ne trouve rien, ne fait RIEN : ni erreur, ni
    message — la dette resterait ouverte alors que le brouillon parait complet. On verifie donc
    en amont, pour transformer un echec muet en diagnostic."""
    if not sales_order:
        return True          # sans `bl`, le script passe par les references de la PE, pas par l'echeance
    rows = frappe.get_all("Payment Schedule",
                          filters={"parent": sales_order, "parenttype": "Sales Order",
                                   "mode_of_payment": "Dette non payée"},
                          fields=["payment_amount"])
    return any(abs((r["payment_amount"] or 0) - (valeur or 0)) < 0.0005 for r in rows)


def bank_refs_already_booked(references) -> set:
    """Sous-ensemble de `references` (references bancaires de l'export) qui apparaissent deja dans
    le `reference_no` d'une Payment Entry soumise.

    Sert de garde-fou pour le flux virement : quand le comptable a deja saisi l'encaissement a la
    main, il ne faut pas en creer un second. Le rapprochement le signale (la dette d'origine, elle,
    reste ouverte sur 'Dettes - A&S') mais ne produit aucune ligne."""
    refs = {str(r).strip() for r in (references or []) if str(r or "").strip()}
    if not refs:
        return set()
    rows = frappe.get_all("Payment Entry",
                          filters={"docstatus": 1, "reference_no": ["is", "set"]},
                          fields=["name", "reference_no"])
    booked = set()
    for r in rows:
        ref_no = r["reference_no"] or ""
        for ref in refs:
            if ref in ref_no:
                booked.add(ref)
    return booked


def etat_encaissements_par_cle() -> dict:
    """Etat des Encaissement Paiement consommant chaque cle bancaire, pour l'identification :

        {"docs":     {flux: {cle: nom de l'encaissement}},        # = documents_par_cle_bancaire
         "etats":    {nom: {"docstatus": 0|1, "ecarts_bloquants": n}},
         "montants_brouillon": {flux: {cle: somme des lignes du brouillon}}}

    Pourquoi les montants de brouillon : `montants_par_cle_bancaire` mesure les PAYMENT ENTRY
    postees sur le compte bancaire — or un encaissement encore en BROUILLON n'a rien poste. Pour
    afficher « le montant trouve dans ERPNext » d'un mouvement couvert par un brouillon (demande
    utilisateur 2026-08-18), la seule source est la somme de ses lignes. Le risque de double
    ligne documente sur `montants_par_cle_bancaire` ne s'applique pas : on ne lit QUE des
    brouillons, dont les PE n'ont pas encore ete remplacees."""
    docs = documents_par_cle_bancaire()
    noms = {n for par_cle in docs.values() for n in par_cle.values()}
    etats, montants = {}, {"cheque": {}, "traite": {}, "aramex": {}, "virement": {}}
    if not noms:
        return {"docs": docs, "etats": etats, "montants_brouillon": montants}

    for d in frappe.get_all("Encaissement Paiement",
                            filters={"name": ["in", list(noms)]},
                            fields=["name", "docstatus"]):
        etats[d["name"]] = {"docstatus": d["docstatus"], "ecarts_bloquants": 0}
    if frappe.db.exists("DocType", "BRS Ecart Encaissement"):
        for e in frappe.get_all("BRS Ecart Encaissement",
                                filters={"encaissement": ["in", list(noms)],
                                         "bloquant": 1, "statut": "À traiter"},
                                fields=["encaissement"]):
            if e["encaissement"] in etats:
                etats[e["encaissement"]]["ecarts_bloquants"] += 1

    brouillons = [n for n, e in etats.items() if e["docstatus"] == 0]
    if brouillons:
        for doctype, cle_field, val_field, flux in (
                ("Liste Cheque", "bon_remise", "valeur", "cheque"),
                ("Liste Traite Bancaire", "bon_remise", "valeur", "traite"),
                ("Liste Aramex", "n_virement", "valeur", "aramex"),
                ("Liste des Dettes client", "n_chèque", "valeur_du_cheque", "virement")):
            if not frappe.db.exists("DocType", doctype):
                continue
            for r in frappe.get_all(doctype,
                                    filters={"parent": ["in", brouillons],
                                             cle_field: ["is", "set"]},
                                    fields=[cle_field, val_field]):
                cle = str(r[cle_field]).strip()
                if not cle:
                    continue
                montants[flux][cle] = round(
                    montants[flux].get(cle, 0.0) + flt(r[val_field]), 3)
    return {"docs": docs, "etats": etats, "montants_brouillon": montants}
