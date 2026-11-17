"""La source email du terminal de paiement BIAT rejoint la table des Settings (17/11/2026).

Sans cette ligne le flux marche deja (repli sur le defaut de `mail/config.py`) ; elle rend l'expediteur
visible et modifiable. On n'ajoute QUE cette ligne : `seed_defaults` remettrait aussi celles que
l'utilisateur aurait supprimees."""
import frappe

from bank_retenue_sync.mail import config as mail_config

CLE = "biat_monetique"


def execute():
    if not frappe.db.exists("DocType", mail_config.SETTINGS):
        return
    doc = frappe.get_single(mail_config.SETTINGS)
    lignes = doc.get(mail_config.TABLE_FIELD) or []
    if not lignes or any(l.cle == CLE for l in lignes):
        return                                         # table vide : les defauts s'appliquent deja en entier
    doc.append(mail_config.TABLE_FIELD, dict(next(d for d in mail_config.DEFAULTS if d["cle"] == CLE)))
    doc.flags.ignore_mandatory = True
    doc.save(ignore_permissions=True)
