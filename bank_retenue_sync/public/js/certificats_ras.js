// Onglet « Certificats TEJ » de la page « Retenue à la source — Ventes ».
//
// POURQUOI CE FICHIER VIT DANS bank_retenue_sync
// Le tableau des factures répond à « qu'ai-je retenu et me manque-t-il le justificatif ? ».
// Cet onglet répond à la question inverse, et elle vient du portail : « qu'ont déclaré mes
// clients, et est-ce dans mes comptes ? ». La logique appartient donc au flux TEJ ; la page
// n'en héberge que l'affichage, et customization_app ne gagne qu'un appel de montage.
//
// Rendu pur : tout vient de bank_retenue_sync.api.certificats.

window.CertificatsRAS = class CertificatsRAS {
  constructor($container) {
    this.$root = $container;
    this._data = null;
    this.$root.html(this._squelette());
    this._bind();
    this.refresh();
  }

  // ------------------------------------------------------------------ données

  // Les deux sens sont lus ensemble, et c'est voulu : « qu'ont déclaré mes clients » et « qu'ai-je
  // comptabilisé sans certificat » sont la même question posée par les deux bouts. N'en afficher
  // qu'une moitié laisse croire qu'un tableau vert suffit.
  refresh() {
    const d = this._dates();
    return Promise.all([
      frappe.call({
        method: "bank_retenue_sync.api.certificats.get_data",
        args: { from_date: d.from, to_date: d.to, inclure_hors_perimetre: this._hors() ? 1 : 0 },
        freeze: true,
        freeze_message: __("Lecture des certificats…"),
      }),
      frappe.call({
        method: "bank_retenue_sync.api.certificats.get_retenues_orphelines",
        args: { from_date: d.from, to_date: d.to },
      }),
    ]).then(([r, o]) => {
      this._data = r.message || {};
      this._orphelines = (o || {}).message || {};
      this._render();
    });
  }

  _dates() {
    const $p = this.$root.closest(".rsv-page");
    return { from: $p.find("#rsv-from").val() || null, to: $p.find("#rsv-to").val() || null };
  }

  _hors() {
    return this.$root.find("[data-role='hors-perimetre']").is(":checked");
  }

  // ------------------------------------------------------------------ rendu

  _squelette() {
    return `
      <div class="rsv-toolbar" style="margin-bottom:14px">
        <span data-role="fraicheur" class="rsv-cust"></span>
        <span class="rsv-spacer"></span>
        <label style="display:flex;align-items:center;gap:6px;text-transform:none">
          <input type="checkbox" data-role="hors-perimetre" style="margin:0"> Afficher les antérieurs à 2026
        </label>
        <button class="btn btn-default btn-sm" data-act="sync">🔄 Synchroniser</button>
        <button class="btn btn-default btn-sm" data-act="pdf">📎 Télécharger les PDF</button>
        <button class="btn btn-default btn-sm" data-act="doublons">🧹 Justificatifs en double</button>
        <button class="btn btn-default btn-sm" data-act="orphelines">🔎 Retenues sans certificat</button>
        <button class="btn btn-primary btn-sm" data-act="creer">➕ Créer les écritures manquantes</button>
      </div>
      <div class="rsv-kpis" data-role="cert-kpis"></div>
      <div data-role="cert-table"></div>`;
  }

  _render() {
    const k = this._data.kpis || {};
    const s = (this._orphelines || {}).synthese || {};
    const dt = (v) => format_currency(v, "TND");
    const tuile = (lbl, val, warn) =>
      `<div class="rsv-kpi ${warn ? "warn" : ""}"><div class="lbl">${lbl}</div><div class="val">${val}</div></div>`;

    this.$root.find("[data-role='cert-kpis']").html(
      [
        tuile(__("Certificats"), k.total || 0),
        tuile(__("Retenue déclarée"), dt(k.retenue_totale)),
        tuile(__("Rapprochés"), `${k.rapproches || 0} <span class="rsv-rate">${dt(k.retenue_rapprochee)}</span>`),
        tuile(__("Sans écriture"), `${k.sans_piece || 0} <span class="rsv-rate">${dt(k.retenue_sans_piece)}</span>`, k.sans_piece),
        tuile(__("À trancher"), (k.ambigus || 0) + (k.non_identifies || 0), (k.ambigus || 0) + (k.non_identifies || 0)),
        tuile(__("Écarts de montant"), `${k.ecarts || 0} <span class="rsv-rate">${dt(k.montant_ecarts)}</span>`, k.ecarts),
        tuile(__("PDF rangés"), `${k.pdf || 0} / ${k.rapproches || 0}`),
        tuile(__("Anomalies"), k.anomalies || 0, k.anomalies),
        // L'AUTRE SENS. Un crédit d'impôt sans certificat n'est pas opposable au fisc : c'est le
        // seul chiffre de cette page qui appelle une relance client.
        tuile(
          __("Retenues sans certificat"),
          `${s.sans_certificat || 0} <span class="rsv-rate">${dt(s.montant_sans_certificat)}</span>`,
          s.sans_certificat
        ),
      ].join("")
    );

    const e = this._data.etat || {};
    const lu = e.last_tej_sync ? frappe.datetime.str_to_user(e.last_tej_sync) : "—";
    this.$root
      .find("[data-role='fraicheur']")
      .html(
        `${__("Dernière synchronisation")} : <b>${lu}</b>` +
          (e.actif ? "" : ` · <span style="color:var(--rsv-warn)">${__("synchronisation désactivée")}</span>`)
      );

    const lignes = this._data.certificats || [];
    if (!lignes.length) {
      this.$root.find("[data-role='cert-table']").html(
        `<div class="rsv-inv" style="padding:18px;text-align:center;color:var(--text-muted)">${__("Aucun certificat sur la période.")}</div>`
      );
      return;
    }
    this.$root.find("[data-role='cert-table']").html(`
      <div class="rsv-inv" style="overflow-x:auto">
        <table class="table" style="margin:0;font-size:12.5px">
          <thead><tr>
            <th>${__("Date")}</th><th>${__("Déclarant")}</th><th>${__("Client")}</th>
            <th class="rsv-num">${__("Assiette")}</th><th class="rsv-num">${__("Retenue")}</th>
            <th class="rsv-num">${__("Écart")}</th><th>${__("État")}</th>
            <th>${__("Écriture")}</th><th>${__("Facture")}</th><th>${__("PDF")}</th>
            <th style="width:90px"></th>
          </tr></thead>
          <tbody>${lignes.map((l) => this._ligne(l)).join("")}</tbody>
        </table>
      </div>`);
  }

  _ligne(l) {
    const dt = (v) => (v ? format_currency(v, "TND") : "—");
    const lien = (dtype, nom) =>
      nom ? `<a href="/app/${frappe.router.slug(dtype)}/${encodeURIComponent(nom)}">${nom}</a>` : "—";
    // Les certificats annulés et hors périmètre restent visibles mais grisés : c'est en les
    // voyant écartés qu'on vérifie qu'ils l'ont bien été.
    const efface = l.etat_depot === "Annule" || l.hors_perimetre ? "opacity:.55;" : "";
    const badge = this._badge(l);
    // ⚠️ LE BOUTON SUIT L'ÉCRITURE, PAS LE STATUT. Poser la facture à la main passe le certificat
    // en « Manually Matched » : avec un test sur le seul statut, « Régulariser » disparaissait
    // alors que rien n'était comptabilisé — l'utilisateur restait sans aucun geste possible.
    const clos = l.match_status === "Ignore" || l.match_status === "Rejected" || l.hors_perimetre || l.anomalie;
    const actions = clos
      ? ""
      : !l.customer
        ? `<button class="btn btn-xs btn-default" data-act="trancher" data-ref="${frappe.utils.escape_html(l.name)}">${__("Trancher")}</button>`
        : !l.payment_entry
          ? `<button class="btn btn-xs btn-default" data-act="regulariser" data-ref="${frappe.utils.escape_html(l.name)}">${__("Régulariser")}</button>`
          : "";
    return `<tr style="${efface}" title="${frappe.utils.escape_html(l.match_raison || "")}">
      <td>${frappe.datetime.str_to_user(l.date_paiement)}</td>
      <td>${frappe.utils.escape_html(l.declarant || "")}<div class="rsv-rate">${frappe.utils.escape_html(l.declarant_matricule || "")}</div></td>
      <td>${l.customer ? lien("Customer", l.customer) : `<span style="color:var(--rsv-warn)">${__("à identifier")}</span>`}
          <div class="rsv-rate">${frappe.utils.escape_html(l.match_method || "")}</div></td>
      <td class="rsv-num">${dt(l.total_brut)}</td>
      <td class="rsv-num"><span class="rsv-chip">${dt(l.montant_retenue)}</span></td>
      <td class="rsv-num" style="${flt(l.ecart_piece) ? "color:var(--rsv-warn);font-weight:600" : ""}">${flt(l.ecart_piece) ? dt(l.ecart_piece) : "—"}</td>
      <td>${badge}</td>
      <td>${lien("Payment Entry", l.payment_entry)}</td>
      <td>${lien("Sales Invoice", l.sales_invoice) }</td>
      <td>${l.pdf_attached_to_pe ? `<span title="${frappe.utils.escape_html(l.pdf_attache_a || "")}">✔</span>` : "—"}</td>
      <td>${actions}</td>
    </tr>`;
  }

  _badge(l) {
    const libelles = {
      "Auto Matched": [__("rapproché"), "var(--rsv-gain)", "var(--rsv-gain-bg)"],
      "Manually Matched": [__("rapproché (manuel)"), "var(--rsv-gain)", "var(--rsv-gain-bg)"],
      "Sans piece": [__("sans écriture"), "var(--rsv-warn)", "var(--rsv-warn-bg)"],
      Ambiguous: [__("ambigu"), "var(--rsv-warn)", "var(--rsv-warn-bg)"],
      Unmatched: [__("client inconnu"), "var(--rsv-loss)", "var(--rsv-loss-bg)"],
      Ignore: [__("écarté"), "var(--text-muted)", "transparent"],
      Rejected: [__("rejeté"), "var(--text-muted)", "transparent"],
    };
    // Un certificat « rapproché » sans écriture ne l'est pas : la facture est posée, la retenue
    // reste à comptabiliser. Le dire en vert serait un tableau qui ment.
    if (!l.payment_entry && (l.match_status === "Auto Matched" || l.match_status === "Manually Matched")) {
      libelles[l.match_status] = [
        l.sales_invoice ? __("facture posée, sans écriture") : __("sans écriture"),
        "var(--rsv-warn)",
        "var(--rsv-warn-bg)",
      ];
    }
    const [txt, fg, bg] = libelles[l.match_status] || [l.match_status, "var(--text-muted)", "transparent"];
    const alerte = l.anomalie ? ` <span title="${frappe.utils.escape_html(l.anomalie_raison || "")}">⚠️</span>` : "";
    const revue = l.revue_requise ? ` <span title="${__("à vérifier")}">👁</span>` : "";
    return `<span class="rsv-chip" style="color:${fg};background:${bg}">${txt}</span>${alerte}${revue}`;
  }

  // ------------------------------------------------------------------ actions

  _bind() {
    this.$root.on("change", "[data-role='hors-perimetre']", () => this.refresh());
    this.$root.on("click", "[data-act='sync']", () => this._synchroniser());
    this.$root.on("click", "[data-act='pdf']", () => this._pdf());
    this.$root.on("click", "[data-act='doublons']", () => this._doublons());
    this.$root.on("click", "[data-act='creer']", () => this._creer(null));
    this.$root.on("click", "[data-act='orphelines']", () => this._orphelines_dialogue());
    this.$root.on("click", "[data-act='regulariser']", (e) => this._regulariser($(e.currentTarget).data("ref")));
    this.$root.on("click", "[data-act='trancher']", (e) => this._trancher($(e.currentTarget).data("ref")));
  }

  // Deux temps volontaires : l'essai à blanc montre ce qui serait écrit, la validation l'écrit.
  // Une écriture comptable ne se retire pas d'un clic.
  _synchroniser() {
    const d = new frappe.ui.Dialog({
      title: __("Synchroniser les certificats"),
      fields: [
        {
          fieldtype: "Check",
          fieldname: "refresh",
          label: __("Demander un nouveau relevé au portail (plusieurs minutes)"),
          description: __("Sans cette option, le dernier export du portail est utilisé."),
        },
        { fieldtype: "HTML", fieldname: "apercu" },
      ],
      primary_action_label: __("Essai à blanc"),
      primary_action: () => {
        const refresh = d.get_value("refresh") ? 1 : 0;
        frappe
          .call({
            method: "bank_retenue_sync.api.certificats.synchroniser",
            args: { refresh, insert: 0, pdf: 0 },
            freeze: true,
            freeze_message: __("Lecture du portail et simulation…"),
          })
          .then((r) => {
            d.fields_dict.apercu.$wrapper.html(this._resume(r.message));
            d.set_primary_action(__("Appliquer"), () => {
              frappe
                .call({
                  method: "bank_retenue_sync.api.certificats.synchroniser",
                  args: { refresh: 0, insert: 1, pdf: 1 },
                  freeze: true,
                  freeze_message: __("Synchronisation…"),
                })
                .then((r2) => {
                  d.hide();
                  frappe.show_alert({ message: __("Synchronisation terminée"), indicator: "green" }, 6);
                  frappe.msgprint({ title: __("Synchronisation"), message: this._resume(r2.message), indicator: "green" });
                  this.refresh();
                });
            });
          });
      },
    });
    d.show();
  }

  _resume(m) {
    if (!m) return "";
    if (m.statut === "desactive")
      return `<div style="color:var(--rsv-warn)">${__("Flux désactivé")} : ${frappe.utils.escape_html(m.detail || "")}</div>`;
    const i = m.ingestion || {};
    const r = m.rapprochement || {};
    const p = m.pdf || {};
    return `<table class="table table-bordered" style="font-size:12px">
      <tr><td>${__("Export du portail")}</td><td>${frappe.utils.escape_html(i.statut || "")}${
        // Le nombre de lignes REÇUES, jamais le nombre de créations : afficher « 5 lignes » quand
        // le portail en a rendu 97 laissait croire à un export tronqué.
        i.lignes != null || i.crees != null
          ? ` — ${i.lignes ?? (i.crees ?? 0) + (i.revus ?? 0)} ${__("lignes reçues")}`
          : ""
      }</td></tr>
      <tr><td>${__("Certificats créés / revus")}</td><td>${i.crees ?? 0} / ${i.revus ?? 0}</td></tr>
      <tr><td>${__("Rapprochés automatiquement")}</td><td>${r.auto ?? 0}</td></tr>
      <tr><td>${__("Sans écriture")}</td><td>${r.sans_piece ?? 0}</td></tr>
      <tr><td>${__("À trancher")}</td><td>${(r.ambigus ?? 0) + (r.non_identifies ?? 0)}</td></tr>
      <tr><td>${__("PDF rangés")}</td><td>${p.attaches ?? 0}${p.erreurs ? ` (${p.erreurs} ${__("en erreur")})` : ""}</td></tr>
    </table>`;
  }

  _pdf() {
    frappe
      .call({
        method: "bank_retenue_sync.api.certificats.telecharger_pdf",
        args: { limite: 10, insert: 1 },
        freeze: true,
        freeze_message: __("Téléchargement des certificats…"),
      })
      .then((r) => {
        const m = r.message || {};
        frappe.show_alert(
          {
            message: __("{0} PDF rangés, {1} déjà justifiés à la main, {2} en erreur", [
              m.attaches || 0,
              m.deja_justifies || 0,
              m.erreurs || 0,
            ]),
            indicator: m.erreurs ? "orange" : "green",
          },
          7
        );
        this.refresh();
      });
  }

  // Les doublons d'AVANT le garde-fou : une pièce qui porte le PDF du portail ET un certificat
  // déposé à la main. Deux exemplaires du même justificatif, c'est un crédit d'impôt qui paraît
  // justifié deux fois — et personne ne le voit, les deux fichiers étant valables.
  _doublons() {
    frappe
      .call({
        method: "bank_retenue_sync.api.certificats.doublons_justificatifs",
        args: { insert: 0 },
        freeze: true,
        freeze_message: __("Recherche des doublons…"),
      })
      .then((r) => {
        const m = r.message || {};
        const d = new frappe.ui.Dialog({
          title: __("Justificatifs en double"),
          size: "large",
          fields: [{ fieldtype: "HTML", fieldname: "corps" }],
        });
        const lignes = (m.detail || [])
          .map(
            (l) => `<tr>
              <td>${frappe.utils.escape_html(l.customer || "")}</td>
              <td>${frappe.utils.escape_html(l.cible)}</td>
              <td class="rsv-rate">${l.portail.map(frappe.utils.escape_html).join("<br>")}</td>
              <td class="rsv-rate">${l.manuel.map(frappe.utils.escape_html).join("<br>")}</td>
              <td style="color:${l.identique ? "var(--rsv-gain)" : "var(--rsv-warn)"}">
                ${l.identique ? "✔ " + __("même document") : "⚠ " + __("à vérifier")}
                <div class="rsv-rate">${frappe.utils.escape_html(l.verdict || "")}</div>
              </td>
            </tr>`
          )
          .join("");
        d.fields_dict.corps.$wrapper.html(`
          <p>${__("Pièces portant à la fois le PDF téléchargé au portail et un certificat déposé à la main. Chaque couple est confronté par son <b>texte</b> : comparer les octets ne sert à rien, le portail regénère le certificat à chaque demande et y inscrit la date, ce qui change une centaine d'octets de métadonnées. Rien n'est supprimé tant que les deux ne se sont pas révélés identiques.")}</p>
          <div style="margin-bottom:10px">
            <label style="text-transform:none;font-weight:normal">${__("Exemplaire à conserver")} :
              <select data-role="garder" class="form-control input-sm" style="display:inline-block;width:auto;margin-left:6px">
                <option value="portail">${__("celui du portail (officiel, retéléchargeable)")}</option>
                <option value="manuel">${__("le dépôt manuel de l'équipe")}</option>
              </select>
            </label>
          </div>
          ${m.pieces
            ? `<table class="table table-bordered" style="font-size:12px">
                 <thead><tr><th>${__("Client")}</th><th>${__("Pièce")}</th><th>${__("Du portail")}</th><th>${__("Déposé à la main")}</th><th>${__("Vérification")}</th></tr></thead>
                 <tbody>${lignes}</tbody></table>`
            : `<div style="padding:14px;text-align:center;color:var(--text-muted)">${__("Aucun doublon : chaque pièce ne porte qu'un justificatif.")}</div>`}`);
        if (m.verifies) {
          d.set_primary_action(__("Dédoublonner {0} pièce(s) vérifiée(s)", [m.verifies]), () => {
            frappe
              .call({
                method: "bank_retenue_sync.api.certificats.doublons_justificatifs",
                args: { insert: 1, garder: d.$wrapper.find("[data-role='garder']").val() || "portail" },
                freeze: true,
              })
              .then((r2) => {
                d.hide();
                frappe.show_alert(
                  { message: __("{0} fichier(s) retiré(s)", [(r2.message || {}).supprimes || 0]), indicator: "green" },
                  7
                );
                this.refresh();
              });
          });
        }
        d.show();
      });
  }

  _creer(references) {
    frappe
      .call({
        method: "bank_retenue_sync.api.certificats.creer_paiements",
        args: { references: references ? JSON.stringify(references) : null, insert: 0 },
        freeze: true,
        freeze_message: __("Simulation…"),
      })
      .then((r) => {
        const m = r.message || {};
        const d = new frappe.ui.Dialog({ title: __("Écritures manquantes"), size: "large", fields: [{ fieldtype: "HTML", fieldname: "corps" }] });
        d.fields_dict.corps.$wrapper.html(this._apercu_creation(m));
        if (m.creables) {
          // Le libellé du bouton dit ce qui va se passer : créer un brouillon et créer une
          // écriture validée ne s'annulent pas de la même façon.
          const libelle = m.valider ? __("Créer et valider {0} écriture(s)", [m.creables]) : __("Créer {0} brouillon(s)", [m.creables]);
          d.set_primary_action(libelle, () => {
            frappe
              .call({
                method: "bank_retenue_sync.api.certificats.creer_paiements",
                args: { references: references ? JSON.stringify(references) : null, insert: 1 },
                freeze: true,
                freeze_message: __("Création…"),
              })
              .then((r2) => {
                const res = r2.message || {};
                d.hide();
                frappe.show_alert(
                  {
                    message: res.valider
                      ? __("{0} écriture(s) créée(s) et validée(s) ; certificat PDF demandé au portail", [res.crees || 0])
                      : __("{0} écriture(s) créée(s) en brouillon", [res.crees || 0]),
                    indicator: "green",
                  },
                  7
                );
                this.refresh();
              });
          });
        }
        d.show();
      });
  }

  _apercu_creation(m) {
    const dt = (v) => format_currency(v, "TND");
    const lignes = (m.detail || [])
      .map(
        (l) => `<tr>
          <td>${frappe.datetime.str_to_user(l.date)}</td>
          <td>${frappe.utils.escape_html(l.declarant || "")}</td>
          <td>${frappe.utils.escape_html(l.customer || "")}</td>
          <td class="rsv-num">${dt(l.montant)}</td>
          <td>${l.sales_invoice ? `<a href="/app/sales-invoice/${encodeURIComponent(l.sales_invoice)}">${l.sales_invoice}</a>` : "—"}</td>
          <td style="color:${l.statut === "a creer" || l.statut === "cree" ? "var(--rsv-gain)" : "var(--rsv-warn)"}">${frappe.utils.escape_html(l.statut)}</td>
          <td class="rsv-rate">${frappe.utils.escape_html(l.raison || l.payment_entry || "")}</td>
        </tr>`
      )
      .join("");
    return `<div style="margin-bottom:8px">${__("Écriture proposée : Dr « Avance impôt société » / Cr « Débiteurs », au montant du certificat, imputée sur la facture identifiée.")}
        ${m.valider ? __("Elle sera <b>validée aussitôt</b>, et le certificat PDF demandé au portail puis attaché à la facture.") : __("Elle restera en <b>brouillon</b>, à valider à la main.")}</div>
      <table class="table table-bordered" style="font-size:12px">
        <thead><tr><th>${__("Date")}</th><th>${__("Déclarant")}</th><th>${__("Client")}</th>
          <th class="rsv-num">${__("Retenue")}</th><th>${__("Facture")}</th><th>${__("Statut")}</th><th>${__("Détail")}</th></tr></thead>
        <tbody>${lignes}</tbody>
      </table>`;
  }

  // L'AUTRE SENS DU FLUX. Le tableau ci-dessus dit ce que les clients ont déclaré ; celui-ci dit
  // ce que nous avons déduit. Un crédit d'impôt sans certificat n'est pas opposable au fisc.
  // La pièce que règle l'écriture : la facture d'abord, la commande à défaut — même ordre que
  // celui où le flux TEJ range ses PDF.
  _piece_reglee(l) {
    if (l.sales_invoice)
      return `<a href="/app/sales-invoice/${encodeURIComponent(l.sales_invoice)}">${frappe.utils.escape_html(l.sales_invoice)}</a>`;
    if (l.sales_order)
      return `<a href="/app/sales-order/${encodeURIComponent(l.sales_order)}">${frappe.utils.escape_html(l.sales_order)}</a>
              <div class="rsv-rate">${__("commande")}</div>`;
    return "—";
  }

  // LE CERTIFICAT PAPIER. Il vaut celui du portail : le montrer ici, avec son lien, évite de
  // relancer un client qui a déjà fourni — et permet de vérifier le document d'un clic.
  _justificatif(l) {
    const manuels = l.justificatifs_manuels || [];
    if (!manuels.length) {
      const autres = (l.justificatifs || []).length;
      return autres ? `<span class="rsv-rate" title="${__("aucune ne se nomme comme un certificat")}">${autres} ${__("pièce(s) jointe(s)")}</span>` : "—";
    }
    return manuels
      .slice(0, 3)
      .map(
        (j) =>
          `<div><a href="${frappe.utils.escape_html(j.file_url)}" target="_blank" title="${__("sur la {0}", [j.source])}">📎 ${frappe.utils.escape_html(j.file_name)}</a></div>`
      )
      .join("");
  }

  _orphelines_dialogue() {
    const m = this._orphelines || {};
    const lignes = m.lignes || [];
    const s = m.synthese || {};
    const dt = (v) => format_currency(v, "TND");
    const couleur = {
      "sans certificat": "var(--rsv-loss)",
      "certificat probable": "var(--rsv-warn)",
      "certificat manuel": "var(--rsv-gain)",
    };
    const corps = lignes
      .map((l) => {
        const c = l.comparaison || {};
        // L'explication de « certificat manuel » porte déjà la comparaison : ne pas la répéter.
        const compare =
          c.texte && l.verdict !== "certificat manuel"
            ? `<div class="rsv-rate" style="${c.concordant === false ? "color:var(--rsv-warn)" : ""}">${frappe.utils.escape_html(c.texte)}</div>`
            : "";
        const alerte = l.alerte
          ? `<div style="color:var(--rsv-loss);font-weight:600">⚠️ ${frappe.utils.escape_html(l.alerte)}</div>`
          : "";
        return `<tr style="${l.alerte ? "background:var(--rsv-loss-bg)" : ""}">
          <td>${frappe.datetime.str_to_user(l.posting_date)}</td>
          <td>${frappe.utils.escape_html(l.customer || "")}</td>
          <td class="rsv-num">${dt(l.montant)}</td>
          <td>${this._piece_reglee(l)}</td>
          <td><a href="/app/payment-entry/${encodeURIComponent(l.name)}">${l.name}</a></td>
          <td style="color:${couleur[l.verdict] || "var(--text-muted)"}">${frappe.utils.escape_html(l.verdict)}</td>
          <td>${this._justificatif(l)}</td>
          <td class="rsv-rate">${frappe.utils.escape_html(l.explication || "")}${compare}${alerte}</td>
        </tr>`;
      })
      .join("");
    const d = new frappe.ui.Dialog({ title: __("Retenues comptabilisées sans certificat"), size: "extra-large", fields: [{ fieldtype: "HTML", fieldname: "corps" }] });
    d.fields_dict.corps.$wrapper.html(`
      <p>${__("Ce que nous avons déduit et que le portail ne déclare pas. « Certificat probable » est un rapprochement à faire ; « certificat manuel » est un certificat papier attaché à la facture ou à la commande — le dépôt au portail n'est obligatoire que depuis le 1<sup>er</sup> avril 2026, la retenue est prouvée et le paiement tenu pour rapproché ; « sans certificat » est un crédit d'impôt sans aucun justificatif, à réclamer au client.")}</p>
      <div class="rsv-kpis" style="margin-bottom:12px">
        <div class="rsv-kpi"><div class="lbl">${__("Écritures orphelines")}</div><div class="val">${s.total || 0} <span class="rsv-rate">${dt(s.montant_total)}</span></div></div>
        <div class="rsv-kpi ${s.probables ? "warn" : ""}"><div class="lbl">${__("Certificat probable")}</div><div class="val">${s.probables || 0} <span class="rsv-rate">${dt(s.montant_probables)}</span></div></div>
        <div class="rsv-kpi"><div class="lbl">${__("Certificat manuel")}</div><div class="val">${s.certificat_manuel || 0} <span class="rsv-rate">${dt(s.montant_certificat_manuel)}</span></div></div>
        <div class="rsv-kpi ${s.sans_certificat ? "warn" : ""}"><div class="lbl">${__("Sans certificat")}</div><div class="val">${s.sans_certificat || 0} <span class="rsv-rate">${dt(s.montant_sans_certificat)}</span></div></div>
        <div class="rsv-kpi ${s.doublons_possibles ? "warn" : ""}"><div class="lbl">${__("Doublon possible")}</div><div class="val">${s.doublons_possibles || 0} <span class="rsv-rate">${dt(s.montant_doublons)}</span></div></div>
        <div class="rsv-kpi ${s.ecarts_certificat ? "warn" : ""}"><div class="lbl">${__("Écarts papier / portail")}</div><div class="val">${s.ecarts_certificat || 0}</div></div>
      </div>
      <div style="overflow-x:auto"><table class="table table-bordered" style="font-size:12px">
        <thead><tr><th>${__("Date")}</th><th>${__("Client")}</th><th class="rsv-num">${__("Retenue")}</th>
          <th>${__("Pièce réglée")}</th><th>${__("Écriture")}</th><th>${__("Verdict")}</th>
          <th>${__("Justificatif")}</th><th>${__("Explication")}</th></tr></thead>
        <tbody>${corps || `<tr><td colspan="8" style="text-align:center;color:var(--text-muted)">${__("Aucune : toute retenue comptabilisée a son certificat.")}</td></tr>`}</tbody>
      </table></div>`);
    d.show();
  }

  // Deux situations, deux gestes. Facture avec du reste à payer : une écriture de retenue suffit.
  // Facture déjà soldée : le règlement a été encaissé pour le TTC entier alors que le client
  // retenait 1 % — il faut le reprendre à la baisse ET créer la retenue, sinon la même part
  // serait payée deux fois.
  _regulariser(reference) {
    frappe
      .call({
        method: "bank_retenue_sync.api.certificats.plan_regularisation",
        args: { reference },
        freeze: true,
        freeze_message: __("Analyse…"),
      })
      .then((r) => {
        const m = r.message || {};
        if (m.voie === "impossible") {
          // Sans facture, on ne sait pas quelle créance s'éteint — et aucune règle ne la
          // retrouvera : c'est la seule impasse qui a une sortie, et elle est humaine.
          if ((m.raison || "").indexOf("facture non identifiee") !== -1) {
            this._poser_facture(reference);
            return;
          }
          frappe.msgprint({
            title: __("Régularisation impossible"),
            message: frappe.utils.escape_html(m.raison || ""),
            indicator: "orange",
          });
          return;
        }
        if (m.voie === "creation") {
          this._creer([reference]);
          return;
        }
        if (m.voie === "choix") {
          this._choisir_reglement(reference, m);
          return;
        }
        const p = m.plan || {};
        const dt = (v) => format_currency(v, "TND");
        const d = new frappe.ui.Dialog({
          title: __("Loger la retenue dans une facture déjà soldée"),
          size: "large",
          fields: [{ fieldtype: "HTML", fieldname: "corps" }],
        });
        // Deux gestes bien distincts, et l'écran doit dire lequel : de l'argent compté (espèces,
        // chèque, virement) ne se réduit jamais — c'est son affectation qui bouge.
        // Chaque réaffectation dit la dette qu'elle éteint : c'est cette ligne-là que l'utilisateur
        // vérifie — « je vois pas la dette » est le seul reproche qui compte ici.
        const reaff = (p.reaffectations || [])
          .map(
            (a) =>
              `<a href="/app/${frappe.router.slug(a.doctype)}/${encodeURIComponent(a.name)}">${frappe.utils.escape_html(a.name)}</a> (${dt(a.montant)})
               — ${__("dette")} <a href="/app/payment-entry/${encodeURIComponent(a.dette_pe)}">${frappe.utils.escape_html(a.dette_pe)}</a> → ${dt(a.dette_reste)}`
          )
          .join("<br>");
        const lignes_argent = p.argent_compte
          ? `<tr><td>${__("Montant du règlement")}</td><td><b>${dt(p.reglement_avant)}</b> — ${__("inchangé : cet argent a été compté")}</td></tr>
             <tr><td>${__("Imputé à cette facture")}</td><td><b>${dt(p.allocation_avant)}</b> → <b>${dt(p.allocation_apres)}</b></td></tr>
             <tr><td>${__("Part libérée ({0})", [dt(p.retenue)])}</td>
                 <td>${reaff || `<b>${__("aucune écriture « Dette non payée » chez ce client")}</b> — ${dt(p.non_affecte)} ${__("resteront en avance à son crédit")}`}</td></tr>`
          : `<tr><td>${__("Montant du règlement")}</td><td><b>${dt(p.reglement_avant)}</b> → <b>${dt(p.reglement_apres)}</b> (${__("dette : aucun argent compté")})</td></tr>`;
        d.fields_dict.corps.$wrapper.html(`
          <p>${p.argent_compte
              ? __("La facture {0} est soldée, mais le client a retenu {1}. Le règlement est en {2} : son montant ne bouge pas — c'est son affectation qui change.", [p.facture, dt(p.retenue), p.reglement_mode])
              : __("La facture {0} est soldée, mais le règlement porte le TTC entier alors que le client a retenu {1}. Le total encaissé ne change pas — c'est sa composition qui devient juste.", [p.facture, dt(p.retenue)])}</p>
          <table class="table table-bordered" style="font-size:12px">
            <tr><td>${__("Règlement à reprendre")}</td>
                <td><a href="/app/payment-entry/${encodeURIComponent(p.reglement)}">${p.reglement}</a> (${frappe.utils.escape_html(p.reglement_mode || "")})</td></tr>
            ${lignes_argent}
            <tr><td>${__("Écriture de retenue créée")}</td><td><b>${dt(p.retenue)}</b> — Dr ${__("Avance impôt société")} / Cr ${__("Débiteurs")}</td></tr>
            <tr><td>${__("Total imputé à la facture")}</td><td>${dt(p.allocation_apres + p.retenue)} (${__("inchangé")})</td></tr>
          </table>
          <div style="padding:10px;border-radius:4px;background:var(--rsv-warn-bg);color:var(--rsv-warn)">
            ${p.brouillon
              ? __("Le règlement d'origine sera annulé et refait ; les deux écritures resteront en BROUILLON. Tant qu'elles ne sont pas validées, cette facture réapparaîtra comme due, et le règlement annulé sera conservé. (Réglage « Valider automatiquement les écritures de retenue » dans Bank Retenue Sync Settings.)")
              : __("Le règlement d'origine sera annulé, refait et validé, et la retenue validée dans la foulée. Le règlement annulé sera ensuite <b>supprimé</b> — il ne reste que l'écriture juste — sauf si un autre document le référence encore. Le certificat PDF sera demandé au portail et attaché à la facture.")}
          </div>`);
        d.set_primary_action(__("Régulariser"), () => {
          frappe
            .call({
              method: "bank_retenue_sync.api.certificats.ajuster",
              args: { reference, insert: 1, reglement: p.reglement },
              freeze: true,
              freeze_message: __("Reprise du règlement…"),
            })
            .then((r2) => {
              const res = r2.message || {};
              d.hide();
              const suite = res.valide
                ? (res.reglement_supprime
                    ? `<br>${__("Le règlement annulé {0} a été supprimé.", [res.reglement])}`
                    : `<br><b>${__("Règlement annulé conservé")}</b> : ${frappe.utils.escape_html(res.suppression_raison || "")}`) +
                  (res.pdf === "demande" ? `<br>${__("Certificat PDF demandé au portail : il sera attaché à la facture d'ici quelques minutes.")}` : "")
                : `<br><b>${__("Les deux écritures sont en brouillon : à valider.")}</b>`;
              frappe.msgprint({
                title: __("Retenue logée"),
                message: __("Règlement {0} repris en {1} ({2}), retenue {3} créée.", [
                  res.reglement, res.reglement_repris, dt(res.reglement_apres), res.payment_entry,
                ]) + suite,
                indicator: res.valide ? "green" : "orange",
              });
              this.refresh();
            });
        });
        d.show();
      });
  }

  // L'assiette déclarée ne correspond à aucune facture (facture partielle, avoir, regroupement) et
  // aucun règlement net ne la désigne. La retenue existe pourtant : c'est à l'utilisateur de dire
  // quelle créance elle éteint, et son choix fait autorité pour toujours.
  _poser_facture(reference) {
    const ligne = (this._data.certificats || []).find((l) => l.name === reference) || {};
    const d = new frappe.ui.Dialog({
      title: __("Quelle facture cette retenue éteint-elle ?"),
      fields: [
        { fieldtype: "HTML", fieldname: "info" },
        {
          fieldtype: "Link",
          fieldname: "sales_invoice",
          label: __("Facture"),
          options: "Sales Invoice",
          reqd: 1,
          description: __("Factures 2026 non encore rattachées à un certificat, les plus récentes d'abord. Cherchez par montant (« 3862 ») ou par date (« 2026-07 ») autant que par numéro."),
          // Requête maison : le Link standard trie par NOM et ne montre que dix lignes — sur un
          // client à 40 factures, « ACC-SINV-2025-… » précède « ACC-SINV-2026-… » et l'année en
          // cours disparaissait de la liste. Elle écarte aussi les factures déjà prises par un
          // autre certificat : deux certificats sur une facture = un crédit d'impôt compté deux fois.
          get_query: () => ({
            query: "bank_retenue_sync.api.certificats.factures_client",
            filters: { customer: ligne.customer, certificat: reference },
            page_length: 20,
          }),
        },
      ],
      primary_action_label: __("Poser la facture"),
      primary_action: (v) => {
        frappe
          .call({
            method: "bank_retenue_sync.api.certificats.poser_facture",
            args: { reference, sales_invoice: v.sales_invoice },
            freeze: true,
          })
          .then((r) => {
            const m = r.message || {};
            d.hide();
            frappe.show_alert(
              {
                message: m.payment_entry
                  ? __("Facture posée — une écriture de retenue y était déjà imputée ({0})", [m.payment_entry])
                  : __("Facture posée — cliquez à nouveau sur « Régulariser » pour loger la retenue"),
                indicator: "green",
              },
              8
            );
            this.refresh();
          });
      },
    });
    // LES FACTURES DU RÈGLEMENT NET, quand il y en a. Le client solde souvent plusieurs factures
    // d'un seul versement : aucune ne vaut l'assiette, mais ce règlement-là les nomme. Les proposer
    // au clic évite de chercher à la main ce que le rapprochement a déjà trouvé.
    const pistes = (ligne.candidats || []).filter((c) => typeof c === "string");
    const suggestions = pistes.length
      ? `<div style="margin-top:8px;padding:8px;border-radius:4px;background:var(--rsv-warn-bg)">
           ${__("Factures réglées par le versement identifié :")}
           ${pistes
             .map(
               (f) =>
                 `<button class="btn btn-xs btn-default" data-piste="${frappe.utils.escape_html(f)}" style="margin:3px 3px 0 0">${frappe.utils.escape_html(f)}</button>`
             )
             .join("")}
         </div>`
      : "";
    d.fields_dict.info.$wrapper.html(`
      <div style="margin-bottom:10px">
        <b>${frappe.utils.escape_html(ligne.declarant || "")}</b> — ${__("client")} ${frappe.utils.escape_html(ligne.customer || "")}<br>
        ${__("Retenue de {0} sur une assiette de {1}, déclarée le {2}", [
          format_currency(ligne.montant_retenue, "TND"),
          format_currency(ligne.total_brut, "TND"),
          frappe.datetime.str_to_user(ligne.date_paiement),
        ])}
        <div class="rsv-rate" style="margin-top:6px">${frappe.utils.escape_html(ligne.match_raison || "")}</div>
        ${suggestions}
      </div>`);
    d.fields_dict.info.$wrapper.on("click", "[data-piste]", (e) => {
      d.set_value("sales_invoice", $(e.currentTarget).data("piste"));
    });
    d.show();
  }

  // Plusieurs règlements peuvent porter la retenue et aucune règle ne les départage : la facture a
  // été soldée par plusieurs versements, et rien dans les comptes ne dit lequel portait la part
  // retenue. On ne choisit pas à la place de l'utilisateur — on lui montre les candidats.
  _choisir_reglement(reference, m) {
    const dt = (v) => format_currency(v, "TND");
    const options = (m.candidats || []).map((c) => ({
      label: `${frappe.datetime.str_to_user(c.date)} — ${dt(c.montant)} (${c.mode}) · ${c.name} → ${dt(c.montant - m.montant)}`,
      value: c.name,
    }));
    const d = new frappe.ui.Dialog({
      title: __("Quel règlement portait la retenue ?"),
      size: "large",
      fields: [
        { fieldtype: "HTML", fieldname: "info" },
        { fieldtype: "Select", fieldname: "reglement", label: __("Règlement à reprendre"), reqd: 1, options },
      ],
      primary_action_label: __("Simuler"),
      primary_action: (v) => {
        frappe
          .call({
            method: "bank_retenue_sync.api.certificats.ajuster",
            args: { reference, insert: 0, reglement: v.reglement },
            freeze: true,
          })
          .then((r) => {
            const p = r.message || {};
            if (!p.ok) {
              frappe.msgprint({ title: __("Impossible"), message: frappe.utils.escape_html(p.raison || ""), indicator: "orange" });
              return;
            }
            d.set_primary_action(__("Régulariser {0} → {1}", [dt(p.reglement_avant), dt(p.reglement_apres)]), () => {
              frappe
                .call({
                  method: "bank_retenue_sync.api.certificats.ajuster",
                  args: { reference, insert: 1, reglement: v.reglement },
                  freeze: true,
                  freeze_message: __("Reprise du règlement…"),
                })
                .then((r2) => {
                  const res = r2.message || {};
                  d.hide();
                  frappe.msgprint({
                    title: __("Retenue logée"),
                    message: __("Règlement {0} repris en {1} ({2}), retenue {3} créée.", [res.reglement, res.reglement_repris, dt(res.reglement_apres), res.payment_entry]) +
                      (res.valide ? "" : `<br><b>${__("Les deux écritures sont en brouillon : à valider.")}</b>`),
                    indicator: res.valide ? "green" : "orange",
                  });
                  this.refresh();
                });
            });
          });
      },
    });
    d.fields_dict.info.$wrapper.html(`
      <div style="margin-bottom:10px">
        ${__("La facture {0} est soldée par plusieurs règlements. Le client a retenu {1} : le règlement choisi sera repris à la baisse d'autant, et la retenue créée pour la différence. Le total imputé à la facture ne bouge pas.", [m.facture, dt(m.montant)])}
        <div class="rsv-rate" style="margin-top:6px">${frappe.utils.escape_html(m.raison || "")}</div>
      </div>`);
    d.show();
  }

  // Le client posé à la main fait autorité — et devient l'alias qui rapprochera tout seul les
  // certificats suivants du même déclarant.
  _trancher(reference) {
    const ligne = (this._data.certificats || []).find((l) => l.name === reference) || {};
    const d = new frappe.ui.Dialog({
      title: __("Quel client a déclaré ce certificat ?"),
      fields: [
        { fieldtype: "HTML", fieldname: "info" },
        { fieldtype: "Link", fieldname: "customer", label: __("Client"), options: "Customer", reqd: 1, default: (ligne.candidats || [])[0] },
      ],
      primary_action_label: __("Valider"),
      primary_action: (v) => {
        frappe
          .call({
            method: "bank_retenue_sync.api.certificats.trancher",
            args: { reference, customer: v.customer },
            freeze: true,
          })
          .then(() => {
            d.hide();
            frappe.show_alert({ message: __("Client enregistré — il servira d'alias aux prochains certificats"), indicator: "green" }, 7);
            this.refresh();
          });
      },
    });
    d.fields_dict.info.$wrapper.html(
      `<div style="margin-bottom:10px">
        <b>${frappe.utils.escape_html(ligne.declarant || "")}</b> — ${__("matricule")} ${frappe.utils.escape_html(ligne.declarant_matricule || "")}<br>
        ${__("Retenue de {0} déclarée le {1}", [format_currency(ligne.montant_retenue, "TND"), frappe.datetime.str_to_user(ligne.date_paiement)])}<br>
        <span class="rsv-rate">${frappe.utils.escape_html(ligne.match_raison || "")}</span>
        ${(ligne.candidats || []).length ? `<br>${__("Candidats")} : ${ligne.candidats.map((c) => frappe.utils.escape_html(c)).join(", ")}` : ""}
      </div>`
    );
    d.show();
  }
};
