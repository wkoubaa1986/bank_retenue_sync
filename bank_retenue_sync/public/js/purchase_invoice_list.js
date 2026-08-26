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
    poser_bouton_recap(listview);
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

// ── Récapitulatif des retenues à la source achat ─────────────────────────────
//
// Le tableau croisé « comptabilité ↔ fisc » depuis le plancher (01/01/2026) : ce que chaque
// facture locale RETIENT (ligne de taxe) face à ce que TEJ a REÇU (certificat attaché — du module
// ou à la main —, dépôt en cours, export du portail). Même grammaire visuelle que l'onglet
// Certificats TEJ de la page « Retenue à la source — Ventes » : cartes KPI puis tableau.

function poser_bouton_recap(listview) {
  if (listview.__brs_bouton_recap) return;
  listview.__brs_bouton_recap = true;
  listview.page.add_inner_button(__("Récap retenues à la source"), () => ouvrir_recap_retenues());

  // Arrivée par le raccourci de l'espace Achat (?recap-retenues=1) : le tableau s'ouvre seul.
  // Le paramètre est retiré de l'URL aussitôt — sans ça, chaque rafraîchissement de la liste
  // rouvrirait le dialogue, et un lien copié depuis cet écran le transporterait par surprise.
  if (new URLSearchParams(window.location.search).has("recap-retenues")) {
    window.history.replaceState(null, "", window.location.pathname);
    ouvrir_recap_retenues();
  }
}

function poser_css_recap() {
  if (document.getElementById("brs-recap-ras-css")) return;
  const style = document.createElement("style");
  style.id = "brs-recap-ras-css";
  style.textContent = `
    .brs-recap-kpis { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px; }
    .brs-recap-kpi { flex:1 1 120px; min-width:120px; padding:10px 12px; border-radius:10px;
                     background: var(--bg-color, var(--fg-color)); border:1px solid var(--border-color); }
    .brs-recap-kpi .lbl { font-size:11px; color:var(--text-muted); text-transform:uppercase;
                          letter-spacing:.4px; margin-bottom:4px; white-space:nowrap; }
    .brs-recap-kpi .val { font-size:17px; font-weight:600; }
    .brs-recap-kpi.warn { border-color:#fdba74; background:#fff7ed; }
    .brs-recap-kpi.warn .val { color:#c2410c; }
    .brs-recap-table th { position:sticky; top:0; background:var(--fg-color); z-index:1; }
    .brs-recap-table td, .brs-recap-table th { padding:5px 8px; }
  `;
  document.head.appendChild(style);
}

const VERDICTS_COMPTA = {
  conforme: { couleur: "green", libelle: __("conforme ✓") },
  manquante: { couleur: "red", libelle: __("retenue manquante") },
  "montant faux": { couleur: "orange", libelle: __("montant faux") },
};

function ouvrir_recap_retenues() {
  frappe.call({
    method: "bank_retenue_sync.achat.retenue.recapitulatif_retenues",
    freeze: true,
    freeze_message: __("Lecture des factures et des certificats…"),
    callback: (r) => {
      const d = r.message || {};
      poser_css_recap();
      poser_css_liste_tej();
      const dt = (v) => format_currency(v || 0, "TND");
      const esc = frappe.utils.escape_html;
      const c = d.compte || {};
      const t = d.totaux || {};

      const tuile = (lbl, val, warn) =>
        `<div class="brs-recap-kpi ${warn ? "warn" : ""}"><div class="lbl">${lbl}</div>
          <div class="val">${val}</div></div>`;
      const kpis = [
        tuile(__("Factures concernées"), c.factures || 0),
        tuile(__("Comptabilité conforme"), c.conformes || 0),
        tuile(__("Retenue manquante"), c.manquantes || 0, c.manquantes),
        tuile(__("Montant faux"), c.fausses || 0, c.fausses),
        tuile(__("Certificat TEJ ✓"), c.tej_emis || 0),
        tuile(__("TEJ en cours"), c.tej_en_cours || 0, c.tej_en_cours),
        tuile(__("Certificat manquant"), c.tej_manquants || 0, c.tej_manquants),
        tuile(__("Manque à retenir"), dt(t.manque), t.manque),
      ].join("");

      const pill = (mod, texte, titre) =>
        `<span class="brs-tej-pill ${mod}" ${titre ? `title="${esc(titre)}"` : ""}>${texte}</span>`;

      const lignes = (d.lignes || [])
        .map((l) => {
          const v = VERDICTS_COMPTA[l.verdict] || { couleur: "gray", libelle: l.verdict };
          const etat_tej =
            l.tej.statut === "emis"
              ? pill("green", __("Certificat TEJ ✓"), l.tej.detail)
              : l.tej.statut === "manquant"
                ? pill("red", __("certificat manquant"))
                : pill(
                    (ETATS_TEJ[l.tej.statut] || {}).couleur || "orange",
                    (ETATS_TEJ[l.tej.statut] || {}).libelle || l.tej.statut,
                    l.tej.detail
                  );
          const lien_pdf = l.tej.file_url
            ? ` <a href="${esc(l.tej.file_url)}" target="_blank" title="${__("Ouvrir le certificat")}">📎</a>`
            : "";
          // Le cas qui pique : certifié au fournisseur mais jamais comptabilisé — ni l'un ni
          // l'autre des deux tableaux simples ne le montrait.
          const alerte =
            l.verdict === "manquante" && l.tej.statut === "emis"
              ? ` <span title="${__("Certificat remis au fournisseur mais AUCUNE ligne de retenue comptabilisée")}">⚠️</span>`
              : "";
          return `<tr>
            <td><a href="/app/purchase-invoice/${encodeURIComponent(l.facture)}">${esc(l.facture)}</a>${alerte}</td>
            <td>${frappe.datetime.str_to_user(l.date)}</td>
            <td>${esc(l.fournisseur)}</td>
            <td>${esc(l.bill_no)}</td>
            <td class="text-right">${dt(l.ttc)}</td>
            <td class="text-right">${dt(l.due)}</td>
            <td class="text-right">${dt(l.saisie)}</td>
            <td>${pill(v.couleur, v.libelle)}</td>
            <td>${etat_tej}${lien_pdf}</td>
          </tr>`;
        })
        .join("");

      const avert_export = d.export_disponible
        ? ""
        : `<p style="margin:0 0 10px;color:#c2410c;font-size:12px">⚠️ ${__(
            "Le service TEJ est injoignable : la colonne TEJ repose sur les seules preuves locales (PDF attachés, dépôts). Un certificat existant sur le portail mais jamais attaché peut apparaître « manquant »."
          )}</p>`;

      const dialog = new frappe.ui.Dialog({
        title: __("Retenues à la source achat — depuis le {0}", [
          frappe.datetime.str_to_user(d.depuis),
        ]),
        size: "extra-large",
        fields: [{ fieldtype: "HTML", fieldname: "corps" }],
      });
      dialog.get_field("corps").$wrapper.html(`
        ${avert_export}
        <div class="brs-recap-kpis">${kpis}</div>
        <div style="max-height:55vh;overflow:auto">
          <table class="table table-bordered brs-recap-table" style="font-size:12px;margin:0">
            <thead><tr>
              <th>${__("Facture")}</th><th>${__("Date")}</th><th>${__("Fournisseur")}</th>
              <th>${__("N° fournisseur")}</th><th class="text-right">${__("TTC avant retenue")}</th>
              <th class="text-right">${__("Retenue due")}</th>
              <th class="text-right">${__("Retenue saisie")}</th>
              <th>${__("Comptabilité")}</th><th>${__("TEJ")}</th>
            </tr></thead>
            <tbody>${lignes || `<tr><td colspan="9">${__("Aucune facture concernée")}</td></tr>`}</tbody>
            <tfoot><tr style="font-weight:600">
              <td colspan="5">${__("Totaux")}</td>
              <td class="text-right">${dt(t.due)}</td>
              <td class="text-right">${dt(t.saisie)}</td>
              <td colspan="2">${__("manque : {0}", [dt(t.manque)])}</td>
            </tr></tfoot>
          </table>
        </div>`);
      dialog.show();
    },
  });
}
