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
