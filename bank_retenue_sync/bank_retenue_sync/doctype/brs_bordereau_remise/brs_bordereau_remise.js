// Bordereau de remise collecté : le nom du scan remplit la banque, le numéro, la date et la nature
// quand il suit la façon de ranger de l'entreprise (« Ordre de versement Chèques Bq BIAT-7473685-du 09-11-2026.pdf »).
frappe.ui.form.on("BRS Bordereau Remise", {
  fichier(frm) {
    if (!frm.doc.fichier || !frm.is_new()) return;
    const nom = decodeURIComponent(String(frm.doc.fichier).split("/").pop());
    frappe.call({ method: "bank_retenue_sync.api.bordereaux.lire_nom", args: { nom } }).then((r) => {
      const lu = r.message || {};
      ["banque", "nature", "numero", "date_remise"].forEach((champ) => {
        // On complète sans écraser une saisie : seule la date, posée d'office à aujourd'hui, est remplacée.
        if (lu[champ] && (!frm.doc[champ] || champ === "date_remise" || champ === "nature")) frm.set_value(champ, lu[champ]);
      });
    });
  },
});

// Lecture par l'IA : en arrière-plan après la collecte. La fiche montre où elle en est, permet de la
// relancer, et de chercher à nouveau dans ERPNext après avoir corrigé un chiffre mal lu.
frappe.ui.form.on("BRS Bordereau Remise", {
  refresh(frm) {
    if (frm.is_new() || frm.doc.nature === "Espèces") return;
    const API = "bank_retenue_sync.api.bordereaux";
    const statut = frm.doc.lecture_statut;
    if (statut === "En attente") {
      frm.dashboard.set_headline(__("⏳ L’IA lit le bordereau et cherche ses pièces dans ERPNext… la fiche se mettra à jour."));
    } else if (statut === "Échec") {
      frm.dashboard.set_headline(__("Lecture par l’IA impossible : {0}", [frappe.utils.escape_html(frm.doc.lecture_message || "")]));
    } else if (statut === "Lue" && frm.doc.lecture_resume) {
      frm.dashboard.set_headline(frappe.utils.escape_html(frm.doc.lecture_resume));
    }
    if (!frm.perm[0] || !frm.perm[0].write) return;
    frm.add_custom_button(__("Revérifier dans ERPNext"), () => {
      const lancer = () => frappe.call({ method: API + ".verifier", args: { name: frm.doc.name }, freeze: true })
        .then(() => frm.reload_doc());
      if (frm.is_dirty()) frm.save().then(lancer); else lancer();
    });
    frm.add_custom_button(__("Relire avec l’IA"), () => {
      frappe.confirm(__("Relire le scan avec l’IA ? Les lignes actuelles, corrections comprises, seront remplacées."), () =>
        frappe.call({ method: API + ".relire", args: { name: frm.doc.name } }).then(() => frm.reload_doc()));
    });
    if (!frm._brs_ecoute) {
      frm._brs_ecoute = true;
      frappe.realtime.on("brs_bordereau_lu", (m) => {
        if (m && m.name === frm.doc.name && !frm.is_dirty()) frm.reload_doc();
      });
    }
  },
});
