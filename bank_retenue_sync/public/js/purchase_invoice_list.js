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

// L'état de paiement, tel qu'ERPNext le tient sur la facture. Le restant s'affiche dès qu'il
// existe : « Partiellement payé » sans montant ne dit rien d'utile.
const ETATS_PAIEMENT = {
  Paid: { couleur: "green", libelle: __("payée ✓") },
  Unpaid: { couleur: "red", libelle: __("impayée") },
  Overdue: { couleur: "red", libelle: __("en retard") },
  "Partly Paid": { couleur: "orange", libelle: __("partiellement payée") },
  "Debit Note Issued": { couleur: "gray", libelle: __("avoir émis") },
};

function pill_paiement(p, dt) {
  const mod = ETATS_PAIEMENT[p.statut] || { couleur: "gray", libelle: p.statut || "—" };
  const restant =
    p.restant > 0.005
      ? ` <span class="text-muted" style="font-size:11px">${__("reste {0}", [dt(p.restant)])}</span>`
      : "";
  return `<span class="brs-tej-pill ${mod.couleur}">${mod.libelle}</span>${restant}`;
}

function ouvrir_recap_retenues(dialog_existante) {
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
        // « 0 » affirme que tout correspond ; export injoignable, on n'en sait RIEN : « — ».
        tuile(
          __("TEJ sans facture"),
          d.export_disponible ? c.tej_orphelins || 0 : "—",
          c.tej_orphelins || !d.export_disponible
        ),
        tuile(__("Manque à retenir"), dt(t.manque), t.manque),
        tuile(
          __("Impayées"),
          `${c.impayees || 0} <span style="font-size:12px;font-weight:400">${dt(t.restant)}</span>`,
          c.impayees
        ),
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
            : l.tej.statut === "emis" && l.tej.reference
              ? ` <button class="btn btn-default btn-xs" data-act="attacher-certificat"
                    data-facture="${esc(l.facture)}" data-reference="${esc(l.tej.reference)}"
                    title="${__("Le certificat n'existe que sur le portail : télécharger son PDF et l'attacher à la facture")}"
                    >📎 ${__("Rapatrier")}</button>`
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
            <td>${pill_paiement(l.paiement || {}, dt)}</td>
            <td>${pill(v.couleur, v.libelle)}${
              l.recreable
                ? ` <button class="btn btn-default btn-xs" data-act="recreer"
                      data-facture="${esc(l.facture)}"
                      title="${__("Rien n'est payé : supprimer et recréer la facture sous le même numéro, retenue posée automatiquement")}"
                      >♻️ ${__("Recréer")}</button>`
                : ""
            }</td>
            <td>${etat_tej}${lien_pdf}</td>
          </tr>`;
        })
        .join("");

      const avert_export = d.export_disponible
        ? ""
        : `<p style="margin:0 0 10px;color:#c2410c;font-size:12px">⚠️ ${__(
            "Le service TEJ est injoignable : la colonne TEJ repose sur les seules preuves locales (PDF attachés, dépôts). Un certificat existant sur le portail mais jamais attaché peut apparaître « manquant »."
          )}</p>`;

      const titre = __("Retenues à la source achat — depuis le {0}", [
        frappe.datetime.str_to_user(d.depuis),
      ]);
      // Le dialogue est une PHOTOGRAPHIE prise au clic : « Actualiser » reprend la photo en
      // place, sans fermer. La colonne TEJ, elle, lit l'export que le service détient — le
      // rescrape du portail reste réservé aux soumissions réelles (worker unique).
      const dialog =
        dialog_existante ||
        new frappe.ui.Dialog({
          title: titre,
          size: "extra-large",
          fields: [{ fieldtype: "HTML", fieldname: "corps" }],
        });
      if (dialog_existante) dialog.set_title(titre);
      dialog.get_field("corps").$wrapper.html(`
        <div style="display:flex;justify-content:flex-end;gap:6px;margin-bottom:8px">
          ${
            (d.lignes || []).some((l) => !l.bill_no && !l.bill_no_scan)
              ? `<button class="btn btn-default btn-xs" data-act="lire-scans"
                   title="${__("Lit le scan des factures sans n° fournisseur (un appel OpenAI par facture) : pose le n° et rend l'identité aux suggestions")}"
                   >📖 ${__("Lire les scans manquants")}</button>`
              : ""
          }
          <button class="btn btn-default btn-xs" data-act="recharger">🔄 ${__("Actualiser")}</button>
        </div>
        ${avert_export}
        <div class="brs-recap-kpis">${kpis}</div>
        <div style="max-height:55vh;overflow:auto">
          <table class="table table-bordered brs-recap-table" style="font-size:12px;margin:0">
            <thead><tr>
              <th>${__("Facture")}</th><th>${__("Date")}</th><th>${__("Fournisseur")}</th>
              <th>${__("N° fournisseur")}</th><th class="text-right">${__("TTC avant retenue")}</th>
              <th class="text-right">${__("Retenue due")}</th>
              <th class="text-right">${__("Retenue saisie")}</th>
              <th>${__("Paiement")}</th>
              <th>${__("Comptabilité")}</th><th>${__("TEJ")}</th>
            </tr></thead>
            <tbody>${lignes || `<tr><td colspan="10">${__("Aucune facture concernée")}</td></tr>`}</tbody>
            <tfoot><tr style="font-weight:600">
              <td colspan="5">${__("Totaux")}</td>
              <td class="text-right">${dt(t.due)}</td>
              <td class="text-right">${dt(t.saisie)}</td>
              <td>${__("reste {0}", [dt(t.restant)])}</td>
              <td colspan="2">${__("manque : {0}", [dt(t.manque)])}</td>
            </tr></tfoot>
          </table>
        </div>
        ${section_orphelins(d)}`);
      dialog
        .get_field("corps")
        .$wrapper.find("[data-act='recharger']")
        .on("click", () => ouvrir_recap_retenues(dialog));
      dialog
        .get_field("corps")
        .$wrapper.find("[data-act='recreer']")
        .on("click", (e) => {
          const $b = $(e.currentTarget);
          frappe.confirm(
            __(
              "Supprimer la facture {0} et la recréer SOUS LE MÊME NUMÉRO avec sa retenue posée ? Rien n'y est payé ; les pièces jointes et l'extraction sont conservées.",
              [$b.data("facture")]
            ),
            () =>
              frappe.call({
                method: "bank_retenue_sync.achat.retenue.recreer_avec_retenue",
                args: { facture: $b.data("facture") },
                freeze: true,
                freeze_message: __("Suppression puis recréation de la facture…"),
                callback: (rv) => {
                  const v = rv.message || {};
                  frappe.msgprint({
                    title: __("Facture recréée"),
                    indicator: v.verdict_apres === "conforme" ? "green" : "orange",
                    message: __("{0} : retenue {1} → {2} ({3}).", [
                      frappe.utils.escape_html(v.facture || ""),
                      v.retenue_avant,
                      v.retenue_apres,
                      frappe.utils.escape_html(v.verdict_apres || ""),
                    ]),
                  });
                  ouvrir_recap_retenues(dialog);
                },
              })
          );
        });
      dialog
        .get_field("corps")
        .$wrapper.find("[data-act='lire-scans']")
        .on("click", () =>
          frappe.call({
            method: "bank_retenue_sync.achat.retenue.lire_scans_manquants",
            freeze: true,
            freeze_message: __("Lancement de la lecture des scans…"),
            callback: (rl) => {
              const v = rl.message || {};
              frappe.msgprint({
                title: __("Lecture des scans"),
                indicator: v.statut === "lance" ? "blue" : "green",
                message:
                  v.statut === "lance"
                    ? __(
                        "{0} scan(s) en cours de lecture en tâche de fond (un appel OpenAI par facture). Cliquez « Actualiser » dans quelques minutes.",
                        [v.factures]
                      )
                    : __("Toutes les factures du périmètre ont déjà leur n° fournisseur."),
              });
            },
          })
        );
      dialog
        .get_field("corps")
        .$wrapper.find("[data-act='attacher-certificat']")
        .on("click", (e) => {
          const $b = $(e.currentTarget);
          frappe.confirm(
            __("Attacher le certificat {0} à la facture {1} ? L'attachement vaut identification.", [
              $b.data("reference"),
              $b.data("facture"),
            ]),
            () =>
              frappe.call({
                method: "bank_retenue_sync.tej.emis.attacher_certificat",
                args: { facture: $b.data("facture"), reference: $b.data("reference") },
                freeze: true,
                freeze_message: __("Téléchargement du certificat depuis le portail…"),
                callback: () => ouvrir_recap_retenues(dialog),
              })
          );
        });
      dialog
        .get_field("corps")
        .$wrapper.find("[data-act='verif-concordance']")
        .on("click", (e) => {
          const $b = $(e.currentTarget);
          frappe.call({
            method: "bank_retenue_sync.tej.emis.verifier_concordance",
            args: { facture: $b.data("facture"), reference: $b.data("reference") },
            freeze: true,
            freeze_message: __("Comparaison des certificats (le PDF du portail se génère)…"),
            callback: (rv) => {
              const v = rv.message || {};
              frappe.msgprint({
                title: __("Concordance des certificats"),
                indicator: v.verdict === "meme" ? "green" : v.verdict === "different" ? "red" : "orange",
                message: frappe.utils.escape_html(v.message || ""),
              });
              // « même document » a été mémorisé (PDF officiel attaché) : la photo a changé.
              if (v.verdict === "meme") ouvrir_recap_retenues(dialog);
            },
          });
        });
      dialog
        .get_field("corps")
        .$wrapper.find("[data-act='verifier-tout']")
        .on("click", () => {
          const paires = (d.suggestions || [])
            .filter((g) => g.facture_tej === "emis" && g.reference)
            .map((g) => ({ facture: g.facture, reference: g.reference }));
          if (!paires.length) return;
          frappe.call({
            method: "bank_retenue_sync.tej.emis.verifier_concordances",
            args: { paires: paires },
            freeze: true,
            freeze_message: __("Vérification de {0} paire(s) — un PDF portail par paire…", [
              paires.length,
            ]),
            callback: (rv) => {
              const res = rv.message || [];
              const icone = (v) => (v === "meme" ? "🟢" : v === "different" ? "🔴" : "🟠");
              frappe.msgprint({
                title: __("Concordance des certificats — {0} paire(s)", [res.length]),
                indicator: res.every((x) => x.verdict === "meme") ? "green" : "orange",
                message:
                  "<ul style='padding-left:18px'><li>" +
                  res
                    .map(
                      (x) =>
                        `${icone(x.verdict)} <b>${frappe.utils.escape_html(x.facture || "")}</b> — ` +
                        frappe.utils.escape_html(x.message || x.verdict || "")
                    )
                    .join("</li><li>") +
                  "</li></ul>",
              });
              ouvrir_recap_retenues(dialog);
            },
          });
        });
      if (!dialog_existante) dialog.show();
    },
  });
}

// L'AUTRE SENS : les certificats vivants du portail qui ne correspondent à AUCUNE facture locale
// (numéro mal saisi côté portail, facture jamais comptabilisée…). Une déclaration au fisc sans
// comptabilité — le symétrique de « retenue sans certificat », et aucun des deux tableaux simples
// ne le montrait. Même plancher que le reste (01/01/2026, sur la date de paiement du certificat,
// à défaut sa création). Section absente quand tout correspond ou que l'export est injoignable.
function section_orphelins(d) {
  const esc = frappe.utils.escape_html;
  const orphelins = d.orphelins || [];
  if (!orphelins.length) return "";
  // Suggestions par (numéro, référence) : même matricule + paiement proche de la
  // comptabilisation. SUGGESTIF — l'écran montre la paire, l'humain tranche.
  const sugg = {};
  for (const g of d.suggestions || []) {
    const cle = `${g.numero}|${g.reference}`;
    (sugg[cle] = sugg[cle] || []).push(g);
  }
  const cellule_suggestion = (o) => {
    const gs = sugg[`${o.numero}|${o.reference}`] || [];
    if (!gs.length) return "—";
    return gs
      .map((g) => {
        // La facture suggérée porte DÉJÀ un certificat : même document (numéro divergent,
        // bénin) ou un AUTRE (double déclaration) ? Le bouton tranche — références si le
        // module a attaché, texte des PDF sinon.
        // Une action par situation : facture déjà certifiée -> vérifier la concordance ;
        // facture sans certificat -> l'orphelin est probablement SON certificat, l'attacher
        // (l'attachement est l'identification : la facture passe « émis », l'orphelin sort).
        const verif =
          g.facture_tej === "emis"
            ? ` <button class="btn btn-default btn-xs" data-act="verif-concordance"
                  data-facture="${esc(g.facture)}" data-reference="${esc(g.reference || "")}"
                  title="${__("Cette facture a déjà un certificat : vérifier si c'est le même")}"
                  >⚖️ ${__("Vérifier")}</button>`
            : g.reference
              ? ` <button class="btn btn-default btn-xs" data-act="attacher-certificat"
                    data-facture="${esc(g.facture)}" data-reference="${esc(g.reference)}"
                    title="${__("Télécharger le certificat du portail et l'attacher à cette facture")}"
                    >📎 ${__("Attacher")}</button>`
              : "";
        return `<a href="/app/purchase-invoice/${encodeURIComponent(g.facture)}">${esc(g.facture)}</a>
           <span class="text-muted" style="font-size:11px">${
             g.motif === "numero"
               ? __("numéros emboîtés")
               : g.ecart_jours === 0
                 ? __("même jour, même matricule")
                 : __("à {0} j, même matricule", [g.ecart_jours])
           }</span>${verif}`;
      })
      .join("<br>");
  };
  const lignes = orphelins
    .map(
      (o) => `<tr>
        <td>${esc(o.reference || "—")}</td>
        <td>${esc(o.numero || "")}</td>
        <td>${esc(o.fournisseur || "")} <span class="text-muted">${esc(o.beneficiaire || "")}</span></td>
        <td>${esc(o.date_paiement || "")}</td>
        <td>${esc(o.etat || "")}</td>
        <td>${cellule_suggestion(o)}</td>
      </tr>`
    )
    .join("");
  const nb_verifiables = (d.suggestions || []).filter(
    (g) => g.facture_tej === "emis" && g.reference
  ).length;
  return `
    <h5 style="margin:18px 0 8px;display:flex;align-items:center;gap:10px">⚠️ ${__(
      "Certificats TEJ sans facture correspondante ({0})",
      [orphelins.length]
    )}${
      nb_verifiables > 1
        ? `<button class="btn btn-default btn-xs" data-act="verifier-tout"
             title="${__("Vérifie chaque paire suggérée (un PDF portail est généré par paire) ; les « même document » sont mémorisés et disparaissent")}"
             >⚖️ ${__("Tout vérifier ({0})", [nb_verifiables])}</button>`
        : ""
    }</h5>
    <p class="text-muted" style="font-size:12px;margin:0 0 8px">${__(
      "Déclarés au fisc, mais aucun « N° chez le déclarant » ne correspond à une facture d'achat validée : numéro mal saisi sur le portail, ou facture absente de la comptabilité. La « facture probable » (même matricule, paiement au même moment) est une suggestion : vérifiez, puis corrigez le bill_no de la facture ou le numéro côté portail."
    )}</p>
    <div style="max-height:30vh;overflow:auto">
      <table class="table table-bordered brs-recap-table" style="font-size:12px;margin:0">
        <thead><tr>
          <th>${__("Référence")}</th><th>${__("N° chez le déclarant")}</th>
          <th>${__("Bénéficiaire")}</th><th>${__("Date de paiement")}</th>
          <th>${__("État")}</th><th>${__("Facture probable")}</th>
        </tr></thead>
        <tbody>${lignes}</tbody>
      </table>
    </div>`;
}
