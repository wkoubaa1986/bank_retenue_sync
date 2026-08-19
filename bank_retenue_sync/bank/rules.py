"""Table declarative des motifs de libelle bancaire.

Avant ce module, les motifs etaient eparpilles et DIVERGENTS : `movements.DECLARATION_KEYWORDS`
existait mais `orchestrator.py:181` reecrivait la liste en dur a cote ; les motifs de credit
vivaient dans `movements.py`, ceux d'exclusion Aramex dans `matching.py`, celui des especes dans
`especes.py`. Ajouter une categorie demandait de toucher quatre fichiers.

Une regle dit : « ce libelle, dans ce sens, appartient a cette categorie, et voici QUI s'en occupe ».
Elle ne dit jamais comment comptabiliser — c'est le role de `expenses/`.

DEUX PIEGES QUI DICTENT LA CONCEPTION
-------------------------------------
1. `_norm_op` COLLE les caracteres : 'COM ENC CHEQUE TN' devient 'COMENCCHEQUETNAC0000657260', qui
   contient 'ENC' ET 'CHEQ'. La commission de remise serait donc prise pour un encaissement de
   cheque. C'est le champ `sens` qui les separe — il n'est pas optionnel.
2. Certains predicats existants NE SONT PAS exprimables en sous-chaines normalisees :
   `matching.is_virement_credit` utilise un regex MOT ENTIER (\bVIR(?:EMENT|T)?\b) pour ne pas
   confondre 'VIR' avec 'SERVIR'. On ne le reecrit pas — le champ `matcher` le reference.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bank_retenue_sync.bank.movements import _norm_op

# --- ce que l'app fait d'un mouvement reconnu ---
ACTION_FLUX = "flux_existant"      # un flux deja code s'en occupe (cheques, traites, aramex...)
ACTION_JOURNAL = "journal_auto"    # une depense recurrente peut creer l'ecriture (expenses/engine)
ACTION_AGREGAT = "agregat"         # NE JAMAIS comptabiliser a l'unite : regroupe (frais bancaires)
ACTION_INFORMATIF = "informatif"   # visible et categorise, aucune automatisation prevue
ACTION_IGNORE = "ignore"           # bruit assume


@dataclass(frozen=True)
class BankRule:
    key: str
    label: str
    sens: str                              # 'debit' | 'credit' | 'both'
    categorie: str
    patterns: tuple = ()                   # motifs normalises : TOUS requis
    any_patterns: tuple = ()               # au moins un requis (si renseigne)
    exclude: tuple = ()                    # aucun ne doit apparaitre
    matcher: str = ""                      # predicat Python, prioritaire sur les motifs
    sous_categorie: str = ""
    action: str = ACTION_INFORMATIF
    flux: str = ""                         # flux cible quand action == ACTION_FLUX
    extractor: str = ""                    # 'bon' | 'effet' | 'cheque' -> movements.find_*_number
    # 'jour'     -> agregation journaliere (frais bancaires, jamais comptabilises a l'unite)
    # 'echeance' -> la ligne appartient a un groupe (reference de contrat, jour) dont la SOMME
    #               egale le decaissement d'une seule ecriture. Cf. classify.apparier_echeances.
    groupe: str = ""
    # Ligne qui n'existe JAMAIS seule : elle decompose une operation portee par d'autres lignes du
    # meme jour. Une TVA isolee n'est donc pas une echeance de leasing mais une TVA sur commission
    # (references 'CHG…' du releve) — sans ce drapeau, elle serait annoncee « echeance non
    # comptabilisee », ce qui est faux.
    composant: bool = False
    priorite: int = 100                    # la premiere regle qui matche (ordre croissant) gagne


# --------------------------------------------------------------------------------------------
# Les motifs ci-dessous sont cales sur les libelles REELS du releve Zitouna presents en base
# (DocType `Bank Transaction`, importes dans le registre par `registry.import_from_bank_transactions`).
# Les libelles de la periode recente restent a confirmer au premier export : tout libelle inconnu
# ressort en statut « a verifier », jamais en silence.
# --------------------------------------------------------------------------------------------

RULES = (
    # ============================== CREDITS ==============================
    # Priorite 10 : le cheque preavise DOIT passer avant `enc_cheque`, dont les motifs
    # ('ENC','CHEQ') matchent aussi 'ENCAISSCHEQUEPREAVISE'.
    BankRule(
        key="enc_cheque_preavise", label="Encaissement de cheque preavise", sens="credit",
        categorie="encaissement", sous_categorie="cheque_preavise",
        patterns=("ENCAISS", "CHEQUE", "PREAVISE"),
        action=ACTION_FLUX, flux="cheque_preavise", extractor="cheque", priorite=10),
    BankRule(
        key="enc_cheque", label="Remise de cheques encaissee", sens="credit",
        categorie="encaissement", sous_categorie="cheque",
        patterns=("ENC", "CHEQ"),
        action=ACTION_FLUX, flux="cheque", extractor="bon", priorite=20),
    BankRule(
        key="enc_effet", label="Encaissement de traite", sens="credit",
        categorie="encaissement", sous_categorie="traite",
        patterns=("ENCAISSEMENT", "EFFET"),
        action=ACTION_FLUX, flux="traite", extractor="effet", priorite=20),
    BankRule(
        key="vir_aramex", label="Virement recu d'Aramex", sens="credit",
        categorie="encaissement", sous_categorie="aramex",
        patterns=("ARAMEX",),
        action=ACTION_FLUX, flux="aramex", priorite=30),
    BankRule(
        key="versement_especes", label="Versement d'especes en agence", sens="credit",
        categorie="caisse", sous_categorie="versement",
        matcher="especes.is_versement_espece",
        action=ACTION_FLUX, flux="especes", priorite=40),
    # Regex mot entier volontairement conserve : 'VIR' ne doit pas matcher 'SERVIR'.
    BankRule(
        key="vir_client", label="Virement client recu", sens="credit",
        categorie="encaissement", sous_categorie="virement",
        matcher="matching.is_virement_credit", exclude=("ARAMEX",),
        action=ACTION_FLUX, flux="virement", priorite=50),

    # =============================== DEBITS ===============================
    # -- frais bancaires : JAMAIS comptabilises a l'unite, regroupes par jour --
    # Priorite 10 : 'TVA' est un libelle nu, il doit etre teste avant les motifs plus larges.
    # /!\ Ces deux lignes NE SONT PAS des frais bancaires, malgre les apparences.
    # Verifie au centime sur les ecritures ACC-JV-2026-00472/00473 : la TVA et le timbre de 1 DT
    # du releve appartiennent aux echeances de LEASING du meme jour.
    #     principal + profit + takaful + timbre 1 DT = HT ;  HT + TVA = TTC = credit Zitouna
    #     324,838 + 281,190 + 136,245 + 1 = 743,273  ->  + 115,145 = 858,418
    # Les laisser en frais bancaires gonflait l'ecriture de frais de 545 DT sur 659 en juin.
    # Il y a exactement DEUX lignes de timbre par jour de leasing : une par contrat.
    BankRule(
        key="tva_bancaire", label="TVA d'echeance de leasing", sens="debit",
        categorie="pret", sous_categorie="tva",
        patterns=("TVA",),
        action=ACTION_JOURNAL, groupe="echeance", composant=True, priorite=10),
    BankRule(
        key="droit_timbre", label="Droit de timbre d'echeance", sens="debit",
        categorie="pret", sous_categorie="timbre",
        patterns=("DROIT", "TIMBRE"),
        action=ACTION_JOURNAL, groupe="echeance", composant=True, priorite=15),
    BankRule(
        key="frais_tenue_compte", label="Frais de tenue de compte", sens="debit",
        categorie="frais_bancaires", sous_categorie="frais",
        patterns=("FRAIS", "COMPTE"),
        action=ACTION_AGREGAT, groupe="jour", priorite=15),
    BankRule(
        # « ENC » est REQUIS (libelle reel : « COM ENC CHEQUE TN AC-… ») : avec le seul couple
        # COM+CHEQUE, « REGLEMENT CHEQUE 4000965 SOUID TRADE COMPANY » matchait par le COM de
        # COMPANY — 1 785,277 DT de reglement fournisseur comptes en frais bancaires (04/2026).
        key="com_enc_cheque", label="Commission de remise de cheques", sens="debit",
        categorie="frais_bancaires", sous_categorie="commission",
        patterns=("COM", "ENC", "CHEQUE"),
        action=ACTION_AGREGAT, groupe="jour", priorite=20),
    BankRule(
        key="com_effet", label="Commission de remise d'effets", sens="debit",
        categorie="frais_bancaires", sous_categorie="commission",
        patterns=("COM", "EFFET"),
        action=ACTION_AGREGAT, groupe="jour", priorite=20),
    BankRule(
        key="com_virement", label="Commission de virement", sens="debit",
        categorie="frais_bancaires", sous_categorie="commission",
        patterns=("COM", "VIR"),
        action=ACTION_AGREGAT, groupe="jour", priorite=20),
    # 'COMM' et non 'COMMISSION' : le releve abrege ('COMM PERQ DE CHANGE-GARANTIE',
    # 'COMM REMISE EFFET'). 'COM VIR' ne contient pas 'COMM' et reste pris par la regle ci-dessus.
    BankRule(
        key="commission_diverse", label="Commission bancaire", sens="debit",
        categorie="frais_bancaires", sous_categorie="commission",
        patterns=("COMM",),
        action=ACTION_AGREGAT, groupe="jour", priorite=25),

    # -- echeances de pret : la banque debite separement le principal et le profit,
    #    ce qui donne la ventilation sans tableau d'amortissement --
    BankRule(
        key="pret_principal", label="Echeance de pret - principal", sens="debit",
        categorie="pret", sous_categorie="principal",
        patterns=("PAIEMENT", "PRINCIPAL"),
        action=ACTION_JOURNAL, groupe="echeance", priorite=30),
    BankRule(
        key="pret_profit", label="Echeance de pret - profit", sens="debit",
        categorie="pret", sous_categorie="profit",
        patterns=("PAIEMENT", "PROFIT"),
        action=ACTION_JOURNAL, groupe="echeance", priorite=30),
    BankRule(
        key="pret_assurance", label="Assurance de pret (Takaful)", sens="debit",
        categorie="pret", sous_categorie="assurance",
        patterns=("PRIME", "TAKAFUL"),
        action=ACTION_JOURNAL, groupe="echeance", priorite=30),

    # -- prelevements fiscaux et sociaux (flux deja automatises) --
    BankRule(
        key="prelevement_finances", label="Prelevement - declaration fiscale", sens="debit",
        categorie="fiscal", sous_categorie="declaration",
        patterns=("PRELEVEMENT", "FINANCES"),
        action=ACTION_FLUX, flux="declaration", priorite=30),
    BankRule(
        key="prelevement_cnss", label="Prelevement - CNSS", sens="debit",
        categorie="social", sous_categorie="cnss",
        patterns=("PRELEVEMENT", "CNSS"),
        action=ACTION_FLUX, flux="cnss", priorite=30),

    # -- alimentation d'une carte depuis le compte : virement INTERNE, pas une charge --
    # Deux formes reelles (constate le 2026-08-18) : l'ancien virement interne « CHARGEMENT
    # CARTE » et, depuis 06/2026, un paiement en ligne « PAIEMENT INTERNET 1808TOTAL TUNISI ».
    # Priorite 40 < 55 : la forme TOTAL est reprise a `paiement_internet` (qui ne sait que
    # categoriser), sinon la recharge restait « a verifier » et la depense recurrente
    # « Recharge carte Total » ne se declenchait jamais (son garde-fou bank_rule re-applique
    # cette regle au mouvement).
    BankRule(
        key="chargement_carte", label="Chargement de carte", sens="debit",
        categorie="virement_interne",
        matcher="rules.is_chargement_carte",
        action=ACTION_JOURNAL, priorite=40),

    # -- virements emis : salaires, loyer, fournisseurs. Le detail vient des lignes
    #    `BRS Depense Recurrente` parametrees par l'utilisateur.
    #
    # /!\ 'VIR' et non 'VIREMENT' : le releve reel abrege en 'VIR TN AUTRE BQ', SANS AUCUN NOM de
    # beneficiaire (verifie sur juin-juillet 2026 : les trois salaires y sont indiscernables par le
    # libelle). Le MONTANT est donc le seul discriminant, d'ou des lignes de depense recurrente
    # calees sur le montant seul. Les commissions ('COM VIR...') restent prises en amont par
    # `com_virement`, de priorite superieure. --
    BankRule(
        key="virement_emis", label="Virement emis", sens="debit",
        categorie="virement_emis",
        patterns=("VIR",),
        action=ACTION_JOURNAL, priorite=50),

    # -- cheques emis : le n° de cheque est dans le libelle (7 chiffres) --
    BankRule(
        key="cheque_emis_preavise", label="Cheque emis preavise", sens="debit",
        categorie="cheque_emis", sous_categorie="preavise",
        patterns=("CHEQUE", "EMIS", "PREAVISE"),
        action=ACTION_INFORMATIF, extractor="cheque", priorite=40),
    BankRule(
        # Priorite 18 < 20 : un libelle portant REGLEMENT est un cheque emis, jamais une
        # commission — le nom du beneficiaire peut contenir n'importe quoi (COMPANY, ENC...),
        # cette regle doit donc passer AVANT toutes les com_*.
        key="reglement_cheque", label="Reglement de cheque emis", sens="debit",
        categorie="cheque_emis",
        patterns=("REGLEMENT", "CHEQUE"),
        action=ACTION_INFORMATIF, extractor="cheque", priorite=18),

    # -- mise en place des financements (mai 2026) : provisions, acomptes, frais de dossier.
    #
    # /!\ 'DEBLOCAGE' CONTIENT 'BLOCAGE' : les deux libelles sont indiscernables par sous-chaine.
    # C'est le SENS qui les separe — le blocage est un debit, le deblocage un credit. Sans ce
    # filtre, un deblocage de 20 000 DT serait compte comme un blocage.
    BankRule(
        key="provision_blocage", label="Blocage de provision", sens="debit",
        categorie="financement", sous_categorie="provision",
        patterns=("BLOCAGE", "PROVISION"),
        action=ACTION_INFORMATIF, groupe="echeance", priorite=30),
    BankRule(
        key="provision_deblocage", label="Deblocage de provision", sens="credit",
        categorie="financement", sous_categorie="provision",
        patterns=("BLOCAGE", "PROVISION"),
        action=ACTION_INFORMATIF, priorite=30),
    BankRule(
        key="deblocage_hamech", label="Deblocage Hamech Jedya (avance leasing)", sens="credit",
        categorie="financement", sous_categorie="avance",
        patterns=("DEBLOCAGE", "HAMECH"),
        action=ACTION_INFORMATIF, priorite=30),
    BankRule(
        key="acompte_financement", label="Acompte sur financement", sens="debit",
        categorie="financement", sous_categorie="acompte",
        patterns=("ACOMPTE", "FINANCEMENT"),
        action=ACTION_INFORMATIF, groupe="echeance", priorite=30),
    BankRule(
        key="frais_dossier", label="Frais d'etude et de montage", sens="debit",
        categorie="financement", sous_categorie="frais_dossier",
        patterns=("FRAIS", "ETUDE", "MONTAGE"),
        action=ACTION_JOURNAL, groupe="echeance", priorite=30),
    BankRule(
        key="cheque_repris", label="Cheque repris (impaye)", sens="debit",
        categorie="impaye",
        patterns=("CHEQUE", "REPRIS"), extractor="cheque",
        action=ACTION_INFORMATIF, priorite=35),

    # -- divers, categorises mais non automatises (hors perimetre assume) --
    BankRule(
        key="reglement_cb", label="Reglement par carte bancaire", sens="debit",
        categorie="carte_bancaire",
        patterns=("REGLEMENT", "CB"),
        action=ACTION_INFORMATIF, priorite=50),
    BankRule(
        key="paiement_internet", label="Paiement en ligne (facturier)", sens="debit",
        categorie="abonnement",
        patterns=("PAIEMENT", "INTERNET"),
        action=ACTION_INFORMATIF, priorite=55),
    BankRule(
        key="retrait_especes", label="Retrait d'especes", sens="debit",
        categorie="caisse", sous_categorie="retrait",
        patterns=("RETRAIT", "ESPECES"),
        action=ACTION_INFORMATIF, priorite=55),
)

RULES_BY_KEY = {r.key: r for r in RULES}


def is_chargement_carte(m: dict) -> bool:
    """Recharge de la carte Total, sous ses DEUX formes bancaires reelles :
    « CHARGEMENT CARTE » (virement interne historique) et « PAIEMENT INTERNET …TOTAL … »
    (forme constatee depuis 06/2026 : 22/06 et 18/08 a 603, 20/07 et 31/07 a 703,5)."""
    op = _norm_op(m.get("operation"))
    if "CHARGEMENT" in op and "CARTE" in op:
        return True
    return "PAIEMENT" in op and "INTERNET" in op and "TOTAL" in op


def _resolve_matcher(dotted: str):
    """Resout 'module.fonction' vers le predicat. Import differe : `matching` importe `movements`,
    qui importerait ce module — la resolution a l'appel casse le cycle."""
    mod, fn = dotted.split(".", 1)
    if mod == "matching":
        from bank_retenue_sync.encaissement import matching
        return getattr(matching, fn)
    if mod == "especes":
        from bank_retenue_sync.encaissement import especes
        return getattr(especes, fn)
    if mod == "rules":
        return globals()[fn]
    raise ValueError(f"matcher inconnu : {dotted}")


def sens_of(m: dict) -> str:
    return "debit" if (m.get("debit") or 0) else "credit"


def matches(rule: BankRule, m: dict) -> bool:
    """Ce mouvement releve-t-il de cette regle ?"""
    if rule.sens != "both" and rule.sens != sens_of(m):
        return False
    op = _norm_op(m.get("operation"))
    if rule.exclude and any(_norm_op(x) in op for x in rule.exclude):
        return False
    if rule.matcher:
        return bool(_resolve_matcher(rule.matcher)(m))
    if rule.patterns and not all(_norm_op(p) in op for p in rule.patterns):
        return False
    if rule.any_patterns and not any(_norm_op(p) in op for p in rule.any_patterns):
        return False
    return bool(rule.patterns or rule.any_patterns)


def find_rule(m: dict, rules=None):
    """Premiere regle applicable, par priorite croissante. None si le libelle est inconnu —
    ce cas produit un statut « a verifier », jamais un silence."""
    for rule in sorted(rules or RULES, key=lambda r: (r.priorite, r.key)):
        if matches(rule, m):
            return rule
    return None


def by_key(key: str) -> BankRule:
    return RULES_BY_KEY[key]


def keywords(key: str) -> tuple:
    """Motifs d'une regle, pour reexporter les constantes historiques de `movements.py`
    sans dupliquer les valeurs."""
    return RULES_BY_KEY[key].patterns


def extract_numero(rule: BankRule, m: dict):
    """N° porte par le libelle, selon l'extracteur de la regle."""
    if not rule or not rule.extractor:
        return None
    from bank_retenue_sync.bank import movements as mv

    op = m.get("operation") or ""
    return {
        "bon": mv.find_bon_number,
        "effet": mv.find_effet_number,
        "cheque": mv.find_cheque_number,
    }[rule.extractor](op)
