import frappe
from frappe import _
from frappe.model.document import Document


class BankRetenueSyncSettings(Document):
    def validate(self):
        if self.service_url:
            self.service_url = self.service_url.rstrip("/")
            if not self.service_url.startswith(("http://", "https://")):
                frappe.throw(_("L'URL du service doit commencer par http:// ou https://"))

        if self.enabled and not self.get_password("push_secret", raise_exception=False):
            # AVERTISSEMENT, plus un refus. Ce controle datait du protocole PUSH : le service
            # devait pousser ses donnees vers un endpoint public signe en HMAC. Tous les flux
            # lisent aujourd'hui les exports du service (pull), et aucun endpoint de reception
            # n'existe. Exiger la cle bloquait `enabled`, donc le coupe-circuit de TOUTES les
            # taches planifiees, pour une exigence sans objet.
            frappe.msgprint(
                _("Aucune cle HMAC : sans objet tant qu'aucun flux n'utilise le protocole push."),
                indicator="orange",
                alert=True,
            )

        if self.webhook_user == "Administrator":
            frappe.msgprint(
                _("Utiliser un compte dedie plutot qu'Administrator pour le webhook."),
                indicator="orange",
                alert=True,
            )

    def on_update(self):
        frappe.clear_cache(doctype="Bank Retenue Sync Settings")
