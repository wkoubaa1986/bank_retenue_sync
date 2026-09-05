// Vue liste des écritures de journal : où en est le certificat de retenue à la source de chacune.
//
// ⚠️ SANS ÇA, L'ÉTAT NE SE VOIT QU'EN OUVRANT LES ÉCRITURES UNE PAR UNE. Une retenue prélevée en
// caisse dont le certificat n'est pas parti, un dépôt en analyse chez l'administration, une ligne
// à laquelle il manque le matricule du fournisseur : c'est ici qu'on cherche « qu'est-ce qui
// reste à faire », et rien de tout ça ne s'y lisait.
//
// ⚠️ ON ÉTEND, ON NE REMPLACE PAS. ERPNext affecte `frappe.listview_settings["Journal Entry"]` en
// entier (champs ajoutés, indicateur Brouillon / Validé / Annulé). Les fichiers de hook étant
// chargés APRÈS celui du DocType, écraser l'objet ici ferait tout disparaître en silence.

frappe.listview_settings["Journal Entry"] = frappe.listview_settings["Journal Entry"] || {};

(() => {
  const reglages = frappe.listview_settings["Journal Entry"];
  const precedent = reglages.refresh;
  reglages.refresh = function (listview) {
    if (precedent) precedent.call(this, listview);
    poser_etats_retenue(listview);
  };
})();

// Un état, une couleur, un libellé. Le vocabulaire est celui de `BRS Depot TEJ`, plus les trois
// états propres à la file des retenues de caisse (`emis_journal`). Le traduire deux fois, c'est
// le laisser dériver.
const ETATS_RETENUE_JE = {
  a_emettre: { couleur: "red", libelle: __("Certificat de retenue à émettre") },
  incomplet: { couleur: "orange", libelle: __("Certificat de retenue : incomplet") },
  emis: { couleur: "green", libelle: __("Certificat TEJ ✓") },
  genere: { couleur: "green", libelle: __("Certificat TEJ ✓") },
  en_envoi: { couleur: "blue", libelle: __("TEJ : envoi en cours") },
  en_analyse: { couleur: "orange", libelle: __("TEJ : dépôt en analyse") },
  incertain: { couleur: "red", libelle: __("TEJ : à vérifier sur le portail") },
  refuse: { couleur: "gray", libelle: __("TEJ : dépôt refusé") },
  echec: { couleur: "gray", libelle: __("TEJ : rien n'a été soumis") },
  abandonne: { couleur: "gray", libelle: __("TEJ : abandonné") },
};

// Même feuille que la liste des factures d'achat (même id : la première chargée gagne, les
// deux sont identiques).
function poser_css_liste_tej_je() {
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

function poser_etats_retenue(listview) {
  const $lignes = listview.$result.find(".list-row-container");
  if (!$lignes.length) return;

  // La ligne elle-même ne porte pas le nom du document : c'est la case à cocher qui le sait.
  const par_nom = {};
  $lignes.each((_, el) => {
    const nom = $(el).find(".list-row-checkbox").attr("data-name");
    if (nom) par_nom[nom] = $(el);
  });
  const noms = Object.keys(par_nom);
  if (!noms.length) return;

  frappe.call({
    method: "bank_retenue_sync.tej.emis_journal.etats",
    args: { ecritures: noms },
    // Pas de `freeze` : la liste ne doit jamais attendre une décoration.
    callback: (r) => {
      const etats = r.message || {};
      if (!Object.keys(etats).length) return;
      poser_css_liste_tej_je();
      for (const [nom, vue] of Object.entries(etats)) {
        const $ligne = par_nom[nom];
        if (!$ligne) continue;
        const modele = ETATS_RETENUE_JE[vue.statut];
        if (!modele) continue;
        // Le n° de dépôt dans le libellé quand il existe : la seule preuve que la déclaration
        // est partie, et il tient en huit caractères.
        const texte =
          vue.statut === "en_analyse" && vue.numero
            ? __("TEJ : dépôt {0} en analyse", [vue.numero])
            : modele.libelle;
        // Ce qui manque (« fournisseur non rattaché… ») ou la référence du certificat, en
        // infobulle : ça se lit sans ouvrir.
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
