"""Remises A CORRIGER : l'alerte de ceux qui peuvent corriger (demande du 27/11/2026).

POURQUOI. Le 27/11/2026 la BIAT credite 701,000 DT pour la remise de cheques n° 9437101. Le bordereau est
photographie, l'IA y lit un cheque n° 0000373 — et aucun paiement par cheque ne le porte dans ERPNext : le
client avait regle un colis Aramex, son paiement etait reste « en attente chez Aramex », et le cheque est parti
a la banque sans passer par la caisse. La remise ne peut pas etre encaissee, et PERSONNE N'EST PREVENU : la
ligne attend dans l'identification bancaire, « a verifier ». Sa demande : « creer une alerte lorsque les cas de
cheque ou traite comme ce cas arrivent, pour Nejib, pour qu'il puisse corriger », puis « meme en carte de
credit, pour Nejib et moi-meme, pour que je sois au courant ».

CE QUI EST A CORRIGER — rien n'est invente, chaque ligne reprend ce qu'un controle existant a deja dit :
  - un BORDEREAU de cheques ou de traites BIAT collecte dont une piece est a signaler (`a_signaler` de
    `api/bordereaux.bilan` : introuvable, montant different…), ou que l'IA n'a pas pu lire. Pour des cheques,
    la ligne disparait des que la remise est encaissee (ligne du releve « Identifie ») ou ecartee a la main ;
  - une REMISE CARTE (terminal de paiement) « A signaler » (`bank/monetique`) et pas encore comptabilisee :
    transaction sans paiement dans ERPNext, ou paiement d'un autre montant dont l'ecart n'est pas accepte.

  - un ECART BLOQUANT d'encaissement (`encaissement/ecarts`, « À traiter ») — c'est la forme que prend le meme cas
    sur le compte ZITOUNA (« meme sur le compte Zitouna aussi », 27/11/2026) : la banque publie le detail de ses
    remises, et un cheque ou une traite sans paiement dans ERPNext (« Sans pièce »), ou d'un autre montant
    (« Delta paiement »), bloque le brouillon d'encaissement jusqu'a ce qu'une personne tranche.

La ligne disparait d'elle-meme quand le controle ne signale plus rien. Une personne peut aussi dire « ne plus
signaler », avec son explication (meme registre que les bordereaux manquants, `bordereaux_manquants.ecarter`).

UNE PISTE quand elle est sure : un cheque introuvable dont le montant est celui d'UN SEUL colis Aramex encore
en attente de reversement est tres probablement le reglement de ce colis (le cas du 27/11/2026) — la ligne le
dit, avec le chemin : Caisse › Encaissement Aramex.

Ceux qui voient l'alerte ont le role `ROLE` (patch `alerte_remises_role` : Nejib et Wassim au depart).
"""
from __future__ import annotations

from bank_retenue_sync.bank.bordereaux_manquants import BANQUE, _chiffres, _dt, _jour

ROLE = "Alerte remises à corriger"
GENRE_BORDEREAU, GENRE_CARTE, GENRE_ECART = "bordereau_a_corriger", "carte_a_corriger", "ecart_a_traiter"
GENRES = (GENRE_BORDEREAU, GENRE_CARTE, GENRE_ECART)
PAGE_IDENTIFICATION = "identification-bancaire"
_PIECE = {"cheque": "Chèque", "traite": "Traite", "aramex": "Colis Aramex"}
STATUT_ENCAISSE = "Identifie"
CONTROLE_A_SIGNALER = "À signaler"


def cle(genre: str, name: str, banque: str = BANQUE) -> str:
    """« BIAT|bordereau_a_corriger|BR-BIAT-9437101 » — stable tant que la fiche existe."""
    return f"{banque}|{genre}|{name}"


def genre_de(cle_ligne: str) -> str:
    """Le genre d'une cle de CE module, « » pour toute autre cle (bordereau manquant, texte quelconque)."""
    morceaux = str(cle_ligne or "").split("|")
    return morceaux[1] if len(morceaux) == 3 and morceaux[1] in GENRES else ""


def ligne_ecart(e: dict) -> dict:
    """La ligne d'alerte d'un ecart bloquant d'encaissement. Pur.
    `e` : {name, flux, bon, suivi, reference_bancaire, type_ecart, client, ref_paiement, montant_advice,
    montant_piece, encaissement, banque}."""
    flux, banque = e.get("flux") or "", e.get("banque") or "Zitouna"
    piece = _PIECE.get(flux, "Pièce") + (f" n° {e['suivi']}" if e.get("suivi") else "")
    client = f" de {e['client']}" if e.get("client") else ""
    if flux == "cheque":
        titre = f"Remise de chèques {banque} n° {e.get('bon') or '?'}"
    elif flux == "traite":
        titre = f"Remise de traites {banque}" + (f" n° {e['bon']}" if _chiffres(e.get("bon")) == str(e.get("bon") or "-") else "")
    elif flux == "aramex":
        titre = f"Virement Aramex ({e.get('reference_bancaire') or banque})"
    else:
        titre = f"Encaissement {banque} ({e.get('reference_bancaire') or flux})"
    if e.get("type_ecart") == "Sans pièce":
        point = f"{piece}{client} ({_dt(abs(float(e.get('montant_advice') or 0)))}) : aucun paiement dans ERPNext."
    elif e.get("type_ecart") == "Delta paiement":
        point = (f"{piece}{client} : {_dt(e.get('montant_advice'))} versés, {_dt(e.get('montant_piece'))} dans ERPNext"
                 + (f" (paiement {e['ref_paiement']})." if e.get("ref_paiement") else "."))
    else:
        point = f"{piece}{client} : {e.get('type_ecart') or 'écart'} à trancher."
    if e.get("encaissement"):
        detail = (f"L’argent est à la banque : l’encaissement {e['encaissement']} reste en brouillon tant que cet écart "
                  "n’est pas tranché.")
        ouvrir = {"doctype": "Encaissement Paiement", "name": e["encaissement"], "route": ["Form", "Encaissement Paiement", e["encaissement"]]}
    else:
        detail = "L’argent est à la banque, mais aucun encaissement ne peut être préparé tant que cet écart n’est pas tranché."
        ouvrir = {"doctype": None, "name": e.get("name"), "route": [PAGE_IDENTIFICATION]}
    return {"genre": GENRE_ECART, **ouvrir, "ecart": e.get("name"), "banque": banque,
            "date": str(e.get("date") or "")[:10], "titre": titre, "detail": detail, "points": [point],
            "pieces": ([{"numero": e.get("suivi"), "montant": abs(float(e.get("montant_advice") or 0)), "etat": "Introuvable"}]
                       if flux in ("cheque", "traite") and e.get("type_ecart") == "Sans pièce" else []),
            "verifier": False}


def _phrases(texte) -> list:
    return [ligne.strip() for ligne in str(texte or "").split("\n") if ligne.strip()]


def piste_aramex(pieces: list, attentes: list) -> str | None:
    """Un cheque (ou une traite) introuvable dont le montant est celui d'UN SEUL colis Aramex en attente. Pur.
    `pieces` : [{numero, montant, etat}] du bordereau ; `attentes` : [{client, montant, colis, commande}].
    Deux colis du meme montant : pas de piste (on ne devine pas)."""
    for p in pieces or ():
        if p.get("etat") != "Introuvable" or p.get("montant") in (None, ""):
            continue
        memes = [a for a in attentes or () if abs(float(a.get("montant") or 0) - float(p["montant"])) < 0.0005]
        if len(memes) == 1:
            a = memes[0]
            colis = f" (colis {a['colis']})" if a.get("colis") else ""
            commande = f", commande {a['commande']}" if a.get("commande") else ""
            return (f"Piste : {_dt(p['montant'])}, c’est le montant du colis Aramex de {a.get('client') or '?'}{colis}"
                    f"{commande}, encore « en attente chez Aramex ». Si c’est son chèque : Caisse › Encaissement "
                    f"Aramex, cochez ce colis et saisissez la pièce n° {p.get('numero') or '?'}.")
    return None


def a_corriger(bordereaux: list, remises: dict, cartes: list, ecartes: dict, attentes_aramex: list = (),
               ecarts: list = ()) -> list:
    """Les remises qu'une personne doit corriger. Pur.

    `bordereaux` : [{name, nature, numero, date_remise, lecture_statut, a_signaler, pieces: [{numero, montant,
      etat}]}] — bordereaux BIAT collectes ;
    `remises` : {n° de bordereau: {statut, date, montant, ignore}} — remises de cheques deja creditees par la BIAT ;
    `cartes` : [{name, remise, date_traitement, etat, date_credit, montant_credite, brut, controle, a_signaler,
      ecriture}] — remises du terminal de paiement ;
    `ecartes` : {cle: …} — lignes qu'une personne a demande de ne plus signaler ;
    `ecarts` : les ecarts bloquants d'encaissement encore « À traiter » (cf. `ligne_ecart`) — Zitouna surtout.
    -> [{cle, genre, doctype, name, route, titre, detail, points: [phrases], piste, verifier}], les plus anciennes
    d'abord. `route` : la fiche (ou la page) ou la correction se fait."""
    out = []
    for b in bordereaux or ():
        nature = b.get("nature")
        if nature not in ("Chèques", "Traites"):
            continue                                            # un versement d'especes n'a pas de pieces
        if b.get("lecture_statut") == "Échec":
            points = ["L’IA n’a pas pu lire le bordereau : saisissez ses pièces sur sa fiche."]
        elif b.get("lecture_statut") == "Lue" and _phrases(b.get("a_signaler")):
            points = _phrases(b.get("a_signaler"))
        else:
            continue                                            # tout est bon, ou lecture encore en cours
        numero = _chiffres(b.get("numero"))
        quoi = "chèques" if nature == "Chèques" else "traites"
        credit = (remises or {}).get(numero) if nature == "Chèques" else None
        if credit and (credit.get("statut") == STATUT_ENCAISSE or credit.get("ignore")):
            continue                                            # remise encaissee (ou ecartee) : plus rien a corriger
        if credit:
            detail = (f"La BIAT a crédité {_dt(credit.get('montant'))} le {_jour(credit.get('date'))} : la remise ne "
                      "peut pas être encaissée tant que ce n’est pas corrigé.")
        elif nature == "Chèques":
            detail = f"Remise du {_jour(b.get('date_remise'))}, pas encore créditée par la BIAT : à corriger avant."
        else:
            detail = f"Remise du {_jour(b.get('date_remise'))} : une traite du bordereau n’est pas retrouvée dans ERPNext."
        out.append({"genre": GENRE_BORDEREAU, "doctype": "BRS Bordereau Remise", "name": b.get("name"),
                    "route": ["Form", "BRS Bordereau Remise", b.get("name")],
                    "date": str((credit or {}).get("date") or b.get("date_remise") or "")[:10],
                    "titre": f"Remise de {quoi} BIAT n° {b.get('numero')}", "detail": detail, "points": points,
                    "piste": piste_aramex(b.get("pieces"), attentes_aramex), "verifier": True})

    for c in cartes or ():
        if c.get("controle") != CONTROLE_A_SIGNALER or c.get("ecriture"):
            continue
        if c.get("date_credit"):
            detail = (f"La BIAT a crédité {_dt(c.get('montant_credite'))} le {_jour(c.get('date_credit'))} : la remise "
                      "n’est pas comptabilisée tant que ce n’est pas corrigé.")
        else:
            detail = f"Remise de {_dt(c.get('brut'))} traitée par la banque le {_jour(c.get('date_traitement'))}, pas encore créditée."
        out.append({"genre": GENRE_CARTE, "doctype": "BRS Remise TPE", "name": c.get("name"),
                    "route": ["Form", "BRS Remise TPE", c.get("name")],
                    "date": str(c.get("date_credit") or c.get("date_traitement") or "")[:10],
                    "titre": f"Remise carte BIAT n° {c.get('remise')}", "detail": detail,
                    "points": _phrases(c.get("a_signaler")) or ["Une transaction est à signaler : voir la fiche."],
                    "piste": None, "verifier": False})

    for e in ecarts or ():
        ligne = ligne_ecart(e)
        ligne["piste"] = piste_aramex(ligne.pop("pieces"), attentes_aramex)
        out.append(ligne)

    for ligne in out:
        ligne["cle"] = cle(ligne["genre"], ligne.pop("ecart", None) or ligne["name"], ligne.pop("banque", BANQUE))
    return sorted((ligne for ligne in out if ligne["cle"] not in (ecartes or {})),
                  key=lambda x: (x["date"] or "9999", x["name"] or ""))


# ---------------------------------------------------------------------------
# Avec le site
# ---------------------------------------------------------------------------

def liste() -> list:
    """Les remises a corriger : bordereaux BIAT collectes, remises carte, ecarts bloquants d'encaissement (Zitouna)."""
    import frappe

    from bank_retenue_sync.bank import biat, bordereaux_manquants, registry
    from bank_retenue_sync.encaissement import ecarts as ecarts_enc
    from bank_retenue_sync.encaissement.ecarts import COMPTE_ARAMEX

    bordereaux = frappe.get_all(
        "BRS Bordereau Remise", limit_page_length=0,
        filters={"banque": BANQUE, "nature": ["in", ["Chèques", "Traites"]]},
        or_filters=[["lecture_statut", "=", "Échec"], ["a_signaler", "is", "set"]],
        fields=["name", "nature", "numero", "date_remise", "lecture_statut", "a_signaler"])
    for b in bordereaux:
        b["pieces"] = frappe.get_all("BRS Bordereau Remise Piece", filters={"parent": b.name}, limit_page_length=0,
                                     fields=["numero", "montant", "etat"], order_by="idx asc")
    remises = {}
    if bordereaux:
        for ligne in frappe.get_all(registry.DOCTYPE, filters={"banque": BANQUE, "sens": "Credit"}, limit_page_length=0,
                                    fields=["date", "operation", "credit", "statut", "ignore_manuel"]):
            vu = biat.detecter(ligne.operation, "Credit")
            if vu["regle"] == "enc_cheque" and vu["numero"]:
                remises[_chiffres(vu["numero"])] = {"statut": ligne.statut, "date": ligne.date, "montant": ligne.credit,
                                                    "ignore": bool(ligne.ignore_manuel)}
    cartes = []
    if frappe.db.exists("DocType", "BRS Remise TPE"):
        cartes = frappe.get_all(
            "BRS Remise TPE", filters={"controle": CONTROLE_A_SIGNALER}, limit_page_length=0,
            fields=["name", "remise", "date_traitement", "etat", "date_credit", "montant_credite", "brut", "controle",
                    "a_signaler", "ecriture"])
    ecarts = frappe.get_all(
        ecarts_enc.DOCTYPE, filters={"statut": "À traiter", "bloquant": 1}, limit_page_length=0, order_by="creation asc",
        fields=["name", "creation as date", "flux", "bon", "suivi", "reference_bancaire", "type_ecart", "client",
                "ref_paiement", "montant_advice", "montant_piece", "encaissement"])
    champ_banque = frappe.db.has_column(ecarts_enc.ENCAISSEMENT_DOCTYPE, "custom_banque_depot")
    for e in ecarts:
        e["banque"] = (frappe.db.get_value(ecarts_enc.ENCAISSEMENT_DOCTYPE, e.encaissement, "custom_banque_depot")
                       if champ_banque and e.encaissement else None) or "Zitouna"
    attentes = []
    if any(p.get("etat") == "Introuvable" for b in bordereaux for p in b["pieces"]) \
            or any(e.flux in ("cheque", "traite") and e.type_ecart == "Sans pièce" for e in ecarts):
        # Les colis Aramex dont le reglement est encore « en attente » : paiement soumis sur le compte d'attente.
        for pe in frappe.db.sql(
                """select pe.name, pe.party as client, pe.paid_amount as montant, pe.reference_no,
                          (select r.reference_name from `tabPayment Entry Reference` r
                            where r.parent = pe.name and r.reference_doctype = 'Sales Order' limit 1) as commande
                   from `tabPayment Entry` pe where pe.docstatus = 1 and pe.paid_to = %s""", (COMPTE_ARAMEX,), as_dict=True):
            attentes.append({"client": pe.client, "montant": pe.montant, "commande": pe.commande,
                             "colis": _chiffres(pe.reference_no) if len(_chiffres(pe.reference_no)) >= 8 else ""})
    return a_corriger(bordereaux, remises, cartes, bordereaux_manquants.ecartes(), attentes, ecarts)
