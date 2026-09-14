// Commande d'achat : ne garder qu'une ligne par article, en sommant les quantités.
//
// L'écran ne décide rien : le regroupement vient de bank_retenue_sync.achat.commande. Il applique
// le résultat aux lignes affichées et laisse ERPNext recalculer les montants.
//
// ⚠️ LE BOUTON N'ÉCRIT RIEN EN BASE. Il modifie le formulaire, rien de plus : c'est l'utilisateur
// qui enregistre, et un simple rechargement annule la fusion. Une fusion faite toute seule à
// l'enregistrement supprimerait des lignes que personne n'a relues — l'entrepôt, la date de
// réception et la description des doublons sont perdus au profit de ceux de la première ligne.

const API_COMMANDE = "bank_retenue_sync.achat.commande";

// Pourquoi deux lignes du même article n'ont pas été regroupées. Le serveur rend le motif, pas sa
// traduction : c'est une décision métier, pas une phrase.
const MOTIFS = {
  prix: "prix différents",
  unite: "unités de mesure différentes",
  "prix et unite": "prix et unités de mesure différents",
};

frappe.ui.form.on("Purchase Order", {
  refresh(frm) {
    // Une commande validée est partie chez le fournisseur, et une commande jamais enregistrée n'a
    // pas de lignes nommées à fusionner.
    if (frm.doc.docstatus !== 0 || frm.is_new()) return;
    frm.add_custom_button(__("Fusionner les lignes en double"), () => fusionner(frm),
                          __("Commande"));
  },
});

function fusionner(frm) {
  const lignes = (frm.doc.items || []).map((l) => ({
    name: l.name, item_code: l.item_code, uom: l.uom, rate: l.rate, qty: l.qty,
  }));
  frappe.call({
    method: `${API_COMMANDE}.fusionner_lignes`,
    args: { items: lignes },
    freeze: true,
    freeze_message: __("Recherche des lignes en double…"),
    callback: (r) => {
      const m = r.message || {};
      if (!m.doublons) {
        frappe.msgprint({
          title: __("Aucune ligne en double"),
          indicator: "blue",
          message: __("Aucun article n'apparaît deux fois avec la même unité et le même prix : rien n'a été modifié.")
            + reste(m),
        });
        return;
      }
      appliquer(frm, m);
    },
  });
}

/** Pose les quantités sommées et retire les lignes en trop. */
function appliquer(frm, m) {
  const supprimees = new Set(m.supprimer || []);
  // Les lignes en trop partent d'abord : ce qui suit recalcule les totaux, autant qu'il le fasse
  // sur la table définitive.
  frm.doc.items = (frm.doc.items || []).filter((l) => !supprimees.has(l.name));
  frm.doc.items.forEach((l, i) => (l.idx = i + 1));
  supprimees.forEach((nom) => frappe.model.clear_doc("Purchase Order Item", nom));
  frm.refresh_field("items");

  // La quantité se pose par `set_value` et non à la main : c'est le gestionnaire `qty` d'ERPNext
  // qui recalcule le montant de la ligne, puis le total, les taxes et le grand total. Le faire
  // nous-mêmes réécrirait, moins bien, un calcul qui existe déjà.
  const attendus = (m.conserver || [])
    .filter((l) => l.fusionnees > 1)
    .map((l) => frappe.model.set_value("Purchase Order Item", l.name, "qty", l.qty));

  Promise.all(attendus).then(() => {
    frm.refresh_field("items");
    // Filet : une ligne fusionnée dont la quantité ne bouge pas (doublon à zéro) ne déclenche
    // aucun recalcul, et la suppression seule n'en déclenche pas non plus.
    if (frm.cscript && frm.cscript.calculate_taxes_and_totals) {
      frm.cscript.calculate_taxes_and_totals();
    }
    frm.dirty();
    frappe.msgprint({
      title: __("Lignes fusionnées"),
      indicator: "green",
      message: __("{0} ligne(s) en double supprimée(s), quantités additionnées sur la première ligne de chaque article.", [m.doublons])
        + `<p class="text-muted">${__("La ligne conservée garde l'entrepôt, la date de réception et la description de la première occurrence. <b>Rien n'est enregistré</b> : relis les quantités, puis enregistre la commande.")}</p>`
        + reste(m),
    });
  });
}

/** Ce qui n'a PAS été fusionné, et pourquoi — sans quoi l'utilisateur croit à un oubli du bouton. */
function reste(m) {
  const signales = m.non_fusionnees || [];
  if (!signales.length) return "";
  const lignes = signales.map(
    (s) => `<li>${__("{0} lignes de {1} non fusionnées : {2}", [
      s.lignes,
      frappe.utils.escape_html(s.item_code || "—"),
      __(MOTIFS[s.motif] || s.motif),
    ])}</li>`
  );
  return `<p>${__("Sommer des lignes au prix ou à l'unité différents inventerait un prix qui n'a été convenu avec personne :")}</p>`
    + `<ul>${lignes.join("")}</ul>`;
}
