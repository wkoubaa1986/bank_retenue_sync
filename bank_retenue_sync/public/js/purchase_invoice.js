// Facture d'achat locale : lire le scan, voir ce qui bloque, voir la retenue due.
//
// L'écran ne décide rien : tout vient de bank_retenue_sync.achat.facture. Il rend visible AVANT la
// validation ce que le contrôle refuserait au moment de valider — sans quoi l'utilisateur découvre
// le problème au pire moment, après avoir tout saisi.

frappe.ui.form.on("Purchase Invoice", {
  refresh(frm) {
    if (frm.doc.docstatus !== 0 || frm.is_new()) return;
    frm.add_custom_button(__("Lire le scan"), () => lire(frm), __("Facture fournisseur"));
    frm.add_custom_button(__("Vérifier avant validation"), () => verifier(frm),
                          __("Facture fournisseur"));
    frm.add_custom_button(__("Poser la retenue à la source"), () => poser(frm),
                          __("Facture fournisseur"));
  },
});

function lire(frm) {
  frappe.call({
    method: "bank_retenue_sync.achat.facture.extraire_maintenant",
    args: { nom: frm.doc.name, forcer: 1 },
    freeze: true,
    freeze_message: __("Lecture du scan par OpenAI…"),
  }).then((r) => {
    const m = r.message || {};
    if (m.statut === "aucun pdf joint") {
      frappe.msgprint({ title: __("Aucun scan"), indicator: "orange",
        message: __("Joignez d'abord le scan de la facture du fournisseur.") });
      return;
    }
    frm.reload_doc();
    const dt = (v) => (v == null ? "—" : format_currency(v, frm.doc.currency || "TND"));
    frappe.msgprint({
      title: __("Ce que porte le scan"),
      indicator: "blue",
      message: `<table class="table table-bordered" style="font-size:12px">
          <tr><td>${__("N° de facture")}</td><td><b>${frappe.utils.escape_html(m.invoice_no || "—")}</b></td></tr>
          <tr><td>${__("Date")}</td><td>${frappe.utils.escape_html(m.invoice_date || "—")}
            ${m.date_ecartee ? `<br><span style="color:var(--red-500)">${__("date lue écartée ({0}) : trop éloignée de la date de comptabilisation, à saisir à la main", [m.date_ecartee])}</span>` : ""}</td></tr>
          <tr><td>${__("Total HT")}</td><td>${dt(m.total_ht)}</td></tr>
          <tr><td>${__("TVA")}</td><td>${dt(m.total_tva)}</td></tr>
          <tr><td>${__("Total TTC")}</td><td>${dt(m.total_ttc)}</td></tr>
          <tr><td>${__("HT + TVA = TTC sur le scan")}</td><td>${m.coherent ? "✔" : "⚠️ " + __("non")}</td></tr>
        </table>
        <p class="text-muted">${__("Lecture automatique : c'est un signal, pas une autorité. En cas d'écart, le PDF tranche.")}</p>`,
    });
  });
}

function verifier(frm) {
  frappe.call({
    method: "bank_retenue_sync.achat.facture.diagnostic_maintenant",
    args: { nom: frm.doc.name },
    freeze: true,
  }).then((r) => {
    const m = r.message || {};
    if (!m.local) {
      frappe.msgprint(__("Fournisseur étranger : aucun contrôle d'achat local ne s'applique."));
      return;
    }
    const ras = m.retenue || {};
    const dev = frm.doc.currency || "TND";
    const retenue = !ras.due
      ? __("Sous le seuil : aucune retenue à la source.")
      : ras.verdict === "conforme"
        ? __("Retenue à la source conforme : {0} (1 % de {1}, timbre exclu)",
             [format_currency(ras.saisie, dev), format_currency(ras.assiette, dev)])
        : `<b style="color:var(--red-500)">${__("Retenue à la source {0} : due {1}, saisie {2}", [ras.verdict, format_currency(ras.due, dev), format_currency(ras.saisie, dev)])}</b>`;
    frappe.msgprint({
      title: m.manques.length ? __("La validation serait refusée") : __("Prêt à valider"),
      indicator: m.manques.length ? "red" : "green",
      message: (m.manques.length
        ? "<ul><li>" + m.manques.map(frappe.utils.escape_html).join("</li><li>") + "</li></ul>"
        : `<p>${__("Scan joint, stock et magasin renseignés, totaux concordants.")}</p>`) +
        `<p>${retenue}</p>`,
    });
  });
}


function poser(frm) {
  frappe.call({
    method: "bank_retenue_sync.achat.retenue.poser_maintenant",
    args: { facture: frm.doc.name },
    freeze: true,
  }).then((r) => {
    const m = r.message || {};
    frm.reload_doc();
    frappe.msgprint({
      title: __("Retenue à la source"),
      indicator: m.statut === "posee" ? "green" : "orange",
      message: m.statut === "posee"
        ? __("Ligne ajoutée : {0} — 1 % de {1} (TTC {2}, timbre exclu).",
             [format_currency(m.due), format_currency(m.assiette),
              format_currency(m.ttc_avant_retenue)])
        : __("Rien à poser ({0}).", [m.statut]),
    });
  });
}
