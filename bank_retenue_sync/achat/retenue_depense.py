"""La retenue à la source ACHAT sur une dépense de caisse facturée.

POURQUOI UN MODULE À PART DE `achat/retenue.py`
-----------------------------------------------
Celui-là pose la retenue dans la TABLE DES TAXES d'une facture d'achat, en ligne de déduction.
Ici il n'y a pas de facture : la dépense de caisse produit directement une écriture de journal.
Le taux, le seuil et le compte sont les mêmes — on les lui emprunte — mais le geste diffère :
une LIGNE DE CRÉDIT dans l'écriture, en face du paiement diminué d'autant.

    Cr  Banque / Espèces            TTC − retenue
    Cr  Retenue a la source achat   retenue
    Dr  TVA
    Dr  Compte de charge            TTC − TVA

⚠️ LE MONTANT NE SUFFIT PAS À DÉCLENCHER. Sur les quatre écritures de plus de 1 000 DT passées en
caisse depuis le 01/09/2026, TROIS sont des primes de salariés (« Prime 3ème Trimestre », type
« Dépense non facturée »). Retenir 1 % dessus serait faux — une prime relève de l'IRPP et de la
CNSS, pas de la retenue sur achat. Le déclencheur est donc le TYPE de dépense autant que le
montant, et cela exclut du même coup Aramex et Total, qui n'entrent jamais par la caisse.

⚠️ L'ASSIETTE EXCLUT LE TIMBRE FISCAL, comme sur les factures d'achat (règle vérifiée au millime
sur 17 factures locales de 2026). La caisse ne saisit pas le timbre séparément : il reste dans le
compte de charge. Le montant proposé est donc calculé sur le TTC, et l'opérateur peut le corriger
à l'écran quand la facture porte un timbre.
"""
from __future__ import annotations

from frappe.utils import flt

from bank_retenue_sync.achat import retenue as R

PRECISION = 3

#: Les types de dépense où une retenue sur achat a un sens : il y a un fournisseur et une facture.
TYPES_ASSUJETTIS = ("Dépense avec facture", "Facture d'achat")

#: Le mode de règlement qui porte la retenue dans la fenêtre de saisie. Ce n'est pas un moyen de
#: paiement : c'est la part que l'on NE verse PAS au fournisseur et qui reste due au Trésor.
MODE_RETENUE = "Retenue a la source achat"


def compte() -> str:
    return R.compte_retenue()


def seuil() -> float:
    return flt(R._seuil(), PRECISION)


def taux() -> float:
    return flt(R._taux(), PRECISION)


def assujettie(type_depense, ttc, timbre=0) -> bool:
    """Cette dépense franchit-elle la barre ? Fonction pure."""
    if type_depense not in TYPES_ASSUJETTIS:
        return False
    return flt(ttc) - flt(timbre) >= seuil()


def retenue_due(ttc, timbre=0, type_depense="Dépense avec facture") -> float:
    """Le montant à retenir, ou 0. Fonction pure — c'est elle que les tests éprouvent."""
    if not assujettie(type_depense, ttc, timbre):
        return 0.0
    base = flt(ttc) - flt(timbre)
    return flt(base * taux() / 100.0, PRECISION)


def net_a_payer(ttc, retenue) -> float:
    """Ce qui sort réellement de la caisse ou de la banque."""
    return flt(flt(ttc) - flt(retenue), PRECISION)


# ── Contrôle de la retenue à la saisie (30/10/2026) ──────────────────────────────────────────
# Cas réel : chèque 4001060 (Ste HEDI RIAHI) émis à 4 666,266 pour une facture de 4 713,400 TTC.
# L'opérateur avait bien déduit 1 % sur le chèque, mais l'écran de caisse n'avait pas proposé la
# retenue (montant rempli par l'analyse de la facture, sans recalcul) et l'écriture est partie au
# brut : écart de −47,134 à l'identification bancaire, fournisseur surpayé d'autant.
#
# Le remède est de partir de ce qui SORT de la banque : si l'opérateur saisit le montant du chèque,
# la retenue en découle (TTC − payé) et on vérifie qu'elle vaut bien le taux sur une assiette
# admissible. Et si une dépense assujettie part sans retenue, on le dit avant d'enregistrer.

#: Écart (DT) toléré entre la retenue déduite du paiement et la retenue théorique : l'arrondi au
#: millime de l'assiette, rien de plus. Au-delà, le chèque ne porte pas « 1 % », il porte autre chose.
TOLERANCE_RETENUE = 0.02

#: Assiettes qu'un opérateur peut légitimement avoir prises : le TTC hors timbre (la règle), le TTC
#: brut (timbre oublié), et le TTC moins 1 DT (timbre de 1 DT présent sur la facture mais non lu).
def _assiettes_admissibles(ttc, timbre=0):
    ttc, timbre = flt(ttc), flt(timbre)
    return {flt(ttc - timbre, PRECISION), flt(ttc, PRECISION), flt(ttc - 1.0, PRECISION)}


def retenue_deduite(ttc, paye):
    """La retenue qu'implique un paiement NET : TTC − payé. None si le paiement ne peut pas être
    un net (nul, négatif, ou au moins égal au TTC)."""
    ttc, paye = flt(ttc), flt(paye)
    if paye <= 0 or paye >= ttc:
        return None
    return flt(ttc - paye, PRECISION)


def retenue_plausible(ttc, retenue, timbre=0) -> bool:
    """La retenue vaut-elle le taux sur une assiette admissible ? Fonction pure."""
    retenue = flt(retenue)
    if retenue <= 0:
        return False
    return any(abs(flt(base * taux() / 100.0, PRECISION) - retenue) <= TOLERANCE_RETENUE
               for base in _assiettes_admissibles(ttc, timbre))


def controle(type_depense, ttc, retenue=None, paye=None, timbre=0) -> dict:
    """Ce que la saisie doit retenir et ce qu'elle doit signaler. Fonction pure.

    Priorité des sources : le montant PAYÉ (ce que la banque verra) prime sur la retenue saisie,
    qui prime sur la proposition. Retourne :
      retenue, net           -> ce qui sera comptabilisé ;
      origine                -> 'paiement' | 'saisie' | 'proposition' ;
      assujettie             -> la dépense franchit le seuil pour son type ;
      alerte_sans_retenue    -> assujettie ET retenue nulle : à confirmer explicitement ;
      avertissements         -> phrases à montrer, jamais bloquantes.
    """
    ttc, timbre = flt(ttc, PRECISION), flt(timbre, PRECISION)
    assuj = assujettie(type_depense, ttc, timbre)
    avertissements = []
    if paye not in (None, "") and flt(paye) > 0:
        origine = "paiement"
        deduite = retenue_deduite(ttc, paye)
        if deduite is None:
            retenue = 0.0
            if flt(paye) > ttc:
                avertissements.append(
                    "Le montant payé (%s) dépasse le TTC de la facture (%s) : vérifiez la saisie."
                    % (flt(paye, PRECISION), ttc))
        else:
            retenue = deduite
    elif retenue not in (None, ""):
        origine = "saisie"
        retenue = flt(retenue, PRECISION)
    else:
        origine = "proposition"
        retenue = retenue_due(ttc, timbre, type_depense)

    if retenue > 0 and not retenue_plausible(ttc, retenue, timbre):
        attendu = retenue_due(ttc, timbre, type_depense) if assuj \
            else flt((ttc - timbre) * taux() / 100.0, PRECISION)
        avertissements.append(
            "La retenue de %s n'est pas %s %% de l'assiette (attendu %s sur %s TTC) : "
            "vérifiez le montant payé ou la retenue."
            % (retenue, flt(taux(), PRECISION), attendu, ttc))
    alerte = bool(assuj and retenue <= 0)
    if alerte:
        avertissements.append(
            "Dépense « %s » de %s TTC, au-dessus du seuil de %s : aucune retenue à la source. "
            "Confirmez que le fournisseur n'est pas assujetti, ou saisissez le montant du chèque."
            % (type_depense, ttc, seuil()))
    return {"retenue": retenue, "net": net_a_payer(ttc, retenue), "origine": origine,
            "assujettie": assuj, "alerte_sans_retenue": alerte,
            "avertissements": avertissements}
