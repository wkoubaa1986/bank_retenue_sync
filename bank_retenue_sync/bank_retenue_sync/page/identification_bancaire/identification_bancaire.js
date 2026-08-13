frappe.pages["identification-bancaire"].on_page_load = function (wrapper) {
  const page = frappe.ui.make_app_page({
    parent: wrapper,
    title: "Identification bancaire",
    single_column: true,
  });
  $(wrapper).find(".layout-main-section").html(
    frappe.render_template("identification_bancaire", {})
  );
  new IdentificationBancaire(wrapper, page);
};

// Rendu pur : lignes, KPI et couverture viennent de
// bank_retenue_sync.api.mouvements.get_data. Le JS ne calcule aucun montant.

const IB_STATUTS = ["Identifie", "Orphelin", "A verifier", "Ignore"];

const IB_STATUT_LABEL = {
  Identifie: "Identifié",
  Orphelin: "Orphelin",
  "A verifier": "À vérifier",
  Ignore: "Ignoré",
};

// Champs affichés dans l'aperçu pop-up, par doctype (affichage uniquement).
const IB_PREVIEW_FIELDS = {
  "Journal Entry": [
    ["posting_date", "Date", "date"],
    ["cheque_no", "Numéro de référence"],
    ["total_debit", "Montant", "currency"],
    ["docstatus", "Statut", "docstatus"],
    ["user_remark", "Libellé"],
  ],
  "Payment Entry": [
    ["posting_date", "Date", "date"],
    ["party", "Tiers"],
    ["reference_no", "Référence"],
    ["paid_amount", "Montant", "currency"],
    ["docstatus", "Statut", "docstatus"],
  ],
  "Encaissement Paiement": [
    ["date", "Date", "date"],
    ["docstatus", "Statut", "docstatus"],
  ],
};

class IdentificationBancaire {
  constructor(wrapper, page) {
    this.$root = $(wrapper).find(".layout-main-section");
    this.page = page;
    this.state = {
      start: 0,
      page_length: 100,
      order_by: "date",
      order_dir: "desc",
      filters: {},
    };
    this._bind();
    this._actions();
    this._boot();
  }

  // ------------------------------------------------------------------ setup

  async _boot() {
    try {
      const r = await frappe.call({ method: this._m("get_filtres") });
      const f = r.message || {};
      this._fill("sens", f.sens || []);
      this._fill("statut", f.statut || f.statuts || [], IB_STATUT_LABEL);
      this._fill("categorie", f.categories || []);
      // Le libellé du choix « au-delà du seuil » porte la valeur du seuil : sans elle, l'option
      // ne dit pas à partir de quel montant une transaction est signalée.
      const seuil = flt(f.seuil_ecart || 5, 3);
      (f.ecarts || []).forEach((o) => {
        const lbl =
          o.valeur === "signale"
            ? `Écart supérieur à ${format_number(seuil, null, 3)}`
            : o.libelle;
        this.$root
          .find('[data-f="ecart"]')
          .append(`<option value="${o.valeur}">${frappe.utils.escape_html(lbl)}</option>`);
      });
      // Borne BASSE seulement : les 90 derniers jours de données réellement PRÉSENTES, et non
      // la date du jour — le registre peut ne contenir qu'un historique ancien, auquel cas un
      // filtre calé sur aujourd'hui afficherait une page vide.
      //
      // /!\ Surtout PAS de borne haute par défaut. Elle serait figée à l'ouverture de l'onglet,
      // et tout mouvement importé ensuite disparaîtrait sans un mot — c'est exactement ce qui
      // s'est produit : une page ouverte avant un import ne montrait plus rien au-delà.
      if (f.date_max) {
        const min = String(frappe.datetime.add_days(f.date_max, -90)).substring(0, 10);
        this.$root
          .find('[data-f="date_from"]')
          .val(f.date_min && String(f.date_min) > min ? String(f.date_min).substring(0, 10) : min);
      }
    } catch (e) {
      // pas bloquant : les filtres restent vides, la table se charge quand même
      console.error("identification-bancaire: get_filtres", e);
    }
    this.load();
    this._load_solde();
  }

  _m(fn) {
    return `bank_retenue_sync.api.mouvements.${fn}`;
  }

  _fill(name, values, labels) {
    const $sel = this.$root.find(`[data-f="${name}"]`);
    values.forEach((v) => {
      $sel.append(
        `<option value="${frappe.utils.escape_html(v)}">${frappe.utils.escape_html(
          (labels && labels[v]) || v
        )}</option>`
      );
    });
  }

  _bind() {
    let timer = null;
    this.$root.on("change", "[data-f]", () => {
      this.state.start = 0;
      this.load();
    });
    this.$root.on("input", '[data-f="search"]', () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        this.state.start = 0;
        this.load();
      }, 400);
    });
    this.$root.on("click", "th.sortable", (e) => {
      const field = $(e.currentTarget).data("field");
      if (this.state.order_by === field) {
        this.state.order_dir = this.state.order_dir === "asc" ? "desc" : "asc";
      } else {
        this.state.order_by = field;
        this.state.order_dir = "desc";
      }
      this.state.start = 0;
      this.load();
    });
    this.$root.on("click", '[data-action="prev"]', () => {
      this.state.start = Math.max(0, this.state.start - this.state.page_length);
      this.load();
    });
    this.$root.on("click", '[data-action="next"]', () => {
      if (this.state.start + this.state.page_length < (this._total || 0)) {
        this.state.start += this.state.page_length;
        this.load();
      }
    });
    this.$root.on("click", "a[data-preview]", (e) => {
      e.preventDefault();
      const $a = $(e.currentTarget);
      this._preview($a.attr("data-doctype"), $a.attr("data-name"));
    });
    this.$root.on("click", "[data-ignorer]", (e) =>
      this._ignorer($(e.currentTarget).attr("data-ignorer"))
    );
    this.$root.on("click", "[data-reactiver]", (e) =>
      this._reactiver($(e.currentTarget).attr("data-reactiver"))
    );
    this.$root.on("click", "[data-creer]", (e) =>
      this._creer($(e.currentTarget).attr("data-creer"))
    );
    this.$root.on("click", '[data-action="historique-solde"]', () => this._historique_solde());
    this.$root.on("click", '[data-action="decomposer"]', () => this._decomposer_ecart());
    this.$root.on("click", '[data-action="rapprochement"]', () => this._rapprochement());
    this.$root.on("click", '[data-action="capturer"]', () =>
      frappe.confirm(
        __("Une nouvelle capture du solde va être demandée à la banque, puis lue. Continuer ?"),
        () => this._load_solde(true)
      )
    );
    this.$root.on("click", "[data-groupe]", (e) =>
      this._groupe($(e.currentTarget).attr("data-groupe"))
    );
  }

  _actions() {
    this.page.set_primary_action(
      __("Reclassifier"),
      () => this._reclassifier(),
      "refresh"
    );
    this.page.add_menu_item(__("Écarts banque ↔ ERPNext"), () => this._rapprochement());
    this.page.add_menu_item(__("Rafraîchir l'export bancaire"), () => this._rafraichir());
    this.page.add_menu_item(__("Exporter en Excel"), () => this._excel());
    this.page.add_menu_item(__("Régler les dépenses récurrentes"), () =>
      frappe.set_route("Form", "Bank Retenue Sync Settings")
    );
  }

  // ------------------------------------------------------------------ data

  _filters() {
    const f = {};
    this.$root.find("[data-f]").each((_, el) => {
      const v = $(el).val();
      if (v) f[$(el).attr("data-f")] = v;
    });
    // « vue » pilote le sens de lecture, il n'est pas un filtre du serveur : le laisser passer
    // ferait échouer get_data sur un argument inconnu.
    delete f.vue;
    return f;
  }

  _vue() {
    return this.$root.find('[data-f="vue"]').val() || "banque";
  }

  async load() {
    this.$root.find('[data-role="table"]').html('<div class="ib-loading">Chargement…</div>');
    // Les filtres du relevé (statut, catégorie, écart) n'ont pas d'équivalent côté pièces :
    // les désactiver vaut mieux que les laisser actifs sans effet.
    const cote_erpnext = this._vue() === "erpnext";
    this.$root
      .find('[data-f="statut"], [data-f="categorie"], [data-f="ecart"]')
      .prop("disabled", cote_erpnext);
    if (cote_erpnext) {
      return this._load_pieces();
    }
    const args = Object.assign({}, this._filters(), {
      start: this.state.start,
      page_length: this.state.page_length,
      order_by: this.state.order_by,
      order_dir: this.state.order_dir,
    });
    try {
      const r = await frappe.call({ method: this._m("get_data"), args });
      this._data = r.message || { rows: [], kpi: {}, total: 0 };
      this._total = this._data.total || 0;
      // Seuil d'alerte sur l'écart de paiement, paramétré dans les Réglages : le serveur fait foi.
      this.seuil_ecart = flt(this._data.seuil_ecart || 5, 3);
      this._render();
    } catch (e) {
      // Un echec ici laissait « Chargement… » a l'ecran indefiniment : l'utilisateur voyait une
      // page qui « ne fait rien » sans savoir pourquoi. On affiche toujours la cause.
      console.error("identification-bancaire: get_data", e);
      this.$root.find('[data-role="table"]').html(
        `<div class="ib-empty">Le chargement a échoué.<br>
         <span style="font-size:12px">${frappe.utils.escape_html(
           (e && (e.message || e.responseText)) || "erreur inconnue"
         )}</span></div>`
      );
    }
  }

  /* Vue inverse : les pièces ERPNext qu'AUCUN mouvement bancaire ne rapproche.
     Le tableau principal part du relevé — une écriture sans mouvement en face n'y a, par
     construction, aucune ligne où s'afficher. C'est l'autre moitié du rapprochement.
     Le volume est faible (une douzaine de lignes) : pas de pagination serveur ici, et les
     filtres sens/recherche s'appliquent côté client. */
  async _load_pieces() {
    const f = this._filters();
    try {
      const r = await frappe.call({
        method: this._m("get_ecarts"),
        args: { date_from: f.date_from, date_to: f.date_to },
      });
      this._ecarts = r.message || {};
      this._render_solde();
      this._render_pieces();
    } catch (e) {
      console.error("identification-bancaire: get_ecarts", e);
      const msg = String((e && (e.message || e.responseText)) || "erreur inconnue");
      // Cause la plus fréquente et la moins devinable : le serveur tourne encore sur l'ancien
      // code, `bench serve` ne rechargeant jamais le Python.
      const indice = /has no attribute|417/.test(msg)
        ? "<br><b>Redémarrez bench</b> : le serveur tourne sur une version antérieure du code."
        : "";
      this.$root.find('[data-role="table"]').html(
        `<div class="ib-empty">Le chargement a échoué.<br>
         <span style="font-size:12px">${frappe.utils.escape_html(msg)}${indice}</span></div>`
      );
    }
  }

  _render_pieces() {
    const d = this._ecarts || {};
    const esc = frappe.utils.escape_html;
    const nb = (v) => format_number(flt(v || 0, 3), null, 3);
    const f = this._filters();

    let rows = d.erpnext_sans_banque || [];
    if (f.sens) rows = rows.filter((p) => p.sens === f.sens);
    if (f.search) {
      const q = String(f.search).toLowerCase();
      rows = rows.filter(
        (p) =>
          String(p.voucher_no || "").toLowerCase().includes(q) ||
          String(p.texte || "").toLowerCase().includes(q)
      );
    }

    // Tuiles : une par verdict, plus le rappel de l'autre côté — les deux sens doivent rester
    // visibles en même temps, sinon on croit avoir tout vu.
    const t = d.totaux || {};
    const bloc = t.erpnext_sans_banque || {};
    const verdicts = [
      ["probable", "Lien à établir", "averifier"],
      ["ecart_de_montant", "Écart de montant", "orphelin"],
      ["doublon_probable", "Doublon probable", "orphelin"],
      ["hors_registre", "Hors registre", ""],
      ["trop_recent", "Trop récent", ""],
      ["sans_trace", "Sans trace", "orphelin"],
    ];
    const tiles = verdicts
      .filter((v) => (bloc[v[0]] || {}).nb)
      .map(
        (v) => `<div class="ib-kpi ${v[2]}">
          <div class="lbl">${v[1]}</div>
          <div class="val">${nb((bloc[v[0]] || {}).montant)}</div>
          <div class="sub">${(bloc[v[0]] || {}).nb} pièce(s)</div>
        </div>`
      );
    tiles.push(`<div class="ib-kpi">
      <div class="lbl">Mouvements sans pièce</div>
      <div class="val">${nb((t.banque_sans_erpnext || {}).montant)}</div>
      <div class="sub">${(t.banque_sans_erpnext || {}).nb || 0} — voir la vue « Mouvements bancaires »</div>
    </div>`);
    this.$root.find('[data-role="kpis"]').html(tiles.join(""));
    this._render_projection();
    this.$root.find('[data-role="cover"]').html(
      `<div class="ib-cover-item" style="grid-column:1/-1">
        <div class="ib-cover-head"><b>Pièces ERPNext sur le compte bancaire</b>
          <span>${d.liees || 0} rapprochées sur ${d.pieces || 0}</span></div>
        <div class="ib-legend">Une remise de chèques est <i>un</i> crédit en banque mais
          <i>une pièce par chèque</i> dans ERPNext : c'est la référence citée, le n° de chèque ou
          le n° de bordereau qui les relie — jamais le montant.</div>
      </div>`
    );

    const lignes = rows
      .map(
        (p) => `<tr data-piece="${esc(p.voucher_no)}">
        <td style="white-space:nowrap">${frappe.datetime.str_to_user(p.posting_date)}</td>
        <td><span class="ib-badge ${esc(p.sens)}">${esc(p.sens)}</span></td>
        <td class="op">${esc((p.texte || "").slice(0, 90))}</td>
        <td class="num">${nb(p.montant)}</td>
        <td><span class="ib-badge Averifier">${esc(p.statut_ecart || "")}</span></td>
        <td><a href="/app/${esc(frappe.router.slug(p.voucher_type))}/${encodeURIComponent(
          p.voucher_no
        )}" target="_blank">${esc(p.voucher_no)}</a></td>
        <td class="raison">${esc(p.motif || "")}</td>
      </tr>`
      )
      .join("");

    this.$root.find('[data-role="table"]').html(
      rows.length
        ? `<table class="ib-tbl">
            <thead><tr><th>Date</th><th>Sens</th><th>Libellé de la pièce</th>
              <th class="num">Montant</th><th>Verdict</th><th>Pièce</th><th>Motif</th></tr></thead>
            <tbody>${lignes}</tbody></table>`
        : `<div class="ib-empty">Aucune pièce ERPNext sans mouvement bancaire sur la période.</div>`
    );
    this.$root
      .find('[data-role="count"]')
      .text(`${rows.length} pièce(s) — ${nb(bloc.montant)} DT`);
    this.$root.find('[data-role="page"]').text("");
    this.$root.find('[data-action="prev"], [data-action="next"]').prop("disabled", true);
  }

  /* Projection de l'écart : ce qu'il deviendrait une fois traité ce qui n'est rapproché d'aucun
     côté. Une pièce sans mouvement se résout de deux façons opposées — la banque finit par
     passer l'opération, ou la pièce était fausse et on l'annule — et les DEUX déplacent l'écart
     du même montant dans le même sens. La projection ne préjuge donc pas de l'issue.
     Les verdicts « Lien à établir », « Écart de montant » et « Hors registre » n'y figurent pas :
     leur contrepartie est déjà au relevé, donc déjà dans le solde bancaire. */
  _render_projection() {
    const p = (this._ecarts || {}).projection;
    const zone = this.$root.find('[data-role="cover"]');
    if (!p || p.ecart === null || p.ecart === undefined) {
      return;
    }
    const esc = frappe.utils.escape_html;
    const nb = (v) => format_number(flt(v || 0, 3), null, 3);
    const signe = (v) => (flt(v, 3) > 0 ? "+" : "") + nb(v);
    const LIB = {
      "Sans trace": "sans trace au relevé",
      "Trop recent": "trop récentes (relevé pas à jour)",
      "Doublon probable": "doublons (excédent seul)",
      "a comptabiliser": "mouvements bancaires à comptabiliser",
    };
    const ligne = (x) => `<tr>
          <td>${x.cote === "banque" ? "Côté banque" : "Côté ERPNext"} — ${esc(
      LIB[x.verdict] || x.verdict
    )}</td>
          <td style="text-align:right">${x.nb}</td>
          <td style="text-align:right;font-weight:600;color:${
            flt(x.effet, 3) > 0 ? "#1e7e34" : "#a93226"
          }">${signe(x.effet)}</td>
        </tr>`;
    // Classées par NATURE et non par côté : un doublon côté ERPNext a presque toujours son
    // pendant côté banque (la même opération, comptée deux fois d'un côté et pas du tout de
    // l'autre). Les séparer par côté masquait qu'ils s'annulent.
    const attente = (p.postes || []).filter((x) => x.nature === "delai").map(ligne).join("");
    const corrections = (p.postes || [])
      .filter((x) => x.nature === "correction")
      .map(ligne)
      .join("");

    zone.append(`
      <div class="ib-cover-item" style="grid-column:1/-1">
        <div class="ib-cover-head"><b>Projection de l'écart</b>
          <span>solde du ${frappe.datetime.str_to_user(p.date)}</span></div>
        <table class="ib-tbl" style="font-size:12px">
          <tbody>
            <tr><td>Écart constaté (banque ${nb(p.banque)} − ERPNext ${nb(p.erpnext)})</td>
                <td style="text-align:right"></td>
                <td class="num" style="font-weight:600">${signe(p.ecart)}</td></tr>
            ${attente}
            <tr><th>Écart projeté — la banque rattrape son retard</th><th></th>
                <th class="num">${signe(p.ecart_projete_delai)}</th></tr>
            ${corrections}
            <tr><th>Écart projeté — corrections passées aussi</th><th></th>
                <th class="num">${signe(p.ecart_projete)}</th></tr>
          </tbody>
        </table>
        <div class="ib-legend">Classement par <b>nature</b>, pas par côté : un doublon côté
          ERPNext a le plus souvent son pendant côté banque — la même opération comptée deux fois
          d'un côté et pas du tout de l'autre — et les deux s'annulent. Les verdicts « Lien à
          établir », « Écart de montant » et « Hors registre » sont exclus : leur contrepartie
          figure déjà au relevé, donc dans le solde bancaire.</div>
      </div>`);
  }

  // ------------------------------------------------------------------ render

  _render() {
    this._render_solde();
    this._render_kpis();
    this._render_cover();
    this._render_table();
    this._render_foot();
  }

  _bucket(sens) {
    return (this._data.kpi || {})[sens] || {};
  }

  _stat(sens, statut) {
    return this._bucket(sens)[statut] || { nb: 0, montant: 0 };
  }

  _money(v) {
    return format_number(v || 0, null, 3);
  }

  async _load_solde(capture = false) {
    try {
      const r = await frappe.call({
        method: this._m("get_solde"),
        args: { capture: capture ? 1 : 0 },
        freeze: capture,
        freeze_message: __("Capture du solde bancaire en cours…"),
      });
      this._solde = r.message || {};
      this._render_solde();
      if (capture && this._solde.erreur) {
        frappe.msgprint({
          title: __("Capture indisponible"),
          message: frappe.utils.escape_html(this._solde.erreur),
          indicator: "orange",
        });
      }
    } catch (e) {
      // Cas courant en developpement : le worker web tourne encore sur une version du module
      // anterieure a l'ajout de la methode (`bench serve` ne recharge pas le Python). Sans
      // message, le bandeau resterait vide sans qu'on sache pourquoi.
      console.error("identification-bancaire: get_solde", e);
      const msg = String((e && (e.message || e.responseText)) || "");
      this._solde_erreur = /has no attribute|Failed to get method/i.test(msg)
        ? "méthode absente du serveur : redémarrez bench (Ctrl+C puis bench start)"
        : msg.slice(0, 120) || "solde indisponible";
      this._render_solde();
    }
  }

  _render_solde() {
    const s = this._solde || {};
    const rel = s.releve || {};
    const esc = frappe.utils.escape_html;
    const nb = (v) => (v === null || v === undefined ? "—" : format_number(v, null, 3));

    // Le solde ERPNext affiché est celui À LA DATE DU RELEVÉ, pas celui d'aujourd'hui :
    // comparer deux dates différentes produirait un écart qui n'existe pas.
    const erpnext = s.erpnext_a_la_date !== undefined ? s.erpnext_a_la_date : s.erpnext;
    const ecart = s.ecart;
    const nul = ecart !== null && ecart !== undefined && Math.abs(ecart) < 0.005;

    const banque = s.banque === null || s.banque === undefined
      ? `<div class="val">—</div>
         <div class="sub">${esc(this._solde_erreur || "aucune capture archivée")}</div>`
      : `<div class="val">${nb(s.banque)}</div>
         <div class="sub">capturé le ${frappe.datetime.str_to_user(
             rel.capture_datetime || rel.date_solde
           )}${
           rel.type_solde && rel.type_solde !== "Non precise" ? " · " + esc(rel.type_solde) : ""
         }${rel.concordance ? " · 2 lectures concordantes" : " · lecture unique"}</div>
         ${
           rel.derniere_operation
             ? `<div class="sub">dernière opération au portail : ${frappe.datetime.str_to_user(
                 rel.derniere_operation
               )}</div>`
             : ""
         }`;

    // Le solde peut courir PLUS LOIN que le registre : l'ecart contient alors des operations
    // simplement pas encore importees, ce qui ne se corrige pas par une ecriture.
    let retard = "";
    if (rel.derniere_operation && s.registre && this._data && this._data.asof) {
      const j = frappe.datetime.get_day_diff(rel.derniere_operation, this._data.asof);
      if (j > 0) {
        retard = `<div class="sub" style="color:var(--text-on-orange,#b9770e)">⚠ ${j} jour${
          j > 1 ? "s" : ""
        } d'opérations non encore importées</div>`;
      }
    }

    /* Écart PROJETÉ, à côté de l'écart constaté : ce que celui-ci deviendrait une fois traitées
       les pièces ERPNext qu'aucun mouvement ne rapproche (dépôts en circulation, doublons,
       pièces sans trace). Un écart brut ne dit pas s'il se résorbera de lui-même — c'est
       précisément ce que ce chiffre ajoute. Le côté banque, lui, reste dans le panneau détaillé :
       il dépend d'écritures à créer, pas d'un simple délai. */
    const pr = s.projection || {};
    const attente = (pr.postes || []).filter((x) => x.nature === "delai");
    const nb_attente = attente.reduce((n, x) => n + (x.nb || 0), 0);
    let projete = "";
    if (pr.ecart_projete_delai !== null && pr.ecart_projete_delai !== undefined && nb_attente) {
      const pnul = Math.abs(flt(pr.ecart_projete_delai, 3)) < 0.005;
      const mieux = Math.abs(flt(pr.ecart_projete_delai, 3)) < Math.abs(flt(pr.ecart, 3));
      projete = `
      <div class="delta ${pnul ? "ok" : ""}">
        <div class="lbl">Écart projeté</div>
        <div class="val" style="${mieux ? "color:var(--text-on-green,#28a745)" : ""}">${nb(
        pr.ecart_projete_delai
      )}</div>
        <div class="sub">${nb_attente} pièce(s) en attente de crédit
          (${flt(pr.effet_delai, 3) > 0 ? "+" : ""}${nb(pr.effet_delai)})</div>
        <div class="sub">corrections à trancher, hors projection :
          ${flt(pr.effet_correction, 3) > 0 ? "+" : ""}${nb(pr.effet_correction)}</div>
      </div>`;
    }

    this.$root.find('[data-role="solde"]').html(`
      <div>
        <div class="lbl">Solde banque (réel)</div>
        ${banque}
      </div>
      <div>
        <div class="lbl">Solde ERPNext</div>
        <div class="val">${nb(erpnext)}</div>
        <div class="sub">${esc(rel.compte ? "compte " + rel.compte : "compte Zitouna")}</div>
      </div>
      <div class="delta ${nul ? "ok" : ""}">
        <div class="lbl">Écart</div>
        <div class="val">${nb(ecart)}</div>
        <div class="sub">${
          ecart === null || ecart === undefined
            ? "capture requise"
            : nul
            ? "les deux soldes concordent"
            : "reste à comptabiliser"
        }</div>
        ${
          // Un écart net d'une ouverture acceptée n'est plus le même chiffre que le brut :
          // le taire ferait croire à une mesure alors que c'est un solde reporté.
          (s.ouverture || {}).date
            ? `<div class="sub">depuis le ${frappe.datetime.str_to_user(
                s.ouverture.date
              )} · brut ${nb(s.ecart_brut)}, ouverture acceptée ${nb(s.ouverture.montant)}</div>`
            : ""
        }
        ${retard}
      </div>
      ${projete}
      <div class="ib-solde-actions" style="gap:6px">
        <button data-action="capturer">Capturer le solde</button>
        <button data-action="decomposer">D'où vient l'écart ?</button>
        <button data-action="rapprochement">Écarts des deux côtés</button>
        <button data-action="historique-solde">Historique</button>
      </div>
    `);
  }

  /* Décomposition de l'écart banque / ERPNext, dans les deux sens.
     On compare les FLUX réellement enregistrés de part et d'autre sur la période du registre,
     plutôt que de se fier au statut : « À vérifier » désigne un mouvement catégorisé sans
     automatisation, ce qui ne veut pas dire qu'il manque en comptabilité. */
  _decomposer_ecart() {
    const d = (this._solde && this._solde.decomposition) || null;
    const esc = frappe.utils.escape_html;
    if (!d || !d.postes || !d.postes.length) {
      frappe.msgprint(__("Décomposition indisponible : capturez d'abord le solde bancaire."));
      return;
    }
    const nb = (v) => format_number(flt(v || 0, 3), null, 3);
    const signe = (v) => (flt(v, 3) > 0 ? "+" : "") + nb(v);

    const postes = d.postes
      .map(
        (p) => `<tr>
          <td>${esc(p.libelle)}</td>
          <td style="text-align:right">${nb(p.banque)}</td>
          <td style="text-align:right">${nb(p.erpnext)}</td>
          <td style="text-align:right;font-weight:600">${nb(p.montant)}</td>
          <td style="text-align:right;color:${
            flt(p.effet, 3) >= 0 ? "#1e7e34" : "#a93226"
          }">${signe(p.effet)}</td>
        </tr>`
      )
      .join("");

    const lignes = (d.lignes || [])
      .map(
        (l) => `<tr>
          <td style="white-space:nowrap">${frappe.datetime.str_to_user(l.date)}</td>
          <td><span class="ib-badge ${esc(l.sens)}">${esc(l.sens)}</span></td>
          <td>${esc(l.reference || "")}</td>
          <td>${esc((l.operation || "").slice(0, 46))}</td>
          <td style="text-align:right">${nb(l.montant)}</td>
          <td>${esc(l.categorie || "")}</td>
        </tr>`
      )
      .join("");

    const ecarts = (d.ecarts || [])
      .map(
        (e) => `<tr>
          <td style="white-space:nowrap">${frappe.datetime.str_to_user(e.date)}</td>
          <td>${esc(e.reference || "")}</td>
          <td>${esc((e.operation || "").slice(0, 40))}</td>
          <td style="text-align:right">${nb(e.montant)}</td>
          <td style="text-align:right">${nb(e.montant_document)}</td>
          <td style="text-align:right;font-weight:600;color:#a93226">${signe(e.ecart)}</td>
        </tr>`
      )
      .join("");

    const per = d.periode
      ? `du ${frappe.datetime.str_to_user(d.periode[0])} au ${frappe.datetime.str_to_user(
          d.periode[1]
        )}`
      : "";

    new frappe.ui.Dialog({
      title: __("D'où vient l'écart ?"),
      size: "extra-large",
      fields: [
        {
          fieldtype: "HTML",
          options: `
        <div style="font-size:13px">
          <p style="color:var(--text-muted)">Comparaison des flux réellement enregistrés de part
          et d'autre, ${esc(per)}. L'<b>effet</b> indique de combien l'écart bougera une fois le
          poste comptabilisé.</p>
          <table class="table table-bordered" style="font-size:12px">
            <thead><tr><th>Poste</th><th style="text-align:right">Banque</th>
              <th style="text-align:right">ERPNext</th><th style="text-align:right">Manque</th>
              <th style="text-align:right">Effet sur l'écart</th></tr></thead>
            <tbody>${postes}</tbody>
            <tfoot>
              <tr><th colspan="4">Écart constaté</th>
                  <th style="text-align:right">${nb(d.ecart)}</th></tr>
              <tr><th colspan="4">Effet attendu des postes ci-dessus</th>
                  <th style="text-align:right">${signe(d.effet_attendu)}</th></tr>
              <tr><th colspan="4">Non expliqué (écritures antérieures au registre,
                  ou sans contrepartie au relevé)</th>
                  <th style="text-align:right">${nb(d.inexplique)}</th></tr>
            </tfoot>
          </table>

          <h5 style="margin-top:18px">Mouvements sans pièce ERPNext (${(d.lignes || []).length})</h5>
          <div style="max-height:260px;overflow:auto">
            <table class="table table-bordered" style="font-size:12px">
              <thead><tr><th>Date</th><th>Sens</th><th>Référence</th><th>Libellé</th>
                <th style="text-align:right">Montant</th><th>Catégorie</th></tr></thead>
              <tbody>${lignes || '<tr><td colspan="6">Aucun.</td></tr>'}</tbody>
            </table>
          </div>

          <h5 style="margin-top:18px">Écarts de paiement sur pièces déjà saisies
            (${(d.ecarts || []).length})</h5>
          <div style="max-height:220px;overflow:auto">
            <table class="table table-bordered" style="font-size:12px">
              <thead><tr><th>Date</th><th>Référence</th><th>Libellé</th>
                <th style="text-align:right">Banque</th><th style="text-align:right">Comptabilisé</th>
                <th style="text-align:right">Écart</th></tr></thead>
              <tbody>${ecarts || '<tr><td colspan="6">Aucun.</td></tr>'}</tbody>
            </table>
          </div>
        </div>`,
        },
      ],
    }).show();
  }

  /* Rapprochement dans LES DEUX SENS.
     La page ne montrait que les mouvements sans pièce. Une écriture ERPNext sans mouvement
     bancaire — saisie en double, montant erroné, opération jamais passée en banque — restait
     invisible, alors qu'elle pèse autant sur l'écart de solde. */
  async _rapprochement() {
    const f = this._filters();
    let r;
    frappe.dom.freeze(__("Rapprochement des deux côtés…"));
    try {
      r = await frappe.call({
        method: this._m("get_ecarts"),
        args: { date_from: f.date_from, date_to: f.date_to },
      });
    } finally {
      frappe.dom.unfreeze();
    }
    const d = (r && r.message) || {};
    const esc = frappe.utils.escape_html;
    const nb = (v) => format_number(flt(v || 0, 3), null, 3);
    const t = d.totaux || {};

    // Le statut d'écart dit CE QU'IL FAUT FAIRE : rien n'est laissé sans verdict.
    const badge = (s) => {
      const couleurs = {
        Probable: "#1e7e34",
        "Ecart de montant": "#a93226",
        "Hors registre": "#5f6368",
        "Trop recent": "#5f6368",
        "Sans trace": "#9c6f00",
      };
      const libelles = {
        Probable: "Lien à établir",
        "Ecart de montant": "Écart de montant",
        "Hors registre": "Hors registre",
        "Trop recent": "Trop récent",
        "Sans trace": "Sans trace",
      };
      return `<span class="ib-badge" style="background:${couleurs[s] || "#5f6368"}22;color:${
        couleurs[s] || "#5f6368"
      }">${esc(libelles[s] || s || "")}</span>`;
    };

    const pieces = (d.erpnext_sans_banque || [])
      .map(
        (p) => `<tr>
          <td style="white-space:nowrap">${frappe.datetime.str_to_user(p.posting_date)}</td>
          <td><a href="/app/${esc(
            frappe.router.slug(p.voucher_type)
          )}/${encodeURIComponent(p.voucher_no)}" target="_blank">${esc(p.voucher_no)}</a></td>
          <td><span class="ib-badge ${esc(p.sens)}">${esc(p.sens)}</span></td>
          <td style="text-align:right;font-weight:600">${nb(p.montant)}</td>
          <td>${badge(p.statut_ecart)}</td>
          <td style="font-size:11.5px;color:var(--text-muted)">${esc(p.motif || "")}</td>
        </tr>`
      )
      .join("");

    const mvts = (d.banque_sans_erpnext || [])
      .map(
        (m) => `<tr>
          <td style="white-space:nowrap">${frappe.datetime.str_to_user(m.date)}</td>
          <td>${esc(m.reference || "")}</td>
          <td><span class="ib-badge ${esc(m.sens)}">${esc(m.sens)}</span></td>
          <td>${esc((m.operation || "").slice(0, 40))}</td>
          <td style="text-align:right;font-weight:600">${nb(m.montant)}</td>
          <td>${badge(m.statut_ecart)}</td>
          <td style="font-size:11.5px;color:var(--text-muted)">${esc(m.motif || "")}</td>
        </tr>`
      )
      .join("");

    const bloc = (b) =>
      b ? `${b.nb} ligne${b.nb > 1 ? "s" : ""} · ${nb(b.montant)} DT` : "—";

    // Décomposition de l'écart de flux sur la période. L'identité est exacte : la somme des
    // soldes par nature vaut la différence des flux, au millime. Un décalage de bord de période
    // se résorbe seul ; un mouvement sans pièce se comptabilise. C'est la nature qui dit
    // quoi faire, pas le montant.
    const x = d.explication || {};
    const signe = (v) => (flt(v, 3) > 0 ? "+" : "") + nb(v);
    const natures = (x.synthese || [])
      .map(
        (s) => `<tr>
          <td>${esc(s.nature)}</td>
          <td style="text-align:right">${s.nb}</td>
          <td style="text-align:right;font-weight:600;color:${
            flt(s.solde, 3) > 0 ? "#a93226" : "#1e7e34"
          }">${signe(s.solde)}</td>
        </tr>`
      )
      .join("");

    new frappe.ui.Dialog({
      title: __("Écarts banque ↔ ERPNext"),
      size: "extra-large",
      fields: [
        {
          fieldtype: "HTML",
          options: `
        <div style="font-size:13px">
          <p style="color:var(--text-muted)">
            ${d.mouvements || 0} mouvements bancaires et ${d.pieces || 0} pièces ERPNext sur le
            compte, du ${frappe.datetime.str_to_user((d.periode || {}).du)} au
            ${frappe.datetime.str_to_user((d.periode || {}).au)}.
            <b>${d.liees || 0}</b> pièces sont reliées au relevé (par référence citée, n° de chèque
            ou n° de bordereau). Une remise de chèques est <i>un</i> crédit en banque mais
            <i>une pièce par chèque</i> dans ERPNext : c'est la clé bancaire qui les relie, jamais
            le montant.
          </p>

          <h5 style="margin-top:16px">D'où vient l'écart de la période</h5>
          <p style="color:var(--text-muted);margin-bottom:6px">
            Flux banque ${signe(x.flux_banque)} · flux ERPNext ${signe(x.flux_erpnext)} ·
            <b>écart ${signe(x.ecart)}</b>. Décomposition exacte : la somme ci-dessous vaut
            l'écart au millime${
              Math.abs(flt(x.controle, 3)) >= 0.005
                ? ` (contrôle ${signe(x.controle)} — une ligne manque)`
                : ""
            }.
          </p>
          <table class="table table-bordered" style="font-size:12px">
            <thead><tr><th>Nature</th><th style="text-align:right">Groupes</th>
              <th style="text-align:right">Effet sur l'écart</th></tr></thead>
            <tbody>${natures || '<tr><td colspan="3">Aucun écart.</td></tr>'}</tbody>
          </table>

          <h5 style="margin-top:18px">Pièces ERPNext sans mouvement bancaire —
            ${bloc(t.erpnext_sans_banque)}</h5>
          <div style="max-height:300px;overflow:auto">
            <table class="table table-bordered" style="font-size:12px">
              <thead><tr><th>Date</th><th>Pièce</th><th>Sens</th>
                <th style="text-align:right">Montant</th><th>Verdict</th><th>Motif</th></tr></thead>
              <tbody>${pieces || '<tr><td colspan="6">Aucune : tout est rapproché.</td></tr>'}</tbody>
            </table>
          </div>

          <h5 style="margin-top:18px">Mouvements bancaires sans pièce ERPNext —
            ${bloc(t.banque_sans_erpnext)}</h5>
          <div style="max-height:300px;overflow:auto">
            <table class="table table-bordered" style="font-size:12px">
              <thead><tr><th>Date</th><th>Référence</th><th>Sens</th><th>Libellé</th>
                <th style="text-align:right">Montant</th><th>Verdict</th><th>Motif</th></tr></thead>
              <tbody>${mvts || '<tr><td colspan="7">Aucun : tout est comptabilisé.</td></tr>'}</tbody>
            </table>
          </div>
        </div>`,
        },
      ],
      primary_action_label: __("Exporter en Excel"),
      primary_action: () => {
        const args = $.param({ date_from: f.date_from || "", date_to: f.date_to || "" });
        window.open(
          `/api/method/bank_retenue_sync.api.mouvements.download_ecarts?${args}`,
          "_blank"
        );
      },
    }).show();
  }

  _historique_solde() {
    const h = (this._solde && this._solde.historique) || [];
    const esc = frappe.utils.escape_html;
    if (!h.length) {
      frappe.msgprint(__("Aucun relevé de solde archivé pour l'instant."));
      return;
    }
    const lignes = h
      .map((r) => {
        // Date ET heure de la capture : deux lectures du même jour n'ont pas la même valeur,
        // et c'est précisément l'écart entre elles qui montre l'activité du compte.
        const quand = r.capture_datetime
          ? frappe.datetime.str_to_user(r.capture_datetime)
          : frappe.datetime.str_to_user(r.date_solde);
        const op = r.derniere_operation
          ? frappe.datetime.str_to_user(r.derniere_operation)
          : "—";
        const ec = r.ecart === null || r.ecart === undefined ? "—" : format_number(r.ecart, null, 3);
        return `<tr>
          <td style="white-space:nowrap">${esc(quand)}</td>
          <td style="text-align:right">${format_number(r.solde_banque || 0, null, 3)}</td>
          <td style="text-align:right">${format_number(r.solde_erpnext || 0, null, 3)}</td>
          <td style="text-align:right;color:${
            Math.abs(r.ecart || 0) < 0.005 ? "#1e7e34" : "#a93226"
          }">${ec}</td>
          <td style="white-space:nowrap">${esc(op)}</td>
          <td>${r.concordance ? "✓" : "⚠"}</td>
          <td>${esc(r.type_solde || "")}</td>
        </tr>`;
      })
      .join("");
    frappe.msgprint({
      title: __("Historique des soldes bancaires"),
      wide: true,
      message: `
        <p style="color:var(--text-muted);font-size:12px">
          Chaque ligne est une lecture du portail, avec sa capture en pièce jointe sur le document.
          La colonne « Dernière opération » dit jusqu'où le solde court réellement.
        </p>
        <table class="table table-bordered" style="font-size:12px">
          <thead><tr>
            <th>Capture (date et heure)</th>
            <th style="text-align:right">Solde banque</th>
            <th style="text-align:right">Solde ERPNext</th>
            <th style="text-align:right">Écart</th>
            <th>Dernière opération</th>
            <th title="Deux lectures concordantes">✓</th>
            <th>Type</th>
          </tr></thead>
          <tbody>${lignes}</tbody>
        </table>`,
    });
  }

  _render_kpis() {
    const tiles = [];
    [
      ["Credit", "Crédits"],
      ["Debit", "Débits"],
    ].forEach(([sens, label]) => {
      [
        ["Identifie", "identifie", "identifiés"],
        ["Orphelin", "orphelin", "orphelins"],
      ].forEach(([statut, cls, mot]) => {
        const s = this._stat(sens, statut);
        tiles.push(`
          <div class="ib-kpi ${cls}">
            <div class="lbl">${label} ${mot}</div>
            <div class="val">${this._money(s.montant)}</div>
            <div class="sub">${s.nb} mouvement${s.nb > 1 ? "s" : ""}</div>
          </div>`);
      });
    });
    const av =
      this._stat("Credit", "A verifier").nb + this._stat("Debit", "A verifier").nb;
    const ig = this._stat("Credit", "Ignore").nb + this._stat("Debit", "Ignore").nb;
    tiles.push(`
      <div class="ib-kpi averifier">
        <div class="lbl">À vérifier</div><div class="val">${av}</div>
        <div class="sub">catégorisés, sans automatisation</div>
      </div>`);
    tiles.push(`
      <div class="ib-kpi">
        <div class="lbl">Ignorés</div><div class="val">${ig}</div>
        <div class="sub">mis à l'écart manuellement</div>
      </div>`);
    // Écarts de paiement au-delà du seuil : ni des frais, ni du bruit — des montants à expliquer.
    const ec = this._data.ecarts || { nb: 0, montant: 0 };
    tiles.push(`
      <div class="ib-kpi ${ec.nb ? "orphelin" : ""}">
        <div class="lbl">Écarts de paiement</div>
        <div class="val">${ec.nb}</div>
        <div class="sub">${
          ec.nb
            ? `${this._money(ec.montant)} — au-delà de ${format_number(
                flt(this._data.seuil_ecart || 5, 3),
                null,
                3
              )}`
            : `aucun au-delà de ${format_number(flt(this._data.seuil_ecart || 5, 3), null, 3)}`
        }</div>
      </div>`);
    this.$root.find('[data-role="kpis"]').html(tiles.join(""));

    const asof = this._data.asof;
    const $al = this.$root.find('[data-role="alert"]');
    if (asof) {
      const jours = frappe.datetime.get_day_diff(frappe.datetime.get_today(), asof);
      if (jours > 4) {
        $al
          .addClass("show")
          .html(
            `Le dernier mouvement du registre date du <b>${frappe.datetime.str_to_user(
              asof
            )}</b> (${jours} jours). Les opérations récentes ne peuvent pas être rapprochées — ` +
              `utilisez « Rafraîchir l'export bancaire ».`
          );
      } else {
        $al.removeClass("show");
      }
    }
  }

  _render_cover() {
    const html = [
      ["Credit", "Encaissements"],
      ["Debit", "Décaissements"],
    ]
      .map(([sens, label]) => {
        const b = this._bucket(sens);
        const total = IB_STATUTS.reduce((s, k) => s + ((b[k] || {}).montant || 0), 0);
        const nb = IB_STATUTS.reduce((s, k) => s + ((b[k] || {}).nb || 0), 0);
        const ident = (b["Identifie"] || {}).montant || 0;
        const pct = total ? Math.round((ident / total) * 100) : 0;
        const seg = (k, cls) => {
          const v = (b[k] || {}).montant || 0;
          const w = total ? (v / total) * 100 : 0;
          return w
            ? `<span class="${cls}" style="width:${w}%" title="${IB_STATUT_LABEL[k]} : ${this._money(
                v
              )}"></span>`
            : "";
        };
        return `
          <div class="ib-cover-item">
            <div class="ib-cover-head">
              <span>${label} <span style="color:var(--text-muted)">— ${nb} mouvements</span></span>
              <b>${pct}% identifié</b>
            </div>
            <div class="ib-bar">
              ${seg("Identifie", "s-identifie")}${seg("Orphelin", "s-orphelin")}
              ${seg("A verifier", "s-averifier")}${seg("Ignore", "s-ignore")}
            </div>
            <div class="ib-legend">
              <i class="s-identifie" style="background:#28a745"></i>identifié
              <i style="background:#c0392b"></i>orphelin
              <i style="background:#e0a800"></i>à vérifier
              <i style="background:#9aa0a6"></i>ignoré
            </div>
          </div>`;
      })
      .join("");
    this.$root.find('[data-role="cover"]').html(html);
  }

  _th(field, label, extra) {
    const on = this.state.order_by === field;
    const arrow = on ? (this.state.order_dir === "asc" ? " ▲" : " ▼") : "";
    return `<th class="sortable ${extra || ""}" data-field="${field}">${label}${arrow}</th>`;
  }

  _render_table() {
    const rows = this._data.rows || [];
    if (!rows.length) {
      this.$root
        .find('[data-role="table"]')
        .html('<div class="ib-empty">Aucun mouvement pour ce filtre.</div>');
      return;
    }
    const head = `
      <tr>
        ${this._th("date", "Date")}
        ${this._th("sens", "Sens")}
        ${this._th("operation", "Libellé")}
        ${this._th("reference", "Référence")}
        ${this._th("montant", "Montant", "num")}
        <th class="num" title="Montant bancaire moins montant comptabilisé">Écart</th>
        ${this._th("categorie", "Catégorie")}
        ${this._th("statut", "Statut")}
        <th>Document</th>
        <th>Raison</th>
        <th></th>
      </tr>`;
    const body = rows.map((r) => this._row(r)).join("");
    this.$root
      .find('[data-role="table"]')
      .html(`<table class="ib-tbl"><thead>${head}</thead><tbody>${body}</tbody></table>`);
  }

  _row(r) {
    const esc = frappe.utils.escape_html;
    const statut_cls = (r.statut || "").replace(/\s/g, "");
    const doc = r.document_name
      ? `<a href="/app/${frappe.router.slug(r.document_type)}/${encodeURIComponent(
          r.document_name
        )}" data-preview data-doctype="${esc(r.document_type)}" data-name="${esc(
          r.document_name
        )}">${esc(r.document_name)}</a>`
      : r.document_type
      ? `<span style="color:var(--text-muted)">${esc(r.document_type)}</span>`
      : "";
    const groupe = r.groupe
      ? `<div class="ib-groupe" data-groupe="${esc(r.groupe)}">${esc(r.groupe)}</div>`
      : "";
    return `
      <tr class="${r.ignore_manuel ? "ib-ignored" : ""}">
        <td>${r.date ? frappe.datetime.str_to_user(r.date) : ""}</td>
        <td><span class="ib-badge ${esc(r.sens)}">${esc(r.sens || "")}</span></td>
        <td class="op">${esc(r.operation || "")}${groupe}</td>
        <td>${esc(r.reference || "")}</td>
        <td class="num">${this._money(r.montant)}</td>
        <td class="num">${this._ecart_cell(r)}</td>
        <td>${esc(r.categorie || "")}${
      r.regle ? `<div style="font-size:11px;color:var(--text-muted)">${esc(r.regle)}</div>` : ""
    }</td>
        <td><span class="ib-badge ${statut_cls}">${esc(
      IB_STATUT_LABEL[r.statut] || r.statut || ""
    )}</span></td>
        <td>${doc}</td>
        <td class="raison">${esc(r.raison || r.ignore_motif || "")}</td>
        <td>${this._actions_cell(r)}</td>
      </tr>`;
  }

  /* Écart de paiement. Sous le seuil, il correspond aux frais bancaires prélevés à la source :
     on l'affiche en discret. Au-dessus, c'est une anomalie de saisie — écart négatif : la pièce
     porte plus que ce que la banque a versé (doublon, chèque impayé) ; positif : la banque a
     crédité plus que ce qui est saisi (encaissement manquant). */
  _ecart_cell(r) {
    const e = flt(r.ecart || 0, 3);
    if (!e) return "";
    const seuil = flt(this.seuil_ecart || 5, 3);
    const alerte = Math.abs(e) > seuil;
    const signe = e > 0 ? "+" : "";
    const titre = alerte
      ? `Écart de ${signe}${format_number(e, null, 3)} — banque ${format_number(
          flt(r.montant, 3),
          null,
          3
        )}, comptabilisé ${format_number(flt(r.montant_document, 3), null, 3)}`
      : "Écart inférieur au seuil : frais bancaires présumés";
    return `<span class="ib-ecart ${alerte ? "alerte" : "mineur"}" title="${frappe.utils.escape_html(
      titre
    )}">${alerte ? "⚠ " : ""}${signe}${format_number(e, null, 3)}</span>`;
  }

  _actions_cell(r) {
    const esc = frappe.utils.escape_html;
    const b = [];
    if (r.ignore_manuel) {
      b.push(`<button data-reactiver="${esc(r.cle)}">Réactiver</button>`);
    } else {
      if (r.statut === "Orphelin" && r.categorie !== "frais_bancaires") {
        b.push(`<button data-creer="${esc(r.cle)}">Créer l'écriture</button>`);
      }
      b.push(`<button data-ignorer="${esc(r.cle)}">Ignorer</button>`);
    }
    return `<div class="ib-act">${b.join("")}</div>`;
  }

  _render_foot() {
    const s = this.state;
    const from = this._total ? s.start + 1 : 0;
    const to = Math.min(s.start + s.page_length, this._total);
    // La periode couverte est affichee en clair : c'est ce qui manquait pour reperer d'un coup
    // d'oeil qu'un filtre ou un import tronquait la vue.
    const asof = this._data.asof
      ? ` — registre jusqu'au ${frappe.datetime.str_to_user(this._data.asof)}`
      : "";
    this.$root
      .find('[data-role="count"]')
      .text(`${from}–${to} sur ${this._total} mouvements${asof}`);
    this.$root.find('[data-role="page"]').text("");
    this.$root.find('[data-action="prev"]').prop("disabled", s.start === 0);
    this.$root
      .find('[data-action="next"]')
      .prop("disabled", s.start + s.page_length >= this._total);
  }

  // ------------------------------------------------------------------ actions

  async _reclassifier() {
    const f = this._filters();
    const r = await frappe.call({
      method: this._m("reclassifier"),
      args: { date_from: f.date_from || null, date_to: f.date_to || null },
      freeze: true,
      freeze_message: __("Reclassification…"),
    });
    const m = r.message || {};
    frappe.show_alert({
      message: __("{0} mouvements reclassés, {1} laissés en l'état (arbitrage manuel)", [
        m.maj || 0,
        m.ignores || 0,
      ]),
      indicator: "green",
    });
    this.load();
  }

  async _rafraichir() {
    frappe.confirm(
      __(
        "Un nouvel export bancaire va être demandé au service. L'opération dure plusieurs minutes. Continuer ?"
      ),
      async () => {
        const r = await frappe.call({
          method: this._m("rafraichir"),
          freeze: true,
          freeze_message: __("Export bancaire en cours, cela peut prendre plusieurs minutes…"),
        });
        const m = r.message || {};
        if (m.erreur) {
          frappe.msgprint({
            title: __("Export indisponible"),
            message: __(
              "Le service bancaire n'a pas répondu : {0}<br><br>Le registre existant reste exploitable.",
              [frappe.utils.escape_html(m.erreur)]
            ),
            indicator: "orange",
          });
        } else {
          frappe.show_alert({
            message: __("{0} nouveaux mouvements, {1} déjà connus", [m.crees || 0, m.revus || 0]),
            indicator: "green",
          });
        }
        this.load();
      }
    );
  }

  _ignorer(cle) {
    const d = new frappe.ui.Dialog({
      title: __("Mettre ce mouvement à l'écart"),
      fields: [
        {
          fieldname: "motif",
          fieldtype: "Small Text",
          label: __("Motif"),
          reqd: 1,
          description: __(
            "Ce mouvement ne sera plus signalé comme restant à traiter. La reclassification ne repassera jamais dessus."
          ),
        },
      ],
      primary_action_label: __("Ignorer"),
      primary_action: async (v) => {
        d.hide();
        await frappe.call({
          method: this._m("ignorer"),
          args: { cles: JSON.stringify([cle]), motif: v.motif },
          freeze: true,
        });
        this.load();
      },
    });
    d.show();
  }

  async _reactiver(cle) {
    await frappe.call({
      method: this._m("reactiver"),
      args: { cles: JSON.stringify([cle]) },
      freeze: true,
    });
    this.load();
  }

  async _creer(cle) {
    try {
      const r = await frappe.call({
        method: this._m("creer_ecriture"),
        args: { cle },
        freeze: true,
        freeze_message: __("Création de l'écriture…"),
      });
      const m = r.message || {};
      frappe.show_alert({
        message: __("Écriture {0} créée en brouillon", [m.name]),
        indicator: "green",
      });
      if (m.name) frappe.set_route("Form", "Journal Entry", m.name);
    } catch (e) {
      // le message d'erreur serveur est déjà affiché par frappe.call
    }
  }

  async _groupe(groupe) {
    const r = await frappe.call({ method: this._m("get_groupe_frais"), args: { groupe } });
    const m = r.message || {};
    const esc = frappe.utils.escape_html;
    const lignes = (m.lignes || [])
      .map(
        (l) => `<tr>
          <td>${l.date ? frappe.datetime.str_to_user(l.date) : ""}</td>
          <td>${esc(l.operation || "")}</td>
          <td>${esc(l.reference || "")}</td>
          <td style="text-align:right">${format_number(l.montant || 0, null, 3)}</td>
        </tr>`
      )
      .join("");
    frappe.msgprint({
      title: __("Frais bancaires — {0}", [groupe]),
      message: `
        <p style="color:var(--text-muted)">Ces frais ne se comptabilisent jamais à l'unité :
        ils sont regroupés par jour.</p>
        <table class="table table-bordered" style="font-size:12px">
          <thead><tr><th>Date</th><th>Libellé</th><th>Référence</th><th style="text-align:right">Montant</th></tr></thead>
          <tbody>${lignes}</tbody>
          <tfoot><tr><th colspan="3">Total</th>
            <th style="text-align:right">${format_number(m.total || 0, null, 3)}</th></tr></tfoot>
        </table>`,
      wide: true,
    });
  }

  _excel() {
    const params = new URLSearchParams(this._filters());
    window.open(
      `/api/method/bank_retenue_sync.api.mouvements.download_excel?${params.toString()}`
    );
  }

  // ------------------------------------------------------------------ aperçu

  async _preview(doctype, name) {
    const fields = IB_PREVIEW_FIELDS[doctype];
    if (!fields) {
      frappe.set_route("Form", doctype, name);
      return;
    }
    const doc = await frappe.db.get_doc(doctype, name);
    const esc = frappe.utils.escape_html;
    const lignes = fields
      .map(([f, label, type]) => {
        let v = doc[f];
        if (v === undefined || v === null || v === "") return "";
        if (type === "date") v = frappe.datetime.str_to_user(v);
        else if (type === "currency") v = format_number(v, null, 3);
        else if (type === "docstatus")
          v = { 0: "Brouillon", 1: "Soumis", 2: "Annulé" }[v] || v;
        return `<tr><th style="width:38%">${esc(label)}</th><td>${esc(String(v))}</td></tr>`;
      })
      .join("");
    const d = new frappe.ui.Dialog({
      title: `${doctype} — ${name}`,
      primary_action_label: __("Ouvrir la fiche"),
      primary_action: () => {
        d.hide();
        frappe.set_route("Form", doctype, name);
      },
    });
    $(d.body).html(`<table class="table table-bordered" style="font-size:13px">${lignes}</table>`);
    d.show();
  }
}
