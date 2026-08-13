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

# Ecart admis entre la facture saisie et la facture scannee. Les deux portent les MEMES totaux
# imprimes : ce n'est pas une tolerance d'arrondi de calcul, c'est la marge de lecture d'un PDF.
TOLERANCE = 0.01


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
        bloquants.append("le numero de la facture fournisseur est vide")
    if not facture.get("bill_date"):
        bloquants.append("la date de la facture fournisseur est vide")

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


def retenue_due(total_ttc, seuil: float = SEUIL_RETENUE, taux: float = TAUX_RETENUE) -> float:
    """Le montant a retenir. 0 en dessous du seuil.

    Le seuil se lit sur le TTC, la retenue se calcule sur le TTC : c'est la regle tunisienne, et
    c'est aussi ce qui la rend verifiable d'un coup d'oeil sur la facture.
    """
    ttc = float(total_ttc or 0)
    if ttc < float(seuil):
        return 0.0
    return round(ttc * float(taux) / 100.0, 3)


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
