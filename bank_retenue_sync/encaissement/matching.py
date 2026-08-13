"""Appariement banque <-> Payment Entry en attente, par categorie d'encaissement.

Produit des "lignes" pretes pour le builder d'Encaissement Paiement, + une liste de
diagnostics (mouvements/effets non apparies) a logguer (jamais fatal).

Quatre flux :
  - cheques  : credit bancaire 'ENC CHEQ <n° bon 8ch>' -> remise (PDF tej-bank-service, extrait
    OpenAI) -> n° de cheques -> PE en attente 'Chèques - A&S'.
  - traites  : credit bancaire 'ENCAISSEMENT EFFET <n° traite>' -> PE en attente
    'Traite Bancaire - A&S' (match direct par n°, pas de PDF).
  - aramex   : advice email (net) -> credit bancaire -> PE en attente 'Livraison Aramex - A&S'.
  - virements: credit bancaire 'VIR ... <nom du payeur>' -> identification du client
    (`party.identify`) -> repartition sur les dettes en attente 'Dettes - A&S' (`allocation`).

Idempotence au niveau mouvement : la cle (n° bon pour cheques, n° traite pour effets, reference
bancaire pour aramex) sert de `bon_remise`/`n_virement` et permet d'ecarter les mouvements deja
traites (cf. builder + pending.already_encaissed_refs).
"""
from __future__ import annotations

import re

import frappe

from bank_retenue_sync.bank import movements as mv
from bank_retenue_sync.encaissement import allocation, party, pending


def _norm_num(s: str) -> str:
    """Numero comparable : ne garde que les chiffres, sans zeros de tete."""
    digits = "".join(c for c in str(s or "") if c.isdigit())
    return digits.lstrip("0") or "0"


def _same_num(a, b) -> bool:
    return _norm_num(a) == _norm_num(b)


# --------------------------------------------------------------------------- cheques
def match_cheques(movements, pending_cheques, remise_loader=None, consumed=None):
    """movements : mouvements bancaires (list). pending_cheques : PE en attente (list de dicts).
    remise_loader(bon) -> dict remise {cheques:[{numero_cheque, montant, banque, emetteur}], date}
      (par defaut : telecharge + extrait via tej-bank-service/OpenAI).
    `consumed` : n° de bons deja encaisses (cf. pending.consumed_bank_keys) — ecartes AVANT tout
    telechargement, car chaque bordereau lu coute un appel OpenAI.
    Retourne (rows, diagnostics)."""
    if remise_loader is None:
        remise_loader = _load_remise
    consumed = consumed if consumed is not None else pending.consumed_bank_keys()["cheque"]
    by_num = {_norm_num(p.get("numero")): p for p in pending_cheques if p.get("numero")}
    rows, diag = [], []
    credits = mv.find_credits_by_keywords(mv.ENC_CHEQUE_KEYWORDS, movements=movements)
    seen_bons = set()
    for m in credits:
        bon = mv.find_bon_number(m["operation"])
        # Ces deux cas passaient en `continue` MUET : un credit d'encaissement disparaissait du
        # rapport sans laisser de trace (cas reel 'Encaiss cheque preavise 8818030', 2500 DT).
        # Un depot non rapproche doit toujours se voir.
        if not bon:
            diag.append({"type": "cheque", "operation": m.get("operation"),
                         "ref": m.get("reference"), "credit": m.get("credit"),
                         "reason": "credit d'encaissement sans n° de bon de remise (8 chiffres) : "
                                   "depot non rapproche — voir le flux cheque preavise"})
            continue
        if bon in seen_bons:
            diag.append({"type": "cheque", "bon": bon, "ref": m.get("reference"),
                         "credit": m.get("credit"),
                         "reason": "2e credit portant le meme n° de bon dans la fenetre : "
                                   "depot non traite"})
            continue
        seen_bons.add(bon)
        if bon in consumed:      # depot deja encaisse : ne pas payer une extraction pour rien
            continue
        try:
            remise = remise_loader(bon)
        except Exception as e:
            diag.append({"type": "cheque", "bon": bon, "reason": f"remise illisible: {e}"})
            continue
        if not remise or not remise.get("cheques"):
            diag.append({"type": "cheque", "bon": bon, "reason": "remise vide/introuvable"})
            continue
        for c in remise["cheques"]:
            pe = by_num.get(_norm_num(c.get("numero_cheque")))
            if not pe:
                diag.append({"type": "cheque", "bon": bon, "cheque": c.get("numero_cheque"),
                             "reason": "aucune PE en attente pour ce n° de cheque"})
                continue
            rows.append({
                "ref_paiement": pe["name"],
                "n_cheque": c.get("numero_cheque") or pe.get("numero"),
                "valeur": pe.get("paid_amount"),
                "emmeteur": c.get("emetteur") or pe.get("party"),
                "bon_remise": bon,
                "date": remise.get("date_remise") or m.get("date"),
                "statut": "Versé",
            })
    return rows, diag


def _load_remise(bon: str) -> dict:
    """Retrouve la remise `bon` dans la liste tej-bank-service, telecharge son PDF, l'extrait."""
    from bank_retenue_sync.ai.remise_extract import extract_remise
    numeros = {r["numero"]: r for r in mv.list_remises_cheques()}
    if bon not in numeros:
        raise ValueError(f"remise {bon} absente de /banque/remises-cheques")
    pdf = mv.fetch_remise_cheque_pdf(bon)
    return extract_remise(pdf, numero_hint=bon)


# ------------------------------------------------------------------- cheques preavises
def match_cheques_preavises(movements, pending_cheques, consumed=None):
    """Cheques encaisses SANS bordereau de remise : credit 'Encaiss cheque preavise <n° 7 chiffres>'.

    La banque encaisse le cheque a l'unite, il n'y a donc aucun bon de remise a telecharger ni a
    faire extraire : le n° de cheque est directement dans le libelle. L'appariement se fait sur les
    memes Payment Entry en attente que le flux remise ('Cheques - A&S').

    Ce flux existe parce que ces credits tombaient jusqu'ici dans le `continue` muet de
    `match_cheques` (cas reel : 'Encaiss cheque preavise 8818030', 2500 DT). La cle bancaire
    memorisee est la REFERENCE du mouvement, faute de n° de bon.

    Retourne (rows, diagnostics)."""
    consumed = consumed if consumed is not None else pending.consumed_bank_keys()["cheque"]
    by_num = {_norm_num(p.get("numero")): p for p in pending_cheques if p.get("numero")}
    rows, diag = [], []
    for m in mv.find_credits_by_keywords(("ENCAISS", "CHEQUE", "PREAVISE"), movements=movements):
        ref = (m.get("reference") or "").strip()
        if ref and ref in consumed:
            continue
        num = mv.find_cheque_number(m["operation"])
        if not num:
            diag.append({"type": "cheque_preavise", "operation": m.get("operation"),
                         "ref": ref, "credit": m.get("credit"),
                         "reason": "n° de cheque (7 chiffres) absent du libelle"})
            continue
        pe = by_num.get(_norm_num(num))
        if not pe:
            diag.append({"type": "cheque_preavise", "cheque": num, "ref": ref,
                         "credit": m.get("credit"),
                         "reason": "aucune PE en attente pour ce n° de cheque"})
            continue
        rows.append({
            "ref_paiement": pe["name"],
            "n_cheque": num,
            "valeur": pe.get("paid_amount"),
            "emmeteur": pe.get("party"),
            "bon_remise": ref or num,
            "date": m.get("date"),
            "statut": "Versé",
        })
    return rows, diag


# --------------------------------------------------------------------------- traites
def match_traites(movements, pending_traites, consumed=None):
    """Traites encaissees : credit 'ENCAISSEMENT EFFET <n°>' -> PE en attente par n°.
    `consumed` : references bancaires deja encaissees (cf. pending.consumed_bank_keys).
    Retourne (rows, diagnostics)."""
    consumed = consumed if consumed is not None else pending.consumed_bank_keys()["traite"]
    by_num = {_norm_num(p.get("numero")): p for p in pending_traites if p.get("numero")}
    rows, diag = [], []
    for m in mv.find_credits_by_keywords(mv.ENC_EFFET_KEYWORDS, movements=movements):
        if m.get("reference") and m["reference"] in consumed:
            continue
        num = mv.find_effet_number(m["operation"])
        if not num:
            diag.append({"type": "traite", "operation": m["operation"], "reason": "n° effet absent"})
            continue
        pe = by_num.get(_norm_num(num))
        if not pe:
            diag.append({"type": "traite", "effet": num,
                         "reason": "aucune PE traite en attente pour ce n°"})
            continue
        rows.append({
            "ref_paiement": pe["name"],
            "n_cheque": num,
            "valeur": pe.get("paid_amount"),
            "emmeteur": pe.get("party"),
            "bon_remise": m.get("reference") or num,
            "date": m.get("date"),
            "statut": "Versé",
        })
    return rows, diag


# --------------------------------------------------------------------------- aramex
def match_aramex(movements, pending_aramex, advices, consumed=None):
    """Flux BANK-FIRST (ordre demande par l'utilisateur) :
      1. detecter un NOUVEAU credit Aramex sur le compte bancaire,
      2. PUIS extraire l'email (Payment Advice) pour le detail des livraisons,
      3. apparier chaque PE Aramex en attente aux n° de l'advice.
    `advices` : PaymentAdvice deja parses depuis les emails. On indexe par net_total et on part
    des credits bancaires : un credit dont le montant == net d'un advice = un virement Aramex.
    Garde-fou : la somme des PE apparies doit egaler le net (sinon lot non apparie).
    `consumed` : references bancaires de virements deja encaisses (cf. pending.consumed_bank_keys).
    Retourne (rows, diagnostics)."""
    consumed = consumed if consumed is not None else pending.consumed_bank_keys()["aramex"]
    rows, diag = [], []
    # advices indexes par net_total (arrondi) -> pour expliquer un credit detecte
    adv_by_net = {}
    for adv in advices or []:
        adv_by_net.setdefault(round(adv.net_total, 3), []).append(adv)

    for m in movements or []:
        if not m.get("credit"):
            continue
        op = (m.get("operation") or "").upper()
        # 1. DETECTION bank-first : un virement Aramex porte le libelle 'ARAMEX'
        #    (ex. reel 'VIR TN AUTRE BQ ARAMEX TUNISIE'). Les virements clients directs (sans
        #    'ARAMEX') sont ignores ici -> flux dedie en v2.
        if "ARAMEX" not in op:
            continue
        if m.get("reference") and m["reference"] in consumed:
            continue
        amt = round(m["credit"], 3)
        cands = adv_by_net.get(amt)
        if not cands:
            # 2. paiement Aramex detecte mais email advice pas (encore) disponible pour le detail
            diag.append({"type": "aramex", "credit": amt, "ref": m.get("reference"),
                         "reason": "virement Aramex detecte mais advice email introuvable (montant)"})
            continue
        adv = cands[0]                       # 2. advice = detail des livraisons
        # `_norm_num` rend "0" pour une valeur vide : sans ce filtre, une PE dont le numero n'a pas
        # pu etre extrait s'apparierait a n'importe quelle ligne d'advice sans reference.
        adv_nums = set()
        for l in adv.lines:
            for v in (getattr(l, "document_number", ""), getattr(l, "reference", "")):
                n = _norm_num(v)
                if n != "0":
                    adv_nums.add(n)
        matched, total = [], 0.0
        for pe in pending_aramex:
            if not pe.get("numero"):
                continue
            if _norm_num(pe.get("numero")) in adv_nums:
                matched.append(pe)
                total += pe.get("paid_amount") or 0.0
        if not matched:
            diag.append({"type": "aramex", "credit": amt, "ref": m.get("reference"),
                         "reason": "advice trouve mais aucune PE Aramex en attente appariee"})
            continue
        if abs(round(total, 3) - amt) >= 0.01:
            # ecart = livraisons de l'advice sans PE en attente (deja encaissees, ou jamais saisies
            # -> `audit.audit_aramex`). On les nomme pour rendre l'ecart diagnosticable.
            manquants = sorted(adv_nums - {_norm_num(pe.get("numero")) for pe in matched})
            diag.append({"type": "aramex", "credit": amt, "total_pe": round(total, 3),
                         "ecart": round(amt - total, 3),
                         "pe_appariees": [pe["name"] for pe in matched],
                         "numeros_advice_sans_pe": manquants[:20],
                         "reason": "somme des PE != credit bancaire -> lot non apparie (garde-fou)"})
            continue
        for pe in matched:
            rows.append({
                "ref_paiement": pe["name"],
                "type": "Virement",
                "n_virement": m.get("reference") or "",
                "n_aramex": pe.get("numero"),
                "valeur": pe.get("paid_amount"),
                "emmeteur": pe.get("party"),
                "date": m.get("date") or adv.payment_date,
            })
    return rows, diag


# --------------------------------------------------------------------------- virements clients
# Le champ `banque` des tables Dettes est un Select ferme, et la banque de l'EMETTEUR n'apparait
# nulle part dans le libelle bancaire. Sa seule contrainte fonctionnelle est d'etre IDENTIQUE
# entre la ligne d'en-tete et ses lignes de detail (c'est la cle de jointure du server script).
# On fige donc notre propre banque : effet de bord utile, la PE finale porte
# `reference_no = '<reference bancaire> - Banque Zitouna'`, donc redetectable a la dedup.
BANQUE = "Banque Zitouna"

_VIR_RE = re.compile(r"\bVIR(?:EMENT|T)?\b")


def is_virement_credit(m: dict) -> bool:
    """Credit correspondant a un virement recu d'un client.

    Detection par MOT ENTIER : `mv.find_credits_by_keywords` colle les caracteres avant de
    comparer, si bien que 'VIR' matcherait n'importe quel libelle contenant ces trois lettres.
    Les virements Aramex sont exclus : ils ont leur propre flux (advice + garde-fou sur le net),
    et les 'VERSEMENT ESPECES ... RECETTE AGENCE' aussi — ce sont des depots de caisse, pas des
    reglements clients."""
    if not m.get("credit"):
        return False
    op = (m.get("operation") or "").upper()
    return bool(_VIR_RE.search(op)) and "ARAMEX" not in op


def match_virements(movements, pending_dettes, consumed=None, booked=None,
                    ai_resolver=None, aliases=None, schedule_check=None):
    """Virements clients recus -> lots prets pour les tables Dettes de l'Encaissement Paiement.

    `pending_dettes` : PE en attente sur 'Dettes - A&S' (cf. pending.get_pending_dettes).
    `consumed`       : references bancaires deja presentes dans un Encaissement Paiement.
    `booked`         : references deja portees par une PE saisie a la main (cf.
                       pending.bank_refs_already_booked) — signalees, jamais rejouees.
    `ai_resolver`    : dernier recours d'identification (cf. ai.party_match.resolver). None = IA
                       coupee, on se contente des alias et du fuzzy.
    `aliases`        : {libelle normalise -> Customer}, appris + forces a la main.
    `schedule_check(sales_order, valeur) -> bool` : verifie que l'echeance existe cote Sales
                       Order (injectable pour les tests).

    Retourne (lots, diagnostics). Un lot = un virement : une ligne d'en-tete par client + une
    ligne de detail par dette soldee."""
    consumed = consumed if consumed is not None else pending.consumed_bank_keys()["virement"]
    booked = booked if booked is not None else set()
    aliases = aliases if aliases is not None else {}
    schedule_check = schedule_check or pending.dette_matches_schedule

    par_client: dict = {}
    for d in pending_dettes or []:
        par_client.setdefault(d["party"], []).append(d)
    candidats = sorted(par_client)

    lots, diag = [], []
    for m in movements or []:
        if not is_virement_credit(m):
            continue
        ref = (m.get("reference") or "").strip()
        if ref and ref in consumed:
            continue
        if ref and ref in booked:
            # Encaissement deja saisi a la main, en general contre la FACTURE et non contre la
            # dette : l'argent est en banque mais la PE de 'Dettes - A&S' reste ouverte pour
            # toujours. On ne rejoue rien (on doublerait l'encaissement), on rend le cas visible.
            diag.append({"type": "virement", "ref": ref, "credit": m.get("credit"),
                         "operation": m.get("operation"),
                         "reason": "virement deja saisi hors flux dettes : la dette du client "
                                   "reste ouverte sur 'Dettes - A&S'"})
            continue

        ident = party.identify(m.get("operation"), candidats, aliases=aliases,
                               ai_resolver=ai_resolver)
        if not ident["customer"]:
            diag.append({"type": "virement", "ref": ref, "credit": m.get("credit"),
                         "operation": m.get("operation"), "score": ident["score"],
                         "reason": "client non identifie : " + ident["raison"]})
            continue

        client = ident["customer"]
        repartition = allocation.allocate(m.get("credit"), par_client.get(client, []))
        if not repartition["lignes"]:
            diag.append({"type": "virement", "ref": ref, "credit": m.get("credit"),
                         "client": client, "methode": ident["method"],
                         "reason": repartition["raison"]})
            continue

        lignes, muettes = [], []
        for ligne in repartition["lignes"]:
            d = ligne["dette"]
            if not schedule_check(d.get("sales_order"), d["paid_amount"]):
                muettes.append(d)
                continue
            lignes.append({
                "ref_paiement": d["name"],
                "bl": d.get("sales_order"),
                "valeur": d["paid_amount"],
                "part": ligne["part"],
                "emmeteur": client,
            })
        if muettes:
            # Le server script apparie l'echeance par egalite stricte : sans correspondance il
            # n'ecrit rien du tout. On abandonne le LOT ENTIER plutot que d'en soumettre une
            # moitie — un virement a moitie impute est plus difficile a rattraper qu'un virement
            # pas impute du tout.
            diag.append({"type": "virement", "ref": ref, "client": client,
                         "credit": m.get("credit"),
                         "dettes": [d["name"] for d in muettes],
                         "reason": "le Sales Order n'a pas d'echeance 'Dette non payée' du meme "
                                   "montant : le server script ignorerait la ligne en silence"})
            continue
        # Les dettes de ce lot sortent du vivier : un second virement du meme client dans le meme
        # run ne doit pas se voir proposer les memes dettes.
        prises = {l["ref_paiement"] for l in lignes}
        par_client[client] = [d for d in par_client[client] if d["name"] not in prises]

        lots.append({
            "client": client,
            "n_virement": ref,
            "banque": BANQUE,
            "date": m.get("date"),
            # Total de l'EN-TETE = somme des parts allouees, pas le credit bancaire : le server
            # script calcule part/total, et une PE dont les references allouent plus que son
            # montant est refusee par ERPNext. L'ecart eventuel est remonte en diagnostic.
            "total": repartition["total_alloue"],
            "credit_bancaire": round(m.get("credit") or 0.0, 3),
            "mode": repartition["mode"],
            "methode_client": ident["method"],
            "lignes": lignes,
        })
        if abs(repartition["ecart"]) >= 0.001:
            diag.append({"type": "virement", "ref": ref, "client": client,
                         "credit": round(m.get("credit") or 0.0, 3),
                         "comptable": repartition["total_alloue"], "ecart": repartition["ecart"],
                         "reason": "dette soldee malgre un ecart (frais de virement) : la PE "
                                   "portera le montant comptable, pas le montant credite"})
    return lots, diag
