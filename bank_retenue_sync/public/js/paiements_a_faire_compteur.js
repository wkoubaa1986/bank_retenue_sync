// Compteur du raccourci « Paiements à faire » (espace Comptabilité).
//
// Frappe ne pose un badge que sur les raccourcis de type DocType (shortcut_widget.js :
// `if (this.type == "DocType") frappe.db.count(...)`). Notre raccourci vise un RAPPORT :
// on complète donc le widget — même pastille, même rendu (set_count), mais le nombre vient
// de la méthode whitelistée du rapport, qui applique la définition exacte des lignes.
//
// Chargé globalement (app_include_js) : le patch ne s'applique qu'une fois et ne touche
// que ce raccourci précis — tous les autres widgets restent intacts.
(function () {
  const RAPPORT = "Paiements a faire";
  const METHODE =
    "bank_retenue_sync.bank_retenue_sync.report.paiements_a_faire.paiements_a_faire.nb_a_faire";

  function patch() {
    const cls =
      frappe.widget && frappe.widget.widget_factory && frappe.widget.widget_factory.shortcut;
    if (!cls || cls.__brs_compteur_paiements) return !!cls;
    cls.__brs_compteur_paiements = true;

    const set_actions = cls.prototype.set_actions;
    cls.prototype.set_actions = function () {
      set_actions.call(this);
      if (this.in_customize_mode) return;
      if (this.type === "Report" && this.link_to === RAPPORT) {
        frappe
          .xcall(METHODE)
          .then((n) => this.set_count(n || 0))
          .catch(() => {});
      }
    };
    return true;
  }

  // widget_factory est défini par le bundle desk, chargé avant les app_include_js — le
  // repli différé ne sert qu'aux pages où le bundle arrive en retard.
  if (!patch()) {
    $(document).ready(patch);
  }
})();
