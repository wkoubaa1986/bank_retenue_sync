frappe.pages["facturation-mensuelle"].on_page_load = function (wrapper) {
  const page = frappe.ui.make_app_page({
    parent: wrapper,
    title: "Facturation mensuelle",
    single_column: true,
  });
  $(wrapper).find(".layout-main-section").html(
    frappe.render_template("facturation_mensuelle", {})
  );
  new FacturationMensuelle(wrapper, page);
};

// Rendu pur : tous les montants viennent de bank_retenue_sync.api.cloture.
// Le JS n'additionne rien — un total calculé ici et un total calculé au serveur finiraient
// par diverger, et c'est toujours celui affiché qu'on croit.

const FM_ONGLETS = {
  caisse: { methode: "get_caisse", rendu: "_rendre_caisse" },
  banque: { methode: "get_banque", rendu: "_rendre_banque" },
  factures: { methode: "get_factures", rendu: "_rendre_factures" },
  charges: { methode: "get_charges", rendu: "_rendre_charges" },
  // Le dossier ne charge rien par la voie commune : son etat se rafraichit tout seul tant
  // qu un job tourne, ce que `_suivre_dossier` gere deja.
  dossier: { methode: null, rendu: null },
};

const FM_STATUT_LABEL = {
  Identifie: "Identifié",
  Orphelin: "Orphelin",
  "A verifier": "À vérifier",
  Ignore: "Ignoré",
};

class FacturationMensuelle {
  constructor(wrapper, page) {
    this.$root = $(wrapper).find(".layout-main-section");
    this.page = page;
    this.mois = null;
    this.onglet = "caisse";
    this.cache = {};
    this.peut_generer = false;
    this._bind();
    this._boot();
  }

  // ---------------------------------------------------------------- amorçage

  async _boot() {
    let ctx;
    try {
      ctx = (await frappe.call({ method: "bank_retenue_sync.api.cloture.get_contexte" })).message;
    } catch (e) {
      this.$root.find('[data-panneau]').html(this._erreur(e));
      return;
    }
    this.mois = ctx.mois;
    this.peut_generer = !!ctx.peut_generer;
    this.mois_offerts = ctx.mois_offerts || [];
    const $sel = this.$root.find('[data-f="mois"]');
    $sel.html((ctx.mois_offerts || [])
      .map((m) => `<option value="${m.cle}">${this._esc(m.libelle)}</option>`).join(""));
    $sel.val(this.mois);
    this._periode(ctx);
    this._charger(this.onglet);
    if (this.onglet !== "dossier") this._amorcer_pastille();
  }

  /** La pastille de retards vit sur l'onglet Dossier : on la remplit sans forcer l'ouverture. */
  async _amorcer_pastille() {
    try {
      const d = (await frappe.call({
        method: "bank_retenue_sync.api.cloture.get_dossier_mensuel",
        args: { mois: this.mois },
      })).message || {};
      if (this.mois === d.mois) this._maj_pastille(d.nb_candidats);
    } catch (e) {
      // La pastille est un rappel, pas une donnée : son échec ne doit rien casser.
    }
  }

  _maj_pastille(n) {
    const $tab = this.$root.find('.fm-tab[data-onglet="dossier"]');
    $tab.find(".pastille").remove();
    if (n > 0) $tab.append(`<span class="pastille alerte">${n}</span>`);
  }

  _bind() {
    this.$root.on("change", '[data-f="mois"]', (e) => {
      this.mois = $(e.currentTarget).val();
      this.cache = {};
      // Le mois d’origine choisi pour le rattrapage vaut pour le mois porteur affiché : en
      // changer sans l’oublier proposerait un mois postérieur, que le serveur refuserait.
      this.mois_origine = null;
      this._arreter_suivi();
      this.$root.find("[data-panneau]").html('<div class="fm-chargement">Chargement…</div>');
      this._charger(this.onglet);
      // Sur l’onglet Dossier, `_charger` passe déjà par `_charger_mensuel`, qui met la pastille
      // à jour : relancer la détection ici la ferait tourner deux fois pour le même résultat.
      if (this.onglet !== "dossier") this._amorcer_pastille();
    });

    this.$root.on("click", ".fm-tab", (e) => {
      const nom = $(e.currentTarget).data("onglet");
      this.$root.find(".fm-tab").removeClass("actif");
      $(e.currentTarget).addClass("actif");
      this.$root.find(".fm-panneau").removeClass("actif");
      this.$root.find(`[data-panneau="${nom}"]`).addClass("actif");
      this.onglet = nom;
      this._charger(nom);
    });

    this.$root.on("click", "[data-doc]", (e) => {
      e.preventDefault();
      const $a = $(e.currentTarget);
      frappe.set_route("Form", $a.attr("data-doc"), $a.attr("data-nom"));
    });

    this.$root.on("click", '[data-action="generer"]', () => this._generer());
    this.$root.on("click", '[data-action="voir-piece"]', (e) =>
      this._voir_piece($(e.currentTarget).attr("data-url"),
                       $(e.currentTarget).attr("data-nom")));
    this.$root.on("click", '[data-action="controler"]', (e) => {
      const $b = $(e.currentTarget);
      this._controler($b.attr("data-dt"), $b.attr("data-dn"), $b.attr("data-url"), $b);
    });
    this.$root.on("click", '[data-action="controler-mois"]', () => this._controler_le_mois());

    // --- envoi au comptable & rattrapage des retards -----------------------------
    this.$root.on("click", '[data-action="marquer-envoye"]', () => this._marquer_envoye());
    this.$root.on("click", '[data-action="annuler-envoi"]', () => this._annuler_envoi());
    this.$root.on("change", 'input[data-action="rattacher"]', (e) => this._rattacher(e));
    this.$root.on("click", '[data-action="verifier-non-envoyees"]',
      () => this._verifier_non_envoyees());

    // --- rattrapage manuel, depuis l'onglet Charges ------------------------------
    this.$root.on("change", '[data-f="mois-origine"]', (e) => {
      this.mois_origine = $(e.currentTarget).val() || null;
      this._charger_anterieures();
    });
    this.$root.on("change", 'input[data-action="rattacher-manuel"]',
      (e) => this._rattacher_manuel(e));
  }

  /** Ouvre le justificatif sans quitter la page — c'est le geste le plus fréquent. */
  _voir_piece(url, nom) {
    if (!url) return;
    const image = /\.(png|jpe?g|gif|webp|bmp)$/i.test(url);
    const d = new frappe.ui.Dialog({ title: nom || "Justificatif", size: "extra-large" });
    d.$body.html(image
      ? `<img src="${frappe.utils.escape_html(url)}" style="max-width:100%;display:block;margin:auto;">`
      : `<iframe src="${frappe.utils.escape_html(url)}"
           style="width:100%;height:78vh;border:0;border-radius:6px;"></iframe>`);
    d.set_primary_action(__("Ouvrir dans un onglet"), () => window.open(url, "_blank"));
    d.show();
    d.$wrapper.find(".modal-dialog").css("max-width", "92vw");
  }

  async _controler(dt, dn, url, $bouton) {
    $bouton.prop("disabled", true).text("…");
    try {
      const r = await frappe.call({
        method: "bank_retenue_sync.api.cloture.controler_justificatif",
        args: { mois: this.mois, document_type: dt, document_name: dn, file_url: url },
        freeze: true,
        freeze_message: __("Lecture du justificatif…"),
      });
      this._montrer_controle(r.message || {});
      // Le résultat est en cache côté serveur : on recharge l'onglet pour que les colonnes
      // « lu » se remplissent partout, plutôt que de rafistoler une seule ligne.
      delete this.cache[`${this.mois}|charges`];
      this._charger("charges");
    } catch (e) {
      frappe.msgprint({ title: __("Contrôle impossible"), message: String(e), indicator: "red" });
      $bouton.prop("disabled", false).text("🔍");
    }
  }

  _montrer_controle(c) {
    if (!c || !c.extrait) return;
    const ligne = (lbl, erp, pdf, ecart) => `<tr><td>${lbl}</td>
      <td class="num">${this._m(erp)}</td><td class="num">${this._m(pdf)}</td>
      <td class="num">${ecart == null ? "" : this._m(ecart)}</td></tr>`;
    const d = new frappe.ui.Dialog({
      title: c.concordant ? __("Justificatif concordant") : __("Écart relevé"),
      size: "large",
    });
    d.$body.html(`
      <div class="fm-scroll"><table class="fm-tbl">
        <thead><tr><th></th><th class="num">Écriture</th><th class="num">PDF</th>
          <th class="num">Écart</th></tr></thead>
        <tbody>
          ${ligne("HT", c.attendu.ht, c.extrait.ht, (c.ecarts || {}).ht)}
          ${ligne("TVA", c.attendu.tva, c.extrait.tva, (c.ecarts || {}).tva)}
          ${ligne("TTC", c.attendu.ttc, c.extrait.ttc, (c.ecarts || {}).ttc)}
        </tbody></table></div>
      <div style="margin-top:10px;font-size:12.5px;">
        <div>Référence — écriture : <b>${this._esc(c.attendu.reference)}</b> ·
          PDF : <b>${this._esc(c.extrait.reference || "—")}</b>
          ${c.reference_ok ? '<span class="fm-badge ok">concorde</span>' : ""}</div>
        <div>Date — écriture : <b>${this._esc(c.attendu.date)}</b> ·
          PDF : <b>${this._esc(c.extrait.date || "—")}</b>
          ${c.date_ok === true ? '<span class="fm-badge ok">concorde</span>'
            : c.date_ok === false ? '<span class="fm-badge warn">diffère</span>' : ""}</div>
        <div>Tiers lu : <b>${this._esc(c.extrait.tiers || "—")}</b>
          ${c.extrait.timbre ? ` · timbre ${this._m(c.extrait.timbre)}` : ""}</div>
        <div class="muted" style="margin-top:6px;">Pièce ${this._esc(c.fichier)} ·
          modèle ${this._esc(c.extrait.modele)}${c.cache ? " · déjà lue" : ""}.
          Aucune écriture n'a été modifiée.</div>
      </div>`);
    d.show();
  }

  async _controler_le_mois() {
    const ok = await new Promise((r) => frappe.confirm(
      `Lire toutes les pièces non encore lues de ${this._esc(this.mois)} ?
       Chaque lecture est un appel payant au modèle.`, () => r(true), () => r(false)));
    if (!ok) return;
    const res = await frappe.call({
      method: "bank_retenue_sync.api.cloture.controler_le_mois",
      args: { mois: this.mois },
    });
    frappe.show_alert({ message: (res.message || {}).message || "Lancé.", indicator: "blue" });
  }

  _periode(d) {
    const p = d && d.periode;
    this.$root.find('[data-role="periode"]').text(
      p ? `Période : ${p.debut} → ${p.fin}` : (d && d.libelle) || ""
    );
  }

  // ---------------------------------------------------------------- chargement

  async _charger(nom) {
    const conf = FM_ONGLETS[nom];
    const $p = this.$root.find(`[data-panneau="${nom}"]`);
    if (!conf.methode) {
      $p.html('<div class="fm-dossier" data-role="dossier">'
        + '<div class="fm-chargement" style="padding:8px;">Lecture de l\u2019état…</div></div>'
        + '<div data-role="mensuel">'
        + '<div class="fm-chargement" style="padding:8px;">Lecture des retards…</div></div>');
      this._suivre_dossier();
      this._charger_mensuel();
      return;
    }
    const cle = `${this.mois}|${nom}`;
    if (this.cache[cle]) {
      $p.html(this[conf.rendu](this.cache[cle]));
      this._apres_rendu(nom);
      return;
    }
    $p.html('<div class="fm-chargement">Chargement…</div>');
    try {
      const r = await frappe.call({
        method: "bank_retenue_sync.api.cloture." + conf.methode,
        args: { mois: this.mois },
      });
      const d = r.message || {};
      this.cache[cle] = d;
      this._periode(d);
      $p.html(this[conf.rendu](d));
      this._apres_rendu(nom);
    } catch (e) {
      $p.html(this._erreur(e));
    }
  }

  /** Le panneau vient d’être réécrit : ce qui n’est pas dans sa donnée doit être remis. */
  _apres_rendu(nom) {
    if (nom !== "charges" || !this.mois_origine) return;
    this.$root.find('[data-f="mois-origine"]').val(this.mois_origine);
    this._charger_anterieures();
  }

  // ---------------------------------------------------------------- caisse

  _rendre_caisse(d) {
    if (!d.disponible) return this._indispo(d.message);
    const t = d.totaux;
    const o = d.origine || {};
    // Le code couleur est celui de la page « Caisse Espèces » : vert ce qui entre, rouge ce qui
    // sort, violet ce qui part en banque, bleu les soldes. Deux écrans sur la même donnée
    // doivent se lire de la même façon, sinon on les compare à la main à chaque fois.
    const kpis = this._kpis([
      ["Solde d'ouverture", this._m(t.ouverture),
        o.veille ? `au ${o.veille}` : "solde d'origine du réglage", "bleu"],
      ["Entrées ventes espèces", this._m(t.entrees), `${d.entrees.length} encaissement(s)`, "vert"],
      ["Sorties achats", this._m(t.achats), `${d.achats.length} ligne(s)`, "rouge"],
      ["Sorties dépenses", this._m(t.depenses), `${d.depenses.length} ligne(s)`, "rouge"],
      ["Versements banque", this._m(t.versements), `${d.versements.length} versement(s)`,
        "violet"],
      ["Solde de clôture", this._m(t.cloture), `mouvement du mois ${this._m(t.mouvement)}`,
        "bleu"],
    ]);

    const filiation = o.date
      ? `<div class="fm-note">Ouverture reconstituée depuis l'origine de la caisse :
         solde du ${o.date} (${this._m(o.solde)}) ${o.cumul_anterieur >= 0 ? "+" : "−"}
         ${this._m(Math.abs(o.cumul_anterieur))} de mouvements jusqu'au ${o.veille || o.date}.
         C'est la même chaîne que la page <a href="/app/caisse-especes">Caisse Espèces</a> :
         à son dernier mois, la clôture ci-dessus retombe sur son solde.</div>`
      : `<div class="fm-note alerte">Aucune date d'origine dans le réglage de la caisse :
         l'ouverture affichée est le solde initial brut, elle ne tient pas compte des mois
         antérieurs.</div>`;

    const exclus = (d.versements_ecartes || []).length
      ? `<div class="fm-note">${d.versements_ecartes.length} versement(s) de ce mois écarté(s)
         par le réglage de la caisse : ${this._esc(d.versements_ecartes
           .map((v) => v.journal_entry_number).join(", "))}.</div>` : "";

    // Trois colonnes côte à côte, comme la page Caisse Espèces : entrées, sorties, versements.
    // Empilées, on ne voyait plus le mouvement d'ensemble — or c'est tout l'intérêt de l'image.
    const colonnes = `<div class="fm-colonnes">
      ${this._bloc_caisse("vert", "▲ Entrées — ventes payées en espèces", t.entrees, [
        ["Date", "date"], ["N° facture", "invoice_number"], ["Client", "client"],
      ], d.entrees, "Sales Invoice", "erp_name", "Aucune entrée espèces sur le mois.")}

      ${this._bloc_caisse("rouge", "▼ Sorties — achats & dépenses en espèces",
        this._m2(t.achats + t.depenses),
        [["Date", "date"], ["Réf / Écriture", "_ref"], ["Fournisseur / Libellé", "_libelle"]],
        [...d.achats.map((r) => ({ ...r, _groupe: "Achats", _ref: r.invoice_number,
                                   _libelle: r.supplier })),
         ...d.depenses.map((r) => ({ ...r, _groupe: "Dépenses", _ref: r.journal_entry_number,
                                     _libelle: r.description }))],
        null, null, "Aucune sortie en espèces sur le mois.")}

      ${this._bloc_caisse("violet", "↓ Versements caisse → banque", t.versements, [
        ["Date", "date"], ["Réf écriture", "journal_entry_number"], ["Libellé", "description"],
      ], d.versements.concat((d.versements_ecartes || []).map((v) => ({ ...v, _ecarte: 1 }))),
        "Journal Entry", "journal_entry_number", "Aucun versement sur le mois.")}
    </div>`;

    return kpis + filiation + exclus + colonnes;
  }

  /** Un bloc de la caisse : en-tête coloré, total à droite, tableau à colonnes nommées. */
  _bloc_caisse(couleur, titre, total, colonnes, rows, doctype, champ_lien, vide) {
    const entete = colonnes.map(([lbl]) => `<th>${this._esc(lbl)}</th>`).join("")
      + '<th class="num">Montant</th>';

    let corps = "";
    let groupe = null;
    for (const r of rows || []) {
      if (r._groupe && r._groupe !== groupe) {
        groupe = r._groupe;
        corps += `<tr class="fm-sep"><td colspan="${colonnes.length + 1}">${
          this._esc(groupe)}</td></tr>`;
      }
      const cellules = colonnes.map(([, champ]) => {
        const v = r[champ];
        if (champ === champ_lien && doctype && v) return `<td>${this._lien(doctype, v)}</td>`;
        return `<td>${this._esc(v == null ? "" : String(v))}</td>`;
      }).join("");
      corps += `<tr class="${r._ecarte ? "ecarte" : ""}">${cellules}
        <td class="num">${this._m(r.montant)}</td></tr>`;
    }

    const table = corps
      ? `<div class="fm-scroll"><table class="fm-tbl">
           <thead><tr>${entete}</tr></thead><tbody>${corps}</tbody></table></div>`
      : `<div class="fm-vide">${this._esc(vide)}</div>`;

    const somme = typeof total === "number" ? this._m(total) : total;
    return `<div class="fm-bloc ${couleur}">
      <h4><span>${this._esc(titre)}</span><span>${somme}</span></h4>${table}</div>`;
  }

  _m2(v) { return this._m(Math.round((parseFloat(v) || 0) * 1000) / 1000); }

  // ---------------------------------------------------------------- banque

  _rendre_banque(d) {
    const kpi = d.kpi || {};
    const compte = (sens, statut) => ((kpi[sens] || {})[statut] || {}).nb || 0;
    const montant = (sens, statut) => ((kpi[sens] || {})[statut] || {}).montant || 0;
    const orph = compte("Debit", "Orphelin") + compte("Credit", "Orphelin");
    const ident = compte("Debit", "Identifie") + compte("Credit", "Identifie");
    const ecarts = d.ecarts || {};

    const kpis = this._kpis([
      ["Mouvements du mois", d.total, `registre au ${d.asof || "—"}`, ""],
      ["Identifiés", ident, this._m(montant("Debit", "Identifie") + montant("Credit", "Identifie")),
        "ok"],
      ["Orphelins", orph, this._m(montant("Debit", "Orphelin") + montant("Credit", "Orphelin")),
        orph ? "bad" : "ok"],
      ["Écarts de paiement", ecarts.nb || 0,
        `au-delà de ${this._m(d.seuil_ecart)} — ${this._m(ecarts.montant)}`,
        (ecarts.nb || 0) ? "warn" : "ok"],
    ]);

    const lien = `<div class="fm-note">Ce volet est une synthèse du tableau de bord
      <a href="/app/identification-bancaire">Identification bancaire</a>, borné au mois.
      Le rapprochement lui-même s'y fait — ici on ne fait que constater ce qu'il reste.</div>`;

    const orphelins = d.orphelins_total
      ? this._sous(`Orphelins du mois (${d.orphelins_total})`)
        + this._mouvements(d.orphelins)
      : "";

    return kpis + lien + orphelins
      + this._sous(`Relevé du mois — ${d.total} mouvement(s)`)
      + this._mouvements(d.mouvements, "Aucun mouvement bancaire sur ce mois.");
  }

  _mouvements(rows, vide) {
    if (!rows || !rows.length) {
      return `<div class="fm-vide">${this._esc(vide || "Aucune ligne.")}</div>`;
    }
    const seuil = parseFloat(this.cache[`${this.mois}|banque`]?.seuil_ecart) || 0;
    const corps = rows.map((r) => {
      const ecart = parseFloat(r.ecart) || 0;
      return `<tr>
      <td>${r.date || ""}</td>
      <td>${this._esc(r.operation || "")}</td>
      <td class="muted">${this._esc(r.reference || "")}</td>
      <td class="num">${r.debit ? this._m(r.debit) : ""}</td>
      <td class="num">${r.credit ? this._m(r.credit) : ""}</td>
      <td>${this._esc(r.categorie || "")}</td>
      <td><span class="fm-badge ${r.statut === "Identifie" ? "ok"
        : r.statut === "Orphelin" ? "bad" : "neutre"}">${
        FM_STATUT_LABEL[r.statut] || r.statut || ""}</span></td>
      <td>${r.document_name
        ? this._lien(r.document_type, r.document_name) : '<span class="muted">—</span>'}</td>
      <td>${this._reglement(r.reglement, seuil)}</td>
      <td class="num">${r.montant_document ? this._m(r.montant_document) : ""}</td>
      <td class="num">${ecart
        ? (Math.abs(ecart) > seuil
          ? `<span class="fm-badge bad">${this._m(ecart)}</span>`
          : `<span class="muted">${this._m(ecart)}</span>`)
        : ""}</td>
      <td class="muted">${this._esc(r.raison || "")}</td>
    </tr>`;
    }).join("");
    return `<div class="fm-scroll"><table class="fm-tbl"><thead><tr>
      <th>Date</th><th>Libellé</th><th>Référence</th><th class="num">Débit</th>
      <th class="num">Crédit</th><th>Catégorie</th><th>Statut</th><th>Pièce</th>
      <th>Règlement</th><th class="num">Montant ERP</th><th class="num">Écart</th><th>Raison</th>
      </tr></thead><tbody>${corps}</tbody></table></div>`;
  }

  /** Ce qu'un mouvement a payé : sans ça, « ACC-PAY-… » ou « ACC-JV-… » ne dit rien. */
  _reglement(d, seuil) {
    if (!d) return '<span class="muted">—</span>';

    // Échéance de prêt ou de leasing : ce qu'on veut savoir, c'est DE QUEL contrat il s'agit.
    // La colonne Pièce porte déjà l'écriture ; la répéter ici n'apprendrait rien.
    const contrat = d.contrat
      ? `<div class="fm-regl-contrat"><span class="fm-badge neutre">${
          this._esc(d.contrat.type || "Contrat")}</span> ${this._esc(d.contrat.libelle)}${
          d.contrat.reference ? ` <span class="muted">${this._esc(d.contrat.reference)}</span>` : ""
        }</div>`
      : "";

    if (!(d.lignes || []).length) {
      // Écriture de journal : pas de facture derrière, la note EST la description.
      const note = d.note
        ? `<div class="fm-regl-note">${this._esc(d.note)}</div>`
        : (contrat ? "" : '<span class="muted">—</span>');
      return contrat + note;
    }

    const lignes = d.lignes.map((l) => {
      const partiel = Math.abs((parseFloat(l.total) || 0) - (parseFloat(l.alloue) || 0)) > 0.01;
      // Le lien porte le LIBELLÉ, pas le nom interne ERPNext : « Achat 1572 — fournisseur »
      // se lit, « ACC-PINV-2026-00075 » se déchiffre. La pièce reste cliquable.
      return `<div class="fm-regl">
        ${l.tiers ? `<span class="tiers">${this._esc(l.tiers)}</span>` : ""}
        ${this._lien_texte(l.document_type, l.document_name, l.libelle)}
        ${l.piece ? `<span class="piece">${this._esc(l.piece)}</span>` : ""}
        <span class="mt">${this._m(l.alloue)}${partiel
          ? ` <span class="fm-badge warn">sur ${this._m(l.total)}</span>` : ""}</span>
      </div>`;
    }).join("");

    // Un bordereau porte plusieurs règlements : on nomme le bordereau, puis on liste ce qu'il
    // contient. Et si la somme des règlements retrouvés ne fait pas le crédit, on le dit.
    const entete = d.bordereau
      ? `<div class="fm-regl-ref">${this._lien_texte("Encaissement Paiement", d.bordereau,
          `Bordereau ${d.bordereau}`)}${d.reference
          ? ` · ${this._esc(d.reference)}` : ""}</div>`
      : (d.reference ? `<div class="fm-regl-ref">${this._esc(d.reference)}</div>` : "");
    // Le crédit dépasse presque toujours la somme allouée d'environ 1 DT : c'est le timbre
    // prélevé à la source. Le signaler en rouge sur douze lignes chaque mois, c'est crier au
    // loup — même seuil que la colonne Écart, donc discret en dessous, alerte au-dessus.
    const ecart = parseFloat(d.reste) || 0;
    const reste = Math.abs(ecart) > 0.01
      ? `<div class="fm-regl"><span class="${Math.abs(ecart) > (seuil || 0)
          ? "fm-badge warn" : "muted"}">non rattaché ${this._m(ecart)}</span></div>`
      : "";
    return contrat + entete + lignes + reste;
  }

  // ---------------------------------------------------------------- factures

  _rendre_factures(d) {
    const t = d.totaux;
    const n = d.numerotation || {};
    const trous = (n.trous_total || 0);
    const doublons = (n.doublons || []).length;

    const kpis = this._kpis([
      ["Factures", t.nombre, `n° ${n.premier ?? "—"} → ${n.dernier ?? "—"}`, ""],
      ["Total HT", this._m(t.ht), "", ""],
      ["Total TVA", this._m(t.tva), "", ""],
      ["Total TTC", this._m(t.ttc), "", "ok"],
      ["Réglé", this._m(t.regle), `reste dû ${this._m(t.reste_du)}`,
        t.reste_du > 0 ? "warn" : "ok"],
      ["Espèces présumées", this._m(t.presume),
        `${t.factures_presumees} facture(s) sans pièce`, t.presume > 0 ? "warn" : "ok"],
      ["Trous / doublons", `${trous} / ${doublons}`,
        trous || doublons ? "à vérifier avant remise" : "série continue",
        trous || doublons ? "bad" : "ok"],
    ]);

    const alertes = [];
    if (Math.abs(t.ecart_base) > 0.01 || Math.abs(t.ecart_tva) > 0.01) {
      alertes.push(`<div class="fm-note alerte"><b>Écart de ventilation</b> — base
        ${this._m(t.ecart_base)}, TVA ${this._m(t.ecart_tva)}. La somme des bases par taux ne
        retombe pas sur le total HT des factures. L'écart est affiché, pas absorbé.</div>`);
    }
    if (t.factures_par_division) {
      alertes.push(`<div class="fm-note">${t.factures_par_division} facture(s) ventilée(s) par
        division faute de détail par article : la base est reconstituée à partir du montant de
        TVA, elle est approchée.</div>`);
    }
    if (t.presume) {
      alertes.push(`<div class="fm-note">${this._m(t.presume)} imputé(s) en espèces sur
        ${t.factures_presumees} facture(s) au titre du reste dû : <b>aucune pièce comptable
        n'existe derrière ce montant</b>, et aucune n'a été créée. La colonne « Reste dû » reste
        le solde réel.</div>`);
    }
    if (trous) {
      alertes.push(`<div class="fm-note alerte"><b>${trous} numéro(s) manquant(s)</b> dans la
        série : ${this._esc((n.trous || []).join(", "))}.</div>`);
    }
    if (doublons) {
      alertes.push(`<div class="fm-note alerte"><b>Numéro(s) en double</b> :
        ${this._esc((n.doublons || []).join(", "))}.</div>`);
    }

    const taux = (t.taux || []).map((x) =>
      `<tr><td>${x.taux}&nbsp;%</td><td class="num">${this._m(x.base)}</td>
       <td class="num">${this._m(x.tva)}</td></tr>`).join("");
    const ventilation = `<div class="fm-scroll"><table class="fm-tbl">
      <thead><tr><th>Taux</th><th class="num">Base</th><th class="num">TVA</th></tr></thead>
      <tbody>${taux}
        <tr><td class="muted">Exonéré / hors champ</td>
            <td class="num">${this._m(t.base_exoneree)}</td><td class="num">—</td></tr>
        <tr><td class="muted">Autres taxes${(t.autres_taxes_libelles || []).length
          ? ` (${this._esc(t.autres_taxes_libelles.join(", "))})` : ""}</td>
            <td class="num">—</td><td class="num">${this._m(t.autres_taxes)}</td></tr>
      </tbody>
      <tfoot><tr><td>Total</td><td class="num">${this._m(t.total_base)}</td>
        <td class="num">${this._m(t.total_tva + (t.autres_taxes || 0))}</td></tr></tfoot>
      </table></div>`;

    const lignes = (d.factures || []).map((f) => `<tr>
      <td><b>${this._esc(f.nom_dossier)}</b></td>
      <td class="muted">${f.date}</td>
      <td>${this._esc(f.client_nom)}</td>
      <td class="num">${this._m(f.ht)}</td>
      <td class="num">${this._m(f.tva)}</td>
      <td class="num"><b>${this._m(f.ttc)}</b></td>
      <td class="num">${this._m(f.regle)}</td>
      <td class="num">${f.reste_du > 0
        ? `<span class="fm-badge warn">${this._m(f.reste_du)}</span>` : "—"}</td>
      <td>${this._paiements_facture(f.paiements)}</td>
      <td>${this._lien("Sales Invoice", f.facture)}</td>
    </tr>`).join("");

    const table = (d.factures || []).length
      ? `<div class="fm-scroll"><table class="fm-tbl"><thead><tr>
          <th>Nom facture</th><th>Date</th><th>Client</th><th class="num">HT</th>
          <th class="num">TVA</th><th class="num">TTC</th><th class="num">Réglé</th>
          <th class="num">Reste dû</th><th>Paiements</th><th>Pièce</th>
          </tr></thead><tbody>${lignes}</tbody>
          <tfoot><tr><td colspan="3">${t.nombre} facture(s)</td>
            <td class="num">${this._m(t.ht)}</td><td class="num">${this._m(t.tva)}</td>
            <td class="num">${this._m(t.ttc)}</td><td class="num">${this._m(t.regle)}</td>
            <td class="num">${this._m(t.reste_du)}</td><td colspan="2"></td></tr></tfoot>
          </table></div>`
      : '<div class="fm-vide">Aucune facture soumise sur ce mois.</div>';

    return kpis + alertes.join("")
      + this._sous("Ventilation par taux") + ventilation
      + this._sous("Factures du mois") + table;
  }

  /** Les règlements d'une facture : mode, n° de pièce, référence bancaire, montant. */
  _paiements_facture(liste) {
    if (!liste || !liste.length) return '<span class="muted">—</span>';
    return liste.map((p) => {
      const presume = parseFloat(p.presume) || 0;
      // La part présumée est signalée sur la ligne : le total équilibre, mais on voit
      // exactement combien n'a aucune pièce derrière lui.
      const marque = presume
        ? (presume >= (parseFloat(p.montant) || 0)
          ? ' <span class="fm-badge warn">présumé</span>'
          : ` <span class="fm-badge warn">dont ${this._m(presume)} présumé</span>`)
        : "";
      const corps = p.piece
        ? this._lien_texte("Payment Entry", p.payment_entry, p.piece)
        : (p.nombre > 1
          ? `<span class="lbl">${p.nombre} versements</span>`
          : (p.nombre
            ? this._lien_texte("Payment Entry", p.payment_entry, "règlement")
            : '<span class="lbl">sans pièce</span>'));
      return `<div class="fm-regl">
        <span class="tiers">${this._esc(p.mode)}</span>
        ${corps}${marque}
        ${p.banque ? `<span class="piece">${this._esc(p.banque)}</span>` : ""}
        <span class="mt">${this._m(p.montant)}</span>
      </div>`;
    }).join("");
  }

  // ---------------------------------------------------------------- dossier

  async _suivre_dossier() {
    const $b = this.$root.find('[data-role="dossier"]');
    if (!$b.length) return;
    let d;
    try {
      d = (await frappe.call({
        method: "bank_retenue_sync.api.cloture.get_dossier",
        args: { mois: this.mois },
      })).message || {};
    } catch (e) {
      $b.html(this._erreur(e));
      return;
    }
    if (this.mois !== d.mois) return;
    const statut = (d.etat || {}).statut;
    $b.html(this._rendu_dossier(d));

    // ⚠️ LA FIN DOIT SE DIRE. Sans ce signal, la seule façon de savoir que la constitution
    // avait abouti était de remarquer qu'une ligne de plus était apparue dans la liste des
    // archives — invisible si on regardait ailleurs pendant les deux minutes du job.
    if (this._precedent === "en cours" && statut !== "en cours") {
      if (statut === "termine") {
        const r = (d.etat || {}).resume || {};
        frappe.show_alert({
          message: __("Dossier de {0} constitué : {1} pièces, {2} Mo", [
            this.mois, r.pieces || 0, ((r.taille || 0) / 1048576).toFixed(1)]),
          indicator: "green",
        }, 10);
      } else if (statut === "echec") {
        frappe.msgprint({
          title: __("La constitution a échoué"), indicator: "red",
          message: frappe.utils.escape_html((d.etat || {}).erreur || ""),
        });
      }
    }
    this._precedent = statut;

    const encours = statut === "en cours";
    this._arreter_suivi();
    if (encours) this._timer = setTimeout(() => this._suivre_dossier(), 4000);
  }

  _arreter_suivi() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
  }

  _rendu_dossier(d) {
    const e = d.etat || {};
    const encours = e.statut === "en cours";
    const bouton = this.peut_generer
      ? `<button data-action="generer" ${encours ? "disabled" : ""}>
           ${encours ? "Constitution en cours…"
             : (e.statut === "echec" ? "Relancer la constitution" : "Constituer le dossier")}
         </button>`
      : '<span class="muted" style="font-size:12px;">Constitution réservée aux gestionnaires comptables.</span>';

    let corps = "";
    if (encours) {
      corps = `<div class="fm-jauge"><span style="width:${e.avancement || 0}%"></span></div>
        <div class="muted" style="font-size:12px;">${this._esc(e.etape || "")} —
        ${e.avancement || 0} %</div>`;
    } else if (e.statut === "termine") {
      const r = e.resume || {};
      corps = `<div class="fm-note" style="border-color:rgba(40,167,69,.4);
        background:rgba(40,167,69,.07);"><b>✅ Dossier constitué</b> —
        ${r.pieces || 0} pièces, ${((r.taille || 0) / 1048576).toFixed(1)} Mo,
        en ${r.secondes || 0} s.<br>
        ${r.factures || 0} facture(s), ${r.charges || 0} ligne(s) de charge,
        ${r.sans_justificatif || 0} sans justificatif exigible.
        <span class="muted">Terminé le ${this._esc(e.fin || "")}.</span></div>`;
    } else if (e.statut === "echec") {
      corps = `<div class="fm-note alerte"><b>❌ La constitution a échoué</b><br>
        ${this._esc(e.erreur || "")}<br>
        <span class="muted">Rien n'a été enregistré. Relance quand la cause est levée.</span>
        </div>`;
    }
    if ((e.erreurs || []).length) {
      corps += `<div class="fm-note alerte"><b>${e.erreurs.length} PDF non produit(s)</b> —
        le reste de l'archive est complet.<br>${this._esc(e.erreurs.slice(0, 5).join(" · "))}</div>`;
    }

    const archives = (d.archives || []).length
      // Le lien passe par la page, pas par `file_url` : l'archive est un fichier privé créé par
      // le worker, donc appartenant à Administrator, et Frappe la refusait à tout le monde
      // d'autre — « 403 Forbidden » sur un dossier qu'on venait de demander.
      ? `<div class="archives"><b>Archives de ce mois</b>${(d.archives || []).map((a) =>
          `<div><a href="/api/method/bank_retenue_sync.api.cloture.telecharger_dossier?${
            new URLSearchParams({ mois: this.mois, fichier: a.name }).toString()
          }">${this._esc(a.file_name)}</a>
           <span class="muted"> — ${frappe.datetime.str_to_user(a.creation)} · ${
            (a.file_size / 1048576).toFixed(1)} Mo</span></div>`).join("")}
         </div>`
      : '<div class="muted" style="font-size:12px;">Aucune archive pour ce mois.</div>';

    return `<div class="tete"><b>Dossier du mois</b>${bouton}</div>${corps}${archives}
      <div class="muted" style="font-size:11.5px;">Caisse espèces, récapitulatif des ventes,
      liste des charges, identification bancaire, un PDF par facture, les justificatifs des
      charges — et le relevé de la banque si tu le demandes. Chaque constitution crée une
      archive datée : elle n\u2019écrase pas la précédente.</div>`;
  }

  async _generer() {
    const choix = await new Promise((resolve) => {
      let repondu = false;
      const d = new frappe.ui.Dialog({
        title: __("Constituer le dossier de {0}", [this.mois]),
        fields: [
          { fieldtype: "Check", fieldname: "avec_pdf", default: 1,
            label: __("Les PDF de toutes les factures du mois (quelques minutes)") },
          { fieldtype: "Check", fieldname: "avec_releve", default: 1,
            label: __("Le relevé mensuel de la banque, en PDF") },
          { fieldtype: "HTML",
            options: `<div class="text-muted" style="font-size:12px;">Le relevé est la seule
              pièce qui sort du bench. Le service le sert aussitôt s'il l'a déjà archivé ; sinon
              il pilote le portail de la banque, ce qui prend quelques minutes. En cas d'échec,
              le dossier est constitué sans lui.</div>` },
        ],
        primary_action_label: __("Constituer"),
        // ⚠️ RÉPONDRE AVANT DE FERMER. `d.hide()` déclenche `onhide` sur-le-champ : résoudre
        // après lui, c'est résoudre en second, et la promesse a déjà pris la valeur `null` de
        // l'abandon. Le bouton « Constituer » ne faisait donc rien du tout.
        primary_action: (v) => { repondu = true; resolve(v); d.hide(); },
      });
      d.onhide = () => { if (!repondu) resolve(null); };
      d.show();
    });
    if (!choix) return;
    try {
      const r = await frappe.call({
        method: "bank_retenue_sync.api.cloture.generer_dossier",
        args: { mois: this.mois, avec_pdf: choix.avec_pdf ? 1 : 0,
                avec_releve: choix.avec_releve ? 1 : 0 },
      });
      frappe.show_alert({ message: (r.message || {}).message || "Lancé.", indicator: "blue" });
      this._suivre_dossier();
    } catch (e) {
      frappe.msgprint({ title: "Erreur", message: String(e), indicator: "red" });
    }
  }

  // ------------------------------------------------ envoi au comptable & retards

  async _charger_mensuel() {
    const $b = this.$root.find('[data-role="mensuel"]');
    if (!$b.length) return;
    let d;
    try {
      d = (await frappe.call({
        method: "bank_retenue_sync.api.cloture.get_dossier_mensuel",
        args: { mois: this.mois },
      })).message || {};
    } catch (e) {
      $b.html(this._erreur(e));
      return;
    }
    if (this.mois !== d.mois) return;
    this._mensuel = d;
    $b.html(this._rendu_mensuel(d));
    this._maj_pastille(d.nb_candidats);
  }

  _rendu_mensuel(d) {
    const e = d.envoi || {};
    const envoye = e.statut === "Envoyé";
    const bouton = d.peut_envoyer
      ? (envoye
        ? '<button data-action="annuler-envoi">Annuler l’envoi</button>'
        : '<button data-action="marquer-envoye">Marquer comme envoyé</button>')
      : '<span class="muted" style="font-size:12px;">Envoi réservé aux gestionnaires comptables.</span>';

    const quand = e.date_envoi_libelle
      || (e.date_envoi ? frappe.datetime.str_to_user(e.date_envoi) : "");
    const banniere = envoye
      ? `<div class="fm-note" style="border-color:rgba(40,167,69,.4);background:rgba(40,167,69,.07);">
           <b>✅ Envoyé au comptable</b> — le ${this._esc(quand)}${
            e.envoye_par ? ` par ${this._esc(e.envoye_par)}` : ""}. Les charges saisies après
           cette date pour ${this._esc(d.libelle)} deviennent des retardataires à rattraper
           ailleurs.</div>`
      : `<div class="fm-note">Ce mois n’est pas encore marqué comme envoyé au comptable.</div>`;

    // ⚠️ UN MOIS ENVOYÉ NE SE RATTACHE PLUS. Cocher une case sur un dossier déjà parti ferait
    // sortir la pièce du vivier — pastille, section, contrôle des non-envoyées — sans qu’elle
    // ait jamais été transmise : le trou même que cet écran est censé fermer. Le serveur refuse ;
    // l’écran ne doit donc pas le proposer, et les rattachements déjà faits restent visibles.
    const lignes = envoye
      ? (d.rattaches || []).map((r) => this._ligne_retard(r, true, true)).join("")
      : [
        ...(d.rattaches || []).map((r) => this._ligne_retard(r, true, false)),
        ...(d.candidats || []).map((r) => this._ligne_retard(r, false, false)),
      ].join("");
    const total = envoye
      ? (d.rattaches || []).length
      : (d.rattaches || []).length + (d.candidats || []).length;
    const table = total
      ? `<div class="fm-scroll"><table class="fm-tbl"><thead><tr>
           <th>Rattacher</th><th>Mois d’origine</th><th>Tiers</th><th>Référence</th>
           <th class="num">Montant</th><th>Justificatif</th><th>Pièce</th>
           </tr></thead><tbody>${lignes}</tbody></table></div>`
      : `<div class="fm-vide">${envoye
        ? "Aucune retardataire n’avait été rattachée à ce mois."
        : "Aucune charge retardataire pour le moment."}</div>`;

    const consigne = envoye
      ? `<div class="fm-note alerte"><b>Mois envoyé, rattachements figés.</b> Les
         ${d.nb_candidats || 0} retardataire(s) encore en attente partiront avec un mois suivant.
         Annulez l’envoi de ${this._esc(d.libelle)} pour modifier ses rattachements.</div>`
      : `<div class="muted" style="font-size:12px;">Charges d’un mois déjà envoyé, saisies après
         son envoi. Cochez-les pour les joindre au dossier de <b>${this._esc(d.libelle)}</b> :
         elles y paraîtront dans un sous-bloc « Retards de … », à sous-total séparé, sans gonfler
         le total du mois.</div>`;

    return `<div class="fm-dossier">
        <div class="tete"><b>Envoi au comptable</b>${bouton}</div>${banniere}
      </div>
      <div class="fm-dossier">
        <div class="tete"><b>À rattraper</b>
          <span class="fm-badge ${d.nb_candidats ? "bad" : "ok"}">${
            d.nb_candidats || 0} candidate(s)</span>
          <span style="flex:1"></span>
          <button data-action="verifier-non-envoyees">Vérifier les factures non envoyées</button>
        </div>
        ${consigne}
        ${table}
      </div>`;
  }

  _ligne_retard(r, coche, fige) {
    return `<tr>
      <td><input type="checkbox" data-action="rattacher"
        data-dt="${this._esc(r.document_type)}" data-dn="${this._esc(r.document_name)}"
        ${coche ? "checked" : ""}${fige ? " disabled" : ""}></td>
      <td>${this._esc(r.libelle_origine || r.mois_origine || "")}</td>
      <td>${this._esc(r.tiers || "")}</td>
      <td>${this._esc(r.reference || "")}</td>
      <td class="num">${this._m(r.montant)}</td>
      <td>${r.avec_justificatif
        ? '<span class="fm-badge ok">présent</span>'
        : '<span class="fm-badge bad">manquant</span>'}</td>
      <td>${this._lien(r.document_type, r.document_name)}</td>
    </tr>`;
  }

  async _marquer_envoye() {
    const ok = await new Promise((r) => frappe.confirm(
      `Marquer ${this._esc(this.mois)} comme envoyé au comptable ?`,
      () => r(true), () => r(false)));
    if (!ok) return;
    try {
      await frappe.call({ method: "bank_retenue_sync.api.cloture.marquer_envoye",
        args: { mois: this.mois } });
      frappe.show_alert({ message: __("Mois marqué comme envoyé."), indicator: "green" });
      this._charger_mensuel();
    } catch (e) {
      frappe.msgprint({ title: __("Envoi impossible"), message: String(e), indicator: "red" });
    }
  }

  async _annuler_envoi() {
    const ok = await new Promise((r) => frappe.confirm(
      `Annuler l’envoi de ${this._esc(this.mois)} ? Le mois redeviendra un brouillon.`,
      () => r(true), () => r(false)));
    if (!ok) return;
    try {
      const r = (await frappe.call({ method: "bank_retenue_sync.api.cloture.annuler_envoi",
        args: { mois: this.mois } })).message || {};
      frappe.show_alert({ message: __("Envoi annulé."), indicator: "orange" });
      // Annuler l’envoi ne rappelle pas ce qui est déjà chez le comptable : si des charges de ce
      // mois sont parties avec un dossier postérieur, on le dit plutôt que de laisser croire que
      // le mois repart d’une page blanche.
      if (r.parties_ailleurs) {
        frappe.msgprint({
          title: __("Des charges de ce mois sont déjà parties"),
          indicator: "orange",
          message: __("{0} charge(s) de {1} ont été rattrapées par le dossier d’un mois "
            + "postérieur et sont déjà chez le comptable. Annuler l’envoi ne les rappelle pas : "
            + "si vous reconstituez ce mois, leurs lignes porteront la mention « déjà remise ».",
            [r.parties_ailleurs, this.mois]),
        });
      }
      this._charger_mensuel();
    } catch (e) {
      frappe.msgprint({ title: __("Annulation impossible"), message: String(e), indicator: "red" });
    }
  }

  async _rattacher(e) {
    const $c = $(e.currentTarget);
    const attacher = $c.is(":checked") ? 1 : 0;
    $c.prop("disabled", true);
    try {
      await frappe.call({ method: "bank_retenue_sync.api.cloture.rattacher",
        args: { mois: this.mois, document_type: $c.attr("data-dt"),
                document_name: $c.attr("data-dn"), attacher } });
      // On recharge : le compteur, la pastille et la répartition coché/candidat suivent le geste.
      this._charger_mensuel();
    } catch (err) {
      frappe.msgprint({ title: __("Rattachement impossible"), message: String(err),
        indicator: "red" });
      $c.prop("checked", !attacher).prop("disabled", false);
    }
  }

  async _verifier_non_envoyees() {
    let res;
    try {
      res = (await frappe.call({
        method: "bank_retenue_sync.api.cloture.verifier_non_envoyees",
        args: { nb_mois: 12 },
        freeze: true,
        freeze_message: __("Recherche des charges non envoyées…"),
      })).message || {};
    } catch (e) {
      frappe.msgprint({ title: __("Vérification impossible"), message: String(e),
        indicator: "red" });
      return;
    }
    const d = new frappe.ui.Dialog({ title: __("Factures non envoyées"), size: "extra-large" });
    d.$body.html(this._rendu_non_envoyees(res));
    d.show();
    d.$wrapper.find(".modal-dialog").css("max-width", "88vw");
  }

  _rendu_non_envoyees(res) {
    if (!(res.groupes || []).length) {
      return `<div class="fm-vide">Aucune charge non envoyée sur les ${
        res.nb_mois || 0} derniers mois : tout ce qui a été saisi après un envoi est déjà
        rattaché.</div>`;
    }
    const blocs = (res.groupes || []).map((g) => {
      const rows = g.lignes.map((r) => `<tr>
        <td>${this._esc(r.tiers || "")}</td>
        <td>${this._esc(r.reference || "")}</td>
        <td class="num">${this._m(r.montant)}</td>
        <td>${r.avec_justificatif
          ? '<span class="fm-badge ok">présent</span>'
          : '<span class="fm-badge bad">manquant</span>'}</td>
        <td>${this._lien(r.document_type, r.document_name)}</td>
      </tr>`).join("");
      return this._sous(`Retards de ${this._esc(g.libelle || g.mois)} — ${
        g.totaux.nombre} ligne(s) · TTC ${this._m(g.totaux.ttc)}`)
        + `<div class="fm-scroll"><table class="fm-tbl"><thead><tr>
             <th>Tiers</th><th>Référence</th><th class="num">Montant</th>
             <th>Justificatif</th><th>Pièce</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }).join("");
    return `<div class="fm-note">${res.nb} charge(s) saisie(s) après l’envoi de leur mois et non
      encore rattachée(s), sur les ${res.nb_mois} derniers mois. Ouvrez le mois porteur voulu et
      cochez-les dans « À rattraper ».</div>${blocs}`;
  }

  // ---------------------------------------------------------------- charges

  _rendre_charges(d) {
    const t = d.totaux;
    const kpis = this._kpis([
      ["Lignes", t.nombre, `${t.avec_justificatif} avec pièce · ${t.exemptes} exemptée(s)`, ""],
      ["Total HT", this._m(t.ht), "", ""],
      ["Total TVA", this._m(t.tva), "", ""],
      ["Total TTC", this._m(t.ttc), "", ""],
      ["Sans justificatif", t.sans_justificatif, "exigible et manquant",
        t.sans_justificatif ? "bad" : "ok"],
      ["Contrôlés", t.controles || 0,
        `${t.concordants || 0} ok · ${t.discordants || 0} écart · ${t.illisibles || 0} illisible`,
        (t.discordants || 0) ? "bad" : "ok"],
    ]);

    const controle = this.peut_generer
      ? `<div class="fm-dossier"><div class="tete"><b>Contrôle des justificatifs</b>
          <button data-action="controler-mois">Lire toutes les pièces non encore lues</button>
          <span class="muted" style="font-size:11.5px;">Chaque lecture est un appel payant au
          modèle ; les pièces déjà lues sont sautées.</span></div></div>`
      : "";

    const absents = (d.comptes_absents || []).length
      ? `<div class="fm-note alerte"><b>Compte(s) introuvable(s)</b> :
         ${this._esc(d.comptes_absents.join(", "))}. Le bloc correspondant est vide — ce n'est
         pas une absence de charges, c'est un compte qui a changé de nom.</div>` : "";

    const blocs = d.blocs.map((b) => {
      if (!b.lignes.length) {
        return this._sous(b.titre) + '<div class="fm-vide">Aucune ligne sur ce mois.</div>';
      }
      const corps = b.lignes.map((l) => {
        const c = l.controle;
        const ecart = c ? (c.ecarts || {}).ttc : null;
        return `<tr>
        <td class="muted">${l.date}</td>
        <td><b>${this._esc(l.reference_export || l.ref)}</b>${
          (l.factures || []).length
            ? `<div class="fm-regl-note">facture ${this._esc(l.factures.join(", "))}</div>` : ""}
        </td>
        <td>${this._esc(l.tiers || "")}</td>
        <td class="muted">${this._esc(l.categorie || "")}</td>
        <td class="num">${this._m(l.ht)}</td>
        <td class="num">${this._m(l.tva)}</td>
        <td class="num"><b>${this._m(l.ttc)}</b></td>
        <td class="num">${l.retenue ? this._m(l.retenue) : ""}${this._controle_ras(l)}</td>
        <td>${this._justificatif(l)}</td>
        <td class="num">${c ? this._m(c.extrait.ht) : ""}</td>
        <td class="num">${c ? this._m(c.extrait.tva) : ""}</td>
        <td class="num">${c ? this._m(c.extrait.ttc) : ""}</td>
        <td>${this._verdict(l, ecart)}</td>
        <td>${this._lien(l.document_type, l.document_name)}</td>
      </tr>`;
      }).join("");
      return this._sous(`${b.titre} — ${b.totaux.nombre} ligne(s)`)
        + `<div class="fm-scroll"><table class="fm-tbl"><thead><tr>
            <th>Date</th><th>Référence export</th><th>Tiers</th><th>Catégorie</th>
            <th class="num">HT</th><th class="num">TVA</th><th class="num">TTC</th>
            <th class="num">Retenue</th><th>Justificatif</th>
            <th class="num">HT lu</th><th class="num">TVA lue</th><th class="num">TTC lu</th>
            <th>Contrôle</th><th>Pièce</th>
            </tr></thead><tbody>${corps}</tbody>
            <tfoot><tr><td colspan="4">Total ${this._esc(b.titre)}</td>
              <td class="num">${this._m(b.totaux.ht)}</td>
              <td class="num">${this._m(b.totaux.tva)}</td>
              <td class="num">${this._m(b.totaux.ttc)}</td>
              <td class="num">${this._m(b.totaux.retenue)}</td>
              <td colspan="6">${b.totaux.sans_justificatif} sans pièce exigible ·
                ${b.totaux.exemptes} exemptée(s)</td></tr></tfoot>
           </table></div>`;
    }).join("");

    return kpis + absents + controle + this._encart_rattrapage(d) + blocs
      + this._blocs_retards(d);
  }

  // ------------------------------------------------ rattrapage manuel (onglet Charges)

  /** Le sélecteur de mois d’origine : uniquement des mois STRICTEMENT antérieurs à l’affiché. */
  _encart_rattrapage(d) {
    if (!d.peut_rattacher) return "";
    const envoye = (d.envoi || {}).statut === "Envoyé";
    const mois = (this.mois_offerts || []).filter((m) => m.cle < this.mois);
    if (!mois.length) {
      return `<div class="fm-dossier"><div class="tete"><b>Joindre des charges d’un mois
        antérieur</b></div><div class="muted" style="font-size:12px;">Aucun mois antérieur
        proposé.</div></div>`;
    }
    const options = ['<option value="">— choisir un mois —</option>']
      .concat(mois.map((m) => `<option value="${this._esc(m.cle)}">${
        this._esc(m.libelle)}</option>`)).join("");

    const consigne = envoye
      ? `<div class="fm-note alerte"><b>Mois envoyé, rattachements figés.</b> Annulez l’envoi de
         ${this._esc(d.libelle)} dans l’onglet Dossier pour joindre des charges antérieures.</div>`
      : `<div class="muted" style="font-size:12px;">Choisissez un mois : toutes ses dépenses et
         factures d’achat s’affichent. Cochez celles qui ne sont jamais parties pour les joindre au
         dossier de <b>${this._esc(d.libelle)}</b>. Elles y paraîtront dans un sous-bloc
         « Retards de … », à sous-total séparé, sans gonfler le total du mois.</div>`;

    return `<div class="fm-dossier">
      <div class="tete"><b>Joindre des charges d’un mois antérieur</b>
        <select data-f="mois-origine">${options}</select></div>
      ${consigne}
      <div data-role="anterieures"></div>
    </div>`;
  }

  async _charger_anterieures() {
    const $b = this.$root.find('[data-role="anterieures"]');
    if (!$b.length) return;
    if (!this.mois_origine) { $b.empty(); return; }
    const demande = `${this.mois}|${this.mois_origine}`;
    $b.html('<div class="fm-chargement">Lecture du mois…</div>');
    let d;
    try {
      d = (await frappe.call({
        method: "bank_retenue_sync.api.cloture.get_charges_anterieures",
        args: { mois: this.mois, mois_origine: this.mois_origine },
      })).message || {};
    } catch (e) {
      $b.html(this._erreur(e));
      return;
    }
    // Le mois a pu changer pendant la lecture : afficher une réponse périmée ferait cocher des
    // pièces au nom d’un autre dossier.
    if (demande !== `${this.mois}|${this.mois_origine}`) return;
    $b.html(this._rendu_anterieures(d));
  }

  _rendu_anterieures(d) {
    if (!d.nb) {
      return `<div class="fm-vide">Aucune dépense ni facture d’achat en
        ${this._esc(d.libelle_origine || d.mois_origine)}.</div>`;
    }
    const envoye = (d.envoi || {}).statut === "Envoyé";
    const fige = envoye || !d.peut_rattacher;

    const bandeau = d.origine_envoyee
      ? ""
      : `<div class="fm-note alerte"><b>${this._esc(d.libelle_origine)} n’est pas marqué comme
         envoyé au comptable.</b> Ces pièces partiront normalement avec le dossier de leur propre
         mois s’il est constitué un jour. En joindre une ici la remettrait alors deux fois — sa
         ligne porterait la mention « déjà remise », mais le montant, lui, n’est jamais compté
         deux fois.</div>`;

    const corps = (d.lignes || []).map((l) => {
      const ailleurs = l.rattachee_a && !l.rattachee_ici;
      return `<tr class="${ailleurs ? "muted" : ""}">
        <td><input type="checkbox" data-action="rattacher-manuel"
          data-dt="${this._esc(l.document_type)}" data-dn="${this._esc(l.document_name)}"
          ${l.rattachee_ici ? "checked" : ""}${fige || ailleurs ? " disabled" : ""}></td>
        <td class="muted">${this._esc(l.date || "")}</td>
        <td><b>${this._esc(l.reference || "")}</b>${l.saisie_apres_envoi
          ? ' <span class="fm-badge warn">saisie après l’envoi</span>' : ""}${ailleurs
          ? `<div class="fm-regl-note">déjà rattachée au dossier de ${
              this._esc(l.libelle_porteur || l.rattachee_a)}</div>` : ""}</td>
        <td>${this._esc(l.tiers || "")}</td>
        <td class="muted">${this._esc(l.categorie || "")}</td>
        <td class="num">${this._m(l.montant)}</td>
        <td>${this._justificatif(l, false)}</td>
        <td>${this._lien(l.document_type, l.document_name)}</td>
      </tr>`;
    }).join("");

    return bandeau + `<div class="fm-scroll"><table class="fm-tbl"><thead><tr>
        <th>Joindre</th><th>Date</th><th>Référence export</th><th>Tiers</th><th>Catégorie</th>
        <th class="num">TTC</th><th>Justificatif</th><th>Pièce</th>
        </tr></thead><tbody>${corps}</tbody>
        <tfoot><tr><td colspan="5">${d.nb} pièce(s) en ${
          this._esc(d.libelle_origine)} · ${d.nb_rattachees_ici} jointe(s) à ${
          this._esc(d.libelle)}</td><td colspan="3"></td></tr></tfoot>
      </table></div>`;
  }

  async _rattacher_manuel(e) {
    const $c = $(e.currentTarget);
    const attacher = $c.is(":checked") ? 1 : 0;
    $c.prop("disabled", true);
    try {
      await frappe.call({
        method: "bank_retenue_sync.api.cloture.rattacher",
        args: { mois: this.mois, document_type: $c.attr("data-dt"),
                document_name: $c.attr("data-dn"), attacher, manuel: 1 },
      });
      // Le sous-bloc « Retards » vit dans la donnée de l’onglet : sans purge du cache, la pièce
      // resterait cochée sans jamais apparaître sous les blocs.
      delete this.cache[`${this.mois}|charges`];
      this._charger("charges");
      this._amorcer_pastille();
    } catch (err) {
      frappe.msgprint({ title: __("Rattachement impossible"), message: String(err),
        indicator: "red" });
      $c.prop("checked", !attacher).prop("disabled", false);
    }
  }

  /** Les charges rattrapées, sous les trois blocs et JAMAIS dans leurs totaux. */
  _blocs_retards(d) {
    const groupes = d.retards || [];
    if (!groupes.length) return "";
    return groupes.map((g) => {
      const t = g.totaux || {};
      const corps = (g.lignes || []).map((l) => `<tr>
        <td class="muted">${this._esc(l.date || "")}</td>
        <td><b>${this._esc(l.reference_export || l.ref || "")}</b></td>
        <td>${this._esc(l.tiers || "")}</td>
        <td class="muted">${this._esc(l.categorie || "")}</td>
        <td class="num">${this._m(l.ht)}</td>
        <td class="num">${this._m(l.tva)}</td>
        <td class="num"><b>${this._m(l.ttc)}</b></td>
        <td>${this._justificatif(l, false)}</td>
        <td>${this._lien(l.document_type, l.document_name)}</td>
      </tr>`).join("");
      return this._sous(`Retards de ${this._esc(g.libelle || g.mois)} — ${
          t.nombre || 0} ligne(s), hors total du mois`)
        + `<div class="fm-scroll"><table class="fm-tbl"><thead><tr>
            <th>Date</th><th>Référence export</th><th>Tiers</th><th>Catégorie</th>
            <th class="num">HT</th><th class="num">TVA</th><th class="num">TTC</th>
            <th>Justificatif</th><th>Pièce</th>
            </tr></thead><tbody>${corps}</tbody>
            <tfoot><tr><td colspan="4">Sous-total ${this._esc(g.libelle || g.mois)}</td>
              <td class="num">${this._m(t.ht)}</td>
              <td class="num">${this._m(t.tva)}</td>
              <td class="num">${this._m(t.ttc)}</td>
              <td colspan="2">${t.sans_justificatif || 0} sans pièce exigible</td>
            </tr></tfoot></table></div>`;
    }).join("");
  }

  /** Le taux de la retenue de vente : 1 % du TTC. Ce qui s'en écarte se voit tout de suite. */
  _controle_ras(l) {
    if (l.retenue_attendue == null) return "";
    const ecart = parseFloat(l.retenue_ecart) || 0;
    if (Math.abs(ecart) <= 0.01) {
      return `<div class="fm-regl-note">${l.taux_ras} % — conforme</div>`;
    }
    return `<div class="fm-regl-note"><span class="fm-badge bad">${
      ecart > 0 ? "+" : ""}${this._m(ecart)}</span> attendu ${this._m(l.retenue_attendue)}</div>`;
  }

  /** La colonne Justificatif : ouvrir la pièce, ou dire pourquoi il n'y en a pas.
   *
   * ⚠️ `avec_controle` EST FAUX HORS DES BLOCS DU MOIS. `controler_justificatif` cherche la ligne
   * dans les charges du mois affiché : sur une pièce d’un autre mois — une retardataire, une
   * candidate au rattrapage — il ne la trouve pas et lève « Ligne introuvable ». Un bouton qui ne
   * peut qu’échouer ne doit pas être offert.
   */
  _justificatif(l, avec_controle = true) {
    const pieces = l.justificatifs || [];
    if (!pieces.length) {
      return l.exemption
        ? `<span class="fm-badge neutre" title="${this._esc(l.exemption)}">non requis</span>`
        : '<span class="fm-badge bad">manquant</span>';
    }
    const boutons = pieces.map((j, i) => `<button class="fm-piece" data-action="voir-piece"
      data-url="${this._esc(j.file_url)}" data-nom="${this._esc(j.file_name)}"
      title="${this._esc(j.file_name)}">📎 ${pieces.length > 1 ? i + 1 : "voir"}</button>`).join("");
    const verifier = avec_controle && this.peut_generer && l.justificatif_requis
      ? `<button class="fm-piece" data-action="controler"
          data-dt="${this._esc(l.document_type)}" data-dn="${this._esc(l.document_name)}"
          data-url="${this._esc(pieces[0].file_url)}"
          title="Lire le PDF et le confronter à l'écriture">🔍</button>`
      : "";
    return `<span class="fm-act">${boutons}${verifier}</span>`;
  }

  _verdict(l, ecart) {
    const c = l.controle;
    if (!c) return '<span class="muted">—</span>';
    const bulle = [
      c.reference_pdf ? `réf. PDF : ${c.reference_pdf}` : "",
      c.date_pdf ? `date PDF : ${c.date_pdf}` : "",
      c.extrait.tiers ? `tiers PDF : ${c.extrait.tiers}` : "",
      c.extrait.timbre ? `timbre : ${c.extrait.timbre}` : "",
      `modèle : ${c.extrait.modele}`,
    ].filter(Boolean).join(" · ");
    if (c.concordant) {
      return `<span class="fm-badge ok" title="${this._esc(bulle)}">concordant</span>`;
    }
    const motifs = [];
    if ((c.hors_tolerance || []).length) motifs.push(c.hors_tolerance.join(", "));
    if (!c.equilibre) motifs.push("PDF déséquilibré");
    return `<span class="fm-badge bad" title="${this._esc(bulle)}">écart ${
      ecart != null ? this._m(ecart) : ""}</span>
      <div class="fm-regl-note">${this._esc(motifs.join(" · "))}</div>`;
  }

  // ---------------------------------------------------------------- fabriques

  _kpis(liste) {
    return `<div class="fm-kpis">${liste.map(([lbl, val, sub, cls]) =>
      `<div class="fm-kpi ${cls || ""}"><div class="lbl">${this._esc(lbl)}</div>
       <div class="val">${val == null ? "—" : val}</div>
       <div class="sub">${this._esc(sub || "")}</div></div>`).join("")}</div>`;
  }

  _sous(titre) {
    return `<div class="fm-sous">${this._esc(titre)}</div>`;
  }

  _lien(doctype, nom) {
    return this._lien_texte(doctype, nom, nom);
  }

  _lien_texte(doctype, nom, texte) {
    const libelle = this._esc(texte || nom || "");
    if (!doctype || !nom) return libelle || '<span class="muted">—</span>';
    return `<a href="#" data-doc="${this._esc(doctype)}" data-nom="${this._esc(nom)}"
      title="${this._esc(nom)}">${libelle}</a>`;
  }

  _indispo(message) {
    return `<div class="fm-note alerte">${this._esc(message || "Donnée indisponible.")}</div>`;
  }

  _erreur(e) {
    return `<div class="fm-note alerte">Chargement impossible : ${this._esc(String(e))}</div>`;
  }

  _m(v) {
    const n = parseFloat(v);
    if (!isFinite(n)) return "—";
    return n.toLocaleString("fr-TN", { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  }

  _esc(s) {
    return frappe.utils.escape_html(s == null ? "" : String(s));
  }
}
