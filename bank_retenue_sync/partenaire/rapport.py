"""Le rapport mensuel d'activite du partenaire : rendu en Markdown, pose sur sa fiche client.

⚠️ AUCUN CHIFFRE NE PASSE PAR LE MODELE, ET C'EST TOUT LE PRINCIPE. Ce rapport part chez le
partenaire : il porte l'echeancier qu'on lui reclame, l'absorption qu'on lui applique et le solde
qu'on lui oppose. Chaque nombre y est rendu par `montant()` a partir de la donnee source —
`economiq.tableau`, `echeancier.consolider`, `imputer` — c'est-a-dire exactement ce que l'ecran
affiche. Un modele qui retape ces nombres finit par en changer un, et personne ne s'en apercevrait
avant que le partenaire ne conteste.

OpenAI n'ecrit donc que ce qu'aucun calcul ne produit : l'explication de l'absorption, la phrase
des echeances couvertes, la projection de l'avance, et une lecture du mois. Le reste est du rendu.

⚠️ ET MEME LA REDACTION EST VERIFIEE. `prose_sure` relit chaque phrase rendue par le modele et
rejette celle qui cite un nombre absent des donnees : la phrase deterministe reprend alors sa
place. Un garde-fou qui laisse passer un chiffre invente ne garde rien.

⚠️ RIEN N'EST COMPTABILISE ICI. Le rapport est un commentaire sur la fiche client : il n'ecrit ni
piece, ni echeance, ni historique. Le regenerer ne change aucun montant du. Le mois, lui, doit
avoir ete arrete par l'ecran (`historique.enregistrer`) pour que le rapport dise la meme chose que
l'echeancier deja communique.
"""
from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import flt

from bank_retenue_sync.facturation import periode
from bank_retenue_sync.partenaire import amorce, echeancier, encaissements, historique
from bank_retenue_sync.partenaire import economiq as M_economiq

PRECISION = 3

CLIENT = M_economiq.CLIENT

#: Le marqueur qui identifie le rapport d'un mois dans les commentaires de la fiche. Invisible a
#: l'ecran (commentaire HTML), il permet de RETROUVER le rapport du mois pour le remplacer : sans
#: lui, regenerer empilerait cinq versions contradictoires sur la meme fiche.
MARQUEUR = "<!-- brs-rapport-economiq:%s -->"


# ------------------------------------------------------------------ rendu des nombres


def montant(v) -> str:
    """9183.72 -> « 9 183,720 ». Trois decimales, virgule decimale, espace de milliers.

    Fonction pure. C'est le SEUL endroit ou un nombre devient du texte dans ce rapport.
    """
    s = "%0.3f" % round(float(v or 0), PRECISION)
    signe, s = ("-", s[1:]) if s.startswith("-") else ("", s)
    entier, decimales = s.split(".")
    groupes = []
    while len(entier) > 3:
        groupes.insert(0, entier[-3:])
        entier = entier[:-3]
    groupes.insert(0, entier)
    return "%s%s,%s" % (signe, " ".join(groupes), decimales)


def maj(texte) -> str:
    """« juillet 2026 » -> « Juillet 2026 ». Sans toucher au reste de la chaine.

    `periode.libelle` rend le mois en minuscule — c'est juste en francais courant, mais pas dans un
    titre de document ni en tete de section.
    """
    t = str(texte or "")
    return t[:1].upper() + t[1:]


#: Les mois qui commencent par une voyelle prennent l'elision : « d’avril », « d’août »,
#: « d’octobre ». Sans elle, le rapport annonce « les échéances de août ».
_VOYELLES = "aàâeéèêiîoôuû"


def de(libelle) -> str:
    """« août 2026 » -> « d’août 2026 » ; « juillet 2026 » -> « de juillet 2026 »."""
    t = str(libelle or "").strip()
    if not t:
        return ""
    return ("d’%s" if t[0].lower() in _VOYELLES else "de %s") % t


#: ⚠️ TOUTES LES ESPACES, PAS SEULEMENT LA NORMALE. Un modele qui compose un montant francais y met
#: une espace INSECABLE ou FINE (U+00A0, U+202F, U+2009). Avec la seule espace normale dans cette
#: classe, « 3 021,000 » ne matchait pas en entier : le regex repartait a « 021,000 » et rendait
#: « 3 21,000 » — un montant CORROMPU dans un document destine au partenaire. Vu le 17/08/2026.
_ESPACES = "     "

#: Un montant a trois decimales dans un texte deja compose, avec ou sans separateur de milliers :
#: « 1205.376 », « 3 021.000 », « 52,594 ».
_MONTANT_TEXTE = re.compile(
    r"(?<![\d,.])(\d{1,3}(?:[%s]\d{3})+|\d+)[.,](\d{3})(?!\d)" % _ESPACES)


def franciser(texte) -> str:
    """Reformate les montants d'un texte deja compose. Fonction pure. Ne recalcule rien.

    Deux textes en ont besoin, pour la meme raison — ils sont justes mais pas au format du rapport :

    - le detail du consolide, fabrique par `echeancier.consolider` avec des `%.3f` (« 1205.376 ») ;
    - la prose du modele, qui ecrit volontiers « 894.000 TND » au point decimal. Observe le
      17/08/2026 : quatre montants au point dans un document ou tous les autres sont a la virgule.

    Les dates (« 2026-04-30 »), les references (« ACC-PAY-2026-05502 ») et les abreviations
    (« ajust. ») ne portent pas de decimales a trois chiffres et restent intactes.
    """
    def _remplacer(m):
        entier = m.group(1)
        for espace in _ESPACES:
            entier = entier.replace(espace, "")
        return montant(float("%s.%s" % (entier, m.group(2))))

    return _MONTANT_TEXTE.sub(_remplacer, str(texte or ""))


# ------------------------------------------------------------------ les donnees du mois


def donnees(mois: str = None) -> dict:
    """Tout ce que le rapport dit, avant d'etre ecrit. -> dict.

    Ne parle ni a OpenAI ni a personne : c'est la meme lecture que l'ecran, rassemblee. Sert aussi
    d'apercu — on peut voir le rapport sans depenser un appel au modele.
    """
    mois = periode.normaliser(mois)
    annee, numero = periode.eclater(mois)
    tableau = M_economiq.tableau(mois)
    if not tableau.get("disponible"):
        frappe.throw(tableau.get("message") or _("Bilan indisponible pour ce mois."))

    debut, fin = tableau["periode"]["debut"], tableau["periode"]["fin"]

    # ⚠️ LE BRUT EST RECALCULE, PAS RELU. C'est le point de depart du raisonnement (« total des
    # commandes divise par trois ») et il doit apparaitre AVANT absorption ; l'echeancier stocke,
    # lui, est deja ajuste. Les deux figurent au rapport, et c'est leur ecart qui s'explique.
    brut = echeancier.brut(tableau["total_commandes"], annee, numero)

    versements = encaissements.recus(amorce.DEPUIS)
    lignes, avance = echeancier.imputer(
        echeancier.consolider([m for m in historique.tous() if m]), versements)

    # Les reglements du MOIS, et les echeances que ces reglements-la ont eteintes. L'imputation est
    # globale (la dette la plus ancienne d'abord) : on ne la refait pas, on la relit.
    paiements = [v for v in versements if debut <= (v.get("date") or "") <= fin]
    pieces_du_mois = {p["payment_entry"] for p in paiements}
    couvertes = []
    for ligne in lignes:
        prises = [r for r in (ligne.get("reglements") or [])
                  if r.get("payment_entry") in pieces_du_mois]
        if prises:
            couvertes.append({"date": ligne["date"], "montant": ligne["montant"],
                              "statut": ligne["statut"],
                              "impute": flt(sum(r.get("impute") or 0 for r in prises), PRECISION)})

    # ⚠️ UN MOIS DE REPRISE N'EST PAS UN MOIS MESURE, ET LE TAIRE EST UN MENSONGE PAR OMISSION.
    # L'amorce inscrit un mois avec un bilan et un total de commandes NULS, et des echeances qui
    # sont le report des mois anterieurs. Le mois enregistre fait foi — on ne recalcule donc rien —
    # mais imprimer « Ventes 0,000 · Benefice 0,000 » sous un tableau qui montre une commande de
    # 9 183,720 TND ferait passer une reprise pour un mois sans activite. Juin 2026 est ce cas.
    total_reel = flt(sum(c["total"] for c in tableau["commandes"]), PRECISION)
    reserves = []
    if tableau["enregistre"] and not flt(tableau["total_commandes"]) and total_reel:
        reserves.append(
            _("Le mois enregistré porte un total de commandes nul, alors que {0} commande(s) "
              "totalisant {1} TND figurent en base. Ce mois a été repris (amorce) : son "
              "échéancier est un report, pas un étalement de ses commandes.")
            .format(len(tableau["commandes"]), montant(total_reel)))
    if tableau["enregistre"] and not any(
            flt((tableau["bilan"] or {}).get(cle, {}).get("benefice"))
            for cle in ("aqua", "partenaire")):
        reserves.append(
            _("Le bilan d’activité enregistré pour ce mois est nul : il n’a pas été mesuré. "
              "L’ajustement de {0} TND ne provient donc pas d’un bilan.")
            .format(montant(tableau["ajustement"])))

    # ⚠️ L'EXCEDENT NON IMPUTE N'EST PAS LA SEULE AVANCE, ET LE CROIRE FAIT MENTIR LE §6. `imputer`
    # rend ce qui n'a trouve AUCUNE echeance a eteindre ; un reglement qui deborde sur une echeance
    # FUTURE, lui, est deja impute — donc invisible dans cet excedent, alors que c'est exactement
    # ce que l'app appelle une avance (l'amorce ecrit « avance 28,210 (excedent du reglement de
    # juin) »). Le 17/08/2026 : 52,594 dorment sur l'echeance du 31/08 et le rapport annonçait
    # « Avance Reportée : 0,000 TND », en contredisant son propre tableau consolide.
    avances_futures = []
    for ligne in lignes:
        if (ligne.get("date") or "") <= fin or not flt(ligne.get("paye")):
            continue
        avances_futures.append({
            "date": ligne["date"], "montant": flt(ligne.get("paye"), PRECISION),
            "echeance": flt(ligne.get("montant"), PRECISION),
            "reste": flt(ligne.get("reste"), PRECISION),
            # La date du reglement compte : dans un rapport de juillet, une avance peut venir d'un
            # versement d'aout. La taire ferait chercher ce montant dans le §4, qui ne l'a pas.
            "reglements": [{"payment_entry": r.get("payment_entry"), "date": r.get("date"),
                            "impute": flt(r.get("impute"), PRECISION)}
                           for r in (ligne.get("reglements") or [])],
        })
    total_avances_futures = flt(sum(a["montant"] for a in avances_futures), PRECISION)

    # ⚠️ LE MOIS CALENDAIRE NE REPOND PAS A « QU'A-T-ON RECU ENTRE DEUX ECHEANCES ? ». Le §4 ne
    # montre que les reglements du mois du rapport ; or les echeances tombent en fin de mois et un
    # reglement arrive n'importe quand. Chaque fenetre court du lendemain de l'echeance precedente
    # (la premiere part de l'ancrage) a l'echeance courante, incluse ; ce qui arrive apres la
    # derniere echeance forme une fenetre ouverte. Les fenetres couvrent TOUS les versements de
    # `recus` : un paiement hors fenetre serait un paiement que le rapport tait.
    fenetres = []
    borne = amorce.DEPUIS
    for ligne in lignes:  # `consolider` trie deja par date
        date_ech = ligne["date"]
        pris = [v for v in versements if borne <= (v.get("date") or "") <= date_ech]
        fenetres.append({"de": borne, "a": date_ech,
                         "echeance": flt(ligne.get("montant"), PRECISION),
                         "paiements": pris,
                         "total": flt(sum(p["montant"] for p in pris), PRECISION)})
        borne = max(borne, str(frappe.utils.add_days(date_ech, 1)))
    apres = [v for v in versements if (v.get("date") or "") >= borne]
    if apres:
        fenetres.append({"de": borne, "a": None, "echeance": None, "paiements": apres,
                         "total": flt(sum(p["montant"] for p in apres), PRECISION)})
    total_recu = flt(sum(v["montant"] for v in versements), PRECISION)

    total_paiements = flt(sum(p["montant"] for p in paiements), PRECISION)
    total_couvert = flt(sum(c["impute"] for c in couvertes), PRECISION)
    annee_s, numero_s = periode.suivant(annee, numero)

    return {
        "mois": mois,
        "libelle": maj(tableau["libelle"]),
        "nom_du_mois": maj((tableau["libelle"] or "").split(" ")[0]),
        "mois_suivant": maj(periode.libelle(periode.cle(annee_s, numero_s))),
        "periode": {"debut": debut, "fin": fin},
        "client": CLIENT,
        "enregistre": tableau["enregistre"],
        "valide": tableau["valide"],
        "reserves": reserves,
        "commandes": tableau["commandes"],
        "totaux_commandes": tableau["totaux_commandes"],
        # ⚠️ « DETTE NON PAYEE » EST DE L'ARGENT QUI N'EST PAS RENTRE, et le rapport n'en disait
        # rien. Une commande peut etre soldee par une piece qui ne constate AUCUN encaissement —
        # portee au compte des dettes, ou en perte. L'ecran le montre colonne par colonne ; le
        # rapport n'affichait que le total, et sur juillet 2026 il taisait ainsi 1 148,056 TND.
        "commandes_en_dette": [c for c in tableau["commandes"] if flt(c.get("non_paye"))],
        "total_commandes": tableau["total_commandes"],
        "total_commandes_reel": total_reel,
        "tiers": flt(flt(tableau["total_commandes"]) / 3, PRECISION),
        "echeancier_brut": brut,
        "bilan": tableau["bilan"],
        "charges_libres": tableau["charges_libres"],
        "total_charges": tableau["total_charges"],
        "solde_net": tableau["solde_net"],
        "ajustement": tableau["ajustement"],
        "journal_entry": tableau.get("journal_entry"),
        "echeancier_corrige": tableau["echeancier"],
        "report": tableau["report"],
        "paiements": paiements,
        "total_paiements": total_paiements,
        "paiements_entre_echeances": fenetres,
        "total_recu": total_recu,
        "depuis": amorce.DEPUIS,
        "echeances_couvertes": couvertes,
        "total_couvert": total_couvert,
        "avance": avance,
        "avances_futures": avances_futures,
        "total_avances_futures": total_avances_futures,
        "consolide": lignes,
    }


# ------------------------------------------------------------------ la redaction


def prose_deterministe(d: dict) -> dict:
    """Les phrases que le rapport porte SANS modele. -> dict.

    Ce sont elles qui partent si OpenAI est indisponible, et elles qui reprennent leur place si le
    modele cite un nombre qui n'existe pas. Un rapport sans commentaire vaut mieux qu'un rapport
    dont on ne peut pas verifier le commentaire.
    """
    absorbees = [e for e in (d["echeancier_corrige"] or []) if flt(e.get("deduit"))]
    if not absorbees:
        absorption = "Aucune absorption ce mois-ci : l’ajustement est nul."
    else:
        absorption = ("Le montant de %s TND a été absorbé pour réduire l’échéance du %s."
                      % (montant(d["ajustement"]), absorbees[0].get("date") or ""))
    if not d["echeances_couvertes"]:
        couvertes = "Aucune échéance couverte ce mois-ci."
    else:
        couvertes = " ; ".join(
            "échéance du %s : %s TND imputés" % (c["date"], montant(c["impute"]))
            for c in d["echeances_couvertes"])
    # ⚠️ NE PROMETS PAS UNE AVANCE QUI N'EXISTE PAS. « Sera utilisée en priorité… » sous un montant
    # nul contredit le tableau consolide, ou une avance peut deja etre POSEE sur une echeance a
    # venir — ce qui n'est pas la meme chose qu'un excedent disponible.
    if flt(d["avance"]):
        projection = ("sera utilisée en priorité pour couvrir les échéances %s"
                      % de(d["mois_suivant"]))
    elif flt(d.get("total_avances_futures")):
        projection = ("aucun excédent ne reste disponible : %s TND sont déjà imputés sur les "
                      "échéances à venir (voir ci-dessous)"
                      % montant(d["total_avances_futures"]))
    else:
        projection = "aucune avance à reporter sur le mois suivant"
    return {"absorption": absorption, "echeances_couvertes": couvertes,
            "projection": projection, "lecture": []}


CHAMPS_PROSE = ("absorption", "echeances_couvertes", "projection")

_NOMBRE_LU = re.compile(r"\d[\d%s]*(?:[.,]\d+)?" % _ESPACES)

#: ⚠️ CE QU'ON RETIRE AVANT DE LIRE LES MONTANTS, PARCE QU'UNE LISTE BLANCHE NE SUFFIT PAS. La
#: version precedente autorisait tous les entiers de 1 a 31 « puisque ce sont des jours » : elle
#: laissait donc passer n'importe quel petit nombre. C'est ainsi qu'un « 3 21,000 » corrompu a
#: survecu au controle le 17/08/2026 — 21 etait un jour valide. On efface donc les formes qui ne
#: sont PAS des montants, et tout ce qui reste doit se retrouver dans les donnees du mois.
#: ⚠️ LES REFERENCES AVANT LES DATES, ET L'ORDRE EST LE BUG. « ACC-PAY-2026-05502 » contient
#: « 2026-05 » : nettoyer les dates d'abord laissait un « 502 » orphelin, lu comme un montant.
_NON_MONTANTS = (
    re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b"),     # ACC-PAY-2026-05502, SAL-ORD-…, IN260052
    # Les references collees : « TT2621295QTD », « FT26209ZZCTH » — des lettres puis des chiffres,
    # sans separateur. Un montant du rapport porte TOUJOURS trois decimales, jamais cette forme.
    re.compile(r"\b[A-Za-z]{1,6}\d[\dA-Za-z]*\b"),
    re.compile(r"\bN\s*:\s*\d+"),                         # « Aramex N: 51330111766 »
    re.compile(r"\d{4}-\d{2}(?:-\d{2})?"),               # 2026-07-31, 2026-07
    re.compile(r"\b(?:19|20)\d{2}\b"),                   # une annee seule : « Août 2026 »
    re.compile(r"\bM\s*\+\s*\d\b"),                     # les notes d'echeance M+1, M+2
    re.compile(r"§\s*\d"),                                # renvoi de section
)


def nombres(texte) -> set:
    """Les MONTANTS qu'un texte affirme, normalises. Fonction pure.

    Les separateurs de milliers — espace normale, insecable ou fine — et la virgule decimale
    disparaissent : « 9 183,720 », « 9183.72 » et « 9 183,72 » designent le meme montant, et un
    garde-fou qui les distinguerait rejetterait des phrases justes.
    """
    propre = str(texte or "")
    for motif in _NON_MONTANTS:
        propre = motif.sub(" ", propre)
    out = set()
    for brut in _NOMBRE_LU.findall(propre):
        compact = brut
        for espace in _ESPACES:
            compact = compact.replace(espace, "")
        compact = compact.replace(",", ".").rstrip(".")
        if not compact:
            continue
        try:
            out.add(round(float(compact), PRECISION))
        except ValueError:
            continue
    return out


def valeurs_autorisees(d: dict) -> set:
    """Tous les nombres que les donnees du mois autorisent a citer. Fonction pure."""
    out = {0.0}
    for v in (d["total_commandes"], d.get("total_commandes_reel"), d["tiers"], d["solde_net"],
              d["ajustement"], d["total_charges"], d["total_paiements"], d["total_couvert"],
              d["avance"], d.get("total_avances_futures"), d["report"]):
        out.add(round(flt(v), PRECISION))
    for section in (d["bilan"] or {}).values():
        for cle in ("ventes", "achats", "benefice"):
            out.add(round(flt(section.get(cle)), PRECISION))
    for suite in (d["echeancier_brut"], d["echeancier_corrige"]):
        for e in suite or []:
            out.add(round(flt(e.get("montant")), PRECISION))
            out.add(round(flt(e.get("deduit")), PRECISION))
    for c in d["commandes"] or []:
        for cle in ("total", "encaisse", "non_paye", "restant", "diminue_bilan"):
            out.add(round(flt(c.get(cle)), PRECISION))
    for cle in ("total", "encaisse", "non_paye", "restant", "nombre"):
        out.add(round(flt((d.get("totaux_commandes") or {}).get(cle)), PRECISION))
    for c in d["charges_libres"] or []:
        out.add(round(flt(c.get("montant")), PRECISION))
    for p in d["paiements"] or []:
        out.add(round(flt(p.get("montant")), PRECISION))
    for f in d.get("paiements_entre_echeances") or []:
        out.add(round(flt(f.get("total")), PRECISION))
        out.add(round(flt(f.get("echeance")), PRECISION))
        for p in f.get("paiements") or []:
            out.add(round(flt(p.get("montant")), PRECISION))
    out.add(round(flt(d.get("total_recu")), PRECISION))
    for c in d["echeances_couvertes"] or []:
        out.add(round(flt(c.get("impute")), PRECISION))
        out.add(round(flt(c.get("montant")), PRECISION))
    for l in d["consolide"] or []:
        for cle in ("montant", "paye", "reste"):
            out.add(round(flt(l.get(cle)), PRECISION))
        # Les apports du detail sont eux aussi des donnees : `consolider` les compose depuis les
        # echeances des mois enregistres. Les omettre ferait crier le garde-fou sur le §5 lui-meme.
        out.update(nombres(franciser(l.get("detail"))))
    for a in d.get("avances_futures") or []:
        for cle in ("montant", "echeance", "reste"):
            out.add(round(flt(a.get(cle)), PRECISION))
        for r in a.get("reglements") or []:
            out.add(round(flt(r.get("impute")), PRECISION))
    # Le diviseur de l'echeancier : « ÷ 3 » est ecrit en clair dans le rapport.
    out.add(3.0)
    # ⚠️ AUCUNE TOLERANCE POUR LES « PETITS NOMBRES ». Les dates, references, annees et notes M+n
    # sont retirees du texte par `nombres()` AVANT lecture : il n'y a donc plus rien a leur
    # concéder ici. Les autoriser en bloc — c'etait le cas des jours 1 a 31 — revenait a laisser
    # passer n'importe quel montant a deux chiffres.
    return out


#: Le rappel « Avance Reportée : 0,000 TND — » que le rendu ecrit DEJA, et que le modele recopie
#: volontiers puisque la consigne le lui montre.
_PREFIXE_AVANCE = re.compile(r"^\s*avance\s+report\w*\s*:?\s*[^—–-]*[—–-]?\s*", re.IGNORECASE)


def sans_prefixe_avance(texte) -> str:
    """Enleve le rappel « Avance Reportée : X TND — » quand le modele l'a recopie. Fonction pure.

    ⚠️ LA COPIE N'EST PAS INOFFENSIVE : elle porte le montant a SA facon. Observe le 17/08/2026 —
    « Avance Reportée : 0,000 TND — Avance Reportée : 0.000 TND — aucune avance disponible » : le
    meme montant deux fois, dont une au point decimal, dans un document ou tous les autres sont a
    la virgule. Une phrase qui ne commence pas par ce rappel n'est pas touchee.
    """
    brut = str(texte or "").strip()
    nettoye = _PREFIXE_AVANCE.sub("", brut).strip()
    return nettoye or brut


def prose_sure(proposee: dict, secours: dict, autorises: set) -> dict:
    """Garde ce que le modele a ecrit, sauf s'il cite un nombre qui n'existe pas. Fonction pure.

    ⚠️ CHAMP PAR CHAMP, PAS EN BLOC. Une projection fantaisiste ne doit pas faire perdre une
    explication d'absorption correcte — et l'inverse. Chaque phrase se juge seule.

    ⚠️ ET ON JUGE LE TEXTE FINAL, PAS CELUI DU MODELE. Entre les deux il y a `franciser`, qui
    reecrit les montants au format du rapport ; le 17/08/2026 il a transforme « 3 021,000 » en
    « 3 21,000 » sur une espace fine, et un controle fait seulement en entree n'a rien vu passer.
    Toute phrase dont la reecriture fait apparaitre un montant sans source est rejetee.

    Rend aussi `_rejetes` : les champs ou la phrase du modele n'a pas ete retenue. Le deduire en
    comparant a la proposition ferait passer un simple reformatage pour un rejet.
    """
    out = dict(secours)
    rejetes = []
    for champ in CHAMPS_PROSE:
        valeur = (proposee or {}).get(champ)
        if not valeur or not str(valeur).strip():
            continue
        reecrite = franciser(str(valeur).strip())
        if champ == "projection":
            reecrite = sans_prefixe_avance(reecrite)
        if nombres(valeur) - autorises or nombres(reecrite) - autorises:
            rejetes.append(champ)
            continue
        out[champ] = reecrite
    out["projection"] = sans_prefixe_avance(out.get("projection"))
    lecture = []
    for brute in (proposee or {}).get("lecture") or []:
        if not str(brute).strip():
            continue
        reecrite = franciser(str(brute).strip())
        if nombres(brute) - autorises or nombres(reecrite) - autorises:
            continue
        lecture.append(reecrite)
    out["lecture"] = lecture
    out["_rejetes"] = rejetes
    return out


CONSIGNE = """Tu rédiges les commentaires d'un rapport financier mensuel destiné à un partenaire
commercial tunisien. Les montants sont en dinars tunisiens (TND), à trois décimales.

RÈGLE ABSOLUE : tu ne calcules rien et tu n'inventes aucun nombre. Tu peux citer un montant
uniquement s'il figure tel quel dans les données fournies. Toute phrase citant un nombre absent des
données sera rejetée et remplacée. En cas de doute, écris la phrase sans chiffre.

LE BILAN EST CROISÉ, ET C'EST LE PIÈGE DE VOCABULAIRE À ÉVITER. Chaque section du bilan est
l'activité qu'une société a réalisée POUR L'AUTRE. Le bénéfice de la section « Aqua World » est donc
un bénéfice DÛ À ECONOMIQ AQUA SOLUTIONS, et le bénéfice de la section « Economiq » est un bénéfice
DÛ À AQUA WORLD. N'écris jamais qu'une société « dégage » ou « gagne » le bénéfice de sa section :
c'est l'autre qui en est créditée, et c'est pour cela que le solde vient en déduction de
l'échéancier.

DEUX AVANCES QUI NE SE CONFONDENT PAS. `avance` est l'excédent qui n'a trouvé AUCUNE échéance à
éteindre. `avances_futures` est ce qui est DÉJÀ imputé sur des échéances à venir — ce n'est pas
disponible, c'est déjà posé. Ne promets pas de mobiliser une avance nulle, et n'ignore pas une
avance déjà imputée.

Tu rends un objet JSON avec ces clés, en français, ton factuel et sobre :
- "absorption" : une phrase expliquant ce que l'ajustement du mois a absorbé sur l'échéancier, et
  pourquoi (bilan d'activité + charges portées par Aqua World).
- "echeances_couvertes" : une phrase disant quelles échéances les règlements du mois ont couvertes,
  ou qu'aucune ne l'a été.
- "projection" : la SUITE d'une phrase déjà commencée par « Avance Reportée : X TND — ». Ne répète
  ni les mots « Avance Reportée », ni le montant : commence directement par un verbe ou un adverbe,
  et parle de l'usage de cette avance sur le mois suivant.
- "lecture" : 2 à 4 puces courtes de lecture du mois (tendance des commandes, marge, ponctualité
  des règlements, risque). Aucune recommandation d'investissement, aucun conseil fiscal."""


def rediger(d: dict) -> dict:
    """Demande a OpenAI les phrases du rapport. -> dict de prose, deja verifie.

    Un echec du modele n'est pas un echec du rapport : on retombe sur la prose deterministe et on
    le dit dans le resultat. Le rapport doit pouvoir sortir un jour ou l'API est en panne.
    """
    import json

    secours = prose_deterministe(d)
    autorises = valeurs_autorisees(d)
    try:
        from bank_retenue_sync.ai.invoice_extract import _get_client_model_temp

        client, modele, temperature = _get_client_model_temp()
        params = {
            "model": modele,
            "messages": [
                {"role": "system", "content": CONSIGNE},
                {"role": "user", "content": json.dumps(_pour_le_modele(d), ensure_ascii=False,
                                                       default=str)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
        }
        try:
            reponse = client.chat.completions.create(**params)
        except Exception as e:
            # Certains modeles n'acceptent que la temperature par defaut. Meme repli que
            # `ai.invoice_extract`, pour la meme raison.
            if "temperature" not in str(e).lower():
                raise
            params.pop("temperature", None)
            reponse = client.chat.completions.create(**params)
        propose = json.loads(reponse.choices[0].message.content or "{}")
    except Exception as e:
        frappe.log_error(title="Rapport Economiq : rédaction OpenAI",
                         message=frappe.get_traceback())
        return {**secours, "_modele": None, "_erreur": str(e)[:200]}

    # `prose_sure` dit elle-meme ce qu'elle a refuse : comparer sa sortie a la proposition
    # signalait un rejet des que le reformatage avait change une virgule.
    return {**prose_sure(propose, secours, autorises), "_modele": modele}


def _pour_le_modele(d: dict) -> dict:
    """Ce qu'on montre au modele : les faits, sans les listes qui ne l'aident pas a ecrire."""
    return {cle: d[cle] for cle in (
        "mois", "libelle", "mois_suivant", "total_commandes", "totaux_commandes",
        "commandes_en_dette", "tiers", "bilan", "charges_libres",
        "total_charges", "solde_net", "ajustement", "echeancier_brut", "echeancier_corrige",
        "paiements", "total_paiements", "paiements_entre_echeances", "total_recu",
        "echeances_couvertes", "total_couvert", "avance",
        "avances_futures", "total_avances_futures", "report", "consolide")}


# ------------------------------------------------------------------ le rendu Markdown


def cellule(v) -> str:
    """Le contenu d'une case de tableau Markdown. Fonction pure.

    ⚠️ UN PIPE NON ECHAPPE CASSE LA LIGNE ENTIERE, ET C'EST LE CAS DE LA COLONNE « DETAIL ».
    `echeancier.consolider` compose son detail en joignant les apports par «  |  » : recopie tel
    quel dans une case, chaque separateur ouvre une colonne fantome et le tableau se decale — le
    montant du au partenaire s'affiche alors sous « Statut ». On echappe, on ne remplace pas : le
    texte source sert aussi a l'ecran, ou le pipe est correct.
    """
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")


def _tableau(entetes: list, lignes: list) -> list:
    return (["| %s |" % " | ".join(cellule(e) for e in entetes),
             "|%s|" % "|".join("---" for _e in entetes)]
            + ["| %s |" % " | ".join(cellule(c) for c in cellules) for cellules in lignes])


def _ou_null(v) -> str:
    """« null » quand la valeur est absente — c'est le format du rapport, pas un oubli."""
    return montant(v) if v not in (None, "") else "null"


def rendre_court(d: dict, prose: dict = None) -> str:
    """Le rapport COURT — celui qui part chez le partenaire. Fonction pure.

    ⚠️ POURQUOI UN SECOND RENDU PLUTOT QU'UN RAPPORT ALLEGE. Le rapport complet fait 5 900
    caracteres, 139 lignes et six tableaux, et il deroule ses propres calculs (« Division par 3 :
    5 572,578 / 3 = 1 857,526 ») pour finalement dire au partenaire ce qu'il doit. C'est un
    justificatif, pas un message : on le garde tel quel pour l'ecran et la verification, et c'est
    cette version-ci qu'on lui envoie (demande utilisateur 03/09/2026).

    CE QU'ON GARDE : qui doit quoi ce mois-ci, les deux benefices qui le fondent, l'ajustement,
    les echeances a payer, ce qui a ete recu, et les impayes a signaler. CE QU'ON RETIRE : le
    detail commande par commande, l'echeancier BRUT (un intermediaire de calcul), les fenetres de
    paiement entre echeances, et le deroule des formules.
    """
    p = prose or prose_deterministe(d)
    L = ["# Activité %s — %s" % (de(d["nom_du_mois"]), CLIENT), ""]

    for reserve in d.get("reserves") or []:
        L += ["> ⚠️ %s" % reserve, ""]

    # LE TITRE EST LA CONCLUSION, PAS L'INTRODUCTION. Le partenaire ouvre le message pour savoir
    # ce qu'il doit ; le lui faire deduire de deux benefices et d'un solde net etait le principal
    # defaut du rapport long.
    aqua, part = d["bilan"]["aqua"], d["bilan"]["partenaire"]
    solde = flt(d["solde_net"])
    if abs(solde) < 0.001:
        L += ["**Les deux activités s’équilibrent ce mois-ci.**", ""]
    elif solde > 0:
        L += ["**%s doit %s TND à AQUA WORLD** au titre de l’activité du mois."
              % (CLIENT, montant(solde)), ""]
    else:
        L += ["**AQUA WORLD doit %s TND à %s** au titre de l’activité du mois."
              % (montant(abs(solde)), CLIENT), ""]

    L += _tableau(["Activité", "Ventes (TND)", "Achats (TND)", "Bénéfice (TND)"],
                  [["Aqua World pour %s" % CLIENT, montant(aqua["ventes"]),
                    montant(aqua["achats"]), montant(aqua["benefice"])],
                   ["%s pour Aqua World" % CLIENT, montant(part["ventes"]),
                    montant(part["achats"]), montant(part["benefice"])]])
    L += ["", "- Solde net : **%s TND**" % montant(solde)]
    if d["charges_libres"]:
        # Le champ est `libelle` cote DocType, `label` cote ecran — comme dans le rapport long.
        # Ne lire que `label` affichait « Charges du mois : 250,000 TND () ».
        L += ["- Charges du mois : %s TND (%s)"
              % (montant(d["total_charges"]),
                 ", ".join(franciser(c.get("libelle") or c.get("label") or "sans libellé")
                           for c in d["charges_libres"]))]
    L += ["- **Ajustement retenu : %s TND** — %s" % (montant(d["ajustement"]), p["absorption"])]

    # Les echeances : la seule partie sur laquelle le partenaire a quelque chose A FAIRE.
    L += ["", "## Échéances", ""]
    L += _tableau(["Date", "À payer (TND)", "Note"],
                  [[e["date"], montant(e["montant"]), franciser(e.get("note") or "")]
                   for e in (d["echeancier_corrige"] or [])])

    reste = [l for l in (d["consolide"] or []) if flt(l.get("reste"))]
    if reste:
        L += ["", "Reste dû sur l’ensemble des échéances : **%s TND** (%s)."
              % (montant(sum(flt(l.get("reste")) for l in reste)),
                 ", ".join("%s : %s" % (l["date"], montant(l.get("reste"))) for l in reste))]

    # Les reglements : le partenaire doit pouvoir reconnaitre ce qu'il a envoye.
    L += ["", "## Règlements reçus ce mois", ""]
    if d["paiements"]:
        L += ["- %s TND au total : %s"
              % (montant(d["total_paiements"]),
                 ", ".join("%s TND le %s (%s)" % (montant(x["montant"]), x["date"],
                                                  franciser(x.get("mode") or ""))
                           for x in d["paiements"]))]
    else:
        L += ["- Aucun règlement reçu ce mois."]
    if flt(d["avance"]):
        L += ["- Avance reportée : %s TND — %s" % (montant(d["avance"]), p["projection"])]

    # ⚠️ LES IMPAYES RESTENT, MEME EN VERSION COURTE. Une commande soldee par une piece qui ne
    # constate aucun encaissement est le point qui coute de l'argent : le rapport de juillet 2026
    # les taisait et 1 148,056 TND passaient inapercus.
    if d.get("commandes_en_dette"):
        total = sum(flt(c.get("non_paye")) for c in d["commandes_en_dette"])
        L += ["", "## À signaler", "",
              "- %d commande(s) sur %d ne constatent **aucun encaissement** — %s TND portés en "
              "dette ou en perte." % (len(d["commandes_en_dette"]), len(d["commandes"] or []),
                                      montant(total))]

    L += ["", "*Le détail commande par commande et le calcul complet restent consultables sur "
          "l’écran Partenaire Economiq.*"]
    return "\n".join(L)


def rendre(d: dict, prose: dict = None) -> str:
    """Le rapport complet en Markdown. Fonction pure : ni base, ni reseau, ni modele.

    Sert de JUSTIFICATIF : il est consultable a l'ecran et deroule tous les calculs. Ce n'est plus
    lui qui part chez le partenaire — voir `rendre_court`.
    """
    p = prose or prose_deterministe(d)
    nom = d["nom_du_mois"]
    L = ["# Rapport Financier Mensuel — %s" % d["libelle"], ""]

    # Les reserves d'abord : elles disent comment lire tout ce qui suit.
    for reserve in d.get("reserves") or []:
        L += ["> ⚠️ %s" % reserve, ""]

    # 1 — commandes et echeancier brut
    L += ["## 1. 🛒 Commandes %s — Tableau + Calcul Échéancier Brut" % nom, "",
          "### Tableau des Commandes", ""]
    if d["commandes"]:
        totaux = d.get("totaux_commandes") or {}
        L += _tableau(
            ["ID", "Date", "Statut", "Total TND", "Encaissé", "Non payé", "Restant"],
            [[c["sales_order"], c["date"], _(c.get("statut") or ""), montant(c["total"]),
              montant(c.get("encaisse")), montant(c.get("non_paye")), montant(c.get("restant"))]
             for c in d["commandes"]]
            # La ligne de total : sans elle, le lecteur additionne sept colonnes a la main pour
            # verifier le montant qui sert de base a l'echeancier.
            + [["**%d commande(s)**" % (totaux.get("nombre") or len(d["commandes"])), "", "",
                "**%s**" % montant(totaux.get("total") or d["total_commandes"]),
                "**%s**" % montant(totaux.get("encaisse")),
                "**%s**" % montant(totaux.get("non_paye")),
                "**%s**" % montant(totaux.get("restant"))]])
        # ⚠️ CE QUE « NON PAYE » VEUT DIRE, ECRIT NOIR SUR BLANC. Ce n'est pas « en retard » :
        # c'est une commande soldee par une piece qui ne constate aucun encaissement. Sans cette
        # phrase, la colonne se lit comme un simple retard de tresorerie.
        if d.get("commandes_en_dette"):
            L += ["",
                  "- *Dette non payée* : %d commande(s) sur %d sont réglées par une pièce qui ne "
                  "constate aucun encaissement (dette portée au compte des dettes, ou perte de "
                  "paiement) — %s TND au total."
                  % (len(d["commandes_en_dette"]), len(d["commandes"]),
                     montant((d.get("totaux_commandes") or {}).get("non_paye")))]
            for c in d["commandes_en_dette"]:
                modes = ", ".join(sorted({r.get("mode") or "" for r in (c.get("reglements") or [])
                                          if not r.get("paye")}))
                L += ["   - %s : %s TND non encaissés sur %s TND%s"
                      % (c["sales_order"], montant(c.get("non_paye")), montant(c.get("total")),
                         " — %s" % modes if modes else "")]
        # ⚠️ UNE COMMANDE DIMINUEE PAR LE BILAN DOIT LE DIRE ICI, SOUS LE TABLEAU QUI INTRIGUE.
        # L'ecriture de bilan credite les Debiteurs en reference a la commande : une part du
        # total n'est plus reclamee, sans qu'aucun encaissement n'existe. Sans cette phrase, le
        # lecteur additionne Encaissé + Non payé, ne retombe pas sur le Total, et conclut a une
        # erreur — juillet 2026 : 412,630 d'ecart muet sur SAL-ORD-2026-02304.
        diminuees = [c for c in d["commandes"] if flt(c.get("diminue_bilan"))]
        if diminuees:
            L += ["",
                  "- *Diminution par le bilan* : %d commande(s) ont une part absorbée par "
                  "l’écriture de bilan d’activité — cette part n’est plus réclamée, elle est "
                  "déduite par le bilan, pas encaissée." % len(diminuees)]
            for c in diminuees:
                pieces = ", ".join(c.get("pieces_bilan") or [])
                L += ["   - %s : diminuée de %s TND sur %s TND%s"
                      % (c["sales_order"], montant(c.get("diminue_bilan")),
                         montant(c.get("total")),
                         " — écriture %s" % pieces if pieces else "")]
    else:
        L += ["| (Aucune commande ce mois-ci) |", "|---|"]
    L += ["", "### Calcul Échéancier Brut", "",
          "- Total des commandes : %s TND" % montant(d["total_commandes"]),
          "- Division par 3 pour l’échéancier : %s ÷ 3 = %s TND" % (
              montant(d["total_commandes"]), montant(d["tiers"])),
          "", "### Tableau Échéancier Brut", ""]
    L += _tableau(["Date", "Montant (TND)", "Note"],
                  [[e["date"], montant(e["montant"]), e.get("note") or ""]
                   for e in d["echeancier_brut"]])

    # 2 — bilan et ajustement
    #
    # ⚠️ LE BILAN EST CROISE, ET UN BENEFICE N'APPARTIENT PAS A CELUI QUI LE DEGAGE. Chaque section
    # est l'activite qu'une societe a realisee POUR L'AUTRE : le benefice qu'elle montre est donc
    # DU a l'autre. Ecrire « Bénéfice Aqua World » sans le dire laissait lire l'inverse — un gain
    # d'Aqua World — alors que c'est ce gain-la qui vient EN DEDUCTION de ce que le partenaire doit.
    L += ["", "## 2. 🔧 Ajustement Bilan d’Activité %s" % nom, ""]
    for cle, titre, beneficiaire in (
            ("aqua", "Tableau Bilan Aqua World", CLIENT),
            ("partenaire", "Tableau Bilan Economiq", "AQUA WORLD")):
        section = (d["bilan"] or {}).get(cle) or {}
        L += ["### %s — activité réalisée pour %s" % (titre, beneficiaire), ""]
        L += _tableau(["Ventes (TND)", "Achats (TND)", "Bénéfice (TND)"],
                      [[montant(section.get("ventes")), montant(section.get("achats")),
                        montant(section.get("benefice"))]])
        L += ["", "- Bénéfice dû à %s : %s TND." % (beneficiaire,
                                                    montant(section.get("benefice"))), ""]
    aqua = (d["bilan"] or {}).get("aqua") or {}
    part = (d["bilan"] or {}).get("partenaire") or {}
    L += ["### Calcul Solde Net", "",
          "- Solde net = Bénéfice Aqua World - Bénéfice Economiq = %s - %s = %s TND" % (
              montant(aqua.get("benefice")), montant(part.get("benefice")),
              montant(d["solde_net"])),
          "- Soit le bénéfice dû à %s moins le bénéfice dû à AQUA WORLD." % CLIENT,
          "", "### Tableau Charges Libres", ""]
    if d["charges_libres"]:
        # Le champ est `libelle` cote DocType ; `label` est accepte pour une charge saisie a
        # l'ecran avant enregistrement, ou l'ecran nomme sa colonne autrement.
        L += _tableau(["Label", "Montant (TND)"],
                      [[c.get("libelle") or c.get("label") or "", montant(c.get("montant"))]
                       for c in d["charges_libres"]])
    else:
        L += ["| (Aucune charge libre ce mois-ci) |", "|---|"]
    L += ["", "### Total Ajustement", "",
          "- Total ajustement = Solde net + Total charges = %s + %s = %s TND" % (
              montant(d["solde_net"]), montant(d["total_charges"]), montant(d["ajustement"]))]
    # L'ecriture de bilan prime sur le calcul : quand elle existe, le total ci-dessus est le SIEN,
    # et le taire laisserait croire a une erreur d'addition.
    if d.get("journal_entry"):
        L += ["- Ajustement arrêté par l’écriture de bilan %s." % d["journal_entry"]]

    # 3 — echeancier corrige
    L += ["", "## 3. ✅ Échéancier %s Corrigé" % nom, "",
          "### Tableau Échéancier Corrigé", ""]
    L += _tableau(["Date", "Montant (TND)", "Note", "Déduit (TND)"],
                  [[e["date"], montant(e["montant"]), e.get("note") or "",
                    montant(e.get("deduit"))] for e in d["echeancier_corrige"]])
    L += ["", "- *Explication de l’absorption* : %s" % p["absorption"]]
    if flt(d["report"]):
        L += ["- *Report* : %s TND — l’ajustement dépasse l’échéancier du mois ; le surplus "
              "descend sur les échéances les plus anciennes du consolidé" % montant(d["report"])]

    # 4 — paiements recus
    L += ["", "## 4. 💰 Paiements Reçus", "", "### Tableau des Paiements Reçus", ""]
    if d["paiements"]:
        L += _tableau(["Référence", "Mode", "Montant (TND)", "Date"],
                      [[p_.get("reference") or p_.get("payment_entry") or "", p_.get("mode") or "",
                        montant(p_.get("montant")), p_.get("date") or ""]
                       for p_ in d["paiements"]])
    else:
        L += ["| (Aucun paiement reçu ce mois-ci) |", "|---|"]

    # Le meme argent, decoupe autrement : fenetre par fenetre entre deux echeances. Le tableau
    # ci-dessus repond a « qu'a-t-on recu ce mois-ci ? », celui-ci a « qu'a-t-on recu entre deux
    # echeances ? » — c'est la question que le partenaire pose a chaque date de versement.
    fenetres = d.get("paiements_entre_echeances") or []
    if fenetres:
        L += ["", "### Paiements reçus entre les échéances", "",
              "Chaque fenêtre court du lendemain de l’échéance précédente (la première part de "
              "l’ancrage du %s) à l’échéance indiquée, incluse." % (d.get("depuis") or "")]
        for f in fenetres:
            if f.get("a"):
                titre = "Du %s au %s — échéance du %s" % (f["de"], f["a"], f["a"])
            elif any(w.get("a") for w in fenetres):
                titre = "Depuis le %s — après la dernière échéance" % f["de"]
            else:
                # Aucune échéance au consolidé : « après la dernière échéance » mentirait.
                titre = "Depuis le %s (ancrage)" % f["de"]
            if not f.get("paiements"):
                L += ["", "**%s** : aucun paiement reçu." % titre]
                continue
            L += ["", "**%s** — %d paiement(s), total %s TND :" % (
                titre, len(f["paiements"]), montant(f["total"])), ""]
            L += _tableau(["Référence", "Mode", "Montant (TND)", "Date"],
                          [[p_.get("reference") or p_.get("payment_entry") or "",
                            p_.get("mode") or "", montant(p_.get("montant")), p_.get("date") or ""]
                           for p_ in f["paiements"]])
        L += ["", "- *Total reçu depuis l’ancrage du %s* : %s TND"
              % (d.get("depuis") or "", montant(d.get("total_recu")))]

    # 5 — consolide
    L += ["", "## 5. 📊 État Consolidé Final", "", "### Tableau État Consolidé", ""]
    L += _tableau(["Date", "Dû (TND)", "Payé (TND)", "Reste (TND)", "Statut", "Détail"],
                  [[l["date"], montant(l["montant"]), _ou_null(l.get("paye")),
                    _ou_null(l.get("reste")), l.get("statut") or "",
                    franciser(l.get("detail"))] for l in d["consolide"]])

    # 6 — avance reportee
    L += ["", "## 6. 🧾 Calcul de l’Avance Reportée", "",
          "1. *Total des paiements reçus* : %s TND" % montant(d["total_paiements"]),
          "2. *Échéances couvertes par ces paiements* : %s" % p["echeances_couvertes"],
          "3. *Formule finale* : Total paiements - somme des échéances couvertes = avance",
          "   - %s - %s = %s TND" % (montant(d["total_paiements"]), montant(d["total_couvert"]),
                                     montant(d["avance"])),
          "4. *Avance Reportée : %s TND* — %s" % (montant(d["avance"]), p["projection"])]

    # ⚠️ UNE AVANCE DEJA POSEE SUR UNE ECHEANCE A VENIR N'EST PAS DANS LA FORMULE CI-DESSUS, ET
    # L'OMETTRE FAISAIT SE CONTREDIRE LE RAPPORT : le §5 montrait 52,594 payés sur le 31/08 pendant
    # que le §6 annonçait « Avance Reportée : 0,000 TND ». La date du règlement est rappelée : dans
    # un rapport de juillet, une avance peut venir d'un versement d'août, absent du §4.
    if d.get("avances_futures"):
        L += ["5. *Avance déjà imputée sur les échéances à venir (à ce jour)* : %s TND"
              % montant(d["total_avances_futures"])]
        for a in d["avances_futures"]:
            origines = ", ".join(
                "%s du %s pour %s TND" % (r["payment_entry"], r["date"], montant(r["impute"]))
                for r in a["reglements"]) or "reprise"
            L += ["   - échéance du %s : %s TND déjà réglés sur %s TND — %s"
                  % (a["date"], montant(a["montant"]), montant(a["echeance"]), origines)]

    if p.get("lecture"):
        L += ["", "## 7. 🧭 Lecture du mois", ""]
        L += ["- %s" % ligne for ligne in p["lecture"]]

    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ le commentaire


def en_html(texte: str) -> str:
    """Le rapport en HTML — celui qui ira sur la fiche, et donc celui qu'on montre en apercu.

    ⚠️ UN SEUL CONVERTISSEUR, SINON L'APERCU MENT. `frappe.markdown` cote client instancie
    showdown SANS l'extension `tables` : les tableaux du rapport y sortiraient en pipes bruts,
    alors que le commentaire pose, lui, passe par `md_to_html` qui les rend. Deux rendus, deux
    verites — et c'est l'apercu qui aurait tort.
    """
    return frappe.utils.md_to_html(texte or "") or ""


def commentaire_existant(mois: str):
    """Le commentaire deja pose pour ce mois, ou None."""
    return frappe.db.get_value("Comment", {
        "reference_doctype": "Customer", "reference_name": CLIENT,
        "comment_type": "Comment", "content": ["like", "%" + (MARQUEUR % mois) + "%"]}, "name")


def poser_commentaire(mois: str, texte: str) -> dict:
    """Pose le rapport en commentaire sur la fiche du partenaire. -> dict.

    Un commentaire par mois, REMPLACE si le rapport est regenere : deux versions du meme mois sur
    la meme fiche, c'est deux verites, et personne ne saurait laquelle a ete envoyee.
    """
    if not frappe.db.exists("Customer", CLIENT):
        frappe.throw(_("Le client {0} n'existe pas sur ce site.").format(CLIENT))

    contenu = (MARQUEUR % mois) + "\n" + en_html(texte)
    nom = commentaire_existant(mois)
    if nom:
        doc = frappe.get_doc("Comment", nom)
        doc.content = contenu
        doc.flags.ignore_permissions = True
        doc.save()
        return {"comment": doc.name, "statut": "remplacé"}

    doc = frappe.get_doc({
        "doctype": "Comment", "comment_type": "Comment",
        "reference_doctype": "Customer", "reference_name": CLIENT,
        "content": contenu,
        "comment_email": frappe.session.user,
        "comment_by": frappe.utils.get_fullname(frappe.session.user),
    })
    doc.flags.ignore_permissions = True
    doc.insert()
    return {"comment": doc.name, "statut": "créé"}


def generer(mois: str = None, avec_ia: bool = True, poser: bool = True) -> dict:
    """Le geste complet : lire le mois, faire rediger, rendre, poser sur la fiche. -> dict.

    ⚠️ C'EST LA VERSION COURTE QUI EST POSEE. Le rapport part chez le partenaire : il doit se
    lire, pas se dechiffrer (demande utilisateur 03/09/2026). Le rapport complet reste rendu
    dans `markdown_detaille` pour l'ecran et la verification.
    """
    d = donnees(mois)
    prose = rediger(d) if avec_ia else prose_deterministe(d)
    texte = rendre_court(d, prose)
    detaille = rendre(d, prose)
    resultat = {"mois": d["mois"], "libelle": d["libelle"], "client": CLIENT,
                "markdown": texte, "html": en_html(texte),
                "markdown_detaille": detaille, "html_detaille": en_html(detaille),
                "modele": prose.get("_modele"), "rejetes": prose.get("_rejetes") or [],
                "erreur_ia": prose.get("_erreur"), "enregistre": d["enregistre"],
                "avance": d["avance"], "total_commandes": d["total_commandes"]}
    if poser:
        resultat.update(poser_commentaire(d["mois"], texte))
        frappe.db.commit()
    return resultat
