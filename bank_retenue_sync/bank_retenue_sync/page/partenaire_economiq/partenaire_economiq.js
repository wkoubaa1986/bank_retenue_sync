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
    // La règle de comptage est PARTAGÉE avec l'écran « Bilan Vente » : on l'écrit, on ne la
    // garde pas pour soi. Sinon les deux écrans annonceraient deux bénéfices différents sur le
    // même mois — et l'ajustement se calculerait sur le mauvais (−516,010 contre +147,640 sur
    // août 2026).
    this.$root.on("change", '[data-f="base"], [data-f="ouvertes"]', () => this._changer_regle());
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

  /** Écrit la règle de comptage, puis relit TOUT.
   *
   * ⚠️ LE CACHE PAR MOIS DOIT SAUTER. `this.cache[mois]` garde le tableau déjà calculé ; sans
   * ce vidage, changer la règle laissait l’écran sur les anciens chiffres et donnait
   * l’impression que le réglage ne servait à rien.
   */
  async _changer_regle() {
    const base = this.$root.find('[data-f="base"]').val() || "livraison";
    const ouvertes = this.$root.find('[data-f="ouvertes"]').is(":checked") ? 1 : 0;
    try {
      await frappe.call({
        method: "customization_app.bilan_vente.set_regle",
        args: { base, exclure_ouvertes: ouvertes },
        freeze: true,
      });
    } catch (e) {
      frappe.show_alert({ message: __("Règle non enregistrée"), indicator: "red" });
      return;
    }
    this.cache = {};
    this.controle_bl = null;    // le périmètre des ventes Economiq suit la règle du bilan
    this.analyse = null;        // les ventes Economiq de chaque mois aussi
    this._charger();
  }

  /** Remet les deux contrôles sur la règle réellement appliquée par le serveur. */
  _refleter_regle(d) {
    const r = (d && d.regle_bilan) || {};
    this.$root.find('[data-f="base"]').val(r.base || "livraison");
    this.$root.find('[data-f="ouvertes"]').prop("checked", !!r.exclure_ouvertes);
    // Qui ne peut pas régler avec le partenaire ne change pas la règle du règlement.
    const fige = r.peut_regler === false;
    this.$root.find('[data-f="base"], [data-f="ouvertes"]')
      .prop("disabled", fige)
      .attr("title", fige
        ? "Reserve aux profils comptables"
        : "Regle partagee avec l ecran Bilan Vente");
  }

  async _charger() {
    const $c = this.$root.find('[data-role="contenu"]');
    if (this.cache[this.mois]) {
      this._periode(this.cache[this.mois]);
      $c.html(this._rendre(this.cache[this.mois]));
      this._recalculer_apercu();
      this._charger_consolide();
      this._charger_controle_bl();
      this._charger_analyse();
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
      this._charger_controle_bl();
      this._charger_analyse();
    } catch (e) {
      $c.html(this._erreur(e));
    }
  }

  /** La barre du haut : période ET état des deux contrôles de règle.
   *
   * Appelée sur les DEUX chemins de `_charger` — celui du cache et celui du serveur — sinon
   * revenir sur un mois déjà consulté laissait les cases sur la règle du mois précédent.
   */
  _periode(d) {
    const p = d && d.periode;
    this.$root.find('[data-role="periode"]').text(
      p ? `Période : ${p.debut} → ${p.fin}` : (d && d.libelle) || ""
    );
    this._refleter_regle(d);
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
      // Le SENS d’abord : un ajustement positif est dû par Aqua World (bénéfice Aqua World dû à
      // Economiq − bénéfice Economiq dû à Aqua World), et c’est pour cela qu’il RÉDUIT ce que
      // le partenaire doit. Le rapport court disait l’inverse jusqu’au 04/11/2026.
      ["Ajustement", this._m(d.ajustement), d.ajustement > 0
        ? "dû par Aqua World — déduit de l’échéancier"
        : "solde net + charges, déduit de l’échéancier", d.ajustement > 0 ? "warn" : "ok"],
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
        // ⚠️ UN MOIS NON ENREGISTRÉ N’A PAS DE STATUT, ET UNE ÉCHÉANCE NULLE N’EST PAS IMPAYÉE.
        // Le calcul à l’instant ne porte pas `statut` : une échéance absorbée s’affichait
        // « non_payé » en rouge. Sans commande du mois (octobre 2026), les trois échéances
        // valent 0 : rien à payer, rien d’absorbé — « néant ».
        const brut = (e.montant || 0) + (e.deduit || 0);
        const neant = Math.abs(brut) < 0.0005;
        const absorbee = !neant && ((e.statut || "") === "absorbé"
          || (!e.statut && (e.montant || 0) <= 0.0005 && (e.deduit || 0) > 0));
        const statut = neant ? "néant" : absorbee ? "absorbé" : (e.statut || "non_payé");
        return `<tr class="${neant || absorbee ? "pe-absorbee" : ""}">
          <td>${e.date}</td>
          <td class="num">${this._m(brut)}</td>
          <td class="num">${e.deduit ? this._m(e.deduit) : "—"}</td>
          <td class="num"><b>${this._m(e.montant)}</b></td>
          <td><span class="pe-badge ${statut === "payé" ? "ok"
            : neant || absorbee ? "neutre" : "bad"}">${this._esc(statut)}</span></td>
          <td class="muted">${this._esc(e.note || "")}</td></tr>`;
      }).join("")}</tbody>
      <tfoot><tr><td colspan="3">Report au consolidé</td>
        <td class="num">${this._m(d.report)}</td><td colspan="2"></td></tr></tfoot>
      </table></div>${this._note_report(d)}`;

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
      + this._sous("Contrôle des BL — ventes faites par Economiq")
      + `<div data-role="controle-bl"><div class="pe-chargement">Chargement…</div></div>`
      + this._sous("Bilan d’activité") + this._regle(d) + bilan
      + this._sous("Écriture de bilan") + ecriture
      + this._sous("Charges libres du mois") + charges
      + this._sous("Échéancier") + ech
      + this._sous("Solde consolidé inter-mois")
      + `<div data-role="consolide"><div class="pe-chargement">Chargement…</div></div>`
      + this._sous("Analyse du compte Economiq Aqua Solution - A&S")
      + `<div data-role="analyse-compte"><div class="pe-chargement">Chargement…</div></div>`
      + this._sous("Commandes du partenaire") + commandes;
  }

  /** Où va ce que l’échéancier du mois n’a pas pu absorber.
   *
   * ⚠️ UN REPORT SANS EXPLICATION SE LIT COMME UNE ERREUR. Octobre 2026 : aucune commande
   * d’Economiq, trois échéances à 0, et 425,070 dus par Aqua World qui « disparaissaient » —
   * ils descendent au consolidé, mais seulement une fois le mois enregistré.
   */
  _note_report(d) {
    if (!(d.report > 0.0005)) return "";
    const tete = (d.total_commandes || 0) > 0.0005
      ? `L’ajustement (<b>${this._m(d.ajustement)}</b>) dépasse l’échéancier du mois`
      : "Aucune commande d’Economiq ce mois-ci : l’échéancier est vide et n’absorbe rien";
    const quand = d.enregistre
      ? ""
      : ` Ils n’y comptent qu’une fois le mois <b>enregistré</b> — la projection est affichée
         sous « Solde consolidé inter-mois ».`;
    return `<div class="pe-note">${tete}. <b>${this._m(d.report)}</b> dus par Aqua World
      descendent au consolidé, sur les échéances encore ouvertes, de la plus ancienne à la plus
      récente.${quand}</div>`;
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
          this.analyse = null;      // l’écriture crédite le compte du partenaire
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
          ${this._apres_ecriture(v.ventes_partenaire || 0)}
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

  /** Toute vente faite PAR Economiq doit porter un BL de main d’œuvre seulement.
   *
   * Les produits posés par le partenaire ne sortent pas de notre stock : un article de stock sur
   * le BL du client final ferait sortir du magasin un produit qui n’en est jamais parti. Le
   * contrôle couvre tous les mois depuis juillet 2026, pas le seul mois affiché : une anomalie
   * d’un mois déjà passé n’en est pas moins à corriger. Lu une fois, gardé tant que la règle du
   * bilan ne change pas.
   */
  async _charger_controle_bl() {
    const $c = this.$root.find('[data-role="controle-bl"]');
    if (!$c.length) return;
    if (!this.controle_bl) {
      try {
        this.controle_bl = (await frappe.call({
          method: "bank_retenue_sync.api.partenaire.get_controle_bl" })).message || {};
      } catch (e) {
        $c.html(this._erreur(e));
        return;
      }
    }
    const k = this.controle_bl;
    if (!k.disponible) {
      $c.html(`<div class="pe-note alerte">${this._esc(k.message || "Contrôle indisponible.")}</div>`);
      return;
    }
    const nb = (k.anomalies || []).length;
    const mois = (k.mois || []).map((m) => {
      const cls = m.conformes < m.ventes ? "bad" : (m.ventes ? "ok" : "neutre");
      return `<span class="pe-badge ${cls}">${this._esc(m.libelle)} : ${m.conformes}/${m.ventes}</span>`;
    }).join(" ");
    const tete = nb
      ? `<div class="pe-note alerte">✗ <b>${nb} vente${nb > 1 ? "s" : ""} sur ${k.ventes}</b>
           faite${nb > 1 ? "s" : ""} par Economiq depuis ${this._esc(k.depuis_libelle)}
           ${nb > 1 ? "n’ont" : "n’a"} pas un BL de main d’œuvre seulement. Un article de stock
           sur ce BL sort du magasin un produit que le partenaire a fourni lui-même.</div>`
      : `<div class="pe-note">✓ <b>${k.ventes} vente${k.ventes > 1 ? "s" : ""}</b> faite${
           k.ventes > 1 ? "s" : ""} par Economiq depuis ${this._esc(k.depuis_libelle)} :
           ${k.ventes ? "toutes ont un BL validé ne portant que de la main d’œuvre" : "rien à contrôler"}
           (groupe « ${this._esc(k.groupe)} »).</div>`;
    const table = nb
      ? `<div class="pe-scroll"><table class="pe-tbl"><thead><tr>
          <th>Mois</th><th>Commande</th><th>Client</th><th class="num">Vente</th>
          <th>Problème</th><th>BL</th><th>Détail</th></tr></thead><tbody>
          ${k.anomalies.map((a) => `<tr>
            <td class="muted">${this._esc(a.mois)}</td>
            <td>${this._lien("Sales Order", a.sales_order)}</td>
            <td>${this._esc(a.customer || "")}</td>
            <td class="num">${this._m(a.vente)}</td>
            <td><span class="pe-badge bad">${a.type === "sans_bl"
              ? "Sans BL validé" : "Articles hors main d’œuvre"}</span></td>
            <td>${(a.bons || []).map((b) => this._lien("Delivery Note", b)).join("<br>") || "—"}</td>
            <td class="muted" style="font-size:11.5px;">${this._esc(a.detail || "")}</td>
          </tr>`).join("")}</tbody></table></div>`
      : "";
    $c.html(tete + `<div class="pe-act" style="margin-top:0;">${mois}</div>` + table);
  }

  async _charger_consolide() {
    const $c = this.$root.find('[data-role="consolide"]');
    if (!$c.length) return;
    try {
      const c = (await frappe.call({
        method: "bank_retenue_sync.api.partenaire.get_consolide",
        args: { mois: this.mois } })).message || {};
      if (!(c.lignes || []).length) {
        $c.html('<div class="pe-vide">Aucun mois enregistré : le consolidé est vide.</div>'
                + this._note_projection(c));
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
      $c.html(saisie + this._note_projection(c) + excedent + dettes
        + `<div class="pe-scroll"><table class="pe-tbl">
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

  /** Ce que deviendrait le consolidé si le mois affiché était enregistré.
   *
   * ⚠️ LE CONSOLIDÉ NE LIT QUE LES MOIS EN BASE. Sans cette projection, le report d’un mois non
   * enregistré n’apparaît nulle part : octobre 2026 annonçait 425,070 « au consolidé » et le
   * reste dû ne bougeait pas.
   */
  _note_projection(c) {
    const p = c && c.projection;
    if (!p) return "";
    const lignes = (p.changements || []).map((x) => (x.nouvelle
      ? `nouvelle échéance du ${this._esc(x.date)} : <b>${this._m(x.montant_apres)}</b>`
      : `échéance du ${this._esc(x.date)} : reste ${this._m(x.reste_avant)} →
         <b>${this._m(x.reste_apres)}</b>${Math.abs(x.montant_apres - x.montant_avant) > 0.0005
          ? ` <span class="muted">(dû ${this._m(x.montant_avant)} → ${this._m(x.montant_apres)})</span>`
          : ""}`));
    const report = p.report > 0.0005
      ? ` Le report de <b>${this._m(p.report)}</b> dû par Aqua World s’impute sur les échéances
         encore ouvertes, de la plus ancienne à la plus récente.`
      : "";
    return `<div class="pe-note">Projection avec <b>${this._esc(p.libelle)}</b> (mois non
      enregistré) : reste dû <b>${this._m(c.reste)}</b> → <b>${this._m(p.reste)}</b>.${report}
      ${lignes.length ? `<br>${lignes.join("<br>")}` : ""}
      ${p.excedent > 0.0005 ? `<br>Excédent non imputé : <b>${this._m(p.excedent)}</b>.` : ""}
      <br><span class="muted">Enregistre le mois pour l’acter : le tableau ci-dessous ne compte que
      les mois enregistrés.</span></div>`;
  }

  /** Le compte « Economiq Aqua Solution - A&S » revient-il à zéro une fois les écritures passées ?
   *
   * Il est débité de ce qu’Economiq encaisse chez nos clients et crédité, chaque mois, des ventes
   * Economiq du bilan par l’écriture de bilan. L’analyse est globale (tous les mois depuis mai
   * 2026) : lue une fois, gardée tant que la règle du bilan ou les écritures ne changent pas.
   */
  async _charger_analyse() {
    const $c = this.$root.find('[data-role="analyse-compte"]');
    if (!$c.length) return;
    if (!this.analyse) {
      try {
        this.analyse = (await frappe.call({
          method: "bank_retenue_sync.api.partenaire.get_analyse_compte" })).message || {};
      } catch (e) {
        $c.html(this._erreur(e));
        return;
      }
    }
    $c.html(this._rendre_analyse(this.analyse));
  }

  _rendre_analyse(a) {
    if (!a.disponible) {
      return `<div class="pe-note alerte">${this._esc(a.message || "Analyse indisponible.")}</div>`;
    }
    const passer = a.a_passer || [];
    const noms = passer.map((x) => x.libelle + (x.en_cours ? " (en cours)" : "")).join(", ");
    const zero = Math.abs(a.projete || 0) <= 0.0005;
    const somme = (liste, cle) => (liste || []).reduce((s, x) => s + (x[cle] || 0), 0);

    const intro = `<div class="pe-note">Ce compte est <b>débité</b> de ce qu’Economiq encaisse chez
      nos clients sur les ventes qu’il réalise pour Aqua World, et <b>crédité</b> chaque mois par
      l’écriture de bilan (« Ventes Economiq »). Une fois l’écriture passée, il doit revenir à
      <b>zéro</b>. Rapproché depuis ${this._esc(a.debut_libelle)}, solde d’ouverture
      ${this._m(a.ouverture)}.</div>`;

    const kpis = this._kpis([
      [`Solde au ${a.date}`, this._m(a.solde), "encaissé par Economiq, pas encore repris", ""],
      ["À créditer", this._m(a.a_crediter),
        passer.length ? `écriture à passer : ${noms}` : "aucune écriture en attente", ""],
      ["Solde projeté", this._m(a.projete), "après ces écritures — doit valoir 0",
        zero ? "ok" : "bad"],
    ]);

    const pc = a.par_cause || {};
    const nb = (code) => (a.commandes || []).filter((c) => (c.cause || {}).code === code).length;
    const causes = [];
    if (pc.taxes) {
      causes.push(`<b>${this._m(pc.taxes)}</b> de taxes en sus du prix (timbre fiscal, TVA d’un
        prix hors taxe) encaissées par Economiq sur ${nb("taxes")} commande(s) : l’écriture
        crédite la vente du bilan, qui ne les compte pas`);
    }
    if (pc.trop_percu) {
      causes.push(`<b>${this._m(pc.trop_percu)}</b> encaissés au-delà de la vente du bilan`);
    }
    if (pc.ailleurs) {
      causes.push(`<b>${this._m(pc.ailleurs)}</b> de ventes Economiq réglées ailleurs que chez
        Economiq : l’écriture les crédite quand même au compte`);
    }
    const pasEncaisse = (pc.non_encaisse || 0) + (pc.partiel || 0);
    if (Math.abs(pasEncaisse) > 0.0005) {
      causes.push(`<b>${this._m(pasEncaisse)}</b> de ventes Economiq pas (encore) encaissées sur
        ce compte`);
    }
    if (pc.ecriture) {
      causes.push(`<b>${this._m(pc.ecriture)}</b> d’écart entre ce que les écritures ont crédité
        et les ventes du bilan recalculé`);
    }
    if (pc.hors_bilan) {
      causes.push(`<b>${this._m(pc.hors_bilan)}</b> de mouvements hors des ventes Economiq
        (détail ci-dessous)`);
    }
    if (pc.ouverture) causes.push(`<b>${this._m(pc.ouverture)}</b> de solde d’ouverture`);

    const apres = passer.length
      ? `une fois l’écriture de bilan ${this._de(noms)} passée (<b>${this._m(a.a_crediter)}</b>
         au crédit), `
      : "";
    let verdict;
    if (!a.coherent) {
      verdict = `<div class="pe-note alerte">Rapprochement incomplet : le solde projeté
        (<b>${this._m(a.projete)}</b>) n’est expliqué qu’à hauteur de
        <b>${this._m(a.explique)}</b>. Un mouvement du compte n’a pas pu être classé — à regarder
        dans le grand livre.</div>`;
    } else if (zero) {
      verdict = `<div class="pe-note">✓ Projection : ${apres || "toutes les écritures sont passées, "}le
        compte revient à zéro — tout ce qu’Economiq a encaissé est repris par le bilan.</div>`;
    } else {
      // ⚠️ UN CONSTAT, PAS UNE CONSIGNE DE SAISIE. Reprendre l’écart au partenaire ramène le
      // compte à zéro et baisse l’ajustement d’autant (ligne d’équilibre de l’écriture) ; mais
      // la boîte de l’écriture confirme les pièces de dette à supprimer sur les montants
      // PROPOSÉS, pas sur ceux saisis : y gonfler « Ventes Economiq » à la main ferait
      // supprimer d’autres pièces que celles montrées. La reprise est une décision de règlement
      // avec le partenaire, à porter dans l’écriture elle-même — pas une correction à la main.
      const reprise = a.projete > 0
        ? `<br>Pour revenir à zéro, ces <b>${this._m(a.projete)}</b> sont à reprendre au
           partenaire — ils baisseraient d’autant l’ajustement dû par Aqua World. L’écriture de
           bilan ne le fait pas encore.`
        : "";
      verdict = `<div class="pe-note alerte">✗ Projection : ${apres}le compte restera à
        <b>${this._m(a.projete)}</b> — il ne revient pas à zéro.${causes.length
          ? `<br>${causes.map((x) => `• ${x}`).join("<br>")}` : ""}${reprise}</div>`;
    }

    const lignes = (a.mois || []).map((l) => {
      const ecart = Math.abs(l.ecart || 0) > 0.0005;
      const credite = l.credite == null
        ? `<span class="muted">${l.a_passer ? `à passer : ${this._m(l.ventes)}` : "—"}</span>`
        : this._m(l.credite) + (Math.abs(l.ecart_ecriture || 0) > 0.0005
          ? `<br><span class="muted" style="font-size:11px;">bilan recalculé :
             ${this._m(l.ventes)}</span>` : "");
      const piece = (l.ecritures || []).length
        ? l.ecritures.map((n) => this._lien("Journal Entry", n)).join("<br>")
        : (l.en_cours ? '<span class="pe-badge neutre">mois en cours</span>'
          : l.a_passer ? '<span class="pe-badge bad">à passer</span>'
            : '<span class="muted">—</span>');
      return `<tr><td>${this._esc(l.libelle)}</td><td class="num">${this._m(l.ventes)}</td>
        <td class="num">${this._m(l.encaisse)}</td><td class="num">${credite}</td>
        <td class="num">${ecart ? `<span class="pe-badge bad">${this._m(l.ecart)}</span>` : "—"}</td>
        <td>${piece}</td></tr>`;
    }).join("");
    const parMois = `<div class="pe-scroll"><table class="pe-tbl">
      <thead><tr><th>Mois du bilan</th><th class="num">Ventes Economiq</th>
        <th class="num">Encaissé sur le compte</th><th class="num">Crédité par l’écriture</th>
        <th class="num">Écart</th><th>Écriture</th></tr></thead>
      <tbody>${lignes}</tbody>
      <tfoot><tr><td>${(a.mois || []).length} mois</td>
        <td class="num">${this._m(somme(a.mois, "ventes"))}</td>
        <td class="num">${this._m(somme(a.mois, "encaisse"))}</td>
        <td class="num">${this._m(somme(a.mois, "credite"))}</td>
        <td class="num">${this._m(somme(a.mois, "ecart"))}</td><td></td></tr></tfoot>
      </table></div>`;

    const titre = (t) => `<div style="font-size:11px;font-weight:600;text-transform:uppercase;
      letter-spacing:.04em;color:var(--text-muted);margin:12px 0 6px;">${this._esc(t)}</div>`;
    const commandes = (a.commandes || []).length
      ? titre("Écarts par commande") + `<div class="pe-scroll"><table class="pe-tbl">
        <thead><tr><th>Commande</th><th>Client</th><th>Bilan</th><th class="num">Vente au bilan</th>
          <th class="num">Encaissé</th><th class="num">Écart</th><th>Cause</th></tr></thead>
        <tbody>${a.commandes.map((c) => `<tr>
          <td>${this._lien("Sales Order", c.sales_order)}</td>
          <td>${this._esc(c.customer || "")}</td><td class="muted">${this._esc(c.libelle)}</td>
          <td class="num">${this._m(c.vente)}</td>
          <td class="num">${this._m(c.encaisse)}${(c.pieces || []).length
            ? `<br><span style="font-size:11px;">${c.pieces.map((p) =>
                this._lien("Payment Entry", p.payment_entry)).join(", ")}</span>` : ""}</td>
          <td class="num"><b>${this._m(c.ecart)}</b></td>
          <td class="muted" style="font-size:11.5px;">${this._esc((c.cause || {}).texte || "")}</td>
        </tr>`).join("")}</tbody>
        <tfoot><tr><td colspan="5">${a.commandes.length} commande(s)</td>
          <td class="num">${this._m(somme(a.commandes, "ecart"))}</td><td></td></tr></tfoot>
        </table></div>`
      : "";
    const hors = (a.hors_bilan || []).length
      ? titre("Mouvements hors des ventes Economiq") + `<div class="pe-scroll"><table class="pe-tbl">
        <thead><tr><th>Date</th><th>Pièce</th><th class="num">Montant</th><th>Nature</th>
          <th>Commande</th></tr></thead>
        <tbody>${a.hors_bilan.map((h) => `<tr><td class="muted">${this._esc(h.date)}</td>
          <td>${this._lien(h.voucher_type, h.voucher_no)}</td>
          <td class="num">${this._m(h.montant)}</td>
          <td class="muted" style="font-size:11.5px;">${this._esc(h.texte || "")}</td>
          <td>${h.sales_order ? this._lien("Sales Order", h.sales_order) : "—"}</td>
        </tr>`).join("")}</tbody>
        <tfoot><tr><td colspan="2">${a.hors_bilan.length} mouvement(s)</td>
          <td class="num">${this._m(somme(a.hors_bilan, "montant"))}</td><td colspan="2"></td>
        </tr></tfoot></table></div>`
      : "";
    return intro + kpis + verdict + parMois + commandes + hors;
  }

  /** Dans la boîte de l’écriture : où en sera le compte du partenaire une fois celle-ci passée.
   *
   * ⚠️ C’EST À CE MOMENT-LÀ QUE L’ÉCART SE CORRIGE, PAS APRÈS. L’écriture crédite le compte des
   * ventes saisies ; si le compte ne revient pas à zéro, on le dit avant de créer la pièce.
   */
  _apres_ecriture(ventes) {
    const a = this.analyse;
    if (!a || !a.disponible) return "";
    const ici = (a.a_passer || []).find((x) => x.mois === this.mois);
    if (!ici) return "";
    const apres = (a.solde || 0) - (ventes || 0);
    const autres = (a.a_passer || []).filter((x) => x.mois !== this.mois);
    const fin = apres - autres.reduce((s, x) => s + (x.ventes || 0), 0);
    const ok = Math.abs(fin) <= 0.0005;
    return `<tr><td colspan="2" style="padding-top:6px;font-size:11.5px;
      color:${ok ? "var(--text-muted)" : "#a93226"};">Compte ${this._esc(a.compte)} :
      ${this._m(a.solde)} aujourd’hui, crédité de ${this._m(ventes)} par cette écriture →
      <b>${this._m(apres)}</b>${autres.length ? ` (${this._m(fin)} une fois passées celles ${this._de(
        autres.map((x) => x.libelle).join(", "))})` : ""}.
      ${ok ? "Il revient à zéro." : "Il ne revient pas à zéro — voir « Analyse du compte »."}</td></tr>`;
  }

  /** « de octobre » → « d’octobre ». Les libellés de mois commencent par une minuscule. */
  _de(texte) {
    const t = String(texte || "");
    return /^[aeiouyàâéèêîïôûœ]/i.test(t) ? `d’${t}` : `de ${t}`;
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
    // Le justificatif complet reste accessible, replié : il sert à vérifier les chiffres avant
    // d'envoyer, mais ce n'est plus lui qui part chez le partenaire.
    const detaille = (resultat && resultat.html_detaille) || apercu.html_detaille || "";
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
      </details>
      <details style="margin-top:6px">
        <summary style="cursor:pointer;font-size:12px;color:var(--text-muted)">${__(
          "Détail complet — commandes, échéancier brut, calculs (ne part pas au partenaire)"
        )}</summary>
        <div style="max-height:40vh;overflow:auto;border:1px solid var(--border-color);
                    border-radius:8px;padding:12px;margin-top:6px">${detaille}</div>
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

  /** La règle de comptage qui a produit ces chiffres — dite, pas seulement appliquée.
   *
   * Les bénéfices de ce tableau viennent de `customization_app.bilan_vente`, qui applique une
   * règle ENREGISTRÉE (Config Bilan Vente). Sans cette mention, on lirait 654,86 ici après avoir
   * lu 1 321,01 la semaine précédente sans pouvoir expliquer l’écart.
   */
  _regle(d) {
    const r = d && d.regle_bilan;
    if (!r) return "";
    const bouts = [`comptées sur la <b>${this._esc(r.libelle)}</b>`];
    if (r.exclure_ouvertes) {
      const n = (r.ecartees || []).length;
      bouts.push(`commandes à <b>tâche ouverte exclues</b>${n ? ` (${n} écartée${n > 1 ? "s" : ""})` : ""}`);
    }
    const fige = d.valide
      ? " Ce mois est <b>validé</b> : son bilan est figé, la règle ne le change plus."
      : "";
    return `<div class="pe-note">Règle du bilan : ${bouts.join(", ")}.${fige}
      <span class="muted">Elle se change sur l’écran « Bilan Vente » et vaut pour les deux.</span></div>`;
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
