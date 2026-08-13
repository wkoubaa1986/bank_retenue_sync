"""L'AUTRE SENS DU FLUX : les retenues comptabilisees qu'aucun certificat ne justifie.

Tout le reste du module TEJ part du portail et cherche la piece : « le client a declare une
retenue, l'ai-je comptabilisee ? ». Cette question-la ne couvre qu'une moitie du sujet. L'autre
est comptable et compte autant : « j'ai comptabilise un credit d'impot — ai-je le certificat qui
le justifie ? ». Sans elle, on peut afficher un tableau entierement vert et reclamer au fisc des
credits qu'aucun justificatif ne soutient.

C'est exactement la lecon de `bank/ecarts.py` pour la banque : un rapprochement qui ne se lit que
dans un sens laisse invisible la moitie des ecarts.

QUATRE VERDICTS, ET AUCUN N'EST UNE ERREUR PAR LUI-MEME
-------------------------------------------------------
- `certificat probable` : un certificat non rapproche colle au montant et a la date, OU il est
  declare sur la piece meme (facture / commande) que regle l'ecriture. Ce n'est pas un trou, c'est
  un rapprochement a faire — souvent parce que le client identifie n'est pas celui que porte
  l'ecriture (deux fiches pour un meme matricule).
- `certificat manuel` : la facture ou la commande porte deja un certificat, remis a la main et
  jamais passe par le portail. Le credit d'impot EST justifie : le paiement est tenu pour
  rapproche, et la ligne sort de la liste des relances.
- `hors periode du portail` : l'ecriture precede le perimetre suivi. Absence de preuve, pas preuve
  d'absence.
- `sans certificat` : la vraie alerte. Nous avons deduit une retenue que le client n'a jamais
  declaree ; le credit d'impot existe dans nos comptes et nulle part ailleurs. C'est celui-la
  qu'il faut reclamer au client, et c'est le seul chiffre de ce module qui appelle une action.

LE CERTIFICAT PAPIER EXISTE, ET IL VAUT LE CERTIFICAT DU PORTAIL
----------------------------------------------------------------
Le depot au portail TEJ n'est obligatoire que depuis le 1er avril 2026. Avant cette date — et
apres, chez les clients qui ne l'ont pas encore adopte — le certificat est remis en papier et
scanne sur la facture ou sur la commande. Lire le seul portail faisait donc crier « credit d'impot
sans justificatif » sur des retenues parfaitement prouvees, et noyait les vraies dans le nombre.

Le justificatif doit SE NOMMER : le tableau des factures compte n'importe quelle piece jointe comme
preuve, ce qui suffit pour un compteur mais pas pour affirmer qu'un credit d'impot est justifie. Un
bon de livraison scanne n'est pas un certificat de retenue. Les pieces jointes qui ne se nomment pas
sont malgre tout SIGNALEES dans l'explication : c'est peut-etre un certificat mal nomme.

⚠️ QUAND LES DEUX EXISTENT, ON LES CONFRONTE. Un certificat papier ET un certificat au portail sur
la meme piece, c'est soit le meme document (le client a regularise son depot), soit deux retenues
distinctes — et dans ce second cas le credit peut avoir ete comptabilise deux fois. La comparaison
des montants tranche, et le doublon possible est annonce en tete de liste.

⚠️ LA MARGE EST CELLE DE L'IDENTIFICATION, PAS CELLE DE L'APPARIEMENT. `tolerance()` plancherait
a 1 DT et rapprocherait une retenue de 3,710 DT de n'importe quelle ecriture entre 2,71 et 4,71.
Ici le montant sert a NOMMER une piece : il doit coller au millime, timbre fiscal compris.
"""
from __future__ import annotations

import re

import frappe
from frappe.utils import add_days, flt, getdate

from bank_retenue_sync.tej import certificats as C
from bank_retenue_sync.tej import pdf as PDF
from bank_retenue_sync.tej import rapprochement as R

DOCTYPE = "Retenue Certificate"

# Meme fenetre que l'appariement : le client declare quand il paie, nous comptabilisons quand nous
# encaissons, et les deux dates s'ecartent de plusieurs semaines sans anomalie.
FENETRE = R.FENETRE_JOURS

PROBABLE = "certificat probable"
CERTIFICAT_MANUEL = "certificat manuel"
HORS_PERIODE = "hors periode du portail"
SANS_CERTIFICAT = "sans certificat"

# Ordre d'affichage : ce qui appelle une action d'abord, ce qui est clos ensuite.
RANG = {SANS_CERTIFICAT: 0, PROBABLE: 1, CERTIFICAT_MANUEL: 2, HORS_PERIODE: 3}

# Statuts d'un certificat qui n'a pas encore trouve sa piece : ce sont les seuls qui peuvent
# expliquer une ecriture orpheline.
STATUTS_LIBRES = ("Sans piece", "Ambiguous", "Unmatched", "Reporte")

# Depuis cette date, le depot au portail est obligatoire : un certificat papier sans declaration
# TEJ cesse d'etre la norme et devient une question a poser au client. Avant, c'est l'inverse.
PORTAIL_OBLIGATOIRE_DEPUIS = "2026-04-01"

# Ou peut vivre le justificatif, et comment on le nomme a l'ecran. Meme ordre que `tej/pdf.cible` :
# la facture d'abord, la commande a defaut, l'ecriture en dernier recours.
LIBELLE_SOURCE = {"Sales Invoice": "facture", "Sales Order": "commande",
                  "Payment Entry": "ecriture"}

# Un fichier qui se nomme comme un certificat de retenue. En sous-chaine pour ce qui ne peut pas
# etre autre chose, en jeton exact pour les sigles — « ras » en sous-chaine attraperait
# « brasserie.pdf », et un faux justificatif est pire que pas de justificatif du tout.
_MOTS_CERTIFICAT = ("certif", "retenue", "attestation", "withhold")
_SIGLES_CERTIFICAT = ("ras", "rs")


def _dans_fenetre(jour, reference, fenetre=FENETRE) -> bool:
    if not jour or not reference:
        return False
    return (getdate(add_days(reference, -fenetre)) <= getdate(jour)
            <= getdate(add_days(reference, fenetre)))


# ------------------------------------------------------- le justificatif hors portail


def nomme_un_certificat(nom_fichier: str) -> bool:
    """Le nom du fichier designe-t-il un certificat de retenue ?"""
    nom = (nom_fichier or "").lower()
    if any(mot in nom for mot in _MOTS_CERTIFICAT):
        return True
    return any(jeton in _SIGLES_CERTIFICAT for jeton in re.split(r"[^a-z0-9]+", nom))


def _est_fichier_tej(nom_fichier: str, noms_tej: set) -> bool:
    """Le PDF telecharge au portail par `tej/pdf.py` n'est pas un justificatif « manuel » : le
    compter comme tel ferait passer pour prouve hors portail ce qui vient du portail."""
    nom = (nom_fichier or "").lower()
    return nom in noms_tej or nom.startswith("certificat_ras_")


def _noms_tej(certificats: list) -> set:
    return {PDF.nom_fichier(c["reference"]).lower() for c in certificats if c.get("reference")}


def justificatifs_manuels(justificatifs: list, noms_tej: set) -> list:
    """Les pieces jointes qui sont un certificat, et qui ne viennent pas du portail."""
    return [j for j in justificatifs
            if not _est_fichier_tej(j.get("file_name"), noms_tej)
            and nomme_un_certificat(j.get("file_name"))]


def _resume(cert: dict, via: str = None) -> dict:
    return {"name": cert.get("name"), "reference": cert.get("reference"),
            "declarant": cert.get("declarant"), "customer": cert.get("customer"),
            "date_paiement": str(cert.get("date_paiement")),
            "montant_retenue": flt(cert.get("montant_retenue"), 3),
            "match_status": cert.get("match_status"),
            "payment_entry": cert.get("payment_entry"),
            "hors_perimetre": cert.get("hors_perimetre"), "via": via}


def comparer(montant: float, manuels: list, certificats: list, jour=None) -> dict:
    """Confronte le justificatif trouve sur la piece a ce que le portail declare.

    C'est la question que l'utilisateur pose en voyant un certificat papier : « et au portail, il
    dit quoi ? ». Les certificats confrontes viennent soit de la piece meme, soit — des lors qu'un
    papier existe — du rapprochement par montant et date : quand les deux preuves se ressemblent a
    ce point, c'est le meme document, et le dire evite de creer une seconde retenue.

    Rend None quand il n'y a rien a confronter.
    """
    if not manuels and not certificats:
        return None
    tej = sorted(certificats,
                 key=lambda c: abs(flt(c.get("montant_retenue"), 3) - flt(montant, 3)))
    ref = tej[0] if tej else None
    out = {"montant_ecriture": flt(montant, 3),
           "montant_tej": flt(ref.get("montant_retenue"), 3) if ref else None,
           "ecart": None, "concordant": None,
           "certificat_tej": _resume(ref) if ref else None,
           "certificats_tej": [_resume(c) for c in tej],
           "justificatifs": manuels}
    if ref:
        out["ecart"] = round(flt(montant, 3) - out["montant_tej"], 3)

    if manuels and ref:
        # Deux preuves pour une meme retenue : soit le meme document des deux cotes, soit deux
        # retenues distinctes. Le montant est ce qui les departage.
        out["concordant"] = abs(out["ecart"]) <= R.MARGE_IDENTIFICATION
        if out["concordant"]:
            timbre = (" ; ecart de %s explique par le timbre fiscal" % out["ecart"]
                      if out["ecart"] and R.ecart_timbre(out["ecart"]) else "")
            out["texte"] = ("le justificatif %s et le certificat TEJ %s portent le meme montant "
                            "(%s DT)%s" % (manuels[0].get("file_name"), ref.get("reference"),
                                           out["montant_tej"], timbre))
        else:
            out["texte"] = ("ECART : le portail declare %s DT (certificat %s, %s) quand l'ecriture "
                            "en porte %s — ecart de %s ; le justificatif %s couvre-t-il la meme "
                            "retenue ?" % (out["montant_tej"], ref.get("reference"),
                                           ref.get("declarant"), out["montant_ecriture"],
                                           out["ecart"], manuels[0].get("file_name")))
    elif manuels:
        out["texte"] = ("aucun certificat au portail sur cette piece : le justificatif %s est la "
                        "seule preuve du credit d'impot" % manuels[0].get("file_name"))
        if jour and getdate(jour) >= getdate(PORTAIL_OBLIGATOIRE_DEPUIS):
            out["texte"] += (" — le depot au portail est obligatoire depuis le %s, a confirmer "
                             "aupres du declarant" % PORTAIL_OBLIGATOIRE_DEPUIS)
    else:
        out["texte"] = ("le certificat %s (%s, %s DT du %s) est declare sur la meme piece"
                        % (ref.get("reference"), ref.get("declarant"), out["montant_tej"],
                           ref.get("date_paiement")))
    return out


def alerte_doublon(piece: str, montant: float, certificats_lies: list) -> str:
    """Le meme montant deja porte par une AUTRE ecriture sur la meme piece.

    C'est le risque que ce module existe pour voir : deux ecritures de retenue imputees a une meme
    facture pour une seule retenue reelle, donc un credit d'impot compte deux fois. On l'annonce,
    on ne le corrige jamais d'office — deux retenues sur une meme facture sont parfois legitimes.
    """
    for c in certificats_lies:
        autre = c.get("payment_entry")
        if (autre and autre != piece
                and abs(flt(c.get("montant_retenue"), 3) - flt(montant, 3))
                <= R.MARGE_IDENTIFICATION):
            return ("une retenue de %s DT est deja portee par l'ecriture %s (certificat %s) sur "
                    "cette piece : verifier une double comptabilisation"
                    % (flt(c.get("montant_retenue"), 3), autre, c.get("reference")))
    return None


def apparier(pieces: list, certificats: list, annee_min: int = None,
             fenetre: int = FENETRE) -> list:
    """Rend un verdict pour CHAQUE ecriture orpheline. Fonction pure : c'est elle qu'on teste.

    `pieces`      : [{name, customer, posting_date, paid_amount, sales_invoice, sales_order,
                      justificatifs, certificats_lies}]
    `certificats` : [{name, reference, declarant, customer, montant_retenue, date_paiement,
                      match_status, payment_entry}]

    `justificatifs`    : pieces jointes de la facture / commande / ecriture (cf. `charger_pieces`).
    `certificats_lies` : certificats TEJ deja rattaches a CETTE facture ou a CETTE commande. Ils
                         sont juges par l'imputation, pas par la date — meme lecon que
                         `rapprochement.apparier_par_facture`.
    """
    annee_min = C.ANNEE_MINIMALE if annee_min is None else annee_min
    libres = [c for c in certificats
              if not c.get("payment_entry") and c.get("match_status") in STATUTS_LIBRES]
    noms_tej = _noms_tej(certificats)
    out = []
    for p in pieces:
        montant = flt(p.get("paid_amount"), 3)
        source = ("facture" if p.get("sales_invoice")
                  else "commande" if p.get("sales_order") else "ecriture")
        justificatifs = p.get("justificatifs") or []
        lies = p.get("certificats_lies") or []
        noms_piece = noms_tej | _noms_tej(lies)
        manuels = justificatifs_manuels(justificatifs, noms_piece)

        candidats = [(c, "montant et date") for c in libres
                     if abs(flt(c.get("montant_retenue"), 3) - montant) <= R.MARGE_IDENTIFICATION
                     and _dans_fenetre(c.get("date_paiement"), p.get("posting_date"), fenetre)]
        vus = {c["name"] for c, _ in candidats}
        # Un certificat declare sur la piece meme, quel que soit son montant : l'imputation designe
        # la creance quand la date et le montant ne disent rien.
        candidats += [(c, "piece") for c in lies
                      if c.get("name") not in vus and not c.get("payment_entry")
                      and c.get("match_status") in STATUTS_LIBRES]

        # QUOI CONFRONTER AU PAPIER. Sans papier, seuls les certificats de la piece disent quelque
        # chose que l'explication ne dit pas deja. Avec papier, le certificat qui colle au montant
        # et a la date entre dans la comparaison : c'est tres probablement LE MEME DOCUMENT, et le
        # constater evite de comptabiliser une seconde fois une retenue deja prouvee.
        confrontables = lies
        if manuels:
            deja = {c.get("name") for c in lies}
            confrontables = lies + [c for c, via in candidats
                                    if via == "montant et date" and c.get("name") not in deja]

        ligne = {**p, "montant": montant,
                 "candidats": [_resume(c, via) for c, via in candidats[:10]],
                 "justificatifs": justificatifs,
                 "justificatifs_manuels": manuels,
                 "certificat_manuel": bool(manuels),
                 "certificats_lies": [_resume(c) for c in lies],
                 "comparaison": comparer(montant, manuels, confrontables, p.get("posting_date")),
                 "alerte": alerte_doublon(p.get("name"), montant, lies)}

        if candidats:
            premier, via = candidats[0]
            ligne["verdict"] = PROBABLE
            if len(candidats) > 1:
                ligne["explication"] = ("%s certificats non rapproches collent a cette ecriture"
                                        % len(candidats))
            elif via == "piece":
                ligne["explication"] = ("le certificat %s (%s, %s DT) est declare sur la meme %s"
                                        % (premier.get("reference"), premier.get("declarant"),
                                           flt(premier.get("montant_retenue"), 3), source))
            else:
                ligne["explication"] = (
                    "le certificat %s (%s, %s DT du %s) colle au montant et a la date"
                    % (premier.get("reference"), premier.get("declarant"),
                       flt(premier.get("montant_retenue"), 3), premier.get("date_paiement")))
        elif manuels:
            # LE CERTIFICAT PAPIER SUFFIT : le credit d'impot est prouve, le paiement est tenu pour
            # rapproche. Ce n'est pas une relance, c'est un dossier complet hors portail.
            ligne["verdict"] = CERTIFICAT_MANUEL
            # Le lieu annonce est celui du FICHIER, pas celui de la piece : un certificat reste sur
            # la commande jusqu'a ce qu'elle soit facturee, et annoncer « sur la facture » enverrait
            # l'utilisateur le chercher ou il n'est pas.
            ligne["explication"] = (
                "certificat %s attache a la %s : la retenue est justifiee hors portail, le "
                "paiement est tenu pour rapproche — %s"
                % (manuels[0].get("file_name"), manuels[0].get("source") or source,
                   ligne["comparaison"]["texte"]))
        elif p.get("posting_date") and getdate(p["posting_date"]).year < annee_min:
            ligne["verdict"] = HORS_PERIODE
            ligne["explication"] = ("ecriture anterieure a %s : le portail n'est pas suivi sur "
                                    "cette periode" % annee_min)
        else:
            ligne["verdict"] = SANS_CERTIFICAT
            ligne["explication"] = ("aucun certificat declare au portail : le credit d'impot n'a "
                                    "pas de justificatif, il est a reclamer au client")
            autres = [j for j in justificatifs
                      if j not in manuels and not _est_fichier_tej(j.get("file_name"), noms_piece)]
            if autres:
                # Un certificat mal nomme reste un certificat : on ne le compte pas comme preuve,
                # mais taire son existence enverrait relancer un client qui a deja fourni.
                ligne["explication"] += (
                    " (%s piece(s) jointe(s), dont aucune ne se nomme comme un certificat : %s)"
                    % (len(autres), ", ".join("%s [%s]" % (a.get("file_name"), a.get("source"))
                                              for a in autres[:3])))
        out.append(ligne)
    # Le doublon possible passe devant tout : c'est le seul cas ou un credit d'impot peut avoir ete
    # compte deux fois, et il ne doit pas attendre la fin d'une liste de cent lignes.
    return sorted(out, key=lambda l: (not l.get("alerte"), RANG.get(l["verdict"], 9),
                                      str(l.get("posting_date"))))


def synthese(lignes: list) -> dict:
    """Les chiffres que la page affiche. Un compte ET un montant par verdict : c'est le montant
    qui dit si l'affaire vaut une relance client."""
    def somme(seq):
        return round(sum(flt(l["montant"], 3) for l in seq), 3)

    par = {v: [l for l in lignes if l["verdict"] == v]
           for v in (PROBABLE, CERTIFICAT_MANUEL, HORS_PERIODE, SANS_CERTIFICAT)}
    alertes = [l for l in lignes if l.get("alerte")]
    ecarts = [l for l in lignes if (l.get("comparaison") or {}).get("concordant") is False]
    return {"total": len(lignes), "montant_total": somme(lignes),
            "probables": len(par[PROBABLE]), "montant_probables": somme(par[PROBABLE]),
            "certificat_manuel": len(par[CERTIFICAT_MANUEL]),
            "montant_certificat_manuel": somme(par[CERTIFICAT_MANUEL]),
            "hors_periode": len(par[HORS_PERIODE]),
            "montant_hors_periode": somme(par[HORS_PERIODE]),
            "sans_certificat": len(par[SANS_CERTIFICAT]),
            "montant_sans_certificat": somme(par[SANS_CERTIFICAT]),
            "doublons_possibles": len(alertes), "montant_doublons": somme(alertes),
            "ecarts_certificat": len(ecarts)}


# ------------------------------------------------------------------ chargement


def _fichiers(par_doctype: dict) -> dict:
    """Les pieces jointes des cibles demandees -> {(doctype, nom): [fichier]}."""
    out = {}
    for doctype, noms in par_doctype.items():
        noms = sorted({n for n in noms if n})
        if not noms:
            continue
        for r in frappe.db.sql("""select attached_to_name, file_name, file_url
                                  from `tabFile`
                                  where attached_to_doctype = %(dt)s
                                    and attached_to_name in %(noms)s
                                    and ifnull(file_url, '') != '' order by creation""",
                               {"dt": doctype, "noms": noms}, as_dict=1):
            out.setdefault((doctype, r.attached_to_name), []).append({
                "file_name": r.file_name or r.file_url.rsplit("/", 1)[-1],
                "file_url": r.file_url, "source": LIBELLE_SOURCE.get(doctype, doctype),
                "source_doctype": doctype, "source_name": r.attached_to_name})
    return out


def charger_certificats_lies(factures: list, commandes: list) -> dict:
    """Les certificats TEJ deja rattaches a ces pieces -> {(doctype, nom): [certificat]}.

    ⚠️ SANS FILTRE DE PERIMETRE, contrairement a `charger_certificats`. Un certificat 2025 pose sur
    la meme facture ne se comptabilise pas, mais il explique ce que le justificatif contient — et
    l'ignorer ferait comparer une piece a du vide.
    """
    champs = ["name", "reference", "declarant", "customer", "montant_retenue", "date_paiement",
              "match_status", "payment_entry", "sales_invoice", "sales_order", "hors_perimetre"]
    out = {}
    for champ, doctype, noms in (("sales_invoice", "Sales Invoice", factures),
                                 ("sales_order", "Sales Order", commandes)):
        noms = sorted({n for n in noms if n})
        if not noms:
            continue
        for c in frappe.db.get_all(DOCTYPE, filters={champ: ["in", noms]}, fields=champs,
                                   limit_page_length=0):
            out.setdefault((doctype, c[champ]), []).append(c)
    return out


def charger_pieces(depuis=None, jusqu_a=None) -> list:
    """Les ecritures de retenue de vente qu'AUCUN certificat ne porte.

    L'exclusion se lit sur le certificat (`payment_entry`), jamais sur l'ecriture : c'est le
    certificat qui designe sa piece, et lui seul.

    Chaque ecriture repart avec ce que porte la creance qu'elle eteint : sa facture ET sa commande,
    leurs pieces jointes, et les certificats TEJ deja rattaches a l'une ou a l'autre. C'est cette
    lecture-la qui permet de conclure « justifie hors portail » plutot que « sans certificat ».
    """
    mode = R.mode_ras()
    depuis = depuis or "%s-01-01" % C.ANNEE_MINIMALE
    conditions, valeurs = ["pe.docstatus = 1", "pe.mode_of_payment = %(mode)s",
                           "pe.posting_date >= %(depuis)s"], {"mode": mode, "depuis": depuis}
    if jusqu_a:
        conditions.append("pe.posting_date <= %(jusqu_a)s")
        valeurs["jusqu_a"] = getdate(jusqu_a)
    rows = frappe.db.sql("""select pe.name, pe.party as customer, pe.posting_date, pe.paid_amount,
                                   pe.reference_no
                            from `tabPayment Entry` pe
                            where %s order by pe.posting_date""" % " and ".join(conditions),
                         valeurs, as_dict=1)
    prises = {r.payment_entry for r in frappe.db.get_all(
        DOCTYPE, filters={"payment_entry": ["is", "set"]}, fields=["payment_entry"],
        limit_page_length=0)}
    orphelines = [r for r in rows if r.name not in prises]
    if not orphelines:
        return []

    noms = [o.name for o in orphelines]
    factures, commandes = {}, {}
    for r in frappe.db.sql("""select parent, reference_name, reference_doctype
                              from `tabPayment Entry Reference`
                              where parent in %(noms)s and allocated_amount != 0""",
                           {"noms": noms}, as_dict=1):
        if r.reference_doctype == "Sales Invoice":
            factures.setdefault(r.parent, []).append(r.reference_name)
        elif r.reference_doctype == "Sales Order":
            commandes.setdefault(r.parent, []).append(r.reference_name)

    # La commande derriere la facture. Tant qu'une commande n'est pas facturee, `tej/pdf.cible`
    # range le certificat SUR ELLE ; ne lire que la facture rendrait ce justificatif invisible.
    toutes_factures = sorted({f for v in factures.values() for f in v})
    par_facture = {}
    if toutes_factures:
        for r in frappe.db.sql("""select distinct parent, sales_order
                                  from `tabSales Invoice Item`
                                  where parent in %(f)s and ifnull(sales_order, '') != ''""",
                               {"f": toutes_factures}, as_dict=1):
            par_facture.setdefault(r.parent, []).append(r.sales_order)
    for pe, fs in factures.items():
        for f in fs:
            for so in par_facture.get(f, []):
                if so not in commandes.setdefault(pe, []):
                    commandes[pe].append(so)

    toutes_commandes = sorted({c for v in commandes.values() for c in v})
    fichiers = _fichiers({"Sales Invoice": toutes_factures, "Sales Order": toutes_commandes,
                          "Payment Entry": noms})
    lies = charger_certificats_lies(toutes_factures, toutes_commandes)

    out = []
    for o in orphelines:
        fs, cs = factures.get(o.name) or [], commandes.get(o.name) or []
        cibles = ([("Sales Invoice", f) for f in fs] + [("Sales Order", c) for c in cs]
                  + [("Payment Entry", o.name)])
        justificatifs = [j for cle in cibles for j in fichiers.get(cle, [])]
        certs = {}
        for cle in cibles:
            for c in lies.get(cle, []):
                certs.setdefault(c["name"], c)
        out.append({"name": o.name, "customer": o.customer, "posting_date": o.posting_date,
                    "paid_amount": flt(o.paid_amount, 3), "reference_no": o.reference_no,
                    "sales_invoice": (fs or [None])[0], "sales_order": (cs or [None])[0],
                    "factures": fs, "commandes": cs, "justificatifs": justificatifs,
                    "certificats_lies": list(certs.values())})
    return out


def charger_certificats() -> list:
    return frappe.db.get_all(
        DOCTYPE, filters={"hors_perimetre": 0},
        fields=["name", "reference", "declarant", "customer", "montant_retenue", "date_paiement",
                "match_status", "payment_entry"], limit_page_length=0)


def inventaire(depuis=None, jusqu_a=None) -> dict:
    """Point d'entree : les orphelines, leur verdict et la synthese."""
    lignes = apparier(charger_pieces(depuis, jusqu_a), charger_certificats())
    return {"lignes": lignes, "synthese": synthese(lignes)}
