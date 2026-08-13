"""Depenses recurrentes par defaut, en Python pur.

Sert a deux choses :
  - amorcer la table `Bank Retenue Sync Settings.depenses_recurrentes` (cf. `engine.seed_defaults`) ;
  - faire tourner les tests du moteur SANS base de donnees, exactement comme `test_especes.py`
    injecte son `je_finder`.

Les montants viennent de l'historique reel des Journal Entry de la societe (14 mois analyses).
Ils sont EDITABLES depuis les Settings : le loyer est deja passe de 5000 a 5500 en aout 2025, et
un changement de salaire ne doit jamais demander une modification de code.

/!\ Les libelles bancaires exacts de la periode recente restent A CONFIRMER au premier export reel
(l'echantillon disponible date d'octobre 2023). D'ou `actif = 0` par defaut sur les lignes dont le
motif n'a pas ete vu dans des donnees reelles : elles se voient dans les Settings, elles ne
declenchent rien tant que l'utilisateur ne les a pas validees.
"""
from __future__ import annotations

BANQUE = "STE430127B - Zitouna - A&S"

# Contrepartie des ecritures ANTICIPEES : on ne credite la banque qu'une fois le virement
# constate au releve (cf. calendrier.py et reglement.py).
DECOUVERT = "Compte de découvert bancaire - A&S"

DEFAULTS = (
    # ---------------------------------------------------------------- salaires
    # /!\ AUCUN `motifs_libelle` : verifie sur l'export reel de juin-juillet 2026, le releve
    # abrege les virements de salaire en « VIR TN AUTRE BQ », SANS AUCUN NOM de beneficiaire.
    # Les trois salaires y sont strictement indiscernables par le libelle — seul le MONTANT
    # les distingue. Une tolerance serree (0.001) evite qu'un virement fournisseur du meme
    # ordre de grandeur ne s'y glisse ; et si deux regles revendiquaient le meme mouvement,
    # le moteur refuse de trancher plutot que de deviner.
    {"cle": "salaire_koubaa_nejib", "libelle": "Salaire Koubaâ Néjib", "type": "Salaire",
     "montant": 1700.000, "tolerance": 0.001, "bank_rule": "virement_emis",
     "compte_charge": "Salaire - A&S", "compte_banque": BANQUE,
     "compte_attente": DECOUVERT, "mode_paiement": "Virement",
     "declencheur": "Calendrier", "jours_avant_fin_mois": 2,
     "periodicite": "Mensuel", "template_reference": "Salaire Koubaâ Néjib {mm}-{yyyy}",
     "actif": 0, "notes": "Confirme sur le releve : 3 occurrences (01/06, 02/07, 30/07/2026)."},
    {"cle": "salaire_jamel_aloui", "libelle": "Salaire Jamel Aloui", "type": "Salaire",
     "montant": 1450.000, "tolerance": 0.001, "bank_rule": "virement_emis",
     "compte_charge": "Salaire - A&S", "compte_banque": BANQUE,
     "compte_attente": DECOUVERT, "mode_paiement": "Virement",
     "declencheur": "Calendrier", "jours_avant_fin_mois": 2,
     "periodicite": "Mensuel", "template_reference": "Salaire Jamel Aloui {mm}-{yyyy}",
     "actif": 0, "notes": "Confirme sur le releve : 3 occurrences."},
    {"cle": "salaire_hedi_chouchene", "libelle": "Salaire Med Hédi Chouchène", "type": "Salaire",
     "montant": 1060.000, "tolerance": 0.001, "bank_rule": "virement_emis",
     "compte_charge": "Salaire - A&S", "compte_banque": BANQUE,
     "compte_attente": DECOUVERT, "mode_paiement": "Virement",
     "declencheur": "Calendrier", "jours_avant_fin_mois": 2,
     "periodicite": "Mensuel", "template_reference": "Salaire Med Hédi Chouchène {mm}-{yyyy}",
     "actif": 0, "notes": "Confirme sur le releve : 3 occurrences."},
    {"cle": "salaire_sadok_bouziri", "libelle": "Salaire Sadok BOUZIRI", "type": "Salaire",
     "montant": 1150.000, "tolerance": 0.001, "bank_rule": "virement_emis",
     "compte_charge": "Salaire - A&S", "compte_banque": BANQUE,
     "compte_attente": DECOUVERT, "mode_paiement": "Virement",
     "declencheur": "Calendrier", "jours_avant_fin_mois": 2,
     "periodicite": "Mensuel", "template_reference": "Salaire Sadok BOUZIRI {mm}-{yyyy}",
     "actif": 0,
     "notes": "ABSENT du releve juin-juillet 2026 : montant a confirmer avant activation."},

    # ------------------------------------------------------------------- loyer
    # Bimestriel, montant constant, sans TVA. Bornes du 15 au 15, comme les saisies manuelles.
    {"cle": "loyer_bureau", "libelle": "Loyer du local", "type": "Loyer",
     "montant": 5500.000, "bank_rule": "virement_emis",
     "compte_charge": "Loyer du Bureau - A&S", "compte_banque": BANQUE,
     "compte_attente": DECOUVERT, "mode_paiement": "Virement", "periodicite": "Bimestriel", "jour_reference": 15,
     "declencheur": "Calendrier", "jour_declenchement": 15, "mois_ancre": 6,
     "template_reference": "Loyer Local du {jour}-{mm}-{yyyy} au {jour}-{mm2}-{yyyy2}",
     "actif": 0,
     "notes": "Montant passe de 5000 a 5500 en aout 2025 : creer une 2e ligne inactive si un "
              "rattrapage sur l'ancien montant est necessaire."},

    # ------------------------------------------------------- honoraire comptable
    # Mensuel, montant constant, TVA 19 % : 231,860 TTC = 193,860 HT + 38,000. Cale sur les
    # saisies reelles (ACC-JV-2026-00375 le 25/05, ACC-JV-2026-00274 le 23/04, ACC-JV-2026-00135
    # le 25/02), toutes autour du 25 et toutes sur « Charges Diverses ».
    # ⚠️ Le paiement porte sur le mois PRECEDENT (« Note d'honoraire comptable 04-2026 » reglee
    # le 25/05) : c'est le modele de reference qui porte cette periode, pas la date d'ecriture.
    # ⚠️ Une note peut en couvrir DEUX (ACC-JV-2026-00522, 463,720 pour 05-06/2026) : le flux en
    # cree une par mois, un regroupement bancaire restera donc a arbitrer a la main.
    {"cle": "honoraire_comptable", "libelle": "Note d'honoraire comptable", "type": "Honoraire",
     "montant": 231.860, "bank_rule": "virement_emis",
     "compte_charge": "Charges Diverses - A&S", "compte_banque": BANQUE,
     "compte_attente": DECOUVERT, "compte_tva": "TVA 19% - A&S", "taux_tva": 19.0,
     "mode_paiement": "Virement", "periodicite": "Mensuel", "jour_reference": 25,
     "declencheur": "Calendrier", "jour_declenchement": 25, "mois_ancre": 0,
     "template_reference": "Note d'honoraire comptable {mm_prec}-{yyyy_prec}",
     "idempotence": "Les deux", "actif": 0,
     "notes": "231,860 TTC constants depuis decembre 2025, regle vers le 25 pour le mois "
              "precedent. /!\\ A LAISSER INACTIF tant que le flux EMAIL tourne : "
              "`orchestrator.process_honoraire` lit la note reelle et produit une ecriture PLUS "
              "JUSTE (retenue a la source, TVA et timbre lus sur le PDF) sous la MEME cle "
              "« Note d'honoraire comptable MM-YYYY ». Les deux ne peuvent pas coexister ; "
              "cette regle n'est qu'un repli si l'email n'arrive plus."},

    # --------------------------------------------------------- recharge carte Total
    # Virement INTERNE, pas une charge : il alimente le compte que le flux Total credite ensuite.
    # Aucun identifiant periodise et plusieurs recharges possibles le meme mois -> l'idempotence
    # DOIT passer par la reference bancaire.
    {"cle": "recharge_carte_total", "libelle": "Recharge carte Total", "type": "Recharge carte",
     "montant": 0, "motifs_libelle": "TOTAL", "bank_rule": "chargement_carte",
     "compte_charge": "Carte Total - A&S", "compte_banque": BANQUE,
     "mode_paiement": "Virement", "periodicite": "Aucune",
     "template_reference": "Recharge Carte Total {reference}",
     "idempotence": "Reference bancaire", "actif": 0,
     "notes": "Absent du releve juin-juillet 2026 : libelle a confirmer (par analogie avec "
              "« CHARGEMENT CARTE TECHNOLOGIQUE »)."},
    {"cle": "chargement_carte_techno", "libelle": "Chargement carte technologique",
     "type": "Recharge carte", "montant": 0, "motifs_libelle": "TECHNOLOGIQUE",
     "bank_rule": "chargement_carte",
     "compte_charge": "Carte technologique - A&S", "compte_banque": BANQUE,
     "mode_paiement": "Virement", "periodicite": "Aucune",
     "template_reference": "Chargement carte technologique {reference}",
     "idempotence": "Reference bancaire", "actif": 0,
     "notes": "Confirme sur le releve : « CHARGEMENT CARTE TECHNOLOGIQUE » 1500 DT le 05/06 et "
              "1700 DT le 13/07/2026. Virement interne, pas une charge : la carte alimente "
              "ensuite les depenses Facebook Ads."},

    # NOTE : les echeances de pret et de leasing ne sont PLUS ici. Elles ont leur propre
    # table (« Contrats de financement »), parce qu'elles demandent un echeancier — dates de
    # debut et de fin, compteur N/total — et une ecriture a quatre lignes avec deux credits
    # bancaires distincts. Voir `expenses/contrats.py`.
)


def as_rows() -> list:
    """Copie mutable des defauts, avec les valeurs implicites remplies."""
    out = []
    for d in DEFAULTS:
        row = {
            "actif": 1, "tolerance": 0.01, "taux_tva": 0, "compte_tva": None,
            "jour_reference": 0, "idempotence": "Les deux", "motifs_libelle": "",
            "declencheur": "Banque", "jours_avant_fin_mois": 0,
            "jour_declenchement": 0, "mois_ancre": 0,
            "bank_rule": "", "notes": "",
        }
        row.update(d)
        out.append(row)
    return out
