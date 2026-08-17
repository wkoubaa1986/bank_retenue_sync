// Vue liste des factures d'achat : où en est le certificat de retenue TEJ de chacune.
//
// ⚠️ SANS ÇA, L'ÉTAT D'UNE DÉCLARATION NE SE VOIT QU'EN OUVRANT LA FACTURE, UNE PAR UNE. Un dépôt
// en analyse chez l'administration fiscale, un dépôt refusé, un envoi qui ne s'est jamais conclu :
// rien de tout ça n'était visible d'ici, alors que c'est exactement l'écran où l'on cherche
// « qu'est-ce qui reste à faire ». La ligne DEP-2026-00234 est restée `refuse` deux jours sans que
// rien ne le dise.
//
// ⚠️ ON ÉTEND, ON NE REMPLACE PAS. ERPNext AFFECTE `frappe.listview_settings["Purchase Invoice"]`
// en entier (statuts Payé / En retard / En attente, actions groupées). Les fichiers de hook étant
// chargés APRÈS celui du DocType, écraser l'objet ici ferait disparaître tout ça en silence.

frappe.listview_settings["Purchase Invoice"] = frappe.listview_settings["Purchase Invoice"] || {};

(() => {
  const reglages = frappe.listview_settings["Purchase Invoice"];
  const precedent = reglages.refresh;
  reglages.refresh = function (listview) {
    if (precedent) precedent.call(this, listview);
    poser_etats_tej(listview);
  };
})();

// Un état, une couleur, un libellé. Le vocabulaire est celui de `BRS Depot TEJ` : le traduire
// deux fois, c'est le laisser dériver.
const ETATS_TEJ = {
  emis: { couleur: "green", libelle: __("Certificat TEJ ✓") },
  genere: { couleur: "green", libelle: __("Certificat TEJ ✓") },
  en_envoi: { couleur: "blue", libelle: __("TEJ : envoi en cours") },
  en_analyse: { couleur: "orange", libelle: __("TEJ : dépôt en analyse") },
  incertain: { couleur: "red", libelle: __("TEJ : à vérifier sur le portail") },
  refuse: { couleur: "gray", libelle: __("TEJ : dépôt refusé") },
  echec: { couleur: "gray", libelle: __("TEJ : rien n'a été soumis") },
  abandonne: { couleur: "gray", libelle: __("TEJ : abandonné") },
};

function poser_css_liste_tej() {
  if (document.getElementById("brs-liste-tej-css")) return;
  const style = document.createElement("style");
  style.id = "brs-liste-tej-css";
  style.textContent = `
    .brs-tej-pill { display: inline-flex; align-items: center; gap: 4px; margin-right: 8px;
                    padding: 1px 8px; border-radius: 10px; font-size: 11px; font-weight: 500;
                    white-space: nowrap; cursor: default; }
    .brs-tej-pill.green  { background: #dcfce7; color: #15803d; }
    .brs-tej-pill.orange { background: #ffedd5; color: #c2410c; }
    .brs-tej-pill.blue   { background: #dbeafe; color: #1d4ed8; }
    .brs-tej-pill.red    { background: #fee2e2; color: #b91c1c; }
    .brs-tej-pill.gray   { background: var(--gray-200); color: var(--text-muted); }
  `;
  document.head.appendChild(style);
}

function poser_etats_tej(listview) {
  const $lignes = listview.$result.find(".list-row-container");
  if (!$lignes.length) return;

  // `dataset.name` porte le nom du document, brut. La ligne elle-même n'en porte pas :
  // c'est la case à cocher qui le sait.
  const par_nom = {};
  $lignes.each((_, el) => {
    const nom = $(el).find(".list-row-checkbox").attr("data-name");
    if (nom) par_nom[nom] = $(el);
  });
  const noms = Object.keys(par_nom);
  if (!noms.length) return;

  frappe.call({
    method: "bank_retenue_sync.tej.emis.etats",
    args: { factures: noms },
    // Pas de `freeze` : la liste ne doit jamais attendre une décoration.
    callback: (r) => {
      const etats = r.message || {};
      if (!Object.keys(etats).length) return;
      poser_css_liste_tej();
      for (const [nom, vue] of Object.entries(etats)) {
        const $ligne = par_nom[nom];
        if (!$ligne) continue;
        const modele = ETATS_TEJ[vue.statut];
        if (!modele) continue;
        // Le n° de dépôt dans le libellé quand il existe : c'est la seule preuve que la
        // déclaration est partie, et il tient en huit caractères.
        const texte =
          vue.statut === "en_analyse" && vue.numero
            ? __("TEJ : dépôt {0} en analyse", [vue.numero])
            : modele.libelle;
        // Le message du service en infobulle — il dit s'il faut attendre ou surtout pas
        // resoumettre, et il n'avait jusqu'ici aucun endroit où se lire.
        const infobulle = vue.message || vue.reference || "";
        const $pill = $('<span class="brs-tej-pill"></span>')
          .addClass(modele.couleur)
          .text(texte);
        if (infobulle) $pill.attr("title", infobulle);
        $ligne.find(".level-right").first().find(".brs-tej-pill").remove();
        $ligne.find(".level-right").first().prepend($pill);
      }
    },
  });
}
