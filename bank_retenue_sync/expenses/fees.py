"""Frais bancaires : regroupes par JOUR, jamais comptabilises a l'unite.

DECISION UTILISATEUR
--------------------
« Les frais bancaires de n'importe quelle operation, je vais les declencher separement, non par
operation, sur une base journaliere. » Une commission n'est donc jamais rattachee au mouvement qui
l'a generee : elle rejoint l'agregat de sa journee.

Cette contrainte est STRUCTURELLE, pas conventionnelle : dans `bank/rules.py`, toutes les regles
de frais portent `action = ACTION_AGREGAT` et `groupe = "jour"`, ce qui empeche le classifieur de
proposer une creation d'ecriture sur une ligne isolee. L'action porte toujours sur le groupe.

CUMUL MENSUEL, ALIMENTE AU JOUR LE JOUR
---------------------------------------
UNE ecriture par mois, refaite en entier a chaque nouveau frais detecte, jusqu'a la cloture. C'est
le document que l'utilisateur montait a la main une fois par mois ; il est desormais alimente
quotidiennement, sans changer de forme.

Le cumul est TOUJOURS recalcule depuis le releve, jamais incremente a partir de l'ecriture
existante : une ligne bancaire vue deux fois ne peut donc pas doubler le total. C'est ce qui rend
l'operation rejouable sans risque.

Le flux ne cree rien tant que `Bank Retenue Sync Settings.frais_bancaires_actifs` n'est pas coche.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import frappe
from frappe.utils import flt, getdate

from bank_retenue_sync.bank import rules as R
from bank_retenue_sync.expenses import journal, lookup
from bank_retenue_sync.encaissement.pending import BANK_ACCOUNT, COMPANY

FEE_ACCOUNT = "Frais bancaire - A&S"
FEE_TVA_ACCOUNT = "TVA 19% - A&S"
FEE_TIMBRE_ACCOUNT = "Timbre Fiscal - A&S"

CATEGORIE = "frais_bancaires"


@dataclass
class FeeGroup:
    jour: date
    cle: str                                  # « Frais bancaire JJ-MM-YYYY »
    lignes: list = field(default_factory=list)
    references: list = field(default_factory=list)
    total: float = 0.0
    total_commission: float = 0.0
    total_tva: float = 0.0
    total_timbre: float = 0.0
    # Delta des paiements recus (perte de non paiement). L'utilisateur le comptabilise DANS
    # l'ecriture de frais, pas a part — verifie sur ACC-JV-2026-00422, dont la remarque dit
    # « Perte de non paiement | 9,896 | FRais banacaire et TVA | 74,392  5,795 | Total 84.288 ».
    total_pertes: float = 0.0
    # Ecarts residuels sur des pieces DEJA rapprochees (droit de timbre du leasing omis,
    # reference de virement saisie a un dinar pres). Meme traitement que les deltas : un cumul
    # mensuel, pas une ecriture par ligne. Cf. expenses/residus.py.
    total_residus: float = 0.0


def _cle_jour(jour) -> str:
    return "Frais bancaire %s" % (jour.strftime("%d-%m-%Y") if hasattr(jour, "strftime") else jour)


def _composants_isoles(movements: list, rules=None) -> set:
    """Cles des lignes TVA / timbre qui n'appartiennent a AUCUNE echeance de leasing.

    POURQUOI CETTE FONCTION EXISTE
    ------------------------------
    `tva_bancaire` et `droit_timbre` ont ete reclasses en categorie « pret » le 08/08 : sur un jour
    de leasing, ils sont bien des composants de l'echeance et non des frais. Mais ce reclassement
    a rendu INATTEIGNABLES les branches `sous_categorie == 'tva' / 'timbre'` ci-dessous, alors
    qu'une TVA de reference `CHG…`, seule de son jour, est bel et bien une TVA sur COMMISSION
    bancaire — et l'utilisateur la compte dans son ecriture mensuelle de frais.
    Mesure : mai 68,597 de commissions + 5,795 de TVA = 74,392, exactement le « FRais banacaire et
    TVA » de sa saisie manuelle (ACC-JV-2026-00422). Sans ces lignes, le cumul de l'app rendait
    68,597 et divergeait de sa pratique.

    Le discriminant est le meme que dans `bank/classify` : un composant SEUL dans son groupe
    (reference, jour) n'est pas une echeance.
    """
    par_cle: dict = {}
    for m in movements or []:
        rule = R.find_rule(m, rules)
        if not rule or rule.groupe != "echeance":
            continue
        cle = ((m.get("reference") or "").strip().upper(), m.get("date"))
        par_cle.setdefault(cle, []).append((rule, m))
    isoles = set()
    for cle, lignes in par_cle.items():
        if len(lignes) == 1 and lignes[0][0].composant:
            isoles.add(id(lignes[0][1]))
    return isoles


def group_daily_fees(movements: list, rules=None) -> list:
    """Regroupe par jour les mouvements de frais bancaires.

    Y compris les TVA et timbres qui ne relevent d'aucune echeance de leasing
    (cf. `_composants_isoles`). Ne cree rien : alimente la page et l'audit.
    """
    groupes: dict = {}
    isoles = _composants_isoles(movements, rules)
    for m in movements or []:
        rule = R.find_rule(m, rules)
        if not rule:
            continue
        if rule.categorie != CATEGORIE and id(m) not in isoles:
            continue
        jour = m.get("date")
        g = groupes.get(jour)
        if g is None:
            g = groupes[jour] = FeeGroup(jour=jour, cle=_cle_jour(jour))
        montant = flt(m.get("debit"), 3)
        g.lignes.append(m)
        ref = (m.get("reference") or "").strip()
        if ref:
            g.references.append(ref)
        g.total = flt(g.total + montant, 3)
        if rule.sous_categorie == "tva":
            g.total_tva = flt(g.total_tva + montant, 3)
        elif rule.sous_categorie == "timbre":
            g.total_timbre = flt(g.total_timbre + montant, 3)
        else:
            g.total_commission = flt(g.total_commission + montant, 3)
    return [groupes[k] for k in sorted(groupes, key=lambda d: (d is None, d))]


# =====================================================================================
# CUMUL MENSUEL — le coeur de la mecanique voulue
#
# Une SEULE ecriture par mois, refaite a l'identique a chaque nouveau frais detecte, jusqu'a la
# cloture. C'est le document que l'utilisateur montait a la main une fois par mois ; il est
# desormais alimenté au jour le jour.
#
# « On annule et on supprime l'ancienne, on recree une nouvelle cumulative » : on ne modifie donc
# jamais une ecriture ligne a ligne, on la REMPLACE en entier. Le cumul est toujours recalcule
# depuis le releve, jamais incremente a partir de lui-meme — une ligne bancaire vue deux fois ne
# peut donc pas doubler le total.
# =====================================================================================

def periode_de(jour) -> str:
    """'2026-07-14' -> '2026-07'."""
    d = getdate(jour)
    return "%04d-%02d" % (d.year, d.month)


# Prefixe du numero de reference de l'ecriture mensuelle. Nomme parce que `classify` doit
# RECONNAITRE ces ecritures pour les ecarter de la resolution par reference : elles citent les
# references de toutes les commissions du mois, et une commission partage sa reference avec
# l'operation qui l'a generee.
PREFIXE_MENSUEL = "Frais bancaire "


def cle_mensuelle(periode: str) -> str:
    """Numero de reference de l'ecriture du mois : « Frais bancaire MM-YYYY »."""
    annee, mois = periode.split("-")
    return "%s%s-%s" % (PREFIXE_MENSUEL, mois, annee)


def cumul_mensuel(movements: list, periode: str, rules=None,
                  avec_pertes: bool = True, avec_residus: bool = True,
                  residus_calcules: list = None) -> FeeGroup:
    """Total des frais bancaires du MOIS, recalcule depuis zero.

    Le groupe porte la date du dernier frais du mois : l'ecriture est ainsi datee du dernier
    evenement connu, et se decale naturellement au fil du mois.

    `avec_pertes` : ajoute les deltas de paiement du mois (ce que la banque a credite en moins
    que le montant comptabilise). C'est la pratique de l'utilisateur — UNE seule ecriture pour
    les frais, leur TVA et ces deltas. Verifie au millime sur mai (68,597 + 5,795 + 9,896 =
    84,288) et juin (109,734 + 8,645 + 15,600 = 133,979).
    """
    jours = [g for g in group_daily_fees(movements, rules) if periode_de(g.jour) == periode]
    cumul = FeeGroup(jour=(jours[-1].jour if jours else None), cle=cle_mensuelle(periode))
    for g in jours:
        cumul.lignes.extend(g.lignes)
        cumul.references.extend(g.references)
        cumul.total = flt(cumul.total + g.total, 3)
        cumul.total_commission = flt(cumul.total_commission + g.total_commission, 3)
        cumul.total_tva = flt(cumul.total_tva + g.total_tva, 3)
        cumul.total_timbre = flt(cumul.total_timbre + g.total_timbre, 3)
    if avec_pertes:
        from bank_retenue_sync.expenses import pertes as _pertes

        ecarts = [e for e in _pertes.ecarts_du_releve(movements)
                  if periode_de(getattr(e, "date", None)) == periode]
        cumul.total_pertes = flt(sum(flt(getattr(e, "ecart", 0), 3) for e in ecarts), 3)
        if cumul.total_pertes:
            cumul.total = flt(cumul.total + cumul.total_pertes, 3)
            # Sans frais du mois, le groupe n'a pas de date : on prend celle du dernier delta.
            if not cumul.jour and ecarts:
                cumul.jour = max(getattr(e, "date", None) for e in ecarts)
    if avec_residus:
        from bank_retenue_sync.expenses import residus as _residus

        lignes = (residus_calcules if residus_calcules is not None
                  else _residus.residus_du_releve(movements, rules=rules))
        cumul.total_residus = _residus.cumul_mensuel(lignes, periode)
        if cumul.total_residus:
            cumul.total = flt(cumul.total + cumul.total_residus, 3)
            if not cumul.jour:
                dates = [r.date for r in lignes if r.date and periode_de(r.date) == periode]
                cumul.jour = max(dates) if dates else None
    return cumul


def cumul_journalier(movements: list, periode: str = None, rules=None) -> list:
    """Suivi jour par jour du cumul du mois — ce que la page affiche.

    Rend [{jour, du_jour, cumul, nb_lignes}]. L'ecriture, elle, ne porte que le total final.
    """
    jours = group_daily_fees(movements, rules)
    if periode:
        jours = [g for g in jours if periode_de(g.jour) == periode]
    suivi, cumul = [], 0.0
    for g in jours:
        cumul = flt(cumul + g.total, 3)
        suivi.append({"jour": g.jour, "du_jour": g.total, "cumul": cumul,
                      "nb_lignes": len(g.lignes), "tva": g.total_tva})
    return suivi


def is_enabled() -> bool:
    """Le flux ne cree rien tant que l'utilisateur ne l'a pas active explicitement."""
    try:
        return bool(frappe.db.get_single_value(
            "Bank Retenue Sync Settings", "frais_bancaires_actifs"))
    except Exception:
        return False


def build_fee_journal_entry(group: FeeGroup, insert: bool = True):
    """Ecriture cumulative du mois.

        Cr  STE430127B - Zitouna - A&S    total du mois (frais + TVA + deltas de paiement)
        Dr  TVA 19% - A&S                 part TVA        (si > 0)
        Dr  Timbre Fiscal - A&S           part timbre     (si > 0)
        Dr  Frais bancaire - A&S          le reste, DELTAS DE PAIEMENT INCLUS

    Les deltas ne vont pas sur un compte distinct : les saisies reelles les fondent dans
    « Frais bancaire » (78,493 = 68,597 de commissions + 9,896 de delta en mai).

    Datee du DERNIER frais connu du mois : l'ecriture suit l'avancement du mois, et se retrouve
    naturellement en fin de mois une fois celui-ci complet.
    """
    if not group.total:
        return None
    cc = frappe.db.get_value("Company", COMPANY, "cost_center")
    reste = flt(group.total - group.total_tva - group.total_timbre, 3)
    lines = [{"account": BANK_ACCOUNT, "credit": group.total}]
    if group.total_tva:
        lines.append({"account": FEE_TVA_ACCOUNT, "debit": group.total_tva, "cost_center": cc})
    if group.total_timbre:
        lines.append({"account": FEE_TIMBRE_ACCOUNT, "debit": group.total_timbre,
                      "cost_center": cc})
    if reste:
        # Un residu NEGATIF (la banque a preleve moins que l'ecriture d'origine) peut rendre le
        # reste negatif : il devient alors un credit de charge, jamais un debit negatif — Frappe
        # accepterait la valeur, mais aucun grand livre ne se lit ainsi.
        sens = "debit" if reste > 0 else "credit"
        lines.append({"account": FEE_ACCOUNT, sens: abs(reste), "cost_center": cc})

    detail = "%d operations bancaires cumulees" % len(group.lignes)
    if group.total_pertes or group.total_residus:
        base = flt(group.total - group.total_pertes - group.total_residus, 3)
        detail += " | frais et TVA %s" % base
        if group.total_pertes:
            detail += " | delta des paiements %s" % group.total_pertes
        if group.total_residus:
            detail += " | ecarts de rapprochement %s" % group.total_residus
    remark = "%s\n%s\nRéf. banque %s" % (
        group.cle, detail, ", ".join(sorted(set(group.references))[:20]))
    je = journal.build_journal_entry(COMPANY, group.jour, lines, remark=remark,
                                     cheque_no=group.cle, cheque_date=group.jour)
    if insert:
        je.insert(ignore_permissions=True)      # BROUILLON : la soumission reste manuelle
    return je


def _supprimer_ecriture(nom: str) -> str:
    """Retire l'ecriture du mois avant de la refaire.

    Une ecriture SOUMISE doit d'abord etre annulee : c'est la seule facon de la retirer du grand
    livre. Une ecriture en brouillon se supprime directement.
    """
    doc = frappe.get_doc("Journal Entry", nom)
    etat = "brouillon"
    if doc.docstatus == 1:
        doc.cancel()
        etat = "soumise (annulee)"
    frappe.delete_doc("Journal Entry", nom, force=True, ignore_permissions=True)
    return etat


def sync_ecriture_mensuelle(movements: list, periode: str = None, insert: bool = True,
                            force: bool = False) -> dict:
    """Refait l'ecriture cumulative du mois si le total a change.

    Retourne {periode, statut, total, je, remplacee}.
    `statut` : cree | remplacee | inchangee | vide | inactif
    """
    periode = periode or periode_de(frappe.utils.nowdate())
    if not (force or is_enabled()):
        return {"periode": periode, "statut": "inactif",
                "raison": "flux desactive : cocher « Comptabiliser les frais bancaires »"}

    cumul = cumul_mensuel(movements, periode)
    reference = cle_mensuelle(periode)
    existant = frappe.db.get_value("Journal Entry", {"cheque_no": reference, "docstatus": ["<", 2]},
                                   ["name", "total_debit", "docstatus"], as_dict=True)

    if not cumul.total:
        return {"periode": periode, "statut": "vide", "total": 0.0,
                "je": existant.name if existant else None}

    if existant and abs(flt(existant.total_debit, 3) - cumul.total) < 0.005:
        # ⚠️ MONTANT INCHANGE N'EST PAS ETAT INCHANGE. Ce raccourci sortait avant
        # meme de regarder le docstatus : un brouillon dont le total ne bouge plus
        # ne partait JAMAIS. Or les frais du mois se figent vite — apres le dernier
        # de la journee, les sept passages suivants repondent tous « inchangee » et
        # l'ecriture dort en brouillon jusqu'au mois suivant. Constate le 02/09/2026
        # sur ACC-JV-2026-00687 : le reglage « soumettre automatiquement » etait
        # pourtant coche, et le correctif de 13h43 bien deploye — il ne se declenche
        # qu'au REMPLACEMENT, jamais sur une ecriture deja au bon montant.
        if insert and existant.docstatus == 0 and journal._auto_submit_enabled():
            try:
                frappe.get_doc("Journal Entry", existant.name).submit()
                return {"periode": periode, "statut": "soumise", "total": cumul.total,
                        "je": existant.name, "soumise": True}
            except Exception:
                # Une ecriture qui refuse de partir (exercice clos, compte gele) ne
                # doit pas emporter la verification des frais avec elle.
                frappe.log_error(frappe.get_traceback(),
                                 "frais bancaires : soumission %s" % existant.name)
        return {"periode": periode, "statut": "inchangee", "total": cumul.total,
                "je": existant.name}

    remplacee, etait_soumise = None, bool(existant and existant.docstatus == 1)
    if existant:
        remplacee = "%s (%s)" % (existant.name, _supprimer_ecriture(existant.name))

    je = build_fee_journal_entry(cumul, insert=insert)
    # RENDRE L'ETAT, PAS SEULEMENT LE MONTANT : remplacer une ecriture SOUMISE par un brouillon
    # sortirait le total du grand livre jusqu'a validation humaine. Avec sept verifications par
    # jour, l'ecriture du mois en cours passerait ses journees hors comptabilite. On re-soumet
    # donc si celle qu'on remplace l'etait.
    #
    # ET LE PREMIER FRAIS DU MOIS SUIT LE REGLAGE DU SITE. Il n'y a rien a
    # remplacer ce jour-la : l'ecriture naissait donc en brouillon, meme quand
    # « Soumettre automatiquement les ecritures » est coche. Resultat : chaque
    # debut de mois, l'ecran d'identification affichait un ecart « reste a
    # comptabiliser » jusqu'a ce que quelqu'un pense a soumettre — constate le
    # 02/09/2026 sur ACC-JV-2026-00675 (1,190). Le reglage decoche, rien ne
    # change : la soumission reste manuelle, comme avant.
    # ⚠️ ET UN BROUILLON DÉJÀ EN PLACE EST RATTRAPÉ. Ne soumettre que la
    # PREMIÈRE écriture du mois laissait septembre en brouillon pour toujours :
    # l'écriture existait déjà (créée avant le correctif), donc `not existant`
    # était faux, et chaque nouveau frais la remplaçait par un autre brouillon.
    # L'écran d'identification affichait « écriture en brouillon, à soumettre »
    # sur tous les frais du mois (constaté 02/09/2026, ACC-JV-2026-00685).
    # Le réglage du site dit « soumettre automatiquement » : on le suit, sans
    # exiger que la précédente l'ait été. Réglage décoché, rien ne part.
    a_soumettre = etait_soumise or journal._auto_submit_enabled()
    if insert and je and a_soumettre:
        je.submit()
    return {"periode": periode, "statut": "remplacee" if remplacee else "cree",
            "total": cumul.total, "lignes": len(cumul.lignes), "soumise": a_soumettre,
            "je": (je.name if insert and je else "(dry-run)"), "remplacee": remplacee}


def periode_debut_gestion() -> str:
    """Plancher de prise en charge ('AAAA-MM', Settings). Les mois anterieurs appartiennent aux
    saisies MANUELLES de l'utilisateur : l'ecriture cumulative ne doit JAMAIS les recalculer —
    le remplacement a une fois ecrase ses frais d'avril-juin 2026 (restaures depuis la
    corbeille, decision utilisateur 2026-08-19)."""
    try:
        return (frappe.db.get_single_value("Bank Retenue Sync Settings",
                                           "periode_debut_gestion") or "").strip()
    except Exception:
        return ""


def process_fees(movements: list, insert: bool = True, context=None, force: bool = False,
                 periodes=None) -> list:
    """Met a jour l'ecriture cumulative de chaque mois represente dans le releve."""
    jours = group_daily_fees(movements)
    if not jours:
        return []
    if periodes is None:
        periodes = sorted({periode_de(g.jour) for g in jours if g.jour})
    debut = periode_debut_gestion()
    out = []
    if debut:
        out = [{"periode": p, "statut": "avant prise en charge",
                "raison": "mois anterieur au plancher %s : saisies manuelles conservees" % debut}
               for p in periodes if p < debut]
        periodes = [p for p in periodes if p >= debut]
    return out + [sync_ecriture_mensuelle(movements, p, insert=insert, force=force)
                  for p in periodes]


# ------------------------------------------------------------ rafraichissement evenementiel

def rafraichir_mois_courant():
    """Resynchronise l'ecriture du mois COURANT depuis le registre.

    Le cumul n'etait refait que par le cron quotidien : tout evenement qui change les montants
    rapproches en journee (soumission d'un encaissement, resolution d'un ecart Aramex) laissait
    l'ecriture — donc le solde ERPNext — faux jusqu'au lendemain. Constate le 24/08/2026 :
    delta cheque resolu a 21 h, ecriture d'aout figee sur l'ancien brouillon a 13 h, ecart
    fantome de 6,200 au tableau de bord toute la soiree.

    Tourne en job (voir `planifier_rafraichissement`) ; idempotent comme le cron : le cumul est
    recalcule depuis le registre, et `sync_ecriture_mensuelle` ne remplace que si le total a
    change. Le plancher de prise en charge est respecte — meme garde que `process_fees`."""
    if not is_enabled():
        return
    from bank_retenue_sync.bank import registry

    periode = periode_de(frappe.utils.nowdate())
    debut = periode_debut_gestion()
    if debut and periode < debut:
        return
    try:
        sync_ecriture_mensuelle(registry.registry_as_movements(), periode)
        frappe.db.commit()
    except Exception:
        frappe.log_error(title="Frais bancaires : echec du rafraichissement evenementiel",
                         message=frappe.get_traceback())


def planifier_rafraichissement():
    """Enfile le rafraichissement du mois courant — a appeler depuis une requete.

    `enqueue_after_commit` : le job ne doit lire le registre et les pieces QU'APRES le commit de
    la transaction appelante, sinon il refait l'ecriture sur l'etat d'avant. Deduplique par
    `job_id` : dix resolutions d'ecarts a la suite ne font qu'un recalcul. Jamais bloquant pour
    l'operation appelante — un echec d'enfilage se journalise, le cron du lendemain rattrape."""
    try:
        frappe.enqueue("bank_retenue_sync.expenses.fees.rafraichir_mois_courant",
                       queue="short", job_id="brs-frais-rafraichir-mois-courant",
                       deduplicate=True, enqueue_after_commit=True)
    except Exception:
        frappe.log_error(title="Frais bancaires : echec d'enfilage du rafraichissement",
                         message=frappe.get_traceback())


def rafraichir_apres_encaissement(doc, method=None):
    """Hook doc_events (on_submit / on_cancel d'Encaissement Paiement) : la piece qui vient de
    changer alimente les ecarts de rapprochement du cumul mensuel."""
    planifier_rafraichissement()
