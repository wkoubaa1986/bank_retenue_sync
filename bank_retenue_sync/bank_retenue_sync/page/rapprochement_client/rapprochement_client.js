// Rapprochement client — espace Banque.
//
// Trois totaux qui devraient se répondre : commandes TTC, BL validés, règlements reçus.
// L'écran ne sert qu'à voir où ils divergent ; il n'écrit aucune pièce comptable. La seule
// décision qu'on y prend est d'ACCEPTER l'écart d'un client, avec son motif.
frappe.pages["rapprochement-client"].on_page_load = function (wrapper) {
  frappe.ui.make_app_page({
    parent: wrapper,
    title: __("Rapprochement client"),
    single_column: true,
  });
  new RapprochementClient(wrapper);
};

class RapprochementClient {
  constructor(wrapper) {
    this.$root = $(wrapper).find(".layout-main-section");
    this.$root.append(frappe.render_template("rapprochement_client", {}));
    this._lier();
    this._boot();
  }

  _lier() {
    const relire = frappe.utils.debounce(() => this._charger(), 250);
    this.$root.on("change", "[data-f]", () => this._charger());
    // La recherche se relit à la frappe, mais amortie : 3 360 clients agrégés à chaque lettre
    // rendrait le champ inutilisable.
    this.$root.on("input", '[data-f="recherche"]', relire);
    this.$root.on("click", '[data-action="actualiser"]', () => this._charger());
    this.$root.on("click", '[data-action="excel"]', () => this._exporter());
    this.$root.on("click", "[data-ventiler]", (e) => {
      const nom = $(e.currentTarget).attr("data-ventiler");
      const $d = this.$root.find(`[data-ventil-de="${CSS.escape(nom)}"]`);
      const ouvert = !$d.prop("hidden");
      $d.prop("hidden", ouvert);
      $(e.currentTarget).text(ouvert ? "Types" : "Replier");
    });
    this.$root.on("click", "[data-livrer]", (e) => {
      const nom = $(e.currentTarget).attr("data-livrer");
      const $d = this.$root.find(`[data-livr-de="${CSS.escape(nom)}"]`);
      const ouvert = !$d.prop("hidden");
      $d.prop("hidden", ouvert);
      $(e.currentTarget).text(ouvert ? "BL" : "Replier");
    });
    this.$root.on("click", "[data-detail]", (e) =>
      this._detail($(e.currentTarget).attr("data-detail")));
    this.$root.on("click", "[data-ignorer]", (e) =>
      this._ignorer($(e.currentTarget).attr("data-ignorer"),
                    $(e.currentTarget).attr("data-nom")));
    this.$root.on("click", "[data-reactiver]", (e) =>
      this._reactiver($(e.currentTarget).attr("data-reactiver")));
    this.$root.on("click", "[data-client]", (e) => {
      e.preventDefault();
      frappe.set_route("Form", "Customer", $(e.currentTarget).attr("data-client"));
    });
  }

  async _boot() {
    let f;
    try {
      f = (await frappe.call({ method: "bank_retenue_sync.api.rapprochement_client.get_filtres" }))
        .message;
    } catch (e) {
      return this.$root.find('[data-role="contenu"]').html(this._erreur(e));
    }
    this.seuils = f.tolerances || { montant: 1, bl: 1 };
    this.$root.find('[data-f="groupe"]').html(
      `<option value="">Tous</option>` +
      (f.groupes || []).map((g) => `<option value="${this._esc(g)}">${this._esc(g)}</option>`).join("")
    );
    this._charger();
  }

  _args() {
    return {
      recherche: this.$root.find('[data-f="recherche"]').val() || "",
      groupe: this.$root.find('[data-f="groupe"]').val() || "",
      type_client: this.$root.find('[data-f="type"]').val() || "",
      tri: this.$root.find('[data-f="tri"]').val() || "delta_paiement",
      seulement_ecarts: this.$root.find('[data-f="ecarts"]').is(":checked") ? 1 : 0,
      masquer_ignores: this.$root.find('[data-f="ignores"]').is(":checked") ? 1 : 0,
    };
  }

  async _charger() {
    const $c = this.$root.find('[data-role="contenu"]');
    $c.html('<div class="rc-chargement">Chargement…</div>');
    try {
      const r = await frappe.call({
        method: "bank_retenue_sync.api.rapprochement_client.get_data",
        args: this._args(),
      });
      this.data = r.message || {};
      $c.html(this._rendre(this.data));
    } catch (e) {
      $c.html(this._erreur(e));
    }
  }

  _rendre(d) {
    if (!d.lignes || !d.lignes.length) {
      return `<div class="rc-vide">Aucun client ne correspond — ou aucun ecart a signaler.</div>`;
    }
    const t = d.totaux;
    const kpis = `<div class="rc-kpis">
      ${this._kpi("Clients affiches", d.nb, `${d.en_ecart} en ecart`)}
      ${this._kpi("Commandes TTC", this._m(t.commandes), "validees")}
      ${this._kpi("Bons de livraison", this._m(t.bl), this._sousBl(d, t),
                  !!(d.livraisons && d.livraisons.brouillons))}
      ${this._kpi("Regle par les clients", this._m(t.regle),
                  `dont journal ${this._m(t.journal)}`)}
      ${this._kpi("Reprise d-historique", this._m(t.reprise),
                  "soldes d-avant la migration, hors comparaison")}
      ${this._kpi("Ecart de reglement", this._m(t.delta_paiement),
                  "regle moins reprise moins commandes",
                  Math.abs(t.delta_paiement) > this.seuils.montant)}
      ${this._kpi("Avances non affectees", this._m(t.avance_non_affectee),
                  "argent recu qui ne pointe sur rien",
                  Math.abs(t.avance_non_affectee) > this.seuils.montant)}
    </div>`;

    const lignes = d.lignes.map((l) => this._ligne(l, d)).join("");
    const tronque = d.tronque
      ? `<div class="rc-tronque">${d.nb} clients correspondent ; les ${d.lignes.length}
         premiers sont affiches. Affine la recherche ou le groupe pour voir les autres.</div>`
      : "";

    return kpis + `<div class="rc-scroll"><table class="rc-tbl">
      <thead><tr>
        <th>Client</th><th class="num">Commandes</th><th class="num">BL valides</th>
        <th class="num">Reglements</th><th class="num">Journal</th><th class="num">Regle</th>
        <th class="num">Ecart reglement</th><th class="num">Ecart livraison</th>
        <th>Avances</th><th></th>
      </tr></thead><tbody>${lignes}</tbody></table></div>` + tronque +
      `<p class="rc-note">Seuils appliques : reglement ${this._m(d.tolerances.montant)},
       livraison ${this._m(d.tolerances.bl)} — en dessous, un delta n-est pas signale (par
       defaut le timbre fiscal). Ils se reglent dans
       <a href="/app/bank-retenue-sync-settings">Reglages</a>, section « Rapprochement client ».
       « Regle » additionne les encaissements et le net des ecritures de journal du client
       (avoirs, regularisations, pertes).</p>`;
  }

  _sousBl(d, t) {
    const sans = d.sans_bl ? `${d.sans_bl} client(s) sans aucun BL` : "tous ont au moins un BL";
    const b = (d.livraisons || {}).brouillons;
    const r = (d.livraisons || {}).retours;
    return [b ? `${this._m(b.total)} en brouillon (${b.nb})` : `${this._m(t.delta_bl)} vs commandes`,
            r ? `${this._m(r.total)} de retours` : sans]
      .filter(Boolean).join(" · ");
  }

  _kpi(libelle, valeur, sous, alerte) {
    return `<div class="rc-kpi"><div class="lbl">${this._esc(libelle)}</div>
      <div class="val${alerte ? " rc-rouge" : ""}">${valeur}</div>
      <div class="sub">${this._esc(sous || "")}</div></div>`;
  }

  _ligne(l, d) {
    const dp = l.ecart_paiement ? "rc-rouge" : "rc-vert";
    const db = l.ecart_bl ? "rc-rouge" : "rc-vert";
    // ⚠️ « Aucun BL » n-est PAS un ecart en soi : une commande de service (entretien, main
    // d-oeuvre) n-en produit pas. On le signale sans le peindre en rouge.
    const badgeBl = l.nb_commandes && !l.a_des_bl
      ? `<span class="rc-badge rc-jaune">aucun BL</span>` : "";
    // Un client dont la situation a BOUGÉ depuis son exclusion revient dans la liste, et on
    // dit ce qui a changé — sinon la première réaction serait de le ré-ignorer sans regarder.
    const bouge = l.situation_changee && l.depuis_decision;
    const badgeIgn = l.ignore
      ? `<span class="rc-badge rc-gris" title="${this._esc(l.motif)}">ecart accepte</span>`
      : bouge
        ? `<span class="rc-badge rc-alerte" title="${this._esc(l.motif)}">revenu : reglement
           ${this._m(l.depuis_decision.delta_paiement)}, livraison
           ${this._m(l.depuis_decision.delta_bl)} depuis l-exclusion</span>` : "";
    const avances = [
      l.avance_non_affectee ? `<div class="rc-rouge">${this._m(l.avance_non_affectee)} non affectee</div>` : "",
      l.avance_sur_commande ? `<div class="rc-meta">${this._m(l.avance_sur_commande)} sur commande</div>` : "",
    ].join("") || `<span class="rc-meta">—</span>`;

    const actions = [
      l.ventilation && l.ventilation.length
        ? `<button class="rc-act" data-ventiler="${this._esc(l.client)}">Types</button>` : "",
      Object.keys(l.livraisons || {}).length
        ? `<button class="rc-act" data-livrer="${this._esc(l.client)}">BL</button>` : "",
      `<button class="rc-act" data-detail="${this._esc(l.client)}">Detail</button>`,
      d.peut_decider
        ? (l.ignore
            ? `<button class="rc-act" data-reactiver="${this._esc(l.client)}">Surveiller</button>`
            : `<button class="rc-act" data-ignorer="${this._esc(l.client)}"
                 data-nom="${this._esc(l.nom)}">Ignorer</button>`)
        : "",
    ].join(" ");

    return `<tr>
      <td><a href="#" class="rc-nom" data-client="${this._esc(l.client)}">${this._esc(l.nom)}</a>
        ${badgeBl} ${badgeIgn}
        <div class="rc-meta">${this._esc(l.telephone || "sans telephone")} ·
          ${this._esc(l.groupe || "sans groupe")} ·
          ${l.type === "Company" ? "Societe" : "Particulier"}</div></td>
      <td class="num">${this._m(l.commandes)}<div class="rc-meta">${l.nb_commandes}</div></td>
      <td class="num">${this._m(l.bl)}<div class="rc-meta">${l.nb_bl}</div></td>
      <td class="num">${this._m(l.paiements)}<div class="rc-meta">${l.nb_paiements}</div></td>
      <td class="num">${l.journal ? this._m(l.journal) : "—"}
        <div class="rc-meta">${l.nb_journal || ""}</div></td>
      <td class="num">${this._m(l.regle)}</td>
      <td class="num ${dp}">${this._m(l.delta_paiement)}</td>
      <td class="num ${db}">${this._m(l.delta_bl)}</td>
      <td>${avances}</td>
      <td><div class="rc-actions">${actions}</div></td>
    </tr>
    ${this._ventilation(l)}
    ${this._livraisons(l)}`;
  }

  /** Le detail des LIVRAISONS : ce qui est parti, ce qui est revenu, ce qui n-est pas valide.
   *
   * La colonne « BL valides » ne dit pas tout. Un retour est un BL valide de montant NEGATIF —
   * il vient en deduction et explique un ecart en faveur du client. Un BL resté en BROUILLON
   * (45 dans la base, 25 705 DT) est de la marchandise sortie que rien ne constate : c-est la
   * premiere cause d-un « livre moins que commande ». Et un BL annule explique une commande qui
   * parait non honoree.
   */
  _livraisons(l) {
    const etats = l.livraisons || {};
    if (!Object.keys(etats).length) return "";
    const libelles = {
      livres: ["Bons de livraison valides", "rc-ok", "livre"],
      retours: ["Retours de marchandise", "rc-jaune", "en deduction"],
      brouillons: ["Bons NON valides (brouillon)", "rc-alerte", "ne compte pas"],
      annules: ["Bons annules", "rc-gris", "ne compte pas"],
    };
    const net = (etats.livres ? etats.livres.total : 0) + (etats.retours ? etats.retours.total : 0);
    const ecart = net - (l.commandes || 0);
    const lignes = ["livres", "retours", "brouillons", "annules"].filter((k) => etats[k])
      .map((k) => {
        const [lib, cls, note] = libelles[k];
        const e = etats[k];
        return `<div class="rc-vent">
          <span class="rc-pastille ${cls}">${this._esc(note)}</span>
          <span>${this._esc(lib)}</span>
          <span class="rc-meta"></span>
          <span class="num">${this._m(e.total)}</span>
          <span class="rc-meta num">${e.nb} bon(s)</span>
        </div>`;
      }).join("");
    const compte = Math.abs(ecart) > (this.seuils ? this.seuils.bl : 1);
    return `<tr class="rc-detail" data-livr-de="${this._esc(l.client)}" hidden>
      <td colspan="10">
        <div class="rc-vents">
          <div class="rc-vent-tete">Livraisons —
            net livre <b>${this._m(net)}</b> contre <b>${this._m(l.commandes)}</b> de commandes
            · <b class="${compte ? "rc-rouge" : "rc-vert"}">ecart ${this._m(ecart)}</b>
            ${etats.brouillons
              ? ` · <b class="rc-rouge">${this._m(etats.brouillons.total)} en brouillon,
                  non comptes</b>` : ""}</div>
          ${lignes}
        </div></td></tr>`;
  }

  /** Le detail des reglements PAR TYPE, deplie sous la ligne du client.
   *
   * ⚠️ « Regle » ne veut pas dire « encaisse ». Une piece de mode « Dette non payee » solde une
   * commande sans qu-un dinar ait bouge — 71 706 DT sur l-ensemble de la base — et un cheque en
   * portefeuille n-est pas encore de l-argent. C-est le GROUPE du compte de destination qui
   * tranche, jamais son `account_type` : dans ce plan comptable, Dettes, Cheques et Perte sont
   * tous types « Bank ».
   */
  _ventilation(l) {
    const cats = l.ventilation || [];
    if (!cats.length) return "";
    const lignes = cats.map((c) => `
      <div class="rc-vent">
        <span class="rc-pastille ${c.encaisse ? "rc-ok" : "rc-jaune"}">${
          c.encaisse ? "regle" : "en attente"}</span>
        <span>${this._esc(c.libelle)}</span>
        <span class="rc-meta">${this._esc(c.compte || "")}</span>
        <span class="num">${this._m(c.total)}</span>
        <span class="rc-meta num">${c.nb} piece(s)</span>
      </div>`).join("");
    return `<tr class="rc-detail" data-ventil-de="${this._esc(l.client)}" hidden>
      <td colspan="10">
        <div class="rc-vents">
          <div class="rc-vent-tete">Reglements par type —
            total <b>${this._m(l.total_ventile)}</b>
            ${Math.abs((l.total_ventile || 0) - (l.regle || 0)) > 0.005
              ? ` <span class="rc-rouge">(la colonne Regle annonce ${this._m(l.regle)})</span>`
              : ` = colonne Regle`}
            · <b class="rc-vert">${this._m(l.encaisse_reel)} qui soldent</b>
            ${l.non_encaisse
              ? ` · <b class="rc-jaune-txt">${this._m(l.non_encaisse)} en attente</b>` : ""}
            ${l.reprise
              ? ` · <b>${this._m(l.reprise)} de reprise</b>, hors comparaison` : ""}</div>
          ${lignes}
        </div></td></tr>`;
  }

  async _detail(client) {
    let d;
    try {
      d = (await frappe.call({
        method: "bank_retenue_sync.api.rapprochement_client.detail",
        args: { client }, freeze: true,
      })).message;
    } catch (e) {
      return frappe.msgprint(this._erreur(e));
    }
    const dlg = new frappe.ui.Dialog({
      title: __("Pieces de {0}", [client]), size: "extra-large",
      fields: [{ fieldname: "vue", fieldtype: "HTML" }],
    });
    const bloc = (titre, lignes, colonnes) => `
      <h6 style="margin:14px 0 6px">${titre} (${lignes.length})</h6>
      ${lignes.length
        ? `<div class="rc-scroll"><table class="rc-tbl"><thead><tr>${
            colonnes.map((c) => `<th class="${c[2] || ""}">${c[0]}</th>`).join("")
          }</tr></thead><tbody>${lignes.map((r) => `<tr>${
            colonnes.map((c) => `<td class="${c[2] || ""}">${c[1](r)}</td>`).join("")
          }</tr>`).join("")}</tbody></table></div>`
        : `<div class="rc-meta">Aucune piece.</div>`}`;
    const lien = (dt, n) =>
      `<a href="/app/${frappe.router.slug(dt)}/${encodeURIComponent(n)}" target="_blank">${this._esc(n)}</a>`;
    dlg.fields_dict.vue.$wrapper.html(
      bloc("Commandes", d.commandes, [
        ["Commande", (r) => lien("Sales Order", r.name)],
        ["Date", (r) => frappe.datetime.str_to_user(r.transaction_date)],
        ["Total TTC", (r) => this._m(r.grand_total), "num"],
        ["Statut", (r) => this._esc(r.status || "")],
        ["Livraison", (r) => this._esc(r.delivery_status || "")]]) +
      bloc("Bons de livraison", d.bl, [
        ["Bon", (r) => lien("Delivery Note", r.name)],
        ["Date", (r) => frappe.datetime.str_to_user(r.posting_date)],
        ["Total", (r) => this._m(r.grand_total), "num"],
        ["Statut", (r) => this._esc(r.status || "")]]) +
      this._blocPaiements(d.paiements, lien) +
      bloc("Ecritures de journal", d.journal, [
        ["Ecriture", (r) => lien("Journal Entry", r.name)],
        ["Date", (r) => frappe.datetime.str_to_user(r.posting_date)],
        ["Debit", (r) => (r.debit ? this._m(r.debit) : "—"), "num"],
        ["Credit", (r) => (r.credit ? this._m(r.credit) : "—"), "num"],
        ["Objet", (r) => this._esc((r.user_remark || "").slice(0, 90))]])
    );
    // Le dépliage vit dans le dialogue, pas dans la page : ces lignes n-existent que là.
    dlg.$wrapper.on("click", "[data-plier]", (e) => {
      const nom = $(e.currentTarget).attr("data-plier");
      const $d = dlg.$wrapper.find(`[data-detail-de="${CSS.escape(nom)}"]`);
      const ouvert = !$d.prop("hidden");
      $d.prop("hidden", ouvert);
      $(e.currentTarget).html(ouvert ? "&#9656;" : "&#9662;");
    });
    dlg.show();
  }

  /** Les règlements, chacun dépliable sur ce qu-il solde.
   *
   * ⚠️ UN RÈGLEMENT EST SOUVENT GROUPÉ : le client paie 3 960 DT et la pièce couvre quatre
   * commandes. La ligne seule ne dit alors rien d-utile — on lit un montant sans savoir ce
   * qu-il éteint, et l-écart du client devient impossible à expliquer.
   */
  _blocPaiements(paiements, lien) {
    if (!paiements || !paiements.length) {
      return `<h6 style="margin:14px 0 6px">Reglements (0)</h6>
              <div class="rc-meta">Aucune piece.</div>`;
    }
    const groupes = paiements.filter((p) => p.groupe).length;
    const lignes = paiements.map((p) => {
      const chevron = p.nb_affectations
        ? `<span class="rc-chev" data-plier="${this._esc(p.name)}">&#9656;</span>` : "";
      const badge = p.groupe
        ? `<span class="rc-badge rc-gris">${p.nb_affectations} pieces</span>` : "";
      const orphelin = p.unallocated_amount
        ? `<span class="rc-rouge">${this._m(p.unallocated_amount)}</span>` : "—";
      const detail = (p.affectations || []).map((a) => `
        <div class="rc-aff">
          <span>${lien(a.doctype, a.nom)}</span>
          <span class="rc-meta">${a.doctype === "Sales Invoice" ? "facture" : "commande"}</span>
          <span class="num">${this._m(a.affecte)}</span>
          <span class="rc-meta">${a.total ? `sur ${this._m(a.total)}` : ""}</span>
        </div>`).join("");
      return `<tr class="rc-pay">
          <td>${chevron} ${lien("Payment Entry", p.name)} ${badge}</td>
          <td>${frappe.datetime.str_to_user(p.posting_date)}</td>
          <td class="num">${this._m(p.paid_amount)}</td>
          <td>${this._esc(p.mode_of_payment || "")}</td>
          <td>${this._esc(p.paid_to || "")}</td>
          <td class="num">${orphelin}</td>
        </tr>
        ${p.nb_affectations
          ? `<tr class="rc-detail" data-detail-de="${this._esc(p.name)}" hidden>
               <td colspan="6"><div class="rc-affs">${detail}</div></td></tr>`
          : ""}`;
    }).join("");
    const sous = groupes ? ` — dont ${groupes} groupe(s) sur plusieurs pieces` : "";
    return `<h6 style="margin:14px 0 6px">Reglements (${paiements.length})${sous}</h6>
      <div class="rc-scroll"><table class="rc-tbl">
        <thead><tr><th>Piece</th><th>Date</th><th class="num">Montant</th><th>Mode</th>
          <th>Compte</th><th class="num">Non affecte</th></tr></thead>
        <tbody>${lignes}</tbody></table></div>`;
  }

  _ignorer(client, nom) {
    // Le motif est OBLIGATOIRE : sans lui, l-exclusion devient un trou de memoire.
    const dlg = new frappe.ui.Dialog({
      title: __("Accepter l-ecart de {0}", [nom || client]),
      fields: [
        { fieldname: "motif", fieldtype: "Small Text", reqd: 1,
          label: __("Pourquoi cet ecart est-il normal ?"),
          description: __("Litige solde autrement, reprise d-historique, compte de regularisation… L-exclusion ne vaut que pour la situation actuelle : si les chiffres du client bougent, il ressortira de lui-meme.") },
      ],
      primary_action_label: __("Accepter cet ecart"),
      primary_action: async (v) => {
        try {
          await frappe.call({
            method: "bank_retenue_sync.api.rapprochement_client.ignorer",
            args: { client, motif: v.motif }, freeze: true,
          });
        } catch (e) {
          return frappe.msgprint(this._erreur(e));
        }
        dlg.hide();
        this._charger();
      },
    });
    dlg.show();
  }

  async _reactiver(client) {
    try {
      await frappe.call({
        method: "bank_retenue_sync.api.rapprochement_client.reactiver",
        args: { client }, freeze: true,
      });
    } catch (e) {
      return frappe.msgprint(this._erreur(e));
    }
    this._charger();
  }

  _exporter() {
    const l = (this.data && this.data.lignes) || [];
    if (!l.length) return frappe.msgprint(__("Rien a exporter."));
    const entetes = ["Client", "Nom", "Telephone", "Groupe", "Type", "Commandes", "Nb commandes",
                     "BL", "Nb BL", "Reglements", "Journal", "Regle", "Ecart reglement",
                     "Ecart livraison", "Avance non affectee", "Avance sur commande", "Ignore"];
    const csv = [entetes.join(";")].concat(l.map((r) => [
      r.client, r.nom, r.telephone, r.groupe, r.type, r.commandes, r.nb_commandes, r.bl, r.nb_bl,
      r.paiements, r.journal, r.regle, r.delta_paiement, r.delta_bl, r.avance_non_affectee,
      r.avance_sur_commande, r.ignore ? "oui" : "",
    ].map((v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`).join(";"))).join("\n");
    // Le point-virgule et le BOM : Excel en francais ne lit pas autrement.
    const url = URL.createObjectURL(new Blob(["﻿" + csv],
                                             { type: "text/csv;charset=utf-8;" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `rapprochement_client_${frappe.datetime.get_today()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  _m(v) {
    return format_currency(v || 0, frappe.defaults.get_global_default("currency") || "TND");
  }
  _esc(v) { return frappe.utils.escape_html(String(v == null ? "" : v)); }
  _erreur(e) {
    const m = (e && (e.message || (e._server_messages && JSON.parse(e._server_messages)[0]))) || e;
    return `<div class="rc-vide">${this._esc(typeof m === "string" ? m : JSON.stringify(m))}</div>`;
  }
}
