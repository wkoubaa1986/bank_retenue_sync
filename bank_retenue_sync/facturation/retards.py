"""Le rattrapage des charges en retard : qu'est-ce qu'une « retardataire », et où la ranger.

Une charge est SAISIE le mois M mais COMPTABILISÉE le mois M-1. Quand M-1 a déjà été envoyé au
comptable, cette charge est partie APRÈS l'envoi — elle manque au dossier de M-1, et rien ne la
signale. C'est ce trou que ce module détecte : une charge dont le mois de comptabilisation est
déjà « envoyé » mais dont la date de saisie (`creation`) est POSTÉRIEURE à cet envoi.

⚠️ TOUT ICI EST PUR : NI FRAPPE, NI BASE. Ces fonctions portent la règle — creation vs date
d'envoi, exclusion des déjà rattachées, regroupement par mois d'origine — et rien d'autre. Les
lectures (quelles charges, quels envois, quelles pièces) vivent dans `charges.py` et `cloture.py`
qui appellent ces fonctions sur des données déjà lues. C'est ce qui les rend testables sans site.

⚠️ ON REGARDE `creation`, PAS `modified`. Une charge modifiée après l'envoi n'est PAS une
retardataire : elle était déjà dans le dossier, seule sa correction est postérieure. Ce qui nous
intéresse, c'est la pièce ENTRÉE trop tard — celle que le comptable n'a jamais vue.
"""
from __future__ import annotations

from datetime import datetime

PRECISION = 3

# Les deux seules familles concernées : un achat (facture) et une dépense (écriture de journal).
# Les ventes et les retenues sont hors périmètre — c'est un rattrapage de CHARGES.
TYPES_CHARGE = ("Purchase Invoice", "Journal Entry")


def cle_voucher(document_type: str, document_name: str) -> str:
    """L'identité d'une pièce, stable d'un appel à l'autre : « Purchase Invoice|ACC-PINV-… »."""
    return "%s|%s" % (document_type or "", document_name or "")


def _as_datetime(valeur) -> datetime | None:
    """Un `datetime` quel que soit ce qu'on reçoit : objet, chaîne ISO, ou rien.

    Frappe rend le plus souvent des `datetime`, mais un état relu du cache ou passé en test peut
    arriver en chaîne. On ne devine pas un format exotique : on tente l'ISO, puis les deux formes
    que le framework écrit, sinon on rend None — et une comparaison contre None se traite en amont.
    """
    if valeur is None or valeur == "":
        return None
    if isinstance(valeur, datetime):
        return valeur
    texte = str(valeur).strip()
    try:
        return datetime.fromisoformat(texte)
    except ValueError:
        pass
    for forme in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(texte, forme)
        except ValueError:
            continue
    return None


def est_en_retard(creation, date_envoi) -> bool:
    """La charge a-t-elle été saisie APRÈS l'envoi de son mois ? Fonction pure.

    Sans date de saisie ou sans date d'envoi lisibles, on répond NON : mieux vaut manquer une
    retardataire douteuse que d'en inventer une sur une donnée qu'on ne sait pas lire.
    """
    c, e = _as_datetime(creation), _as_datetime(date_envoi)
    if c is None or e is None:
        return False
    return c > e


def detecter_retardataires(candidats: list[dict], envois: dict, rattachees) -> list[dict]:
    """Parmi les charges candidates, celles qui sont vraiment des retardataires. Fonction pure.

    · `candidats` : lignes de charge, chacune portant au moins `document_type`, `document_name`,
      `mois_origine` (« YYYY-MM ») et `creation`.
    · `envois` : { mois -> date d'envoi } des mois DÉJÀ marqués envoyés. Un mois absent = pas
      encore envoyé, donc ses charges tardives ne sont pas encore un manque.
    · `rattachees` : ensemble des clés de pièces déjà rattachées à UN dossier, quel qu'il soit —
      une charge déjà rattrapée ailleurs ne doit pas ressurgir.

    L'ordre d'entrée est conservé : c'est au lecteur (l'écran, le ZIP) de trier.
    """
    deja = set(rattachees or ())
    out = []
    for c in candidats:
        mois = c.get("mois_origine")
        envoi = envois.get(mois) if mois else None
        if not envoi:
            continue
        if not est_en_retard(c.get("creation"), envoi):
            continue
        if cle_voucher(c.get("document_type"), c.get("document_name")) in deja:
            continue
        out.append(c)
    return out


def grouper_par_mois_origine(retards: list[dict]) -> list[dict]:
    """[{mois, lignes, totaux}] trié du mois d'origine le plus ancien au plus récent.

    Le regroupement est ce qui interdit le double comptage : chaque mois d'origine forme son
    propre sous-bloc, avec son propre sous-total, jamais fondu dans le total du mois porteur.
    """
    par_mois: dict[str, list] = {}
    for l in retards:
        par_mois.setdefault(l.get("mois_origine") or "", []).append(l)
    out = []
    for mois in sorted(par_mois):
        lignes = par_mois[mois]
        out.append({"mois": mois, "lignes": lignes, "totaux": sous_total(lignes)})
    return out


def sous_total(lignes: list[dict]) -> dict:
    """Le sous-total d'un paquet de retardataires — les mêmes colonnes que les blocs de charges."""
    def somme(champ):
        return round(sum(float(l.get(champ) or 0) for l in lignes), PRECISION)

    return {
        "nombre": len(lignes),
        "ht": somme("ht"),
        "tva": somme("tva"),
        "ttc": somme("ttc"),
        "retenue": somme("retenue"),
        "sans_justificatif": sum(1 for l in lignes
                                 if l.get("justificatif_requis") and not l.get("justificatifs")),
        "avec_justificatif": sum(1 for l in lignes if l.get("justificatifs")),
    }
