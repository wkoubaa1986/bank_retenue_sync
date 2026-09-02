"""Construction du brouillon 'Encaissement Paiement' a partir des lignes appariees.

L'Encaissement Paiement est le DocType custom (customization_app, DB-only) dont la SOUMISSION
declenche le server script "Traitement des encaissement" (After Submit) qui compense les
Payment Entry (compte d'attente -> banque). On cree ici le doc EN BROUILLON : l'utilisateur revoit
puis soumet. On ne touche PAS a customization_app.

Mapping des champs (verifie sur le server script) :
  chèques_a_encaisser (Liste Cheque)        : ref_paiement, statut='Versé', bon_remise, date,
                                              n_cheque, valeur, emmeteur
  traite_bancaire_a_encaissées (Liste Traite): idem (bon_remise = ref/n° effet)
  livraison_aramex_a_encaisser (Liste Aramex): ref_paiement, type='Virement', n_virement,
                                              n_aramex, valeur, emmeteur, date
  dette_client (Liste des Dettes client)     : EN-TETE, un par (client, virement recu) :
                                              client, type='Virement', n_chèque=<ref bancaire>,
                                              banque, valeur_du_cheque=<montant recu>
  dettes_a_encaisser (Liste Dettes)          : DETAIL, un par dette soldee : ref_paiement, bl
                                              (Sales Order), valeur=<montant de la dette>,
                                              type/n_chèque/banque IDENTIQUES a l'en-tete
                                              (= cle de jointure), valeur_du_cheque=<part alloue>

Le server script lit surtout ref_paiement + statut (cheque/traite) / type + n_virement (aramex) ;
les autres champs sont pour la lisibilite.

Le flux DETTES fonctionne autrement : le script calcule une PROPORTION
`detail.valeur_du_cheque / entete.valeur_du_cheque` par dette, reecrit le `payment_schedule` du
Sales Order, SUPPRIME la PE de dette, puis cree une PE sur Zitouna avec une Payment Entry
Reference par Sales Order au prorata. C'est ce mecanisme qui rend le paiement PARTIEL et le
virement GROUPE possibles sans code supplementaire — d'ou la contrainte : la somme des parts de
detail doit valoir le total de l'en-tete, sinon les references de la PE allouent plus que son
montant et ERPNext refuse la soumission.

/!\ LA TABLE CHOISIE N'EST PAS COSMETIQUE : les trois branches du server script ne se valent pas.
  cheques/traites : la PE est ANNULEE puis recopiee, `posting_date` = la date bancaire fournie,
                    le mode de paiement est conserve.
  aramex          : la PE d'origine est SUPPRIMEE (delete_doc force=True), `posting_date` et
                    `reference_date` sont forcees a AUJOURD'HUI, `mode_of_payment` est force a
                    "Virement", et le `payment_schedule` du Sales Order est reecrit.
Classer une traite dans la table Aramex (p. ex. parce que son `reference_no` porte un prefixe
'Aramex N:' saisi par erreur) detruirait donc la PE et fausserait date et mode de paiement.
=> le classement suit le COMPTE D'ATTENTE de la PE, jamais le libelle de sa reference.
"""
from __future__ import annotations

import frappe

ENCAISSEMENT_DOCTYPE = "Encaissement Paiement"


def build_encaissement(cheque_rows=None, traite_rows=None, aramex_rows=None,
                       virement_lots=None, insert: bool = True):
    """Cree un Encaissement Paiement BROUILLON avec les lignes fournies.
    `virement_lots` : lots produits par `matching.match_virements` (en-tete + detail).
    Retourne le doc (in-memory si insert=False), ou None si aucune ligne."""
    cheque_rows = cheque_rows or []
    traite_rows = traite_rows or []
    aramex_rows = aramex_rows or []
    virement_lots = virement_lots or []
    if not (cheque_rows or traite_rows or aramex_rows or virement_lots):
        return None

    doc = frappe.new_doc(ENCAISSEMENT_DOCTYPE)

    for r in cheque_rows:
        doc.append("chèques_a_encaisser", {
            "ref_paiement": r["ref_paiement"],
            "statut": r.get("statut", "Versé"),
            "bon_remise": r.get("bon_remise"),
            "n_cheque": r.get("n_cheque"),
            "valeur": r.get("valeur"),
            "emmeteur": r.get("emmeteur"),
            "date": r.get("date"),
        })
    for r in traite_rows:
        doc.append("traite_bancaire_a_encaissées", {
            "ref_paiement": r["ref_paiement"],
            "statut": r.get("statut", "Versé"),
            "bon_remise": r.get("bon_remise"),
            "n_cheque": r.get("n_cheque"),
            "valeur": r.get("valeur"),
            "emmeteur": r.get("emmeteur"),
            "date": r.get("date"),
        })
    for r in aramex_rows:
        doc.append("livraison_aramex_a_encaisser", {
            "ref_paiement": r["ref_paiement"],
            "type": r.get("type", "Virement"),
            "n_virement": r.get("n_virement"),
            "n_aramex": r.get("n_aramex"),
            "valeur": r.get("valeur"),
            "emmeteur": r.get("emmeteur"),
            "date": r.get("date"),
        })

    for lot in virement_lots:
        doc.append("dette_client", {
            "client": lot["client"],
            "valeur_total": lot["total"],
            "date": lot.get("date"),
            "type": "Virement",
            "n_chèque": lot.get("n_virement"),
            "banque": lot.get("banque"),
            "valeur_du_cheque": lot["total"],
        })
        for l in lot["lignes"]:
            doc.append("dettes_a_encaisser", {
                "ref_paiement": l["ref_paiement"],
                "emmeteur": lot["client"],      # doit valoir le `name` du Customer : le script
                                                # s'en sert comme cle vers la ligne d'en-tete
                "bl": l.get("bl"),
                "valeur": l["valeur"],
                "date": lot.get("date"),
                "type": "Virement",
                "n_chèque": lot.get("n_virement"),
                "banque": lot.get("banque"),
                "valeur_du_cheque": l["part"],
            })

    # Totaux indicatifs (le server script ne s'en sert pas ; confort de relecture).
    doc.total_des_chèques = round(sum((r.get("valeur") or 0) for r in cheque_rows
                                     if r.get("statut", "Versé") == "Versé"), 3)
    doc.total_des_traites_bancaires_encaissés = round(sum((r.get("valeur") or 0) for r in traite_rows
                                                          if r.get("statut", "Versé") == "Versé"), 3)
    doc.total_virement_a = round(sum((r.get("valeur") or 0) for r in aramex_rows), 3)
    doc.total_virement_d = round(sum(lot["total"] for lot in virement_lots), 3)

    # ⚠️ SANS CE DRAPEAU, NOTRE IMPUTATION EST DÉTRUITE À L'ENREGISTREMENT.
    # Le server script « generartion_list dette » se déclenche à CHAQUE save :
    # il vide `dettes_a_encaisser` et la régénère en FIFO PAR DATE, sans jamais
    # chercher d'égalité. `allocation.allocate` a pourtant déjà tranché, et bien
    # mieux — dette exacte d'abord, sous-ensemble ensuite, FIFO en dernier.
    #
    # Cas réel du 01/09/2026 (ENC-01-09-2026-00001) : 898 DT reçus d'American
    # Cooperative School, qui devait EXACTEMENT 898 sur SAL-ORD-2026-02770. Le
    # FIFO a payé une dette de 31 DT de 2025, puis 867 sur celle de 898 — et
    # recréé 31 DT d'impayé sur la bonne commande. Notre allocateur avait choisi
    # la dette exacte.
    #
    # Le champ s'appelle « allocation manuelle » parce qu'il a été créé pour la
    # caisse (l'employé choisit ses dettes), mais ce qu'il dit au script est :
    # « l'imputation est DÉJÀ décidée, ne la refais pas ». C'est exactement
    # notre cas. On ne le pose que si une allocation de dettes existe : sans
    # lot de virement, il n'y a rien à protéger.
    if virement_lots:
        doc.custom_allocation_manuelle = 1

    if insert:
        doc.insert(ignore_permissions=True)  # BROUILLON : jamais submit en v1
    return doc
