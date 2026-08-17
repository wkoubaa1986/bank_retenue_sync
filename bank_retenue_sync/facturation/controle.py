"""Confronter une ecriture a son justificatif, en lisant le PDF avec le modele.

Ce que le script externe faisait en bloc (`apply_llm_to_df_docs` sur tout le mois), on le fait
LIGNE PAR LIGNE, a la demande. Deux raisons :

  · chaque lecture est un appel payant — relire quarante PDF pour en verifier un seul est un
    cout qu'on ne justifie pas ;
  · le resultat est CONSERVE. Un justificatif ne change pas : une fois lu, il n'a plus a l'etre.
    Le cache porte sur le fichier, pas sur la ligne, et une nouvelle piece invalide l'ancienne
    lecture d'elle-meme puisque son nom differe.

⚠️ ON NE CORRIGE RIEN. Le controle rend un ECART, jamais une modification : c'est un humain qui
tranche entre « le PDF a raison » et « la saisie a raison ». Ecrire ici, ce serait faire confiance
a une lecture automatique pour modifier une comptabilite.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate

from bank_retenue_sync.facturation import charges as M_charges
from bank_retenue_sync.facturation import periode

PRECISION = 3
DUREE_CACHE = 30 * 24 * 3600

# Au-dela de ce seuil, l'ecart entre le PDF et l'ecriture est signale. En deca, il releve de
# l'arrondi ou du timbre, comme partout ailleurs dans cette page.
TOLERANCE = 1.01


def _cle(fichier: str) -> str:
    return "brs:controle:%s" % fichier


def _lire(fichier: str):
    """⚠️ `expires=True` N'EST PAS DECORATIF. `set_value(..., expires_in_sec=…)` n'ecrit PAS
    `frappe.local.cache`, alors que `get_value` y depose le `None` d'un miss. Une lecture avant
    l'ecriture empoisonne donc le cache local pour tout le reste de la requete : le controle
    qu'on venait de payer paraissait absent, et la reference d'export n'en tenait pas compte.
    `expires=True` court-circuite le cache local — c'est ce que la docstring de Frappe indique
    pour les cles a duree de vie."""
    return frappe.cache().get_value(_cle(fichier), expires=True)


def lire_cache(noms_fichiers: list) -> dict:
    """Les controles deja passes, par nom de fichier. -> {file_name: resultat}"""
    out = {}
    for nom in noms_fichiers or []:
        valeur = _lire(nom)
        if valeur:
            out[nom] = valeur
    return out


def _contenu(file_url: str) -> tuple:
    """-> (nom du File, octets). Leve si la piece est introuvable."""
    doc = frappe.get_all("File", filters={"file_url": file_url},
                         fields=["name", "file_name"], limit_page_length=1)
    if not doc:
        frappe.throw(_("Pièce introuvable : {0}").format(file_url))
    fichier = frappe.get_doc("File", doc[0].name)
    return fichier.file_name, fichier.get_content()


def _attendu(ligne: dict) -> dict:
    """Ce que l'ecriture dit, dans la forme que le modele rend."""
    return {
        "reference": ligne.get("ref") or "",
        "date": ligne.get("date") or "",
        "ht": flt(ligne.get("ht"), PRECISION),
        "tva": flt(ligne.get("tva"), PRECISION),
        "ttc": flt(ligne.get("ttc"), PRECISION),
        "tiers": ligne.get("tiers") or "",
    }


def _comparer(attendu: dict, extrait: dict) -> dict:
    """Les ecarts, champ par champ, plus un verdict d'ensemble."""
    ecarts = {}
    for champ, cle_extrait in (("ht", "total_ht"), ("tva", "total_tva"), ("ttc", "total_ttc")):
        lu = extrait.get(cle_extrait)
        if lu is None:
            continue
        ecarts[champ] = flt(flt(lu) - attendu[champ], PRECISION)

    # ⚠️ LE TIMBRE FISCAL FAUSSE LE SEUL ECART DU HT. La facture le presente a part, l'ecriture
    # le porte DANS le compte de charge : sur une petite depense, le HT diverge exactement de
    # 1 DT et rien d'autre. Le rattraper ici evite de signaler un ecart sur toutes les factures
    # timbrees — et, dans l'autre sens, evite qu'un vrai ecart de 1 DT passe pour un timbre,
    # puisqu'on ne corrige que si l'ajout du timbre RAPPROCHE les deux montants.
    timbre = flt(extrait.get("stamp_duty"), PRECISION)
    timbre_dans_ht = False
    if timbre and "ht" in ecarts:
        avec = flt(ecarts["ht"] + timbre, PRECISION)
        if abs(avec) < abs(ecarts["ht"]):
            ecarts["ht"] = avec
            timbre_dans_ht = True

    # La reference : on ne compare pas des chaines a l'identique — un n° de facture apparait
    # rarement sous la meme forme des deux cotes. On cherche l'un DANS l'autre.
    ref_erp = (attendu["reference"] or "").strip().lower()
    ref_pdf = (extrait.get("invoice_no") or "").strip().lower()
    reference_ok = bool(ref_erp and ref_pdf and (ref_pdf in ref_erp or ref_erp in ref_pdf))

    date_ok = None
    if extrait.get("invoice_date") and attendu["date"]:
        try:
            date_ok = getdate(extrait["invoice_date"]) == getdate(attendu["date"])
        except Exception:
            date_ok = None

    # Une fois le timbre neutralise, la tolerance n'a plus a l'absorber : elle redescend a
    # l'arrondi. Un ecart de 1 DT qui n'est PAS un timbre doit se voir.
    seuil = 0.01 if timbre_dans_ht or not timbre else TOLERANCE
    hors_tolerance = [c for c, v in ecarts.items() if abs(v) > seuil]

    # ⚠️ TOUS LES JUSTIFICATIFS NE SONT PAS DES FACTURES, ET UN DOCUMENT QU'ON NE SAIT PAS LIRE
    # N'EST PAS UN ECART. Un certificat de retenue, un recu de salaire ou une recharge n'ont pas
    # de triplet HT/TVA/TTC : le modele rend des champs vides, `_balanced` tombe a faux, et le
    # controle criait « discordant » sur huit pieces parfaitement regulieres. On distingue donc
    # trois issues — concordant, ecart, illisible — au lieu d'un booleen qui melange l'anomalie
    # comptable et l'echec de lecture.
    lisible = all(extrait.get(c) is not None
                  for c in ("total_ht", "total_tva", "total_ttc"))
    if not lisible:
        verdict = "illisible"
    elif hors_tolerance or not extrait.get("_balanced"):
        verdict = "ecart"
    else:
        verdict = "concordant"

    return {
        "ecarts": ecarts,
        "timbre_dans_ht": timbre_dans_ht,
        "hors_tolerance": hors_tolerance,
        "reference_ok": reference_ok,
        "reference_pdf": extrait.get("invoice_no") or "",
        "date_ok": date_ok,
        "date_pdf": extrait.get("invoice_date") or "",
        "lisible": lisible,
        # `equilibre` n'a de sens que sur un document qui porte les trois montants.
        "equilibre": bool(extrait.get("_balanced")) if lisible else None,
        "verdict": verdict,
        # La reference ne fait pas verdict : elle est saisie a la main des deux cotes et
        # diverge pour de bonnes raisons.
        "concordant": verdict == "concordant",
    }


def verifier(mois: str, document_type: str, document_name: str, file_url: str = None,
             force: bool = False) -> dict:
    """Lit LES justificatifs d'une ligne et confronte a l'ecriture celui qui est une facture.

    ⚠️ UNE ECRITURE PORTE SOUVENT DEUX PIECES, ET UNE SEULE EST COMPARABLE. La facture voisine
    le scan du cheque de paiement ou le bon de livraison : sur ces derniers il n'y a ni HT ni
    TVA, et les confronter aux montants de l'ecriture ne produit que du bruit. On lit donc
    TOUTES les pieces — chacune a son interet, et chacune est mise en cache — mais le verdict
    est rendu par celle qui porte des montants.
    """
    mois = periode.normaliser(mois)
    ligne = _trouver_ligne(mois, document_type, document_name)
    if not ligne:
        frappe.throw(_("Ligne introuvable dans les charges de {0}.").format(mois))
    if not ligne["justificatifs"]:
        frappe.throw(_("Aucun justificatif attaché à cette écriture."))

    if not file_url and len(ligne["justificatifs"]) > 1:
        lus = []
        for justificatif in ligne["justificatifs"]:
            try:
                lus.append(_lire_une(mois, ligne, justificatif["file_url"], force))
            except Exception as e:
                lus.append({"fichier": justificatif["file_name"], "verdict": "illisible",
                            "erreur": str(e)[:160], "extrait": {}, "ecarts": {}})
        # La facture est la piece dont les trois montants ont ete lus. A defaut, la premiere :
        # mieux vaut un verdict « illisible » explicite qu'aucun verdict du tout.
        retenue = next((r for r in lus if r.get("verdict") == "concordant"), None) \
            or next((r for r in lus if r.get("verdict") == "ecart"), None) or lus[0]
        return dict(retenue, pieces_lues=[{"fichier": r.get("fichier"),
                                           "verdict": r.get("verdict")} for r in lus])

    return _lire_une(mois, ligne, file_url or ligne["justificatifs"][0]["file_url"], force)


def _lire_une(mois: str, ligne: dict, url: str, force: bool = False) -> dict:
    """Lit UNE piece et la confronte a l'ecriture. Le resultat est mis en cache par fichier."""
    nom_fichier, octets = _contenu(url)

    if not force:
        deja = _lire(nom_fichier)
        if deja:
            return dict(deja, cache=True)

    from bank_retenue_sync.ai import invoice_extract

    indice = "Tiers attendu : %s. Montant TTC attendu : %s." % (
        ligne.get("tiers") or "?", flt(ligne.get("ttc"), PRECISION))
    extrait = invoice_extract.extract_invoice_any(octets, extra_hint=indice)

    attendu = _attendu(ligne)
    resultat = {
        "fichier": nom_fichier,
        "file_url": url,
        "document_type": ligne["document_type"],
        "document_name": ligne["document_name"],
        "attendu": attendu,
        "extrait": {
            "ht": flt(extrait.get("total_ht"), PRECISION),
            "tva": flt(extrait.get("total_tva"), PRECISION),
            "ttc": flt(extrait.get("total_ttc"), PRECISION),
            "timbre": flt(extrait.get("stamp_duty"), PRECISION),
            "reference": extrait.get("invoice_no") or "",
            "date": extrait.get("invoice_date") or "",
            "tiers": extrait.get("supplier_name") or "",
            "modele": extrait.get("_model") or "",
        },
        **_comparer(attendu, extrait),
    }
    frappe.cache().set_value(_cle(nom_fichier), resultat, expires_in_sec=DUREE_CACHE)
    return dict(resultat, cache=False)


def _trouver_ligne(mois: str, document_type: str, document_name: str):
    for bloc in M_charges.liste(mois)["blocs"]:
        for ligne in bloc["lignes"]:
            if ligne["document_type"] == document_type and ligne["document_name"] == document_name:
                return ligne
    return None


def verifier_le_mois(mois: str) -> dict:
    """Passe en revue toutes les charges exigibles du mois qui portent une piece non encore lue.

    Appele dans un job : chaque PDF est un appel au modele, et un mois en compte plusieurs
    dizaines. Les pieces deja lues sont sautees — c'est ce qui rend le geste repetable.
    """
    mois = periode.normaliser(mois)
    donnees = M_charges.liste(mois)
    a_faire = [l for b in donnees["blocs"] for l in b["lignes"]
               if l["justificatif_requis"] and l["justificatifs"]]

    fait, saute, echecs = 0, 0, []
    for i, ligne in enumerate(a_faire, 1):
        try:
            # Sans `file_url`, `verifier` lit TOUTES les pieces de la ligne et retient la
            # facture. Lui passer la premiere en dur laissait la seconde jamais lue — et sept
            # ecritures verdict « illisible » alors que leur facture etait la, en piece 2.
            resultat = verifier(mois, ligne["document_type"], ligne["document_name"])
            if resultat.get("cache"):
                saute += 1
            else:
                fait += 1
        except Exception as e:
            echecs.append("%s : %s" % (ligne["document_name"], str(e)[:140]))
        frappe.publish_progress(100.0 * i / max(1, len(a_faire)),
                                title=_("Contrôle des justificatifs"),
                                description="%d/%d" % (i, len(a_faire)))
    return {"mois": mois, "total": len(a_faire), "lus": fait, "deja_lus": saute,
            "echecs": echecs}


def _verdict_de(resultat: dict) -> str:
    """Le verdict d'un controle, y compris pour ceux lus AVANT que la notion existe.

    ⚠️ ON NE RELIT PAS LES PDF POUR REQUALIFIER. Chaque lecture est payante et 37 pieces etaient
    deja en cache quand la troisieme issue est apparue. Le verdict se rededuit de ce qui est
    stocke : un controle dont les ecarts ne couvrent pas les trois montants portait forcement
    une extraction incomplete — c'est la definition d'illisible.
    """
    if resultat.get("verdict"):
        return resultat["verdict"]
    if set((resultat.get("ecarts") or {}).keys()) != {"ht", "tva", "ttc"}:
        return "illisible"
    return "concordant" if resultat.get("concordant") else "ecart"


def attacher_aux_lignes(donnees: dict) -> dict:
    """Pose sur chaque ligne le controle deja en cache, s'il y en a un. Ne lit aucun PDF."""
    fichiers = [j["file_name"] for b in donnees.get("blocs") or []
                for l in b["lignes"] for j in l["justificatifs"]]
    cache = lire_cache(fichiers)
    if not cache:
        return donnees
    ordre = {"concordant": 0, "ecart": 1, "illisible": 2}
    for bloc in donnees["blocs"]:
        compte = {"controles": 0, "concordants": 0, "discordants": 0, "illisibles": 0}
        for ligne in bloc["lignes"]:
            lus = [cache[j["file_name"]] for j in ligne["justificatifs"]
                   if j["file_name"] in cache]
            if not lus:
                continue
            for resultat in lus:
                resultat["verdict"] = _verdict_de(resultat)
                resultat["concordant"] = resultat["verdict"] == "concordant"
            # ⚠️ ENTRE DEUX PIECES LUES, C'EST LA FACTURE QUI FAIT FOI. Une ecriture porte
            # souvent la facture ET le scan du cheque de paiement. Retenir la premiere de la
            # liste affichait « illisible » alors que la facture, juste a cote, concordait.
            # On garde donc la piece la plus concluante, et on signale les autres.
            retenu = sorted(lus, key=lambda r: ordre[r["verdict"]])[0]
            ligne["controle"] = retenu
            if len(lus) > 1:
                ligne["controle"] = dict(retenu, pieces_lues=[
                    {"fichier": r.get("fichier"), "verdict": r["verdict"]} for r in lus])
            # La reference d'export d'une depense contient le n° de facture LU dans le
            # justificatif : elle ne peut se calculer qu'ici, une fois le controle pose.
            ligne["reference_export"] = M_charges.reference_export(ligne)
            compte["controles"] += 1
            compte[{"concordant": "concordants", "ecart": "discordants",
                    "illisible": "illisibles"}[retenu["verdict"]]] += 1
        bloc["totaux"].update(compte)
    for cle in ("controles", "concordants", "discordants", "illisibles"):
        donnees["totaux"][cle] = sum(b["totaux"].get(cle, 0) for b in donnees["blocs"])
    return donnees
