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
