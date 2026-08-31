"""Reglement des dettes fournisseur : du compte d'attente vers la banque, au virement emis.

LE CYCLE VOULU PAR L'UTILISATEUR (identique pour les deux flux)
--------------------------------------------------------------
1. A la reception de la piece (email), l'ecriture credite un compte d'ATTENTE : la charge est
   constatee, la dette existe. Facture Aramex -> `Crediteurs - A&S` (tiers ARAMEX) ;
   note d'honoraire -> `Compte de decouvert bancaire - A&S`.
2. Au virement emis du meme montant, cette ecriture est **annulee puis supprimee**, et une
   NOUVELLE est creee creditant la banque, portant la reference bancaire du virement.

L'etape 1 est donc TRANSITOIRE. C'est ce qui explique que les factures Aramex ET les notes
d'honoraire reelles creditent toutes Zitouna : on n'observe jamais que l'etat final.
Verifie : la note « 05-06-2026 » (463,720) porte la reference FT26210FTJJH, qui est bien un
`VIR TN AUTRE BQ` de 463,720 au 30/07/2026.

DEUX INVARIANTS A NE PAS PERDRE
-------------------------------
1. **La date de comptabilisation ne bouge JAMAIS** : elle reste le dernier jour du mois de la
   facture, pas la date du virement. Verifie sur le reel : facture de juin postee au 30/06,
   virement du 09/07, ecriture toujours au 30/06. C'est l'inverse de la regle des encaissements,
   ou c'est la date bancaire qui prime — ne pas confondre les deux.
2. **La piece jointe suit** : le PDF de la facture est attache a l'ecriture de l'etape 1. La
   supprimer sans recopier ses fichiers perdrait le justificatif.

/!\ LE LIBELLE BANCAIRE NE DIT PAS « ARAMEX »
---------------------------------------------
Le virement emis apparait au releve en **`VIR TN AUTRE BQ`**, sans nom de beneficiaire — comme les
salaires. Le MONTANT est le seul discriminant. On ne se contente donc pas de lui : le rapprochement
exige un montant exact, un debit posterieur a la facture, et il REFUSE de trancher des qu'il y a
plusieurs candidats d'un cote ou de l'autre (cf. `apparier`).
"""
from __future__ import annotations

import re

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.expenses import journal

BANK_ACCOUNT = "STE430127B - Zitouna - A&S"

# Les deux cycles. Chacun a besoin d'un DISCRIMINANT sur son compte d'attente, car aucun des deux
# comptes ne lui est reserve :
#   - `Crediteurs - A&S` porte tous les fournisseurs -> on filtre sur le TIERS ;
#   - `Compte de decouvert bancaire - A&S` porte aussi les transitaires et les prestations
#     (SUNLINE, Frotec, Elestar...) -> aucun tiers n'y est renseigne, on filtre sur le LIBELLE.
CYCLES = (
    {"cle": "aramex", "libelle": "facture Aramex",
     "compte": "Créditeurs - A&S", "party": "ARAMEX", "marqueur": None},
    {"cle": "honoraire", "libelle": "note d'honoraire",
     "compte": "Compte de découvert bancaire - A&S", "party": None, "marqueur": "honoraire"},
)


def cycle(cle: str) -> dict:
    for c in CYCLES:
        if c["cle"] == cle:
            return c
    raise ValueError("cycle de reglement inconnu : %s" % cle)
# Tolerance nulle ou presque : le montant est le SEUL discriminant, il doit tomber au millime.
TOLERANCE = 0.005
# Un virement anterieur a la facture ne peut pas la regler ; au-dela de 120 jours, ce n'est plus
# le reglement de cette facture-la mais une coincidence de montant.
DELAI_MAX_JOURS = 120


def pieces_en_attente(cyc: dict, company: str = None) -> list:
    """Pieces encore posees sur le compte d'attente de ce cycle (donc non reglees).

    Retourne [{name, posting_date, montant, docstatus, cheque_no, user_remark}].
    """
    company = company or journal._company_of(journal.ARAMEX_CHARGE_ACCOUNT)
    where_tiers = "and jea.party = %(tiers)s" if cyc.get("party") else ""
    rows = frappe.db.sql(
        f"""
        select je.name, je.posting_date, je.docstatus, je.cheque_no, je.user_remark,
               sum(jea.credit_in_account_currency) as montant
        from `tabJournal Entry` je
        join `tabJournal Entry Account` jea on jea.parent = je.name
        where je.company = %(company)s and je.docstatus < 2
          and jea.account = %(compte)s {where_tiers}
          and jea.credit_in_account_currency > 0
        group by je.name
        order by je.posting_date
        """,
        {"company": company, "compte": cyc["compte"], "tiers": cyc.get("party")}, as_dict=True)
    out = []
    marqueur = (cyc.get("marqueur") or "").upper()
    for r in rows:
        # Sans tiers, seul le libelle distingue la piece des autres occupants du compte.
        if marqueur and marqueur not in ("%s %s" % (r.cheque_no or "", r.user_remark or "")).upper():
            continue
        r["montant"] = flt(r["montant"], 3)
        out.append(r)
    return out


def virements_emis(movements: list) -> list:
    """Debits bancaires susceptibles d'etre un virement emis (le libelle ne nomme personne)."""
    out = []
    for m in movements or []:
        if not flt(m.get("debit"), 3):
            continue
        op = (m.get("operation") or "").upper()
        if "VIR" not in op:
            continue
        out.append(m)
    return out


def apparier(factures: list, mouvements: list, consommes: set = None,
             cle: str = "aramex") -> tuple:
    """(paires, diagnostics). Une paire = {facture, mouvement}.

    Le montant etant le seul critere, on n'apparie QUE si la reponse est unique des deux cotes :
    une facture qui voit deux virements possibles, ou un virement que deux factures revendiquent,
    ne produit rien. Mieux vaut un rapprochement manquant qu'une ecriture detruite a tort.
    """
    consommes = {str(r).strip().upper() for r in (consommes or set())}
    candidats = [m for m in virements_emis(mouvements)
                 if (m.get("reference") or "").strip().upper() not in consommes]
    paires, diag = [], []

    for f in factures:
        possibles = [
            m for m in candidats
            if abs(flt(m["debit"], 3) - f["montant"]) <= TOLERANCE
            and 0 <= (getdate(m["date"]) - getdate(f["posting_date"])).days <= DELAI_MAX_JOURS
        ]
        if not possibles:
            diag.append({"flux": "reglement_%s" % cle, "je": f["name"], "montant": f["montant"],
                         "status": "en attente",
                         "raison": "aucun virement emis de ce montant apres le %s"
                                   % f["posting_date"]})
            continue
        if len(possibles) > 1:
            diag.append({"flux": "reglement_%s" % cle, "je": f["name"], "montant": f["montant"],
                         "status": "ambigu",
                         "raison": "%d virements du meme montant : %s — non tranche"
                                   % (len(possibles), ", ".join(m.get("reference") or "?"
                                                                for m in possibles[:4]))})
            continue
        m = possibles[0]
        # Le virement ne doit pas non plus etre revendique par une autre facture.
        rivales = [g for g in factures
                   if g["name"] != f["name"] and abs(g["montant"] - flt(m["debit"], 3)) <= TOLERANCE]
        if rivales:
            diag.append({"flux": "reglement_%s" % cle, "je": f["name"], "montant": f["montant"],
                         "status": "ambigu",
                         "raison": "le virement %s solderait aussi %s — non tranche"
                                   % (m.get("reference"), ", ".join(g["name"] for g in rivales))})
            continue
        paires.append({"facture": f, "mouvement": m})
    return paires, diag


def references_deja_utilisees() -> set:
    """References bancaires deja consommees par un reglement anterieur : celles citees
    « Réf de paiement : FT... » dans le libelle d'une ecriture non annulee (la forme que
    `_remarque_reglee` produit, et celle des saisies manuelles historiques).

    Sans ce garde-fou, un virement ayant deja regle une piece pourrait en regler une seconde du
    meme montant a un passage ulterieur — cas realiste : les notes d'honoraire se repetent d'un
    mois a l'autre, et une piece deja reglee QUITTE le vivier, donc le controle des rivales
    d'`apparier` ne voit plus la premiere."""
    refs = set()
    rows = frappe.db.sql(
        """select user_remark from `tabJournal Entry`
           where docstatus < 2 and user_remark like %s""", ("%Réf de paiement%",))
    for (remark,) in rows:
        for m in re.finditer(r"R[ée]f de paiement\s*:?\s*([A-Za-z0-9]+)", remark or ""):
            refs.add(m.group(1).strip().upper())
    return refs


def _fichiers_de(nom_je: str) -> list:
    """[(nom, contenu)] des pieces jointes d'une ecriture, lues AVANT sa suppression."""
    out = []
    for f in frappe.get_all("File", filters={"attached_to_doctype": "Journal Entry",
                                             "attached_to_name": nom_je},
                            fields=["name", "file_name"], limit_page_length=0):
        try:
            doc = frappe.get_doc("File", f.name)
            out.append((doc.file_name or f.file_name, doc.get_content()))
        except Exception:
            continue          # un justificatif illisible ne doit pas bloquer le reglement
    return out


def _remarque_reglee(ancienne: str, reference: str) -> str:
    """Libelle de l'ecriture reglee, au format des saisies manuelles.

    Reel : « Fac ARAMEX au 30-06-2026 | Réf de paiement :FT26189PFRLK ». On ne reecrit pas la
    partie facture, on lui ajoute la reference — et on evite de l'ajouter deux fois.
    """
    ancienne = (ancienne or "").strip()
    if reference and reference.upper() in ancienne.upper():
        return ancienne
    return ("%s | Réf de paiement : %s" % (ancienne, reference)).strip(" |")


def lignes_de_reglement(lignes_anciennes, montant: float, cyc: dict) -> list:
    """Les lignes de l'ecriture RECREEE sur la banque, a partir de celles de l'anticipee.

    SEULE LA CONTREPARTIE CHANGE : le compte d'attente devient la banque. Tout le reste de
    l'ecriture est repris a l'identique — la charge, la TVA, ET LES AUTRES CREDITS.

    ⚠️ NE PAS SE CONTENTER DES DEBITS. Une note d'honoraire porte une RETENUE A LA SOURCE, qui
    est un CREDIT : la laisser tomber desequilibre l'ecriture recreee du montant exact de la
    retenue, et le reglement echoue au lieu de basculer l'attente vers la banque. Constate le
    31/08/2026 sur la note 07-2026 — Dr=239,000 != Cr=231,860, soit les 7,140 de retenue perdus :
    le virement du comptable est reste orphelin au releve pendant que la charge dormait sur le
    compte d'attente. Les notes precedentes n'en portaient pas, le defaut ne s'etait jamais vu.

    La retenue reste une DETTE jusqu'a sa declaration : elle n'a a etre ni passee au debit, ni
    fondue dans le montant vire.
    """
    def _est_la_dette(a):
        """La ligne que la banque remplace : celle que `pieces_en_attente` a totalisee.

        Le tiers fait partie du critere quand le cycle en a un — `Crediteurs` porte TOUS les
        fournisseurs, et ce virement-la ne solde que la dette ARAMEX.
        """
        if _valeur(a, "account") != cyc["compte"]:
            return False
        return not cyc.get("party") or _valeur(a, "party") == cyc["party"]

    out = [{"account": BANK_ACCOUNT, "credit": flt(montant, 3)}]
    for a in lignes_anciennes or []:
        debit = flt(_valeur(a, "debit_in_account_currency"), 3)
        credit = flt(_valeur(a, "credit_in_account_currency"), 3)
        if debit > 0:
            out.append({"account": _valeur(a, "account"), "debit": debit,
                        "cost_center": _valeur(a, "cost_center")})
        elif credit > 0 and not _est_la_dette(a):
            out.append({"account": _valeur(a, "account"), "credit": credit,
                        "cost_center": _valeur(a, "cost_center"),
                        "party_type": _valeur(a, "party_type") or None,
                        "party": _valeur(a, "party") or None})
    return out


def _valeur(ligne, champ):
    """Lit un champ, que la ligne soit un document Frappe ou un simple dict (tests)."""
    if isinstance(ligne, dict):
        return ligne.get(champ)
    return getattr(ligne, champ, None)


def regler(facture: dict, mouvement: dict, insert: bool = True,
           cle: str = "aramex") -> dict:
    """Remplace l'ecriture sur Crediteurs par son equivalent sur la banque.

    La date de comptabilisation de l'ancienne est REPRISE telle quelle : c'est l'invariant.
    """
    ancienne = frappe.get_doc("Journal Entry", facture["name"])
    reference = (mouvement.get("reference") or "").strip()
    company = ancienne.company
    montant = facture["montant"]

    lignes = lignes_de_reglement(ancienne.accounts, montant, cycle(cle))

    nouvelle = journal.build_journal_entry(
        company, ancienne.posting_date, lignes,
        remark=_remarque_reglee(ancienne.user_remark, reference),
        cheque_no=ancienne.cheque_no,
        cheque_date=getdate(mouvement["date"]),
        mode_of_payment="Virement")

    if not insert:
        return {"flux": "reglement_%s" % cle, "status": "a regler", "je_ancienne": ancienne.name,
                "je": "(dry-run)", "montant": montant, "reference": reference,
                "date": str(ancienne.posting_date)}

    pieces = _fichiers_de(ancienne.name)
    if ancienne.docstatus == 1:
        ancienne.flags.ignore_links = True
        ancienne.cancel()
    frappe.delete_doc("Journal Entry", ancienne.name, force=True, ignore_permissions=True)

    nouvelle.insert(ignore_permissions=True)
    # L'ecriture de REGLEMENT constate un debit deja au releve : c'est le moment ou l'operation
    # devient certaine. La laisser en brouillon alors que l'anticipee etait soumise sortirait le
    # montant du grand livre au moment meme ou la banque le confirme.
    if journal._auto_submit_enabled():
        nouvelle.submit()
    if pieces:
        journal._attach(nouvelle, pieces)
    return {"flux": "reglement_%s" % cle, "status": "regle", "je_ancienne": facture["name"],
            "je": nouvelle.name, "montant": montant, "reference": reference,
            "date": str(nouvelle.posting_date)}


def process_reglements(movements: list, insert: bool = True, consommes: set = None,
                       only=None) -> list:
    """Point d'entree : pour chaque cycle, apparie puis regle.

    `consommes=None` (le defaut) charge les references deja utilisees par un reglement
    anterieur — passer un set explicite (tests) court-circuite la lecture en base."""
    out = []
    if consommes is None:
        consommes = references_deja_utilisees()
    for cyc in CYCLES:
        if only and cyc["cle"] not in ({only} if isinstance(only, str) else set(only)):
            continue
        pieces = pieces_en_attente(cyc)
        if not pieces:
            out.append({"flux": "reglement_%s" % cyc["cle"], "status": "rien a faire",
                        "raison": "aucune %s ouverte sur %s" % (cyc["libelle"], cyc["compte"])})
            continue
        paires, diag = apparier(pieces, movements, consommes=consommes, cle=cyc["cle"])
        out += diag
        for p in paires:
            try:
                out.append(regler(p["facture"], p["mouvement"], insert=insert, cle=cyc["cle"]))
            except Exception as e:
                out.append({"flux": "reglement_%s" % cyc["cle"], "je": p["facture"]["name"],
                            "status": "error", "error": str(e)[:160]})
    return out
