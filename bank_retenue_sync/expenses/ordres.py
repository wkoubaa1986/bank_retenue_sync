"""Ordres de paiement : l'attente entre l'ecriture anticipee et le virement reel.

Les salaires et le loyer sont comptabilises AVANT que la banque ne bouge (2 jours avant la fin du
mois, le 15 tous les deux mois). L'ecriture seule ne prouve donc rien : elle dit ce qu'on doit
payer, pas ce qui est parti. Un ordre de paiement porte cette attente, et le rapprochement
bancaire la solde.

Trois issues, jamais silencieuses :
  - `Vire`      : un debit du bon montant est apparu dans la fenetre attendue ;
  - `Ecart`     : un debit plausible est apparu, mais d'un montant different -> a examiner ;
  - `En attente`: rien n'est parti. Passe la date prevue, c'est une alerte.

UN MOUVEMENT NE CONFIRME QU'UN SEUL ORDRE. Les trois salaires tombent le meme jour, sur le meme
compte, avec des libelles identiques ('VIR TN AUTRE BQ') : sans consommation exclusive, un seul
debit de 1700 pourrait solder les trois.
"""
from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import flt, getdate

DOCTYPE = "BRS Ordre de Paiement"

STATUT_ATTENTE = "En attente"
STATUT_VIRE = "Vire"
STATUT_ECART = "Ecart"

# Fenetre autour de la date prevue : l'ecriture est anticipee de quelques jours, et la banque
# peut executer le virement avec un decalage.
FENETRE_JOURS = 7
# Au-dela, deux montants ne designent plus la meme operation.
TOLERANCE_MONTANT = 0.005


def creer_ordre(libelle, montant, date_prevue, journal_entry=None, type_depense=None,
                beneficiaire=None, compte_banque=None, source_regle=None, periode=None,
                insert: bool = True):
    """Cree l'ordre attache a une ecriture anticipee. Idempotent sur (source_regle, periode)."""
    if source_regle and periode and frappe.db.exists(
            DOCTYPE, {"source_regle": source_regle, "periode": periode,
                      "statut": ["!=", "Annule"]}):
        return None
    doc = frappe.new_doc(DOCTYPE)
    doc.update({
        "libelle": libelle, "montant": flt(montant, 3), "date_prevue": date_prevue,
        "journal_entry": journal_entry, "type_depense": type_depense,
        "beneficiaire": beneficiaire, "compte_banque": compte_banque,
        "source_regle": source_regle, "periode": periode, "statut": STATUT_ATTENTE,
    })
    if insert:
        doc.insert(ignore_permissions=True)
    return doc


def ordres_en_attente(date_max=None) -> list:
    filters = {"statut": STATUT_ATTENTE}
    if date_max:
        filters["date_prevue"] = ["<=", getdate(date_max)]
    return frappe.db.get_all(
        DOCTYPE, filters=filters, limit_page_length=0, order_by="date_prevue asc",
        fields=["name", "libelle", "montant", "date_prevue", "compte_banque", "source_regle",
                "journal_entry", "periode"])


def confirmer_par_banque(movements: list, fenetre: int = FENETRE_JOURS,
                         regler_ecritures: bool = True) -> dict:
    """Confronte les ordres en attente aux debits du releve.

    `movements` : mouvements au format dict nu (registre ou export).

    `regler_ecritures=True` : quand un ordre passe a « Vire », son ecriture ANTICIPEE — posee sur
    le compte d'attente — est annulee, supprimee, et recreee sur la BANQUE avec la reference du
    virement, a date de comptabilisation inchangee. Meme cycle que les factures Aramex et les
    notes d'honoraire (cf. expenses/reglement.py), a une difference pres qui le rend plus sur :
    ici le rapprochement est deja fait (l'ordre porte le lien vers l'ecriture ET vers le
    mouvement), la ou `reglement` doit le retrouver par le seul montant.
    Le passer a False rend a cette fonction son ancien contrat : ne toucher qu'aux statuts.
    """
    from bank_retenue_sync.bank import registry
    from bank_retenue_sync.expenses import reglement

    debits = [m for m in (movements or []) if flt(m.get("debit"), 3) and m.get("date")]
    ordres = ordres_en_attente()
    out = {"vires": 0, "ecarts": 0, "en_attente": 0, "detail": []}
    consommes = set()

    for o in ordres:
        prevue = getdate(o.date_prevue)
        debut, fin = prevue - timedelta(days=fenetre), prevue + timedelta(days=fenetre)
        candidats = [
            m for m in debits
            if debut <= getdate(m["date"]) <= fin
            and registry.movement_key(m) not in consommes
        ]
        exacts = [m for m in candidats
                  if abs(flt(m["debit"], 3) - flt(o.montant, 3)) <= TOLERANCE_MONTANT]

        if exacts:
            # Le plus proche de la date prevue : deux salaires du meme montant ne peuvent pas
            # exister, mais deux echeances du meme montant a un mois d'ecart, si.
            m = min(exacts, key=lambda x: abs((getdate(x["date"]) - prevue).days))
            cle = registry.movement_key(m)
            consommes.add(cle)
            frappe.db.set_value(DOCTYPE, o.name, {
                "statut": STATUT_VIRE,
                "mouvement": cle if frappe.db.exists("BRS Bank Movement", cle) else None,
                "date_virement": m["date"], "montant_virement": flt(m["debit"], 3), "ecart": 0,
            })
            out["vires"] += 1
            detail = {"ordre": o.name, "statut": STATUT_VIRE,
                      "libelle": o.libelle, "date": str(m["date"])}
            if regler_ecritures and o.journal_entry and frappe.db.exists(
                    "Journal Entry", o.journal_entry):
                try:
                    res = reglement.regler(
                        {"name": o.journal_entry, "montant": flt(o.montant, 3)}, m,
                        insert=True, cle="calendrier")
                    detail["je"] = res.get("je")
                    detail["je_anticipee"] = res.get("je_ancienne")
                    frappe.db.set_value(DOCTYPE, o.name, "journal_entry", res.get("je"))
                except Exception as e:
                    # Le statut « Vire » est acquis : l'argent est bien parti. Un echec de
                    # recreation ne doit pas le remettre en cause, mais il doit se voir.
                    detail["erreur_reglement"] = str(e)[:160]
            out["detail"].append(detail)
            continue

        out["en_attente"] += 1
        out["detail"].append({"ordre": o.name, "statut": STATUT_ATTENTE, "libelle": o.libelle,
                              "date_prevue": str(prevue),
                              "raison": "aucun debit du montant attendu dans la fenetre"})
    return out


def alertes(jour=None) -> list:
    """Ordres dont la date prevue est passee sans confirmation bancaire."""
    jour = getdate(jour or frappe.utils.nowdate())
    return [o for o in ordres_en_attente(date_max=jour - timedelta(days=FENETRE_JOURS))]
