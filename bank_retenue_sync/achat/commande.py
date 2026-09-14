"""La commande d'achat : regrouper les lignes qui portent deux fois le meme article.

CE QUE CE FICHIER DECIDE
------------------------
Une commande d'import se construit rarement d'un coup : on ajoute une ligne au fil des besoins, et
le meme article finit saisi trois fois. Le fournisseur, lui, lit une commande : trois lignes du
meme article au meme prix sont pour lui une seule ligne de trois fois la quantite.

⚠️ LE REGROUPEMENT SE DECIDE SUR L'ARTICLE, SON UNITE ET SON PRIX — PAS SUR L'ARTICLE SEUL. Deux
lignes du meme article a deux prix differents ne sont PAS un doublon : c'est une negociation, un
reliquat a l'ancien tarif, ou une erreur de saisie que personne ne peut trancher a notre place.
Les sommer inventerait un prix qui n'a ete convenu avec personne. Elles restent donc telles
quelles, et `non_fusionnees` les signale pour que l'ecran le dise au lieu de se taire.

⚠️ LA PREMIERE OCCURRENCE GAGNE, ET C'EST UNE PERTE D'INFORMATION ASSUMEE. Seule la quantite est
sommee ; l'entrepot, la date de reception et la description retenus sont ceux de la premiere
ligne. Si les doublons different sur ces champs, le choix est arbitraire — c'est pourquoi la
fusion ne s'applique qu'au clic d'un humain, et jamais toute seule a l'enregistrement.

Fonctions PURES : aucune base, aucun reseau. Le seul point d'entree qui touche a Frappe est la
methode whitelistee, qui ne fait qu'exposer la regle au formulaire — elle n'ecrit rien.
"""
from __future__ import annotations

import frappe

#: Les quantites se somment au millieme : c'est la precision des quantites d'ERPNext, et additionner
#: des flottants sans arrondir fait apparaitre des 2.9999999999999996 dans la case de l'utilisateur.
PRECISION_QTY = 3

#: Le prix ne sert qu'a COMPARER deux lignes. On l'arrondit avant de s'en servir comme cle : deux
#: saisies identiques peuvent differer au quinzieme chiffre apres la virgule et paraitre distinctes.
PRECISION_PRIX = 6


def _qty(ligne) -> float:
    return round(float(ligne.get("qty") or 0), PRECISION_QTY)


def _cle(ligne):
    """Ce qui fait que deux lignes sont LA MEME ligne. None si la ligne ne se regroupe pas.

    Une ligne sans article n'a pas d'identite : on n'y touche pas.
    """
    article = (ligne.get("item_code") or "").strip()
    if not article:
        return None
    return (article, (ligne.get("uom") or "").strip(),
            round(float(ligne.get("rate") or 0), PRECISION_PRIX))


def _conservee(ligne) -> dict:
    return {"name": ligne.get("name"), "item_code": ligne.get("item_code"), "qty": _qty(ligne),
            "fusionnees": 1}


def _non_fusionnees(groupes) -> list:
    """Les articles restes sur plusieurs lignes, et pourquoi. -> [{item_code, lignes, motif}].

    ⚠️ CE N'EST PAS UN AVERTISSEMENT DE CONFORT. L'utilisateur clique pour qu'il ne reste qu'une
    ligne par article ; s'il en reste deux, il doit savoir que ce n'est pas un oubli du bouton mais
    un desaccord de prix ou d'unite entre ses propres lignes.
    """
    par_article = {}
    for article, uom, prix in groupes:
        par_article.setdefault(article, []).append((uom, prix))
    signales = []
    for article, cles in par_article.items():
        if len(cles) < 2:
            continue
        prix_differents = len({p for _, p in cles}) > 1
        unites_differentes = len({u for u, _ in cles}) > 1
        signales.append({
            "item_code": article,
            "lignes": len(cles),
            "motif": ("prix et unite" if prix_differents and unites_differentes
                      else "prix" if prix_differents else "unite"),
        })
    return signales


def regrouper(lignes) -> dict:
    """Regroupe les lignes du meme article en une seule, en sommant les quantites. Fonction PURE.

    Chaque ligne attendue porte au moins `name`, `item_code`, `uom`, `rate` et `qty`.

    -> {"conserver": [{name, item_code, qty, fusionnees}],  # TOUTES les lignes qui restent, dans
                                                            # leur ordre d'origine
        "supprimer": [name],                                # les lignes en trop
        "doublons": int,                                    # combien de lignes disparaissent
        "non_fusionnees": [{item_code, lignes, motif}]}

    La ligne conservee est la PREMIERE de son groupe : elle garde sa place, son entrepot, sa date
    de reception et sa description. `fusionnees` dit combien de lignes d'origine elle represente —
    a 1, sa quantite n'a pas bouge et l'ecran n'a rien a y toucher.
    """
    groupes = {}
    conserver = []
    supprimer = []
    for ligne in lignes or []:
        cle = _cle(ligne)
        if cle is None:
            conserver.append(_conservee(ligne))
            continue
        gardee = groupes.get(cle)
        if gardee is None:
            gardee = _conservee(ligne)
            groupes[cle] = gardee
            conserver.append(gardee)
            continue
        gardee["qty"] = round(gardee["qty"] + _qty(ligne), PRECISION_QTY)
        gardee["fusionnees"] += 1
        supprimer.append(ligne.get("name"))
    return {"conserver": conserver, "supprimer": supprimer, "doublons": len(supprimer),
            "non_fusionnees": _non_fusionnees(groupes)}


@frappe.whitelist()
def fusionner_lignes(items):
    """Bouton « Fusionner les lignes en double » du formulaire de commande d'achat.

    ⚠️ CETTE METHODE N'ECRIT RIEN. Elle recoit les lignes telles qu'elles sont A L'ECRAN — donc y
    compris les modifications non encore enregistrees — et ne rend qu'un calcul. C'est le
    formulaire qui applique le resultat, et c'est l'utilisateur qui enregistre : tant qu'il ne l'a
    pas fait, la commande en base n'a pas bouge et un simple rechargement annule la fusion.
    """
    frappe.only_for(["System Manager", "Purchase Manager", "Purchase User", "Accounts Manager"])
    if isinstance(items, str):
        items = frappe.parse_json(items) or []
    return regrouper(items)
