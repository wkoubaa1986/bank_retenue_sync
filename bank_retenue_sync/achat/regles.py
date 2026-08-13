"""Les regles de la facture d'achat locale. Fonctions PURES : aucune base, aucun reseau.

CE QUE CE FICHIER DECIDE
------------------------
Une facture d'achat aupres d'un fournisseur TUNISIEN engage trois choses que la saisie seule ne
garantit pas : la preuve (le scan de la facture reelle), le stock (la marchandise entre quelque
part), et l'impot (1 % retenu a la source au-dela de 1 000 DT TTC). Aucune des trois n'est
rattrapable apres validation sans annuler l'ecriture.

⚠️ MAIS DEUX DES TROIS SE CORRIGENT, ET NE BLOQUENT DONC RIEN. Le stock et la retenue se posent
seuls a l'enregistrement (cf. `achat/facture.py`) : refuser une facture pour une case a cocher
qu'on sait cocher soi-meme fait perdre du temps sans rien proteger. Ce fichier ne decide donc que
de ce qui reste vraiment indecidable par une machine — cf. `bloquants`.

⚠️ LE FOURNISSEUR ETRANGER EST HORS SUJET. Une facture chinoise n'a ni retenue a la source ni
timbre : lui appliquer ces regles bloquerait des saisies parfaitement legitimes. Le pays du
fournisseur, et lui seul, ouvre ou ferme tout ce fichier.
"""
from __future__ import annotations

PAYS_LOCAL = "Tunisia"

# Seuil legal : 1 % de retenue a la source sur les acquisitions de biens et services a partir de
# 1 000 DT TTC. Le seuil et le taux restent parametrables — la loi de finances les revise.
SEUIL_RETENUE = 1000.0
TAUX_RETENUE = 1.0

# ⚠️ SEUL UN ECART IMPORTANT BLOQUE, ET C'EST LA CALIBRATION QUI FAIT TOUT. Un scan ne se lit pas
# au millime : sur ELECTROQUIP, le modele rend 922,688 la ou la facture porte 923,700 — un chiffre
# mal reconnu, pas une erreur de saisie. Refuser la validation pour cela ferait crier au loup a
# chaque facture.
#
# ⚠️ MAIS TROIS GRANDEURS NE SE TOLERENT PAS PAREIL, ET LES CONFONDRE A LAISSE PASSER UNE VRAIE
# ERREUR. Un seuil unique, calcule sur le TTC, etait applique tel quel aux trois : sur
# ACC-PINV-2026-00092 il valait 10,937 DT, soit 6,2 % d'une TVA de 175 DT. Une TVA ramenee a la main
# de 175,311 a 170,000 — 5,311 DT deduits en trop — est passee sans un mot, et la retenue a la
# source, calculee sur un TTC devenu faux, avec elle. Chaque grandeur a donc desormais son seuil.
ECART_TTC = 1.5      # TTC : ecart ABSOLU admis. Le bruit de lecture constate vaut 1,012 DT
                     # (1 099,011 saisi contre 1 097,999 lu) : 1,5 le couvre, 1,0 ne le couvrait pas.
ECART_TVA_TAUX = 0.1  # TVA : pourcentage de la TVA LUE — 0,175 DT sur ELECTROQUIP. La TVA se lit
                     # bien mieux que le HT : les trois extractions en base la rendent au millime
                     # exact, la ou le HT derape de 1,012 DT sur le meme scan.
# HT : le PLUS GRAND de 1 DT et d'un pourcentage du TTC. Volontairement large — c'est la grandeur
# ou vit le bruit de lecture, et la seule des trois qui ne serait pas resserree sans faire refuser
# des factures saines.
ECART_MINIMAL = 1.0
ECART_TAUX = 1.0

# Ecart admis sur la retenue elle-meme. Celle-la est CALCULEE, pas lue : le millime suffit.
TOLERANCE_RETENUE = 0.01

# Mots qui identifient une ligne de taxe dans le plan comptable. Le compte porte le sens, pas le
# libelle saisi a la main.
MOT_RETENUE = "etenue"
MOT_TIMBRE = "imbre"
MOT_TVA = "TVA"


def pdf_present(pieces_jointes) -> bool:
    """Y a-t-il un PDF parmi les pieces jointes ? Fonction pure.

    ⚠️ « UNE PIECE JOINTE » NE SUFFIT PAS : c'est un PDF qu'il faut. Un JPG ou un DOCX se joint
    aussi bien, mais il ne se lit pas de la meme facon, ne s'imprime pas pareil au controle, et
    l'extraction ne sait pas l'ouvrir. Sur les 222 pieces deja attachees a des factures d'achat,
    219 sont des PDF, 2 des JPG et 1 un DOCX : la regle entérine la pratique, elle ne l'invente pas.

    La qualite du scan ne se juge pas ici — elle se prouve plus loin, quand les totaux lus tombent
    sur les totaux saisis. Un document illisible echoue de lui-meme a ce test.
    """
    return any((f.get("file_name") or "").lower().strip().endswith(".pdf")
               for f in (pieces_jointes or []))


def est_local(pays) -> bool:
    return (pays or "").strip().lower() == PAYS_LOCAL.lower()


def bloquants(facture: dict, pieces_jointes: list, extraction: dict = None,
              minimal: float = ECART_MINIMAL, taux: float = ECART_TAUX,
              ecart_ttc: float = ECART_TTC, tva_taux: float = ECART_TVA_TAUX) -> list:
    """Ce qui empeche de valider cette facture. -> [str], vide si rien ne s'y oppose.

    ⚠️ DEUX MOTIFS SEULEMENT, ET C'EST UN CHOIX. Tout ce qui peut etre CORRIGE l'est plutot que
    refuse : le stock, le magasin, le numero, les dates et la retenue a la source se posent tout
    seuls a l'enregistrement (cf. `achat/facture.py`). Refuser une facture pour une case a cocher
    qu'on sait cocher soi-meme fait perdre du temps sans rien proteger.

    Restent bloquants :
    - l'absence de PDF, parce qu'aucune machine ne peut inventer la piece justificative ;
    - un ecart IMPORTANT sur les totaux, parce que la seule chose qu'on ne peut pas trancher a la
      place de l'utilisateur, c'est lequel des deux documents dit vrai.
    """
    if not est_local(facture.get("pays_fournisseur")):
        return []
    if not pdf_present(pieces_jointes):
        return ["aucun scan PDF de la facture fournisseur n'est joint : la piece justificative est "
                "obligatoire pour un fournisseur local, et au format PDF"
                + (" (%s piece(s) jointe(s), mais aucune en PDF)" % len(pieces_jointes)
                   if pieces_jointes else "")]
    return ecarts_importants(facture, extraction, minimal, taux, ecart_ttc, tva_taux)


def seuil_ecart(total_ttc, minimal: float = ECART_MINIMAL, taux: float = ECART_TAUX) -> float:
    """Le seuil du HT : le plus grand de 1 DT et d'un pourcentage du TTC."""
    return round(max(float(minimal), abs(float(total_ttc or 0)) * float(taux) / 100.0), 3)


def seuil_tva(tva_lue, taux: float = ECART_TVA_TAUX) -> float:
    """Le seuil de la TVA : un pourcentage de la TVA LUE SUR LE SCAN.

    Sur la valeur lue, et non sur la saisie : c'est le scan qui fait foi, et une saisie gonflee ne
    doit pas s'acheter au passage la tolerance qui va avec.
    """
    return round(abs(float(tva_lue or 0)) * float(taux) / 100.0, 3)


def ecarts_importants(facture: dict, extraction: dict = None, minimal: float = ECART_MINIMAL,
                      taux: float = ECART_TAUX, ecart_ttc: float = ECART_TTC,
                      tva_taux: float = ECART_TVA_TAUX) -> list:
    """Les desaccords MATERIELS entre la saisie et le scan. -> [str].

    ⚠️ ON NE COMPARE QUE CE QUE LE SCAN LIT DE FACON COHERENTE. Si HT + TVA + timbre ne tombe pas
    sur le TTC du scan, c'est la LECTURE qui est fausse, pas forcement la saisie : accuser la
    facture sur cette base reviendrait a transformer une erreur de reconnaissance en refus de
    validation. Sur ELECTROQUIP, deux modeles ont rendu 922,688 et 971,25 pour le meme HT — le
    second ne bouclait pas, et n'aurait jamais du peser sur une decision.

    ⚠️ CHAQUE GRANDEUR AVEC SON SEUIL. Le TTC en absolu (1,5 DT), la TVA en pourcentage d'elle-meme
    (0,1 %), le HT sur le TTC parce que c'est la qu'est le bruit. Un seuil unique calcule sur le TTC
    donnait a la TVA une tolerance de 6,2 % — la porte par laquelle est passee ACC-PINV-2026-00092.
    """
    if not extraction or not extraction.get("coherent"):
        return []
    out = []
    for cle, libelle in (("total_ttc", "TTC"), ("total_tva", "TVA"), ("total_ht", "HT")):
        lu, saisi = extraction.get(cle), facture.get(cle)
        if lu is None or saisi is None:
            continue
        if cle == "total_ttc":
            seuil = round(abs(float(ecart_ttc)), 3)
        elif cle == "total_tva":
            seuil = seuil_tva(lu, tva_taux)
        else:
            seuil = seuil_ecart(facture.get("total_ttc"), minimal, taux)
        ecart = round(float(saisi) - float(lu), 3)
        if abs(ecart) > seuil:
            out.append("%s : la facture porte %s, le scan porte %s — ecart de %s (au-dela de %s)"
                       % (libelle, round(float(saisi), 3), round(float(lu), 3), ecart, seuil))
    return out


def _somme(lignes, deduire=False, mot=None) -> float:
    total = 0.0
    for l in lignes or []:
        sens = (l.get("add_deduct_tax") or "Add")
        if (sens == "Deduct") != bool(deduire):
            continue
        if mot and mot.lower() not in (l.get("account_head") or "").lower():
            continue
        total += float(l.get("tax_amount") or 0)
    return round(total, 3)


def retenue_saisie(lignes) -> float:
    """Ce que la facture retient deja, en ligne de deduction."""
    return _somme(lignes, deduire=True, mot=MOT_RETENUE)


def timbre(lignes) -> float:
    return _somme(lignes, mot=MOT_TIMBRE)


def tva_facturee(lignes) -> float:
    """La TVA REELLE de la facture — la somme des lignes de TVA.

    ⚠️ NE PAS CONFONDRE AVEC `total_taxes_and_charges`, qui est le NET de la table : sur
    ELECTROQUIP il vaut 163,321, soit 175,311 de TVA MOINS 11,990 de retenue. Comparer ce net a la
    TVA lue sur le scan accusait la facture d'un ecart qui n'existait pas.
    """
    return _somme(lignes, mot=MOT_TVA)


def ttc_avant_retenue(grand_total, lignes) -> float:
    """Le TTC tel que le fournisseur le facture, AVANT que nous ne retenions notre 1 %.

    C'est ce montant-la qui figure sur le scan et qui sert d'assiette — le `grand_total` d'ERPNext,
    lui, est deja net de la retenue (1 087,021 au lieu de 1 099,011).
    """
    return round(float(grand_total or 0) + retenue_saisie(lignes), 3)


def assiette_retenue(grand_total, lignes) -> float:
    """L'assiette : le TTC avant retenue, TIMBRE FISCAL DEDUIT.

    ⚠️ LE TIMBRE NE SUPPORTE PAS LA RETENUE, et ce n'est pas une supposition : sur les 17 factures
    locales de 2026 depassant le seuil, la regle « 1 % du TTC hors timbre » tombe au millime sur
    dix d'entre elles. C'est aussi ce que le portail applique en sens inverse pour les ventes, ou
    l'assiette declaree vaut notre TTC moins le timbre de 1 DT.
    """
    return round(ttc_avant_retenue(grand_total, lignes) - timbre(lignes), 3)


def retenue_due(assiette, seuil: float = SEUIL_RETENUE, taux: float = TAUX_RETENUE,
                ttc=None) -> float:
    """Le montant a retenir. 0 en dessous du seuil.

    Le SEUIL se lit sur le TTC (1 000 DT), la RETENUE se calcule sur l'assiette (TTC hors timbre) :
    les deux ne portent pas sur le meme nombre, et les confondre change le resultat d'un millime
    sur chaque facture timbree.
    """
    base = float(assiette or 0)
    reference = float(ttc if ttc is not None else base)
    if reference < float(seuil):
        return 0.0
    return round(base * float(taux) / 100.0, 3)


def controle_retenue(grand_total, lignes, seuil: float = SEUIL_RETENUE,
                     taux: float = TAUX_RETENUE, tolerance: float = TOLERANCE_RETENUE) -> dict:
    """Confronte la retenue saisie a celle qui est due. -> {due, saisie, ecart, verdict}."""
    ttc = ttc_avant_retenue(grand_total, lignes)
    due = retenue_due(assiette_retenue(grand_total, lignes), seuil, taux, ttc=ttc)
    saisie = retenue_saisie(lignes)
    ecart = round(saisie - due, 3)
    if abs(ecart) <= tolerance:
        verdict = "conforme"
    elif not saisie:
        verdict = "manquante"
    else:
        verdict = "montant faux"
    return {"ttc_avant_retenue": ttc, "assiette": assiette_retenue(grand_total, lignes),
            "due": due, "saisie": saisie, "ecart": ecart, "verdict": verdict}


# Ecart maximal admis entre la date lue sur le scan et la date de comptabilisation. Large : une
# facture peut etre saisie plusieurs semaines apres son emission, ou a cheval sur deux exercices.
MARGE_DATE_JOURS = 400


def date_plausible(date_lue, date_facture, marge_jours: int = MARGE_DATE_JOURS) -> bool:
    """La date lue sur le scan est-elle croyable ? Fonction pure.

    ⚠️ LA LECTURE DE L'ANNEE EST LE POINT FAIBLE DU MODELE. Sur une meme facture, trois lectures
    successives ont rendu 2020, 2023 et 2026 — le numero (26FA01134) et les montants, eux, etaient
    stables. Poser une date fausse dans « Date de la facture fournisseur » serait pire que ne rien
    poser : elle deciderait de l'exercice de rattachement sans que personne ne la relise.
    """
    if not date_lue or not date_facture:
        return False
    try:
        from datetime import date, datetime

        def _d(v):
            if isinstance(v, datetime):
                return v.date()
            if isinstance(v, date):
                return v
            return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()

        return abs((_d(date_lue) - _d(date_facture)).days) <= marge_jours
    except Exception:
        return False
