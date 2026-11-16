"""Bordereau de remise collecte (cheques, traites, especes) — BIAT ou Zitouna.

Demande du 16/11/2026 : « ajoute un truc qui collecte les bordereaux de remise, pour BIAT ou Zitouna ;
apres, l'identification bancaire fera le necessaire ; juste collecter les bordereaux ». Le portail de
la BIAT ne montre pas le detail d'une remise comme celui de Zitouna : le seul document qui dit quels
cheques elle contient est le bordereau papier. On le range ici, avec les trois choses qui permettent
de le retrouver depuis le releve — la banque, son numero (celui que la banque reprend dans le libelle
du credit) et sa date. Une fois range, l'IA le lit en arriere-plan et cherche ses pieces dans ERPNext
(`api/bordereaux.lire`) : un controle, qui ne bloque ni ne comptabilise rien.
"""
import frappe
from frappe.model.document import Document

from bank_retenue_sync.api import bordereaux as B


class BRSBordereauRemise(Document):
    def autoname(self):
        self.numero = B.numero_propre(self.numero)
        self.name = B.nom_du_bordereau(self.banque, self.numero)
        if frappe.db.exists(self.doctype, self.name):
            frappe.throw(f"Ce bordereau est déjà collecté : {self.name}.", title="Bordereau déjà collecté")

    def validate(self):
        self.numero = B.numero_propre(self.numero)
        if not self.remis_par:
            self.remis_par = frappe.session.user
        if not self.is_new() and self.name != B.nom_du_bordereau(self.banque, self.numero):
            frappe.throw("La banque et le numéro identifient le bordereau : pour les corriger, supprimez-le et "
                         "collectez-le à nouveau.")

    def after_insert(self):
        if self.nature in B.NATURES_LUES:
            B.lancer_lecture(self.name)
