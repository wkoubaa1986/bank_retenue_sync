// Dépenses en euros — espace Banque (02/11/2026).
//
// Plusieurs dépenses payées depuis le compte en euros, un taux EUR → TND lu sur internet (modifiable), et à la
// validation une écriture de journal multi-devises par ligne : débit charge en dinars, crédit compte euros.
frappe.pages["depenses-euros"].on_page_load = function (wrapper) {
  frappe.ui.make_app_page({ parent: wrapper, title: __("Dépenses en euros"), single_column: true });
  new DepensesEuros(wrapper);
};

const DE_API = "bank_retenue_sync.api.depenses_euros.";
const de_esc = (s) => frappe.utils.escape_html(s == null ? "" : String(s));
const de_q = (n, d = 3) => (Math.round((+n || 0) * 10 ** d) / 10 ** d).toLocaleString("fr-FR", { minimumFractionDigits: d, maximumFractionDigits: d });

class DepensesEuros {
  constructor(wrapper) {
    this.$root = $(wrapper).find(".layout-main-section");
    this.$root.append(frappe.render_template("depenses_euros", {}));
    this.lignes = [];
    this.source = "";
    this._lier();
    this._boot();
  }

  _lier() {
    this.$root.on("click", '[data-action="taux"]', () => this._taux());
    this.$root.on("click", '[data-action="ajouter"]', () => { this._ajouter(); this._peindre(); });
    this.$root.on("click", '[data-action="valider"]', () => this._valider());
    this.$root.on("input change", '[data-f="taux"]', () => this._peindre(false));
    this.$root.on("input change", "[data-lignes] [data-k]", (ev) => {
      const $i = $(ev.currentTarget), i = +$i.closest("tr").attr("data-i");
      this.lignes[i][$i.attr("data-k")] = $i.val();
      if ($i.attr("data-k") === "montant_eur") this._peindre(false);
    });
    this.$root.on("click", "[data-lignes] .de-x", (ev) => { this.lignes.splice(+$(ev.currentTarget).closest("tr").attr("data-i"), 1); this._peindre(); });
  }

  async _boot() {
    this.ctx = (await frappe.call({ method: DE_API + "contexte" })).message;
    const $c = this.$root.find('[data-f="compte_euros"]');
    $c.html(this.ctx.comptes_euros.map((a) => `<option value="${de_esc(a)}" ${a === this.ctx.compte_euros ? "selected" : ""}>${de_esc(a)}</option>`).join(""));
    this._ajouter();
    this._peindre();
    this._historique(this.ctx.historique);
    this._taux(true);
  }

  async _taux(silencieux = false) {
    try {
      const r = (await frappe.call({ method: DE_API + "taux_eur_tnd", freeze: !silencieux })).message;
      this.$root.find('[data-f="taux"]').val(r.taux);
      this.source = r.source;
      this.$root.find("[data-src]").text(`${__("source")} : ${r.source}`);
      this._peindre(false);
    } catch (e) {
      this.$root.find("[data-src]").text(__("Taux introuvable : saisissez-le à la main."));
    }
  }

  _ajouter() {
    const prec = this.lignes[this.lignes.length - 1];
    this.lignes.push({ date: (prec && prec.date) || this.ctx.aujourd_hui, libelle: "", montant_eur: "", compte_charge: (prec && prec.compte_charge) || this.ctx.compte_charge });
  }

  _taux_val() { return +this.$root.find('[data-f="taux"]').val() || 0; }

  _peindre(lignes = true) {
    const taux = this._taux_val();
    if (lignes) {
      const opts = (sel) => this.ctx.comptes_charge.map((c) => `<option value="${de_esc(c.name)}" ${c.name === sel ? "selected" : ""}>${de_esc(c.name)}</option>`).join("");
      this.$root.find("[data-lignes]").html(this.lignes.map((l, i) => `<tr data-i="${i}">
        <td><input type="date" data-k="date" value="${de_esc(l.date)}"></td>
        <td><input type="text" class="lib" data-k="libelle" value="${de_esc(l.libelle)}" placeholder="${__("Pub Google octobre, abonnement…")}"></td>
        <td class="n"><input type="number" step="0.01" min="0" data-k="montant_eur" value="${de_esc(l.montant_eur)}" placeholder="0.00"></td>
        <td><select data-k="compte_charge">${opts(l.compte_charge)}</select></td>
        <td class="n" data-tnd></td><td><span class="de-x" title="${__("Retirer")}">✕</span></td></tr>`).join(""));
    }
    let eur = 0;
    this.$root.find("[data-lignes] tr").each((i, tr) => {
      const m = +(this.lignes[i].montant_eur || 0); eur += m;
      $(tr).find("[data-tnd]").text(m && taux ? de_q(m * taux) : "");
    });
    this.$root.find("[data-total]").text(`${this.lignes.length} ${__("ligne(s)")} · ${de_q(eur, 2)} € · ${taux ? de_q(eur * taux) + " DT" : __("taux manquant")}`);
  }

  async _valider() {
    const taux = this._taux_val(), lignes = this.lignes.filter((l) => l.libelle || l.montant_eur);
    if (!lignes.length) { frappe.msgprint(__("Aucune ligne remplie.")); return; }
    if (!taux) { frappe.msgprint(__("Indiquez le taux EUR → TND.")); return; }
    const eur = lignes.reduce((a, l) => a + (+l.montant_eur || 0), 0);
    frappe.confirm(__("Passer {0} écriture(s) de journal : {1} € au taux {2}, soit {3} DT, crédités sur {4} ?",
        [lignes.length, de_q(eur, 2), taux, de_q(eur * taux), de_esc(this.$root.find('[data-f="compte_euros"]').val())]), async () => {
      try {
        const r = (await frappe.call({ method: DE_API + "creer_depenses", freeze: true, freeze_message: __("Écritures en cours…"),
          args: { lignes, taux, compte_euros: this.$root.find('[data-f="compte_euros"]').val(), source: this.source } })).message;
        this.$root.find("[data-resultat]").html(`<div class="de-ok">✅ ${r.crees.length} ${__("écriture(s) passée(s)")} · ${de_q(r.resume.eur, 2)} € = ${de_q(r.resume.tnd)} DT<ul style="margin:6px 0 0 16px">${
          r.crees.map((c) => `<li>${de_esc(c.date)} · <a href="/app/journal-entry/${encodeURIComponent(c.name)}">${de_esc(c.name)}</a> · ${de_esc(c.libelle)} · ${de_q(c.eur, 2)} € → ${de_q(c.tnd)} DT · ${de_esc(c.compte_charge)}</li>`).join("")}</ul></div>`);
        this.lignes = []; this._ajouter(); this._peindre();
        this._historique((await frappe.call({ method: DE_API + "historique" })).message);
      } catch (e) { /* le message d'erreur du serveur est déjà affiché */ }
    });
  }

  _historique(rows) {
    this.$root.find("[data-hist]").html((rows || []).map((h) => `<tr><td>${de_esc(frappe.datetime.str_to_user(h.posting_date))}</td>
      <td><a href="/app/journal-entry/${encodeURIComponent(h.name)}">${de_esc(h.name)}</a></td><td>${de_esc(h.libelle || "")}</td>
      <td class="n">${de_q(h.eur, 2)}</td><td class="n">${de_q(h.taux, 4)}</td><td class="n">${de_q(h.tnd)}</td><td>${de_esc(h.compte_charge || "")}</td></tr>`).join("")
      || `<tr><td colspan="7" class="de-src">${__("Aucune dépense en euros enregistrée.")}</td></tr>`);
  }
}
