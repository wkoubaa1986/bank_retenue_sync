"""L'historique mensuel du partenaire, en base — le remplacant de `economiq_history.json`.

⚠️ CE FICHIER JSON ETAIT LA SOURCE DE VERITE DU CONSOLIDE, SUR UN POSTE. Charges libres,
ajustement, echeancier ajuste, report d'un mois sur l'autre : tout le calcul inter-mois en
dependait, sans sauvegarde, sans historique de modification, et lisible par une seule personne.
Un DocType nomme `YYYY-MM` donne l'unicite gratuitement et rend la continuite verifiable.

⚠️ ET UN MOIS VALIDE NE SE RECALCULE PLUS. Le bilan vient d'ERPNext et bouge tant que des pieces
s'y ajoutent ; une fois le mois arrete et son echeancier communique au partenaire, le recalculer
changerait des montants deja annonces. La case « Validé » gele le mois.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

from bank_retenue_sync.facturation import periode
from bank_retenue_sync.partenaire import echeancier

DOCTYPE = "BRS Economiq Mois"
PRECISION = 3


def existe(mois: str) -> bool:
    return bool(frappe.db.exists(DOCTYPE, mois))


def lire(mois: str) -> dict | None:
    """Le mois enregistre, ou None. -> dict sans document Frappe autour."""
    mois = periode.normaliser(mois)
    if not existe(mois):
        return None
    doc = frappe.get_doc(DOCTYPE, mois)
    return {
        "mois": doc.mois,
        "libelle": doc.libelle,
        "valide": bool(doc.valide),
        "journal_entry": doc.journal_entry,
        "bilan": {
            "aqua": {"ventes": flt(doc.ventes_aqua, PRECISION),
                     "achats": flt(doc.achats_aqua, PRECISION),
                     "benefice": flt(doc.benefice_aqua, PRECISION)},
            "partenaire": {"ventes": flt(doc.ventes_partenaire, PRECISION),
                           "achats": flt(doc.achats_partenaire, PRECISION),
                           "benefice": flt(doc.benefice_partenaire, PRECISION)},
        },
        "charges": [{"libelle": c.libelle, "montant": flt(c.montant, PRECISION)}
                    for c in doc.charges],
        "total_commandes": flt(doc.total_commandes, PRECISION),
        "solde_net": flt(doc.solde_net, PRECISION),
        "total_charges": flt(doc.total_charges, PRECISION),
        "ajustement": flt(doc.total_ajustement, PRECISION),
        "report": flt(doc.report_vers_consolide, PRECISION),
        "echeances": [{"date": str(e.date), "montant_brut": flt(e.montant_brut, PRECISION),
                       "deduit": flt(e.deduit, PRECISION), "montant": flt(e.montant, PRECISION),
                       "note": e.note or "", "statut": e.statut or "non_payé",
                       "paye": flt(e.paye, PRECISION), "reste": flt(e.reste, PRECISION)}
                      for e in doc.echeancier],
    }


def enregistrer(mois: str, bilan: dict, charges: list, total_commandes: float = 0.0,
                valide: bool = False, ajustement_impose=None, journal_entry=None) -> dict:
    """Recalcule l'echeancier du mois, puis enregistre.

    LA CHAINE, dans l'ordre — c'est celle du rapport que le partenaire recoit :
        echeancier brut = total des COMMANDES du mois / 3
        ajustement      = (benefice Aqua − benefice Economiq) + charges libres
        echeancier du   = brut − ajustement, absorbe des la premiere echeance

    ⚠️ NI L'ECHEANCIER NI L'AJUSTEMENT NE SE SAISISSENT. Tous deux DECOULENT des commandes, du
    bilan et des charges. Les rendre modifiables ouvrirait la porte a un echeancier qui ne somme
    pas a ce qui a ete annonce.

    ⚠️ SAUF `ajustement_impose`, QUI NE VIENT QUE DE LA COMPTABILITE. C'est la ligne d'equilibre
    de l'ecriture de bilan du mois, lue par `ecriture.lire` — pas une saisie. Elle prime parce
    qu'elle repose sur les montants reels, la ou le bilan recalcule repose sur des achats
    sous-evalues tant que `tabItem Price` est vide : 389,450 contre 652,000 sur juin 2026. Un
    appelant qui passerait ici une valeur d'origine humaine contournerait la regle ci-dessus.
    """
    mois = periode.normaliser(mois)
    annee, numero = periode.eclater(mois)
    if verrouille(mois):
        frappe.throw("Le mois {0} est validé : il ne se recalcule plus.".format(mois))

    charges = [{"libelle": (c.get("libelle") or "").strip(),
                "montant": flt(c.get("montant"), PRECISION)}
               for c in (charges or []) if (c.get("libelle") or "").strip()]
    total_charges = flt(sum(c["montant"] for c in charges), PRECISION)

    aqua = bilan.get("aqua") or {}
    partenaire = bilan.get("partenaire") or {}
    total_commandes = flt(total_commandes, PRECISION)
    solde = echeancier.solde_net(aqua.get("benefice"), partenaire.get("benefice"))
    ajustement = (flt(ajustement_impose, PRECISION) if ajustement_impose is not None
                  else echeancier.ajustement(aqua.get("benefice"), partenaire.get("benefice"),
                                             total_charges))
    brut = echeancier.brut(total_commandes, annee, numero)
    ajuste, report = echeancier.ajuster(brut, ajustement)

    doc = frappe.get_doc(DOCTYPE, mois) if existe(mois) else frappe.new_doc(DOCTYPE)
    doc.mois = mois
    doc.libelle = periode.libelle(mois)
    doc.valide = 1 if valide else 0
    doc.ventes_aqua = flt(aqua.get("ventes"), PRECISION)
    doc.achats_aqua = flt(aqua.get("achats"), PRECISION)
    doc.benefice_aqua = flt(aqua.get("benefice"), PRECISION)
    doc.ventes_partenaire = flt(partenaire.get("ventes"), PRECISION)
    doc.achats_partenaire = flt(partenaire.get("achats"), PRECISION)
    doc.benefice_partenaire = flt(partenaire.get("benefice"), PRECISION)
    doc.total_commandes = total_commandes
    doc.solde_net = solde
    doc.total_charges = total_charges
    doc.total_ajustement = ajustement
    doc.report_vers_consolide = report
    if journal_entry:
        doc.journal_entry = journal_entry

    doc.set("charges", [])
    for c in charges:
        doc.append("charges", c)

    # ⚠️ LE SUIVI DES PAIEMENTS SURVIT AU RECALCUL. Une echeance deja payee doit le rester : on
    # reprend `paye` de l'echeance de meme date avant de tout reecrire.
    #
    # ⚠️ MAIS LE RESTE SE RECALCULE, IL NE SE REPREND PAS. Il vaut toujours `montant − paye`.
    # Le recopier de l'ancienne ligne conservait un reste calcule sur un montant qui n'existe
    # plus : un mois re-enregistre avec un ajustement plus fort gardait le reste d'avant
    # absorption, et le consolide affichait un reste superieur au du — 3 748,925 a rembourser
    # sur une echeance de 3 542,610.
    ancien = {str(e.date): e for e in (doc.echeancier or [])}
    doc.set("echeancier", [])
    for e in ajuste:
        precedent = ancien.get(e["date"])
        montant = flt(e["montant"], PRECISION)
        absorbee = montant <= 0.001 and flt(e.get("deduit")) > 0
        paye = flt(precedent.paye, PRECISION) if precedent else 0.0
        reste = max(0.0, flt(montant - paye, PRECISION))
        doc.append("echeancier", {
            "date": e["date"], "montant_brut": flt(montant + flt(e.get("deduit")), PRECISION),
            "deduit": flt(e.get("deduit"), PRECISION), "montant": montant, "note": e.get("note"),
            "paye": paye, "reste": reste,
            "statut": ("absorbé" if absorbee
                       else ("payé" if paye and reste <= 0.001
                             else ("partiel" if paye else "non_payé"))),
        })

    doc.flags.ignore_permissions = True
    doc.save()
    frappe.db.commit()
    return lire(mois)


def verrouille(mois: str) -> bool:
    return bool(frappe.db.get_value(DOCTYPE, mois, "valide")) if existe(mois) else False


def tous() -> list:
    """Tous les mois enregistres, du plus ancien au plus recent, pour le consolide."""
    return [lire(m.name) for m in frappe.get_all(DOCTYPE, fields=["name"], order_by="name asc",
                                                 limit_page_length=0)]


def projete(tableau: dict) -> dict | None:
    """Le mois affiche TEL QU'IL SERAIT ENREGISTRE, pour le consolide — None s'il l'est deja.

    Fonction pure : meme forme que `lire`, memes statuts que `enregistrer`, rien n'est ecrit.

    ⚠️ SANS ELLE, LE REPORT D'UN MOIS NON ENREGISTRE N'APPARAIT NULLE PART. Le consolide ne lit
    que les mois en base : en octobre 2026, les 425,070 dus par Aqua World etaient annonces
    « reportés sur les échéances les plus anciennes » alors que le reste du affiche (6 427,660)
    ne bougeait pas — le rapport du mois se contredisait lui-meme. On montre donc ce que
    deviendrait le consolide, en le disant : c'est une projection tant que le mois n'est pas
    enregistre.
    """
    if not tableau or not tableau.get("disponible") or tableau.get("enregistre"):
        return None

    def arr(v):
        return round(float(v or 0), PRECISION)

    echeances = []
    for e in tableau.get("echeancier") or []:
        montant, deduit = arr(e.get("montant")), arr(e.get("deduit"))
        echeances.append({
            "date": str(e.get("date")), "montant_brut": arr(montant + deduit), "deduit": deduit,
            "montant": montant, "note": e.get("note") or "", "paye": 0.0, "reste": montant,
            "statut": "absorbé" if montant <= 0.001 and deduit > 0 else "non_payé"})
    return {"mois": tableau.get("mois"), "libelle": tableau.get("libelle"), "valide": False,
            "projete": True, "ajustement": arr(tableau.get("ajustement")),
            "report": arr(tableau.get("report")), "echeances": echeances}


def pour_le_consolide(tableau: dict | None = None) -> tuple[list, dict | None]:
    """Les mois du consolide : ceux en base, et le mois affiche en projection s'il ne l'est pas.

    -> (mois enregistres, mois projete ou None).

    ⚠️ ON NE PROJETTE QUE LE MOIS QUI SUIT LE DERNIER ENREGISTRE. Projeter un mois anterieur
    deverserait son report sur des echeances deja arretees ; en projeter un apres un trou
    (`continuite`) montrerait un consolide ampute des mois manquants, que l'ecran signale deja
    comme faux.
    """
    enregistres = [m for m in tous() if m]
    projection = projete(tableau) if tableau else None
    if projection and enregistres and projection["mois"] <= enregistres[-1]["mois"]:
        projection = None
    if projection and continuite(projection["mois"]):
        projection = None
    return enregistres, projection


def continuite(mois: str) -> str:
    """Les mois manquants entre le dernier enregistre et celui-ci, ou "" si la suite est intacte.

    ⚠️ UN TROU DANS L'HISTORIQUE FAUSSE LE CONSOLIDE SANS RIEN DIRE. Le report d'un mois se
    deverse sur les echeances suivantes : sauter un mois, c'est perdre son report et reclamer
    une somme qui a deja ete absorbee.
    """
    mois = periode.normaliser(mois)
    connus = sorted(m.name for m in frappe.get_all(DOCTYPE, fields=["name"],
                                                   limit_page_length=0))
    if not connus or mois in connus:
        return ""
    annee, numero = periode.eclater(connus[-1])
    manquants = []
    while True:
        annee, numero = periode.suivant(annee, numero)
        cle = periode.cle(annee, numero)
        if cle == mois or len(manquants) > 24:
            break
        manquants.append(cle)
    return ", ".join(manquants)
