frappe.pages["partenaire-economiq"].on_page_load = function (wrapper) {
  frappe.ui.make_app_page({
    parent: wrapper,
    title: "Economiq Aqua Solution",
    single_column: true,
  });
  $(wrapper).find(".layout-main-section").html(
    frappe.render_template("partenaire_economiq", {})
  );
  new PartenaireEconomiq(wrapper);
};

// Rendu pur : tous les montants viennent de bank_retenue_sync.api.partenaire.
// Le bilan lui-même vient de customization_app.bilan_vente — on ne le recalcule nulle part,
// sinon deux écrans afficheraient deux bénéfices pour le même mois.

class PartenaireEconomiq {
  constructor(wrapper) {
    this.$root = $(wrapper).find(".layout-main-section");
    this.mois = null;
    this.cache = {};
    this.$root.on("change", '[data-f="mois"]', (e) => {
      this.mois = $(e.currentTarget).val();
      this._charger();
    });
    this.$root.on("click", '[data-action="ajouter-charge"]', () => this._ajouter_charge());
    this.$root.on("click", ".pe-x", (e) => {
      $(e.currentTarget).closest("tr").remove();
      this._recalculer_apercu();
    });
    this.$root.on("input", '[data-charge]', () => this._recalculer_apercu());
    this.$root.on("input", '[data-f="ajustement"]', () => this._recalculer_apercu());
    this.$root.on("click", '[data-action="enregistrer"]', () => this._enregistrer(0));
    this.$root.on("click", '[data-action="valider"]', () => this._enregistrer(1));
    this.$root.on("click", '[data-action="creer-ecriture"]', () => this._creer_ecriture());
    this.$root.on("click", '[data-action="saisir-reglement"]', () => this._saisir_reglement());
    this.$root.on("click", '[data-action="rapport"]', () => this._rapport());

    this.$root.on("click", "[data-doc]", (e) => {
      e.preventDefault();
      const $a = $(e.currentTarget);
      frappe.set_route("Form", $a.attr("data-doc"), $a.attr("data-nom"));
    });
    this._boot();
  }

  async _boot() {
    let ctx;
    try {
      ctx = (await frappe.call({ method: "bank_retenue_sync.api.partenaire.get_contexte" })).message;
    } catch (e) {
      this.$root.find('[data-role="contenu"]').html(this._erreur(e));
      return;
    }
    this.mois = ctx.mois;
    const $sel = this.$root.find('[data-f="mois"]');
    $sel.html((ctx.mois_offerts || [])
      .map((m) => `<option value="${m.cle}">${this._esc(m.libelle)}</option>`).join(""));
    $sel.val(this.mois);
    this._charger();
  }

  async _charger() {
    const $c = this.$root.find('[data-role="contenu"]');
    if (this.cache[this.mois]) {
      this._periode(this.cache[this.mois]);
      $c.html(this._rendre(this.cache[this.mois]));
      this._recalculer_apercu();
      this._charger_consolide();
      return;
    }
    $c.html('<div class="pe-chargement">Chargement…</div>');
    try {
      const r = await frappe.call({
        method: "bank_retenue_sync.api.partenaire.get_tableau",
        args: { mois: this.mois },
      });
      const d = r.message || {};
      this.cache[this.mois] = d;
      this._periode(d);
      $c.html(this._rendre(d));
      this._recalculer_apercu();
      this._charger_consolide();
    } catch (e) {
      $c.html(this._erreur(e));
    }
  }

  _periode(d) {
    const p = d && d.periode;
    this.$root.find('[data-role="periode"]').text(
      p ? `Période : ${p.debut} → ${p.fin}` : (d && d.libelle) || ""
    );
  }

  _rendre(d) {
    if (!d.disponible) {
      return `<div class="pe-note alerte">${this._esc(d.message || "Donnée indisponible.")}</div>`;
    }
    const a = d.bilan.aqua;
    const p = d.bilan.partenaire;
    const tc = d.totaux_commandes;

    const kpis = this._kpis([
      ["Bénéfice Aqua World", this._m(a.benefice), `ventes ${this._m(a.ventes)}`, ""],
      ["Bénéfice Economiq", this._m(p.benefice), `ventes ${this._m(p.ventes)}`, ""],
      ["Achats Economiq", this._m(p.achats), "coût des commandes", ""],
      ["Ajustement", this._m(d.ajustement), "solde net + charges, déduit de l’échéancier",
        d.ajustement > 0 ? "warn" : "ok"],
      ["Commandes du mois", tc.nombre, `${this._m(tc.total)} au total`, ""],
      ["Non payé", this._m(tc.non_paye), "dettes réelles", tc.non_paye > 0 ? "bad" : "ok"],
    ]);

    const avert = `<div class="pe-note">Les charges se saisissent ici ; l’ajustement et
      l’échéancier en découlent et ne se saisissent jamais. <b>Deux gestes écrivent en
      comptabilité</b> — créer l’écriture de bilan, et saisir un règlement reçu. Tous deux
      suppriment des pièces de dette validées et demandent confirmation en les nommant.</div>`;

    const etat = d.valide
      ? `<div class="pe-note">Mois <b>validé</b> — le bilan et l’échéancier sont figés. Ils ne se
         recalculent plus depuis ERPNext, même si des pièces s’y ajoutent.</div>`
      : (d.enregistre ? "" :
        `<div class="pe-note">Mois <b>non enregistré</b> : ce que tu vois est calculé à
         l’instant. Enregistre-le pour qu’il compte dans le consolidé.</div>`);
    const trou = d.manquants
      ? `<div class="pe-note alerte">Mois manquants avant celui-ci : <b>${this._esc(d.manquants)}</b>.
         Le consolidé sera faux tant qu’ils ne sont pas enregistrés — le report de chacun se
         déverse sur les échéances des suivants.</div>` : "";

    const charges = `<div class="pe-scroll"><table class="pe-tbl" data-role="charges">
      <thead><tr><th>Libellé</th><th class="num">Montant</th><th></th></tr></thead>
      <tbody>${(d.charges_libres || []).map((c) => this._ligne_charge(c)).join("")}</tbody>
      </table></div>
      <div class="pe-act">
        <button data-action="ajouter-charge" ${d.valide ? "disabled" : ""}>+ Charge</button>
        <label style="font-size:12px;color:var(--text-muted);">Ajustement</label>
        <b data-role="ajustement" style="font-variant-numeric:tabular-nums;">${this._m(d.ajustement)}</b>
        <span class="muted" style="font-size:11.5px;">= solde net du bilan + charges, déduit de
          l’échéancier à partir de la première échéance. Ne se saisit pas.</span>
        <span style="flex:1"></span>
        <button data-action="rapport">Rapport du mois…</button>
        <button data-action="enregistrer" class="fort" ${d.valide ? "disabled" : ""}>
          Enregistrer</button>
        <button data-action="valider" ${d.valide ? "disabled" : ""}>Enregistrer et figer</button>
      </div>
      <div class="pe-note" data-role="apercu"></div>`;

    const bilan = `<div class="pe-scroll"><table class="pe-tbl">
      <thead><tr><th>Société</th><th class="num">Ventes</th><th class="num">Achats</th>
        <th class="num">Bénéfice</th><th class="num">Espèces</th><th class="num">Reste dû</th>
        </tr></thead><tbody>
        <tr><td>Aqua World &amp; Servicing</td><td class="num">${this._m(a.ventes)}</td>
          <td class="num">${this._m(a.achats)}</td>
          <td class="num"><b>${this._m(a.benefice)}</b></td>
          <td class="num">${this._m(a.especes)}</td><td class="num">${this._m(a.reste)}</td></tr>
        <tr><td>Economiq Aqua Solution</td><td class="num">${this._m(p.ventes)}</td>
          <td class="num">${this._m(p.achats)}</td>
          <td class="num"><b>${this._m(p.benefice)}</b></td>
          <td class="num">${this._m(p.especes)}</td><td class="num">${this._m(p.reste)}</td></tr>
      </tbody></table></div>`;

    const ecriture = this._ecriture(d);

    const ech = `<div class="pe-scroll"><table class="pe-tbl">
      <thead><tr><th>Échéance</th><th class="num">Brut</th><th class="num">Absorbé</th>
        <th class="num">Dû</th><th>Statut</th><th>Note</th></tr></thead>
      <tbody>${(d.echeancier || []).map((e) => {
        const absorbee = (e.statut || "") === "absorbé";
        return `<tr class="${absorbee ? "pe-absorbee" : ""}">
          <td>${e.date}</td>
          <td class="num">${this._m((e.montant || 0) + (e.deduit || 0))}</td>
          <td class="num">${e.deduit ? this._m(e.deduit) : "—"}</td>
          <td class="num"><b>${this._m(e.montant)}</b></td>
          <td><span class="pe-badge ${e.statut === "payé" ? "ok"
            : absorbee ? "neutre" : "bad"}">${this._esc(e.statut || "non_payé")}</span></td>
          <td class="muted">${this._esc(e.note || "")}</td></tr>`;
      }).join("")}</tbody>
      <tfoot><tr><td colspan="3">Report au consolidé</td>
        <td class="num">${this._m(d.report)}</td><td colspan="2"></td></tr></tfoot>
      </table></div>`;

    const commandes = (d.commandes || []).length
      ? `<div class="pe-scroll"><table class="pe-tbl"><thead><tr>
          <th>Commande</th><th>Date</th><th>Statut</th><th class="num">Total</th>
          <th class="num">Encaissé</th><th class="num">Non payé</th><th class="num">Restant</th>
          <th>Règlements</th></tr></thead><tbody>
          ${d.commandes.map((c) => `<tr>
            <td>${this._lien("Sales Order", c.sales_order)}</td>
            <td class="muted">${c.date}</td>
            <td><span class="pe-badge neutre">${this._esc(c.statut || "")}</span></td>
            <td class="num">${this._m(c.total)}</td>
            <td class="num">${this._m(c.encaisse)}</td>
            <td class="num">${c.non_paye
              ? `<span class="pe-badge bad">${this._m(c.non_paye)}</span>` : "—"}</td>
            <td class="num">${this._m(c.restant)}</td>
            <td class="muted">${this._esc((c.reglements || [])
              .map((r) => `${r.mode || "?"} ${this._m(r.montant)}`).join(" · "))}</td>
          </tr>`).join("")}</tbody>
          <tfoot><tr><td colspan="3">${tc.nombre} commande(s)</td>
            <td class="num">${this._m(tc.total)}</td><td class="num">${this._m(tc.encaisse)}</td>
            <td class="num">${this._m(tc.non_paye)}</td><td class="num">${this._m(tc.restant)}</td>
            <td></td></tr></tfoot></table></div>`
      : '<div class="pe-vide">Aucune commande du partenaire sur ce mois.</div>';

    return kpis + avert + etat + trou
      + this._sous("Bilan d’activité") + bilan
      + this._sous("Écriture de bilan") + ecriture
      + this._sous("Charges libres du mois") + charges
      + this._sous("Échéancier") + ech
      + this._sous("Solde consolidé inter-mois")
      + `<div data-role="consolide"><div class="pe-chargement">Chargement…</div></div>`
      + this._sous("Commandes du partenaire") + commandes;
  }

  /** L’écriture de bilan du mois — celle qui fixe l’ajustement.
   *
   * ⚠️ ON MONTRE L’ÉCART, ON NE LE MASQUE PAS. L’ajustement calculé repose sur des achats
   * sous-évalués tant que `tabItem Price` est vide ; sans cette ligne, l’écran afficherait un
   * chiffre issu de la comptabilité et personne ne saurait que le calcul, lui, en donne un
   * autre — ni de combien.
   */
  _ecriture(d) {
    const e = d.ecriture;
    if (!e) {
      return `<div class="pe-note alerte">Aucune écriture de bilan sur ce mois. L’ajustement
        affiché (<b>${this._m(d.ajustement)}</b>) est <b>calculé</b>, et les prix d’achat
        manquent en base : les bénéfices sont surévalués, donc l’ajustement aussi. Passe
        l’écriture avant de figer le mois.</div>
        <div class="pe-act"><button data-action="creer-ecriture" class="fort">
          Créer le brouillon d’écriture</button>
          <span class="muted" style="font-size:11.5px;">brouillon uniquement — tu relis et tu
          soumets dans ERPNext</span></div>`;
    }
    // ⚠️ COMPARER L’ÉCRITURE AU CALCUL, PAS AU MOIS ENREGISTRÉ. Un mois figé porte le montant
    // qui a été annoncé au partenaire et n’a pas à s’aligner sur quoi que ce soit ; le comparer
    // ferait crier l’écran sur le mois d’ancrage, dont l’échéancier est repris et n’a pas de
    // bilan derrière.
    const ecart = (e.ajustement || 0) - (d.ajustement_calcule || 0);
    const alerte = Math.abs(ecart) > 0.001
      ? `<div class="pe-note alerte">L’ajustement retenu vient de l’écriture
         (<b>${this._m(e.ajustement)}</b>). Le calcul depuis le bilan donnerait
         <b>${this._m(d.ajustement_calcule)}</b>, soit <b>${this._m(Math.abs(ecart))}</b> d’écart —
         les prix d’achat manquent en base, les bénéfices sont surévalués. C’est l’écriture qui
         fait foi.</div>`
      : "";
    const perime = (!d.valide && d.enregistre
                    && Math.abs((d.ajustement || 0) - (e.ajustement || 0)) > 0.001)
      ? `<div class="pe-note alerte">Le mois enregistré porte un ajustement de
         <b>${this._m(d.ajustement)}</b>, l’écriture dit <b>${this._m(e.ajustement)}</b>.
         Ré-enregistre le mois pour que l’échéancier reparte de la comptabilité.</div>`
      : "";
    const autres = (e.autres || []).length
      ? `<div class="pe-note alerte">Plusieurs écritures de bilan sur ce mois :
         ${e.autres.map((n) => this._lien("Journal Entry", n)).join(", ")}. Seule la plus récente
         est retenue.</div>` : "";
    return alerte + perime + autres + `<div class="pe-scroll"><table class="pe-tbl">
      <thead><tr><th>Compte</th><th>Tiers</th><th>Remarque</th>
        <th class="num">Débit</th><th class="num">Crédit</th></tr></thead>
      <tbody>${(e.lignes || []).map((l) => `<tr>
        <td>${this._esc(l.compte)}</td><td class="muted">${this._esc(l.party || "")}</td>
        <td class="muted">${this._esc(l.remarque || "")}</td>
        <td class="num">${l.debit ? this._m(l.debit) : "—"}</td>
        <td class="num">${l.credit ? this._m(l.credit) : "—"}</td></tr>`).join("")}</tbody>
      <tfoot><tr><td colspan="3">${this._lien("Journal Entry", e.journal_entry)}
        <span class="muted">— ${this._esc(e.date)}</span></td>
        <td colspan="2" class="num">Ajustement <b>${this._m(e.ajustement)}</b></td></tr></tfoot>
      </table></div>`;
  }

  _ligne_charge(c) {
    return `<tr>
      <td><input data-charge="libelle" value="${this._esc((c && c.libelle) || "")}"
                 placeholder="Salaire Fatma"></td>
      <td class="num"><input data-charge="montant" class="num" type="number" step="0.001"
                 value="${(c && c.montant) || 0}"></td>
      <td style="width:28px;"><button class="pe-x" title="Retirer">×</button></td>
    </tr>`;
  }

  _ajouter_charge() {
    this.$root.find('[data-role="charges"] tbody').append(this._ligne_charge(null));
  }

  /** Les charges saisies à l’écran, telles qu’elles partiront au serveur. */
  _charges_saisies() {
    const out = [];
    this.$root.find('[data-role="charges"] tbody tr').each((_, tr) => {
      const libelle = ($(tr).find('[data-charge="libelle"]').val() || "").trim();
      const montant = parseFloat($(tr).find('[data-charge="montant"]').val()) || 0;
      if (libelle) out.push({ libelle, montant });
    });
    return out;
  }

  /** Aperçu immédiat : l’ajustement suit la saisie des charges, sans attendre l’enregistrement.
   *
   * ⚠️ MÊME FORMULE QUE LE SERVEUR, PAS UNE APPROXIMATION. `echeancier.ajustement` fait
   * solde net + charges ; un aperçu qui calculerait autrement annoncerait un montant que
   * l’enregistrement contredirait aussitôt.
   */
  _recalculer_apercu() {
    const d = this.cache[this.mois];
    if (!d) return;
    const total = this._charges_saisies().reduce((s, c) => s + c.montant, 0);
    const solde = (d.bilan.aqua.benefice || 0) - (d.bilan.partenaire.benefice || 0);
    const ajust = solde + total;
    this.$root.find('[data-role="ajustement"]').text(this._m(ajust));
    this.$root.find('[data-role="apercu"]').html(
      `Aperçu : solde net <b>${this._m(solde)}</b> + charges <b>${this._m(total)}</b>
       → ajustement <b>${this._m(ajust)}</b>, déduit de l’échéancier dès la première échéance.
       <span class="muted">Enregistre pour figer le calcul et alimenter le consolidé.</span>`);
  }

  async _enregistrer(valide) {
    if (valide && !(await new Promise((r) => frappe.confirm(
      `Figer ${this._esc(this.mois)} ? Le bilan et l’échéancier ne se recalculeront plus.`,
      () => r(true), () => r(false))))) return;
    try {
      await frappe.call({
        method: "bank_retenue_sync.api.partenaire.enregistrer",
        args: { mois: this.mois, charges: this._charges_saisies(), valide },
        freeze: true, freeze_message: __("Enregistrement…"),
      });
      frappe.show_alert({ message: __("Mois {0} enregistré", [this.mois]), indicator: "green" });
      delete this.cache[this.mois];
      this._charger();
    } catch (e) {
      frappe.msgprint({ title: __("Enregistrement impossible"), message: String(e),
                        indicator: "red" });
    }
  }

  /** Le brouillon d’écriture de bilan, saisi puis posé en docstatus 0.
   *
   * ⚠️ LES DEUX MONTANTS DOUTEUX SONT ÉDITABLES, PAS MASQUÉS. Bénéfice Aqua et achats Economiq
   * dépendent de prix d’achat absents de la base ; les pré-remplir sans le dire ferait signer
   * un chiffre faux, les cacher empêcherait de le corriger.
   */
  async _creer_ecriture() {
    let p;
    try {
      p = (await frappe.call({ method: "bank_retenue_sync.api.partenaire.preparer_ecriture",
                               args: { mois: this.mois }, freeze: true })).message;
    } catch (e) {
      frappe.msgprint({ title: __("Préparation impossible"), message: String(e),
                        indicator: "red" });
      return;
    }

    const d = new frappe.ui.Dialog({
      title: __("Écriture de bilan — {0}", [p.libelle]),
      fields: [
        { fieldtype: "HTML", fieldname: "avert", options:
          `<div style="font-size:12px;padding:8px 10px;border-radius:6px;
             border:1px solid rgba(192,57,43,.4);background:rgba(192,57,43,.07);">
             Les deux premiers montants sortent du bilan recalculé, dont les <b>prix d’achat
             manquent en base</b> : ils sont probablement faux. Corrige-les avant de créer le
             brouillon. Les ventes, elles, sont fiables.</div>` },
        { fieldtype: "Currency", fieldname: "benefice_aqua", reqd: 1,
          label: __("Bénéfice Aqua World (débit) ⚠"), default: p.benefice_aqua.montant },
        { fieldtype: "Currency", fieldname: "achats_partenaire", reqd: 1,
          label: __("Achats Economiq (débit) ⚠"), default: p.achats_partenaire.montant },
        { fieldtype: "Currency", fieldname: "ventes_partenaire", reqd: 1,
          label: __("Ventes Economiq (crédit)"), default: p.ventes_partenaire.montant },
        { fieldtype: "Check", fieldname: "auto", label: __("Valider automatiquement l’écriture "
          + "et les pièces de dette recréées"), default: p.auto_validation ? 1 : 0,
          description: __("Réglage global, conservé d’une fois sur l’autre. Décoché, tout reste "
                          + "en brouillon et tu soumets dans ERPNext.") },
        { fieldtype: "HTML", fieldname: "resume" },
      ],
      primary_action_label: __("Créer l’écriture"),
      primary_action: async (v) => {
        d.hide();
        try {
          await frappe.call({ method: "bank_retenue_sync.api.partenaire.set_auto_validation",
                              args: { actif: v.auto ? 1 : 0 } });
          if (!(await this._confirmer_liberation(p))) return;
          const r = (await frappe.call({
            method: "bank_retenue_sync.api.partenaire.executer_ecriture",
            args: { mois: this.mois, benefice_aqua: v.benefice_aqua,
                    achats_partenaire: v.achats_partenaire,
                    ventes_partenaire: v.ventes_partenaire, charges: p.charges,
                    liberer: p.non_impute > 0.001 ? 1 : 0 },
            freeze: true, freeze_message: __("Écriture en cours…") })).message;
          const detruites = (r.supprimees || []).length
            ? __("<br>Pièces supprimées : {0}", [r.supprimees.join(", ")]) : "";
          const refaites = (r.recreees || []).length
            ? __("<br>Dette recréée : {0}", [r.recreees.map(
                (x) => `${x.payment_entry} (${this._m(x.montant)})`).join(", ")]) : "";
          frappe.msgprint({
            title: r.validee ? __("Écriture validée") : __("Brouillon créé"), indicator: "green",
            message: __("{0} — ajustement {1}.{2}{3}",
                        [frappe.utils.get_form_link("Journal Entry", r.journal_entry, true),
                         this._m(r.ajustement), detruites, refaites]) });
          delete this.cache[this.mois];
          this._charger();
        } catch (e) {
          frappe.msgprint({ title: __("Création impossible"), message: String(e),
                            indicator: "red" });
        }
      },
    });

    const resume = () => {
      const v = d.get_values(true) || {};
      const eq = (v.benefice_aqua || 0) + (v.achats_partenaire || 0) + (p.total_charges || 0)
        - (v.ventes_partenaire || 0);
      // ⚠️ LA RÉPARTITION VIENT DU SERVEUR ET NE SUIT PAS LA SAISIE. Elle a été calculée sur
      // l’ajustement proposé ; si tu corriges les montants, elle ne colle plus et le serveur
      // refuse la pièce. C’est voulu — recalculer ici en silence produirait une répartition que
      // personne n’a vérifiée contre le crédit restant des commandes.
      const decale = Math.abs(eq - (p.equilibre || 0)) > 0.001;
      const plan = (p.repartition || []).length
        ? p.repartition.map((r) => `${r.sales_order} → ${this._m(r.montant)}`).join("<br>")
        : `<span style="color:#a93226;">aucune commande ne peut absorber l’ajustement</span>`;
      d.get_field("resume").$wrapper.html(
        `<table style="width:100%;font-size:12.5px;border-collapse:collapse;">
          <tr><td>Charges libres du mois (${p.charges.length})</td>
            <td style="text-align:right;">${this._m(p.total_charges)}</td></tr>
          <tr><td colspan="2" style="color:var(--text-muted);font-size:11px;">
            ${p.charges.map((c) => this._esc(c.libelle)).join(" · ") || "aucune"}</td></tr>
          <tr><td style="padding-top:6px;border-top:1px solid var(--border-color);">
            <b>Ajustement (ligne au débiteur)</b></td>
            <td style="text-align:right;padding-top:6px;border-top:1px solid var(--border-color);">
            <b>${this._m(eq)}</b></td></tr>
          <tr><td colspan="2" style="padding-top:6px;">Réduction imputée sur les commandes
            en dette :<br>${plan}</td></tr>
          ${p.non_impute > 0.001 ? `<tr><td colspan="2" style="color:#a93226;font-size:11.5px;">
            ${this._m(p.non_impute)} ne peut être imputé : les commandes en dette sont saturées
            par leur pièce de dette. Il faut annuler la pièce avant.</td></tr>` : ""}
          ${decale ? `<tr><td colspan="2" style="color:#a93226;font-size:11.5px;">
            Tu as modifié les montants : la répartition ci-dessus vise
            ${this._m(p.equilibre)}, pas ${this._m(eq)}. La création sera refusée — relance la
            préparation après avoir corrigé le bilan.</td></tr>` : ""}
          <tr><td colspan="2" style="color:var(--text-muted);font-size:11px;">
            écriture datée du ${this._esc(p.date)} · ${this._esc(p.societe)}</td></tr>
        </table>`);
    };
    ["benefice_aqua", "achats_partenaire", "ventes_partenaire"].forEach((f) => {
      d.get_field(f).df.onchange = resume;
    });
    d.show();
    resume();
  }

  /** Saisir un règlement reçu du partenaire, et voir son affectation avant d’écrire.
   *
   * ⚠️ ON MONTRE L’AFFECTATION AVANT DE CRÉER. Les pièces existantes ne livrent pas de règle
   * d’ordre stable ; c’est l’écran qui tranche, et l’utilisateur doit voir quelles commandes
   * son versement va éteindre avant que la pièce parte en comptabilité.
   */
  async _saisir_reglement() {
    let p;
    const recharger = async (v) => {
      p = (await frappe.call({ method: "bank_retenue_sync.api.partenaire.preparer_paiement",
        args: { montant: v.montant || 0, date: v.date } })).message;
      return p;
    };
    try {
      p = (await frappe.call({ method: "bank_retenue_sync.api.partenaire.preparer_paiement",
        args: { montant: 0, date: frappe.datetime.get_today() }, freeze: true })).message;
    } catch (e) {
      frappe.msgprint({ title: __("Préparation impossible"), message: String(e),
                        indicator: "red" });
      return;
    }
    if (!(p.comptes || []).length) {
      frappe.msgprint({ title: __("Aucun compte connu"), indicator: "red",
        message: __("Aucun règlement du partenaire en base : impossible de proposer un mode et "
                    + "un compte cohérents avec la banque.") });
      return;
    }

    // ⚠️ LE COMPTE D’ABORD, LE MODE ENSUITE. La question que se pose celui qui saisit est
    // « où est arrivé l’argent ? » — sur le compte courant, sur Tawfir, en caisse. Le mode de
    // paiement n’est qu’une conséquence de ce choix, et le mettre en tête faisait chercher une
    // information que le relevé ne donne jamais sous cette forme.
    const etiquette = (c) => `${c.compte} — ${c.mode}`;
    const d = new frappe.ui.Dialog({
      title: __("Règlement reçu d’Economiq Aqua Solution"),
      fields: [
        { fieldtype: "Select", fieldname: "compte", label: __("Où l’argent est arrivé"), reqd: 1,
          options: p.comptes.map(etiquette).join("\n"), default: etiquette(p.comptes[0]),
          description: __("Seuls les comptes déjà servis par ce client sont proposés : une "
                          + "combinaison inconnue de la banque ne se retrouverait pas au "
                          + "rapprochement.") },
        { fieldtype: "Currency", fieldname: "montant", label: __("Montant reçu"), reqd: 1 },
        { fieldtype: "Date", fieldname: "date", label: __("Date"), reqd: 1,
          default: frappe.datetime.get_today() },
        { fieldtype: "Data", fieldname: "reference", label: __("Référence bancaire"),
          description: __("N° de chèque, de virement ou de transaction — celui que la banque "
                          + "affichera au rapprochement.") },
        { fieldtype: "HTML", fieldname: "apercu" },
      ],
      primary_action_label: __("Créer le règlement"),
      primary_action: async (v) => {
        const choisi = p.comptes[p.comptes.map(etiquette).indexOf(v.compte)];
        if (!choisi) {
          frappe.msgprint({ title: __("Compte inconnu"), indicator: "red",
                            message: __("Choisis un compte dans la liste.") });
          return;
        }
        // ⚠️ ON RECALCULE LE PLAN AVANT DE CONFIRMER, PAS APRÈS. L’aperçu est temporisé de
        // 250 ms : un clic rapide après la saisie du montant confirmerait un plan périmé, que
        // le serveur refuserait ensuite au motif que les dettes ont bougé — alors que rien
        // n’avait bougé, sinon l’écran lui-même.
        const bouton = d.get_primary_btn();
        bouton.prop("disabled", true);
        try {
          p = await recharger(v);
        } catch (e) {
          bouton.prop("disabled", false);
          frappe.msgprint({ title: __("Préparation impossible"), message: String(e),
                            indicator: "red" });
          return;
        }
        if (!(await this._confirmer_reglement(p))) {
          bouton.prop("disabled", false);
          return;
        }
        d.hide();
        try {
          const r = (await frappe.call({
            method: "bank_retenue_sync.api.partenaire.creer_paiement",
            args: { montant: v.montant, date: v.date, mode: choisi.mode, compte: choisi.compte,
                    reference: v.reference, empreinte: p.empreinte },
            freeze: true, freeze_message: __("Création du règlement…") })).message;
          const avance = r.avance > 0.001
            ? __("<br>{0} en avance : le versement dépasse les dettes en cours.",
                 [this._m(r.avance)]) : "";
          const remplacees = (r.supprimees || []).length
            ? __("<br>Dettes remplacées : {0}", [r.supprimees.join(", ")]) : "";
          const restantes = (r.recreees || []).length
            ? __("<br>Dette restante : {0}", [r.recreees.map(
                (x) => `${x.payment_entry} (${this._m(x.montant)})`).join(", ")]) : "";
          frappe.msgprint({
            title: r.validee ? __("Règlement validé") : __("Règlement en brouillon"),
            indicator: "green",
            message: __("{0} — {1} sur {2} dette(s).{3}{4}{5}",
                        [frappe.utils.get_form_link("Payment Entry", r.payment_entry, true),
                         this._m(r.montant), r.repartition.length, avance, remplacees,
                         restantes]) });
          this._charger_consolide();
        } catch (e) {
          frappe.msgprint({ title: __("Création impossible"), message: String(e),
                            indicator: "red" });
        }
      },
    });

    let enCours = null;
    const apercu = async () => {
      const v = d.get_values(true) || {};
      if (enCours) clearTimeout(enCours);
      enCours = setTimeout(async () => {
        const q = await recharger(v);
        // ⚠️ DIRE POURQUOI LE TABLEAU EST VIDE. Les commandes cibles sont celles ANTÉRIEURES à
        // la date saisie : un règlement daté avant la première commande en dette n’a rien à
        // éteindre et part intégralement en avance. Un tableau vide sans explication laisse
        // croire à une panne.
        const vide = !(q.cibles || []).length
          ? __("aucune dette ouverte à la date du {0} : la totalité irait en avance",
               [this._esc(q.date)])
          : __("saisis un montant");
        const lignes = (q.repartition || []).length
          ? q.repartition.map((r) => `<tr><td>${this._esc(r.sales_order)}</td>
              <td class="muted">${this._esc(r.date)}</td>
              <td style="text-align:right;">${this._m(r.dette_avant)}</td>
              <td style="text-align:right;"><b>${this._m(r.regle)}</b></td>
              <td style="text-align:right;">${this._m(r.dette_apres)}</td></tr>`).join("")
          : `<tr><td colspan="5" style="color:var(--text-muted);">${vide}</td></tr>`;
        d.get_field("apercu").$wrapper.html(
          `<table style="width:100%;font-size:12.5px;border-collapse:collapse;">
            <thead><tr style="text-align:left;color:var(--text-muted);font-size:11px;">
              <th>Commande</th><th>Date</th><th style="text-align:right;">Dette</th>
              <th style="text-align:right;">Réglé</th><th style="text-align:right;">Reste</th>
            </tr></thead><tbody>${lignes}</tbody></table>
           <div style="color:var(--text-muted);font-size:11px;margin-top:4px;">
             dette totale ${this._m(q.dette_totale)} sur ${q.cibles.length} commande(s)
             ${q.avance > 0.001
               ? ` · <span style="color:#9c6f00;">${this._m(q.avance)} irait en avance</span>`
               : ""}
             · validation auto : ${q.auto_validation ? "oui" : "non"}</div>`);
      }, 250);
    };
    ["montant", "date"].forEach((f) => { d.get_field(f).df.onchange = apercu; });
    d.show();
    apercu();
  }

  /** La confirmation d’un règlement — il détruit des pièces de dette validées.
   *
   * ⚠️ MÊME EXIGENCE QUE POUR LA LIBÉRATION : nommer les pièces. Un règlement ne se contente pas
   * de s’ajouter, il REMPLACE la dette de la commande ; l’utilisateur doit voir laquelle
   * disparaît et ce qui la remplace avant que ça parte en comptabilité.
   */
  async _confirmer_reglement(p) {
    if (!(p.repartition || []).length) return true;
    const pieces = p.repartition.flatMap((r) => r.pieces || []);
    if (!pieces.length) return true;
    const lignes = p.repartition.map((r) => `<tr>
      <td>${this._esc(r.sales_order)}</td>
      <td class="muted">${this._esc((r.pieces || []).join(", "))}</td>
      <td style="text-align:right;">${this._m(r.dette_avant)}</td>
      <td style="text-align:right;">${this._m(r.regle)}</td>
      <td style="text-align:right;">${this._m(r.dette_apres)}</td></tr>`).join("");
    return new Promise((r) => {
      // ⚠️ RÉPONDRE AVANT DE FERMER : hide() déclenche onhide, qui résout au refus.
      let repondu = false;
      const fini = (v) => { if (!repondu) { repondu = true; r(v); } };
      const c = new frappe.ui.Dialog({
        title: __("Remplacer {0} pièce(s) de dette ?", [pieces.length]),
        fields: [{ fieldtype: "HTML", fieldname: "corps", options:
          `<div style="font-size:12.5px;">
            <p><b>Ce règlement remplace de la dette.</b> Les pièces ci-dessous seront annulées
            puis supprimées, le règlement créé à leur place, et le solde de dette recréé.</p>
            <table style="width:100%;border-collapse:collapse;font-size:12px;">
              <thead><tr style="text-align:left;">
                <th>Commande</th><th>Pièce de dette</th><th style="text-align:right;">Avant</th>
                <th style="text-align:right;">Réglé</th><th style="text-align:right;">Après</th>
              </tr></thead><tbody>${lignes}</tbody></table>
            <p style="color:var(--text-muted);">Validation auto :
            ${p.auto_validation ? "oui" : "non"}</p>
          </div>` }],
        primary_action_label: __("Remplacer et créer"),
        primary_action: () => { fini(true); c.hide(); },
        secondary_action_label: __("Annuler"),
        secondary_action: () => { fini(false); c.hide(); },
        onhide: () => fini(false),
      });
      c.show();
    });
  }

  /** La confirmation avant destruction — nommer chaque pièce, ou ne pas détruire.
   *
   * ⚠️ UN « ÊTES-VOUS SÛR ? » NE SUFFIT PAS ICI. On supprime des Payment Entry validées ;
   * personne ne peut consentir à ça sans voir lesquelles, pour combien, et ce qui sera recréé
   * en face. Un refus laisse la base intacte : rien n’a encore été touché à ce stade.
   */
  async _confirmer_liberation(p) {
    if (!(p.non_impute > 0.001)) return true;
    let plan;
    try {
      plan = (await frappe.call({
        method: "bank_retenue_sync.api.partenaire.get_plan_liberation",
        args: { mois: this.mois, ajustement: p.non_impute }, freeze: true })).message;
    } catch (e) {
      frappe.msgprint({ title: __("Plan indisponible"), message: String(e), indicator: "red" });
      return false;
    }
    if (!plan.suffisant) {
      frappe.msgprint({ title: __("Libération impossible"), indicator: "red",
        message: __("Les pièces de dette disponibles ne dégagent que {0} sur les {1} "
                    + "nécessaires. Rien n’a été supprimé.",
                    [this._m(plan.degage), this._m(p.non_impute)]) });
      return false;
    }
    const lignes = plan.a_supprimer.map((c) => `<tr>
      <td>${this._esc(c.payment_entry)}</td><td>${this._esc(c.sales_order)}</td>
      <td style="text-align:right;">${this._m(c.montant)}</td>
      <td style="text-align:right;">${this._m(c.montant - p.non_impute > 0
        ? c.montant - p.non_impute : 0)}</td></tr>`).join("");
    return new Promise((r) => {
      // ⚠️ RÉPONDRE AVANT DE FERMER, JAMAIS L’INVERSE. `hide()` déclenche `onhide`, donc
      // `fini(false)` : écrire `c.hide(); fini(true);` verrouille le drapeau sur le refus et le
      // bouton ne fait plus rien du tout, sans la moindre erreur. Le drapeau seul ne suffit
      // pas — c’est l’ordre des deux appels qui décide.
      let repondu = false;
      const fini = (v) => { if (!repondu) { repondu = true; r(v); } };
      const c = new frappe.ui.Dialog({
        title: __("Supprimer {0} pièce(s) de dette ?", [plan.a_supprimer.length]),
        fields: [{ fieldtype: "HTML", fieldname: "corps", options:
          `<div style="font-size:12.5px;">
            <p><b>Cette suppression est définitive.</b> Les pièces ci-dessous seront annulées
            puis supprimées pour libérer les commandes, l’écriture de bilan sera posée, puis la
            dette restante recréée.</p>
            <table style="width:100%;border-collapse:collapse;font-size:12px;">
              <thead><tr style="text-align:left;">
                <th>Pièce</th><th>Commande</th><th style="text-align:right;">Dette</th>
                <th style="text-align:right;">Recréée</th></tr></thead>
              <tbody>${lignes}</tbody>
            </table>
            <p style="color:var(--text-muted);">À imputer : ${this._m(p.non_impute)} ·
            dégagé : ${this._m(plan.degage)} ·
            validation auto : ${plan.auto_validation ? "oui" : "non"}</p>
          </div>` }],
        primary_action_label: __("Supprimer et poursuivre"),
        primary_action: () => { fini(true); c.hide(); },
        secondary_action_label: __("Annuler"),
        secondary_action: () => { fini(false); c.hide(); },
        onhide: () => fini(false),
      });
      c.show();
    });
  }

  async _charger_consolide() {
    const $c = this.$root.find('[data-role="consolide"]');
    if (!$c.length) return;
    try {
      const c = (await frappe.call({
        method: "bank_retenue_sync.api.partenaire.get_consolide" })).message || {};
      if (!(c.lignes || []).length) {
        $c.html('<div class="pe-vide">Aucun mois enregistré : le consolidé est vide.</div>');
        return;
      }
      const excedent = c.excedent > 0.001
        ? `<div class="pe-note">Excédent non imputé : <b>${this._m(c.excedent)}</b> —
           plus d’échéance ouverte à éteindre. Il s’imputera sur le prochain mois enregistré.</div>`
        : "";
      const dettes = (c.dettes || []).length
        ? `<div class="pe-note alerte">${c.dettes.length} pièce(s) portée(s) au compte des dettes
           depuis le ${this._esc(c.depuis)}, pour <b>${this._m(
             c.dettes.reduce((s, d) => s + d.montant, 0))}</b>. Ce n’est pas de l’argent reçu :
           rien n’est imputé dessus.</div>`
        : "";
      const saisie = `<div class="pe-act">
        <button data-action="saisir-reglement" class="fort">+ Règlement reçu</button>
        <span class="muted" style="font-size:11.5px;">tu indiques où l’argent est arrivé et
          combien ; la pièce est créée dans ERPNext et les dettes ajustées. Le consolidé
          l’impute au prochain chargement.</span></div>`;
      $c.html(saisie + excedent + dettes + `<div class="pe-scroll"><table class="pe-tbl">
        <thead><tr><th>Échéance</th><th class="num">Dû</th><th class="num">Payé</th>
          <th class="num">Reste</th><th>Statut</th><th>Règlements</th>
          <th>D’où ça vient</th></tr></thead>
        <tbody>${c.lignes.map((l) => `<tr>
          <td>${l.date}</td><td class="num"><b>${this._m(l.montant)}</b></td>
          <td class="num">${l.paye ? this._m(l.paye) : "—"}</td>
          <td class="num">${l.reste ? this._m(l.reste) : "—"}</td>
          <td><span class="pe-badge ${l.statut === "payé" ? "ok"
            : l.statut === "partiel" ? "neutre" : "bad"}">${this._esc(l.statut)}</span></td>
          <td class="muted" style="font-size:11px;">${(l.reglements || []).length
            ? l.reglements.map((r) => `${this._lien("Payment Entry", r.payment_entry)}
                ${this._m(r.impute)} <span class="muted">${this._esc(r.date)}</span>`).join("<br>")
            : "—"}</td>
          <td class="muted" style="font-size:11px;">${this._esc(l.detail)}</td></tr>`).join("")}
        </tbody>
        <tfoot><tr><td>${c.mois_enregistres.length} mois</td>
          <td class="num">${this._m(c.total)}</td><td class="num">${this._m(c.paye)}</td>
          <td class="num">${this._m(c.reste)}</td><td colspan="3"></td></tr></tfoot>
        </table></div>`);
    } catch (e) {
      $c.html(this._erreur(e));
    }
  }

  /** Le rapport mensuel : on le RELIT avant de le faire rédiger, et avant qu il ne soit posé.
   *
   * ⚠️ LES CHIFFRES NE VIENNENT PAS DU MODÈLE. L aperçu ci-dessous est déjà définitif : il est
   * rendu depuis les mêmes données que cet écran. OpenAI n écrit que les phrases de commentaire,
   * et une phrase citant un nombre absent des données est rejetée côté serveur.
   */
  async _rapport() {
    let apercu;
    try {
      apercu = await frappe.xcall("bank_retenue_sync.api.partenaire.apercu_rapport",
                                  { mois: this.mois });
    } catch (e) {
      return this._erreur(e);
    }
    const d = new frappe.ui.Dialog({
      title: __("Rapport mensuel — {0}", [apercu.libelle]),
      size: "extra-large",
      fields: [{ fieldname: "vue", fieldtype: "HTML" }],
      primary_action_label: __("Rédiger avec OpenAI et enregistrer"),
      primary_action: () => this._generer_rapport(d),
    });
    d.fields_dict.vue.$wrapper.html(this._vue_rapport(apercu));
    d.show();
  }

  _vue_rapport(apercu, resultat) {
    const notes = [];
    if (!apercu.enregistre) {
      notes.push(`<div class="pe-note alerte">${__(
        "Ce mois n’est pas encore enregistré : son échéancier est un calcul, pas un engagement communiqué au partenaire. Enregistre-le avant d’envoyer le rapport."
      )}</div>`);
    }
    if (apercu.commentaire_existant && !resultat) {
      notes.push(`<div class="pe-note">${__(
        "Un rapport existe déjà pour ce mois sur la fiche client : il sera remplacé."
      )}</div>`);
    }
    if (resultat) {
      const rejets = (resultat.rejetes || []).length
        ? `<br>${__("Phrases rejetées faute de correspondre aux données : {0}. Les phrases déterministes ont pris leur place.",
                    [this._esc((resultat.rejetes || []).join(", "))])}`
        : "";
      const ia = resultat.erreur_ia
        ? __("OpenAI n’a pas répondu ({0}) : le rapport porte les phrases déterministes.",
             [this._esc(resultat.erreur_ia)])
        : __("Rédigé par {0}.", [this._esc(resultat.modele || "—")]);
      notes.push(`<div class="pe-note">${__("Rapport {0} sur la fiche {1}.",
        [this._esc(resultat.statut || ""), this._esc(apercu.client || "ECONOMIQ AQUA SOLUTIONS")])}
        ${" " + ia}${rejets}</div>`);
    }
    const texte = (resultat && resultat.markdown) || apercu.markdown || "";
    // Le HTML vient du SERVEUR, du même convertisseur que le commentaire posé sur la fiche :
    // frappe.markdown instancie showdown sans l’extension « tables » et rendrait des pipes bruts.
    const html = (resultat && resultat.html) || apercu.html || "";
    return `${notes.join("")}
      <div style="max-height:52vh;overflow:auto;border:1px solid var(--border-color);
                  border-radius:8px;padding:12px">${html}</div>
      <details style="margin-top:10px">
        <summary style="cursor:pointer;font-size:12px;color:var(--text-muted)">${__(
          "Markdown source (à copier)"
        )}</summary>
        <pre style="max-height:30vh;overflow:auto;font-size:11px;white-space:pre-wrap">${this._esc(
          texte
        )}</pre>
      </details>`;
  }

  async _generer_rapport(d) {
    d.set_primary_action(__("Rédaction…"), () => {});
    d.disable_primary_action();
    let res;
    try {
      res = await frappe.xcall("bank_retenue_sync.api.partenaire.generer_rapport",
                               { mois: this.mois });
    } catch (e) {
      d.enable_primary_action();
      return this._erreur(e);
    }
    d.fields_dict.vue.$wrapper.html(this._vue_rapport(
      { libelle: res.libelle, markdown: res.markdown, html: res.html,
        enregistre: res.enregistre, client: res.client }, res));
    d.set_primary_action(__("Voir sur la fiche client"), () => {
      d.hide();
      frappe.set_route("Form", "Customer", res.client || "ECONOMIQ AQUA SOLUTIONS");
    });
    d.enable_primary_action();
    frappe.show_alert({ message: __("Rapport {0}.", [res.statut || ""]), indicator: "green" });
  }

  _kpis(liste) {
    return `<div class="pe-kpis">${liste.map(([lbl, val, sub, cls]) =>
      `<div class="pe-kpi ${cls || ""}"><div class="lbl">${this._esc(lbl)}</div>
       <div class="val">${val == null ? "—" : val}</div>
       <div class="sub">${this._esc(sub || "")}</div></div>`).join("")}</div>`;
  }

  _sous(titre) { return `<div class="pe-sous">${this._esc(titre)}</div>`; }

  _lien(doctype, nom) {
    if (!doctype || !nom) return '<span class="muted">—</span>';
    return `<a href="#" data-doc="${this._esc(doctype)}" data-nom="${this._esc(nom)}">${
      this._esc(nom)}</a>`;
  }

  _erreur(e) {
    return `<div class="pe-note alerte">Chargement impossible : ${this._esc(String(e))}</div>`;
  }

  _m(v) {
    const n = parseFloat(v);
    if (!isFinite(n)) return "—";
    return n.toLocaleString("fr-TN", { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  }

  _esc(s) { return frappe.utils.escape_html(s == null ? "" : String(s)); }
}
