// Commande d'achat : ne garder qu'une ligne par article, en sommant les quantités.
//
// L'écran ne décide rien : le regroupement vient de bank_retenue_sync.achat.commande. Il applique
// le résultat aux lignes affichées et laisse ERPNext recalculer les montants.
//
// ⚠️ LE BOUTON N'ÉCRIT RIEN EN BASE. Il modifie le formulaire, rien de plus : c'est l'utilisateur
// qui enregistre, et un simple rechargement annule la fusion. Une fusion faite toute seule à
// l'enregistrement supprimerait des lignes que personne n'a relues — l'entrepôt, la date de
// réception et la description des doublons sont perdus au profit de ceux de la première ligne.
//
// ⚠️ NE REMETS NI LE GROUPE, NI LE `frm.is_new()` (#26). Le bouton a d'abord vécu dans un groupe
// `__("Commande")` et caché tant que la commande n'était pas enregistrée : personne ne le trouvait.
// Un groupe se rend en menu déroulant « Commande ▾ » qu'il faut ouvrir, coincé entre les menus
// d'ERPNext. Et une commande d'import se construit ligne par ligne AVANT le premier
// enregistrement — c'est exactement le moment où l'on empile les doublons ; les lignes non
// enregistrées portent un nom local que `set_value` et `clear_doc` traitent sans broncher, rien
// n'empêche la fusion. D'où trois accès au même geste, et ce n'est pas du luxe : sur écran étroit,
// Frappe replie les boutons de la barre dans « ⋯ » (même parade que encaissement_paiement.js), le
// bouton du pied de tableau reste là où l'utilisateur regarde ses lignes, et le bandeau orange est
// le seul des trois à se voir sans rien chercher.
//
// ⚠️ LE LIBELLÉ EST CELUI QUE L'UTILISATEUR CHERCHE DES YEUX, PAS CELUI QUI DÉCRIT LE MIEUX LE
// CALCUL (#26). Il s'appelait « Fusionner les lignes en double » et on le cherchait sous « enlever
// les doublons » : un bouton qu'on ne reconnaît pas est un bouton absent. « Fusionner » n'a pas
// disparu pour autant — il est passé dans l'infobulle et dans les messages, là où il explique au
// lieu de servir d'étiquette, parce que ce geste ADDITIONNE les quantités et ne jette rien.

const API_COMMANDE = "bank_retenue_sync.achat.commande";

// Le libellé fait aussi office de clé : la barre d'outils et la grille indexent leurs boutons
// dessus et refusent d'en poser un deuxième. Il se relit à chaque appel — la langue peut changer
// entre deux formulaires.
const LIBELLE = () => __("Enlever les doublons");

// « Enlever » est un raccourci ; l'infobulle dit ce qui se passe vraiment, pour que personne ne
// craigne d'y perdre des quantités.
const INFOBULLE = () =>
  __("Regroupe les lignes qui portent le même article, la même unité et le même prix : les quantités sont additionnées sur la première ligne. Rien n'est enregistré avant que tu enregistres la commande.");

// Marque NOTRE bandeau parmi les messages du formulaire, pour le retirer sans toucher aux autres.
const CLASSE_BANDEAU = "brs-doublons";

// Pourquoi deux lignes du même article n'ont pas été regroupées. Le serveur rend le motif, pas sa
// traduction : c'est une décision métier, pas une phrase.
const MOTIFS = {
  prix: "prix différents",
  unite: "unités de mesure différentes",
  "prix et unite": "prix et unités de mesure différents",
};

frappe.ui.form.on("Purchase Order", {
  refresh(frm) {
    // Une commande validée est partie chez le fournisseur, une commande annulée ne sert plus à
    // rien : dans les deux cas il n'y a plus de lignes à fusionner. Le bouton du pied de tableau
    // survit aux rafraîchissements de la grille, il faut le retirer à la main.
    if (frm.doc.docstatus !== 0) {
      frm.__brs_boutons_fusion = [];
      retirer_bandeau(frm);
      const table = grille(frm);
      if (table && table.grid_buttons) table.clear_custom_buttons();
      return;
    }
    const barre = frm.add_custom_button(LIBELLE(), () => fusionner(frm));
    const table = grille(frm);
    const pied = table ? table.add_custom_button(LIBELLE(), () => fusionner(frm)) : null;
    [barre, pied].forEach(($btn) => $btn && $btn.attr("title", INFOBULLE()));
    // `signaler` retravaille ces boutons hors du rafraîchissement du formulaire (ajout ou retrait
    // d'une ligne) : on garde la main dessus au lieu de les redemander, car `add_custom_button`
    // réinscrit au passage une entrée dans le menu « ⋯ » du mode mobile.
    frm.__brs_boutons_fusion = [[barre, "btn-default"], [pied, "btn-secondary"]];
    signaler(frm);
  },
});

// Ajouter une ligne, en retirer une ou changer son article : le décompte des doublons bouge.
frappe.ui.form.on("Purchase Order Item", {
  items_add: signaler,
  items_remove: signaler,
  items_delete: signaler,
  item_code: signaler,
});

function grille(frm) {
  return frm.fields_dict.items && frm.fields_dict.items.grid;
}

/** Combien d'articles figurent sur plus d'une ligne. */
function articles_repetes(frm) {
  const compte = {};
  (frm.doc.items || []).forEach((ligne) => {
    const article = (ligne.item_code || "").trim();
    if (article) compte[article] = (compte[article] || 0) + 1;
  });
  return Object.keys(compte).filter((article) => compte[article] > 1).length;
}

/** Rend les doublons visibles : boutons en orange et bandeau cliquable.
 *
 * ⚠️ CE DÉCOMPTE N'EST PAS LA RÈGLE MÉTIER, et ne doit pas le devenir. La règle (article + unité +
 * prix) reste au serveur ; ici on compte les codes articles répétés, rien de plus, pour attirer
 * l'œil. Le bandeau peut donc s'allumer alors que la fusion ne gardera rien — c'est le message
 * « non fusionnées » qui dit alors pourquoi.
 */
function signaler(frm) {
  if (frm.doc.docstatus !== 0) return;
  const repetes = articles_repetes(frm);
  (frm.__brs_boutons_fusion || []).forEach(([$btn, neutre]) => {
    if (!$btn) return;
    $btn.toggleClass(neutre, !repetes).toggleClass("btn-warning", Boolean(repetes));
  });
  retirer_bandeau(frm);
  if (!repetes) return;
  // Bandeau PERMANENT (troisième paramètre) : sans lui, Frappe ajoute une croix de fermeture, et
  // un bandeau qu'on chasse d'un clic distrait redevient un bouton introuvable (#26). Il s'efface
  // de lui-même dès qu'il n'y a plus de doublon. `show_message` est la méthode de `frm.layout` qui
  // sait poser un bloc permanent — `frm.dashboard.set_headline` ne transmet pas ce paramètre.
  frm.layout.show_message(
    `<div class="${CLASSE_BANDEAU}">`
      + __("{0} article(s) apparaissent sur plusieurs lignes — ", [repetes])
      + `<a class="brs-fusionner" title="${frappe.utils.escape_html(INFOBULLE())}"`
      + ' style="text-decoration:underline;cursor:pointer;font-weight:bold">'
      + __("enlever les doublons")
      + "</a></div>",
    "orange",
    true
  );
  frm.$wrapper.find(`.${CLASSE_BANDEAU} .brs-fusionner`).on("click", () => fusionner(frm));
}

/** Retire NOTRE bandeau, et lui seul.
 *
 * `frm.layout.show_message` empile les blocs sans effacer les précédents — sans ce retrait, chaque
 * rafraîchissement en ajouterait un de plus. Et `clear_headline()` viderait tout le conteneur, y
 * compris les messages d'ERPNext : on cible donc notre marqueur.
 */
function retirer_bandeau(frm) {
  const messages = frm.layout && frm.layout.message;
  if (!messages || !messages.length) return;
  messages.find(`.${CLASSE_BANDEAU}`).closest(".form-message").remove();
  if (!messages.children().length) messages.addClass("hidden");
}

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
    // La table a changé sans passer par la grille : c'est à nous d'éteindre le bandeau.
    signaler(frm);
    frappe.msgprint({
      title: __("Doublons enlevés"),
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
