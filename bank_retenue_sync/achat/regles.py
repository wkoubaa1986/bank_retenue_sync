"""Les regles de la facture d'achat locale. Fonctions PURES : aucune base, aucun reseau.

CE QUE CE FICHIER DECIDE
------------------------
Une facture d'achat aupres d'un fournisseur TUNISIEN engage trois choses que la saisie seule ne
garantit pas : la preuve (le scan de la facture reelle), le stock (la marchandise entre quelque
part), et l'impot (1 % retenu a la source au-dela de 1 000 DT TTC). Aucune de ces trois n'est
rattrapable apres validation sans annuler l'ecriture — d'ou des controles BLOQUANTS, et non des
avertissements que personne ne lit.

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

# Ecart admis entre la facture saisie et la facture scannee. Ce n'est pas une tolerance de calcul :
# c'est la marge de LECTURE d'un scan. Sur ELECTROQUIP, le modele a rendu 1 098,999 la ou la facture
# porte 1 099,011 — douze millimes d'ecart de reconnaissance, sur une TVA lue au millime pres.
TOLERANCE = 0.05

# Ecart admis sur la retenue elle-meme. Celle-la est CALCULEE, pas lue : le millime suffit.
TOLERANCE_RETENUE = 0.01

# Mots qui identifient une ligne de taxe dans le plan comptable. Le compte porte le sens, pas le
# libelle saisi a la main.
MOT_RETENUE = "etenue"
MOT_TIMBRE = "imbre"
MOT_TVA = "TVA"


def est_local(pays) -> bool:
    return (pays or "").strip().lower() == PAYS_LOCAL.lower()


def manques(facture: dict, pieces_jointes: list, extraction: dict = None,
            tolerance: float = TOLERANCE) -> list:
    """Ce qui empeche de valider cette facture. -> [str], vide si tout est en regle.

    L'ordre compte : on annonce d'abord ce qui manque (la piece, le stock), ensuite seulement les
    ecarts de montant — reprocher un ecart de TVA a quelqu'un qui n'a pas encore joint son scan
    n'aide personne.
    """
    if not est_local(facture.get("pays_fournisseur")):
        return []

    bloquants = []
    if not pieces_jointes:
        bloquants.append("aucun scan de la facture fournisseur n'est joint : la piece justificative "
                         "est obligatoire pour un fournisseur local")
    if not facture.get("update_stock"):
        bloquants.append("« Mettre a jour le stock » n'est pas coche : la marchandise entrerait "
                         "sans mouvement de stock")
    if not facture.get("set_warehouse"):
        bloquants.append("aucun magasin n'est choisi : le stock ne saurait pas ou entrer")
    if not facture.get("bill_no"):
        bloquants.append("le numero de la facture fournisseur est vide%s"
                         % (" et le scan n'en donne pas" if pieces_jointes else ""))
    if not facture.get("bill_date"):
        # La date lue puis ecartee est plus utile qu'une absence : elle dit ou regarder, et
        # pourquoi la machine n'a pas voulu la poser a la place de l'utilisateur.
        lue = (extraction or {}).get("invoice_date")
        bloquants.append("la date de la facture fournisseur est vide%s"
                         % (" — le scan porte %s, trop eloigne de la date de comptabilisation pour "
                            "etre posee sans relecture" % lue if lue else ""))

    controle = facture.get("controle_retenue") or {}
    if controle.get("verdict") == "manquante" and controle.get("due"):
        bloquants.append("la retenue a la source de %s DT (1 %% de %s) n'est pas dans les taxes"
                         % (controle["due"], controle.get("assiette")))
    elif controle.get("verdict") == "montant faux":
        bloquants.append("la retenue a la source saisie (%s) ne vaut pas celle qui est due (%s) : "
                         "ecart de %s" % (controle["saisie"], controle["due"], controle["ecart"]))

    if not bloquants and extraction:
        bloquants += ecarts(facture, extraction, tolerance)
    return bloquants


def ecarts(facture: dict, extraction: dict, tolerance: float = TOLERANCE) -> list:
    """Les desaccords entre ce qui est saisi et ce que le scan porte. -> [str].

    ⚠️ C'EST LE CONTROLE QUI JUSTIFIE TOUT LE RESTE. Une facture saisie de travers passe tous les
    autres tests : elle a son scan, son stock, son magasin. Seule la confrontation des totaux dit
    que le montant paye n'est pas celui que le fournisseur reclame — et c'est aussi ce qui rend la
    retenue a la source juste, puisqu'elle se calcule sur ce TTC.
    """
    out = []
    for cle, libelle in (("total_ttc", "TTC"), ("total_tva", "TVA")):
        lu = extraction.get(cle)
        saisi = facture.get(cle)
        if lu is None or saisi is None:
            continue
        if abs(float(lu) - float(saisi)) > tolerance:
            out.append("%s de la facture saisie (%s) different du %s lu sur le scan (%s)"
                       % (libelle, round(float(saisi), 3), libelle, round(float(lu), 3)))
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
