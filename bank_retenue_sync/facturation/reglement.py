"""Ce qu'un mouvement bancaire a REELLEMENT paye : le detail derriere la piece comptable.

Portage de `enrich_observations_with_payment_details` (outil externe) et du Server Script
`payment_details`. Sans lui, un mouvement identifie affiche « ACC-PAY-2026-04530 » — vrai, et
inutilisable : on ne sait ni quelles factures ce virement solde, ni s'il les solde en entier.

⚠️ L'ORIGINAL NE COUVRAIT QUE LES ENCAISSEMENTS, ET CELA NE SUFFIT PAS ICI. `payment_details` ne
regarde que les references de type Sales Invoice, avec repli sur Sales Order. Sur un mois reel,
les seuls paiements rattachables etaient des reglements FOURNISSEUR, sur Purchase Invoice : le
port fidele rendait zero ligne enrichie sur 138 mouvements. Les trois types de reference sont
donc traites, et l'ecriture de journal — 98 lignes du mois a elle seule — rend sa note.

⚠️ ET C'EST UN LOT, PAS UN APPEL PAR LIGNE. Le script interrogeait l'API en HTTP une fois par
mouvement, avec un `frappe.get_doc` par facture cote serveur. Ici tout est lu en six requetes.
"""
from __future__ import annotations

import re
from collections import defaultdict

import frappe
from frappe.utils import flt

from bank_retenue_sync.facturation import factures as M_factures

PRECISION = 3


def details_par_piece(pieces: list) -> dict:
    """[(doctype, nom)] -> {(doctype, nom): {reference, tiers, note, lignes, total_alloue}}"""
    par_type = defaultdict(set)
    for doctype, nom in pieces or []:
        if doctype and nom:
            par_type[doctype].add(nom)

    out = {}
    out.update(_paiements(sorted(par_type.get("Payment Entry", ()))))
    out.update(_journaux(sorted(par_type.get("Journal Entry", ()))))
    return out


def details_par_mouvement(mouvements: list) -> dict:
    """La description de chaque mouvement du releve. -> {cle du mouvement: detail}

    Trois familles, trois chemins — le registre ne les traite pas de la meme facon :
      · une piece NOMMEE (Payment Entry, Journal Entry) : on lit ce qu'elle porte ;
      · un encaissement SANS nom : le registre sait la cle consommee, pas le document —
        on refait le lien par la cle bancaire ;
      · une echeance de PRET ou de LEASING : la piece ne dit pas de quel contrat il s agit,
        c est le contrat qu'il faut nommer.
    """
    par_piece = details_par_piece(
        [(m.get("document_type"), m.get("document_name")) for m in mouvements or []])
    sans_nom = _encaissements([m for m in mouvements or []
                               if not m.get("document_name") and m.get("document_type")])
    contrats = _contrats_du_releve(mouvements)

    out = {}
    for m in mouvements or []:
        cle = m.get("cle")
        detail = par_piece.get((m.get("document_type"), m.get("document_name"))) \
            or sans_nom.get(cle)
        contrat = contrats.get(cle)
        if contrat:
            detail = dict(detail or {"reference": m.get("reference") or "", "tiers": "",
                                     "note": "", "lignes": [], "total_alloue": 0.0})
            detail["contrat"] = contrat
            # ⚠️ LE TITRE D'UNE ECHEANCE EST SON `cheque_no`, PAS SON `title`. Le champ `title`
            # d'une ecriture de journal vaut « STE430127B - Zitouna - A&S » — le nom du compte
            # bancaire, identique sur les six ecritures du mois et sans aucune valeur. Le titre
            # utile, « Remboursement 7/10 Pret nantissement », vit dans le numero de reference.
            # On le promeut en tete plutot que de le laisser en petites capitales au-dessus
            # d'un pave technique.
            detail["note"] = detail.get("reference") or detail.get("note") or ""
            detail["reference"] = ""
        if detail:
            out[cle] = detail
    return out


# ------------------------------------------------------------------ paiements


def _paiements(noms: list) -> dict:
    if not noms:
        return {}

    paiements = {p.name: p for p in frappe.get_all(
        "Payment Entry", filters={"name": ["in", noms]},
        fields=["name", "reference_no", "party_name", "party", "payment_type"],
        limit_page_length=0)}
    if not paiements:
        return {}

    refs = frappe.get_all("Payment Entry Reference",
                          filters={"parent": ["in", list(paiements)]},
                          fields=["parent", "reference_doctype", "reference_name",
                                  "total_amount", "allocated_amount"],
                          order_by="idx asc", limit_page_length=0)
    par_paiement = defaultdict(list)
    for r in refs:
        par_paiement[r.parent].append(r)

    libelles = _libelles(refs)

    out = {}
    for nom, paiement in paiements.items():
        ventes, achats, avances = [], [], []
        for r in par_paiement.get(nom, []):
            ligne = {"libelle": libelles.get((r.reference_doctype, r.reference_name),
                                             r.reference_name),
                     "total": flt(r.total_amount, PRECISION),
                     "alloue": flt(r.allocated_amount, PRECISION),
                     "document_type": r.reference_doctype,
                     "document_name": r.reference_name}
            if r.reference_doctype == "Sales Invoice":
                ventes.append(ligne)
            elif r.reference_doctype == "Purchase Invoice":
                achats.append(ligne)
            elif r.reference_doctype == "Sales Order":
                avances.append(ligne)
        # ⚠️ L'AVANCE SUR COMMANDE N'EST QU'UN REPLI. Un paiement qui solde des factures ET porte
        # un reliquat d'avance se lit sur ses factures ; montrer les deux ferait compter deux
        # fois le meme argent a l'oeil. C'est deja la regle du Server Script d'origine.
        lignes = (ventes + achats) or avances
        if not lignes:
            continue
        out[("Payment Entry", nom)] = {
            "reference": paiement.reference_no or "",
            "tiers": paiement.party_name or paiement.party or "",
            "note": "",
            "lignes": lignes,
            "total_alloue": flt(sum(l["alloue"] for l in lignes), PRECISION),
        }
    return out


def _libelles(refs: list) -> dict:
    """Le nom d'usage de chaque piece referencee. -> {(doctype, nom): libelle}"""
    par_type = defaultdict(set)
    for r in refs:
        par_type[r.reference_doctype].add(r.reference_name)

    out = {}
    for f in frappe.get_all("Sales Invoice",
                            filters={"name": ["in", sorted(par_type.get("Sales Invoice", ()))]},
                            fields=["name", "posting_date", "custom_numero_facture"],
                            limit_page_length=0) if par_type.get("Sales Invoice") else []:
        out[("Sales Invoice", f.name)] = M_factures.nom_de_piece_depuis_date(
            f.posting_date, f.custom_numero_facture)

    for f in frappe.get_all("Purchase Invoice",
                            filters={"name": ["in", sorted(par_type.get("Purchase Invoice", ()))]},
                            fields=["name", "bill_no", "supplier_name", "supplier"],
                            limit_page_length=0) if par_type.get("Purchase Invoice") else []:
        # Cote achat, la reference utile est celle du FOURNISSEUR : c'est ce numero-la qui figure
        # sur la piece papier, pas le nom interne ERPNext.
        out[("Purchase Invoice", f.name)] = "Achat %s — %s" % (
            f.bill_no or f.name, f.supplier_name or f.supplier or "")

    for c in frappe.get_all("Sales Order",
                            filters={"name": ["in", sorted(par_type.get("Sales Order", ()))]},
                            fields=["name", "transaction_date"],
                            limit_page_length=0) if par_type.get("Sales Order") else []:
        # ⚠️ UNE COMMANDE SE DATE SUR `transaction_date`. Le Server Script d'origine lisait
        # `posting_date`, champ inexistant sur Sales Order : sa branche « avance » ne pouvait
        # pas aboutir.
        date = str(c.transaction_date)[:7] if c.transaction_date else ""
        out[("Sales Order", c.name)] = "Avance sur commande %s%s" % (
            c.name, " (%s)" % date if date else "")
    return out


# ------------------------------------------------------------------ journaux


def _journaux(noms: list) -> dict:
    """La note d'une ecriture de journal — sur ce mois, deux mouvements sur trois.

    Il n'y a pas de facture derriere : la description EST la note saisie a l'ecriture. C'est
    exactement ce que la page Caisse Espèces affiche dans sa colonne « Libellé ».
    """
    if not noms:
        return {}
    out = {}
    # ⚠️ `title` N'EST PAS LU. Sur ce site il vaut le nom du compte bancaire pour toutes les
    # ecritures automatiques : le retenir en repli ferait afficher « STE430127B - Zitouna - A&S »
    # sur des dizaines de lignes, ce qui est pire que rien.
    for j in frappe.get_all("Journal Entry", filters={"name": ["in", noms]},
                            fields=["name", "cheque_no", "user_remark", "mode_of_payment"],
                            limit_page_length=0):
        note = (j.user_remark or "").strip()
        if not note:
            continue
        out[("Journal Entry", j.name)] = {
            "reference": j.cheque_no or "",
            "tiers": j.mode_of_payment or "",
            "note": note,
            "lignes": [],
            "total_alloue": 0.0,
        }
    return out


# ------------------------------------------------------------------ encaissements


# Le flux dont releve chaque regle d'encaissement, et la cle que porte l'Encaissement Paiement :
# le bordereau de cheques indexe son n° de BON, les autres la reference bancaire.
FLUX_PAR_REGLE = {
    "enc_cheque": "cheque", "enc_cheque_preavise": "cheque",
    "enc_effet": "traite", "vir_aramex": "aramex", "vir_client": "virement",
}


def _encaissements(mouvements: list) -> dict:
    """Les factures derriere un credit bancaire. -> {cle du mouvement: detail}

    ⚠️ UN CREDIT N'EST PAS UN PAIEMENT, C'EST UN BORDEREAU. Une remise de cheques porte quatre
    reglements de quatre clients, chacun soldant ses propres factures. Le registre n'en retient
    que la cle consommee ; ici on redescend jusqu'aux factures, comme le faisait
    `payment_details` — avec le meme repli « avance sur commande » quand aucune facture n'est
    referencee.

    ⚠️ ET ON NE PASSE PAS PAR `ref_paiement`. Les lignes du bordereau pointent la Payment Entry
    d'ORIGINE, que le server script annule et remplace a la soumission : elle n'existe plus.
    Les pieces vivantes citent la cle bancaire dans leur `reference_no`.
    """
    interessants = [m for m in mouvements
                    if (m.get("regle") or "") in FLUX_PAR_REGLE or
                    m.get("document_type") in ("Encaissement Paiement", "Payment Entry")]
    if not interessants:
        return {}

    try:
        from bank_retenue_sync.encaissement import pending
        par_flux = pending.documents_par_cle_bancaire()
        pe_par_cle = pending.paiements_par_cle_bancaire()
    except Exception:
        frappe.log_error(title="Encaissements : cles bancaires illisibles",
                         message=frappe.get_traceback())
        par_flux, pe_par_cle = {}, {}

    cles = {m["cle"]: _cle_bancaire(m) for m in interessants}
    noms_pe = sorted({n for c in cles.values() for n in (pe_par_cle.get(c) or [])})
    # Les factures de chaque reglement : exactement la mecanique des paiements fournisseurs.
    factures_par_pe = _paiements(noms_pe)
    entetes_pe = {p.name: p for p in frappe.get_all(
        "Payment Entry", filters={"name": ["in", noms_pe]},
        fields=["name", "party_name", "party", "paid_amount", "reference_no"],
        limit_page_length=0)} if noms_pe else {}

    out = {}
    for m in interessants:
        cle_bancaire = cles.get(m["cle"]) or ""
        flux_du_mouvement = FLUX_PAR_REGLE.get(m.get("regle") or "")
        montant = flt(m.get("credit") or m.get("debit"), PRECISION)
        lignes = []
        for nom in (pe_par_cle.get(cle_bancaire) or []):
            entete = entetes_pe.get(nom) or {}
            payeur = entete.get("party_name") or entete.get("party") or ""
            piece = _piece_de_paiement(entete.get("reference_no"), flux_du_mouvement,
                                       cle_bancaire)
            detail = factures_par_pe.get(("Payment Entry", nom))
            if detail:
                for l in detail["lignes"]:
                    lignes.append(dict(l, tiers=payeur, piece=piece))
            else:
                # Reglement sans facture referencee : on montre au moins qui a paye combien.
                lignes.append({"libelle": "Règlement sans facture rattachée",
                               "tiers": payeur, "piece": piece,
                               "total": flt(entete.get("paid_amount"), PRECISION),
                               "alloue": flt(entete.get("paid_amount"), PRECISION),
                               "document_type": "Payment Entry", "document_name": nom})

        bordereau = (par_flux.get(flux_du_mouvement) or {}).get(cle_bancaire) \
            if flux_du_mouvement else None

        if not lignes and not bordereau:
            continue
        out[m["cle"]] = {
            "reference": cle_bancaire,
            "tiers": "",
            "note": "",
            "bordereau": bordereau,
            "lignes": lignes,
            "total_alloue": flt(sum(l["alloue"] for l in lignes), PRECISION),
            # L'ecart entre le credit et la somme des reglements retrouves : s'il n'est pas nul,
            # une piece manque a l'appel et il vaut mieux le dire que de laisser croire au compte.
            "reste": flt(montant - sum(l["alloue"] for l in lignes), PRECISION) if lignes else 0.0,
        }
    return out


def _cle_bancaire(m: dict) -> str:
    """La cle qu'un encaissement porte : n° de BON pour un cheque, reference bancaire sinon."""
    if FLUX_PAR_REGLE.get(m.get("regle") or "") == "cheque":
        return _numero_de_bon(m.get("operation")) or (m.get("reference") or "").strip()
    return (m.get("reference") or "").strip()


def _numero_de_bon(libelle: str) -> str:
    """« ENC CHEQ TN NUM 90027988 » -> « 90027988 ». Le n° de bon de remise, rien d'autre."""
    trouve = re.search(r"\b(\d{6,})\b", libelle or "")
    return trouve.group(1) if trouve else ""


# Le `reference_no` d'une piece de remplacement porte le moyen de paiement AVANT la cle du
# bordereau, selon quatre formes relevees en base :
#     cheque   « 9416665 - BNA / BR:90027975 »
#     traite   « 11376605424- / RE:FT26198BPZR0 »
#     aramex   « Aramex N: 48812240514 Virement recu N: FT26189VC29V »
#     virement « FT26211D5NMC - Banque Zitouna »   (la tete EST la cle : rien a montrer)
_RX_ARAMEX = re.compile(r"Aramex\s*N\s*:\s*(\S+)", re.I)
_RX_CLE_FIN = re.compile(r"\s*/\s*(?:BR|RE)\s*:.*$", re.I)
_RX_CLE_CITEE = re.compile(r"(?:BR|RE)\s*:\s*(\S+)", re.I)
_RX_VIREMENT_RECU = re.compile(r"Virement\s+recu\s+N\s*:\s*(\S+)", re.I)
# Une reference bancaire Zitouna : deux lettres, puis chiffres et lettres. « FT26189VC29V ».
_RX_REF_BANCAIRE = re.compile(r"\b([A-Z]{2}\d{2,}[A-Z0-9]{4,})\b")

ETIQUETTE_FLUX = {"cheque": "chèque", "traite": "traite"}


def decomposer_reference(reference_no: str) -> dict:
    """Sépare le numero de la PIECE de la reference BANCAIRE. -> {"piece", "banque"}

    Un `reference_no` de reglement client empile les deux, et l'utilisateur cherche tantot
    l'un — le numero inscrit sur le cheque que le client lui a remis — tantot l'autre : la
    reference que la banque affiche sur son releve. Les separer permet de montrer les deux.

        « 9416665 - BNA / BR:90027975 »                        -> 9416665 - BNA   | 90027975
        « 11376605424- / RE:FT26198BPZR0 »                     -> 11376605424     | FT26198BPZR0
        « Aramex N: 48812240514 Virement recu N: FT26189VC29V » -> Aramex n° 4881… | FT26189VC29V
        « FT26211D5NMC - Banque Zitouna »                      -> (rien)          | FT26211D5NMC
    """
    ref = (reference_no or "").strip()
    if not ref:
        return {"piece": "", "banque": ""}

    trouve = _RX_ARAMEX.search(ref)
    if trouve:
        recu = _RX_VIREMENT_RECU.search(ref)
        return {"piece": "Aramex n° %s" % trouve.group(1),
                "banque": recu.group(1) if recu else ""}

    citee = _RX_CLE_CITEE.search(ref)
    banque = citee.group(1) if citee else ""
    tete = _RX_CLE_FIN.sub("", ref).split(" / ")[0].strip().rstrip("-").strip()
    if not banque:
        # Pas de BR:/RE: : la reference bancaire est citee en clair, souvent en tete.
        bancaire = _RX_REF_BANCAIRE.search(tete)
        if bancaire:
            return {"piece": "", "banque": bancaire.group(1)}
    return {"piece": tete, "banque": banque}


def _piece_de_paiement(reference_no: str, flux: str, cle: str) -> str:
    """« 9416665 - BNA / BR:90027975 » + flux cheque -> « chèque 9416665 - BNA ».

    C'est le numero que le client reconnait — celui inscrit sur son cheque ou sur son colis
    Aramex. Sans lui, une remise de quinze reglements ne se pointe pas.
    """
    piece = decomposer_reference(reference_no)["piece"]
    if not piece or (cle and cle in piece):
        return ""
    if piece.lower().startswith("aramex"):
        return piece
    etiquette = ETIQUETTE_FLUX.get(flux or "")
    return "%s %s" % (etiquette, piece) if etiquette else piece


# ------------------------------------------------------------------ prets et leasings


def _contrats_du_releve(mouvements: list) -> dict:
    """Le contrat de financement derriere chaque echeance. -> {cle du mouvement: {...}}

    Deux identifications, parce que les deux familles ne se reconnaissent pas pareil :
      · un LEASING porte sa reference de contrat `LD…` sur chaque ligne du releve ;
      · deux PRETS partagent le meme libelle bancaire et ne se distinguent que par le TOTAL
        MENSUEL constant de leur echeance (principal + profit du jour).
    C'est exactement la regle de `expenses/contrats.py`, appliquee ici en lecture seule.
    """
    echeances = [m for m in mouvements or [] if (m.get("categorie") or "") == "pret"]
    if not echeances:
        return {}
    try:
        from bank_retenue_sync.expenses import contrats as C
        liste = C.load_contrats()
    except Exception:
        return {}
    if not liste:
        return {}

    def decrire(c):
        return {"cle": c.get("cle"), "libelle": c.get("libelle") or c.get("cle"),
                "type": c.get("type") or "", "reference": c.get("reference_bancaire") or ""}

    par_reference = {(c.get("reference_bancaire") or "").strip().upper(): c
                     for c in liste if c.get("reference_bancaire")}

    # Total du jour des seules lignes principal + profit : la signature d'un pret.
    totaux = defaultdict(float)
    for m in echeances:
        if (m.get("regle") or "") in ("pret_principal", "pret_profit"):
            totaux[str(m.get("date"))] = flt(totaux[str(m.get("date"))] + flt(m.get("debit")),
                                             PRECISION)

    out = {}
    for m in echeances:
        contrat = par_reference.get((m.get("reference") or "").strip().upper())
        if not contrat and (m.get("regle") or "") in ("pret_principal", "pret_profit"):
            total = totaux.get(str(m.get("date")), 0.0)
            for c in liste:
                attendu = flt(c.get("total_mensuel"), PRECISION)
                if attendu and abs(total - attendu) <= flt(c.get("tolerance") or 0.01, PRECISION):
                    contrat = c
                    break
        if contrat:
            out[m["cle"]] = decrire(contrat)
    return out


# ------------------------------------------------------------------ rendu texte


def texte(detail: dict) -> str:
    """La forme texte, pour l'export Excel — une piece par ligne, comme dans l'outil."""
    if not detail:
        return ""
    contrat = (detail.get("contrat") or {}).get("libelle")
    lignes = ["%s %s" % (detail["contrat"].get("type") or "Contrat", contrat) if contrat else "",
              detail.get("reference") or ""]
    if detail.get("bordereau"):
        lignes.append("Bordereau %s" % detail["bordereau"])
    if detail.get("note"):
        lignes.append(detail["note"])
    for l in detail.get("lignes") or []:
        lignes.append("  %s%s%s / Total TTC: %s / Alloué: %s"
                      % ("%s · " % l["tiers"] if l.get("tiers") else "",
                         l["libelle"],
                         " (%s)" % l["piece"] if l.get("piece") else "",
                         l["total"], l["alloue"]))
    if abs(flt(detail.get("reste"), PRECISION)) > 0.01:
        lignes.append("  non rattaché : %s" % flt(detail["reste"], PRECISION))
    return "\n".join(x for x in lignes if x.strip())
