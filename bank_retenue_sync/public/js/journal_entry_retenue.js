// Écriture de journal : émettre le certificat de la retenue à la source prélevée sur une dépense.
//
// L'écran ne décide rien : tout vient de bank_retenue_sync.tej.emis_journal. Il rend visible ce
// qui empêcherait d'émettre — fournisseur non rattaché, fiche sans matricule — et permet de le
// corriger sans quitter la pièce.
//
// ⚠️ CE QUI PART SUR TEJ EST DÉCLARATIF ET IRRÉVERSIBLE. Le bouton ne soumet jamais tout seul :
// il propose d'abord une répétition à blanc, et la soumission demande un second geste.

const API = "bank_retenue_sync.tej.emis_journal";

frappe.ui.form.on("Journal Entry", {
  refresh(frm) {
    // Avant validation, ni le montant ni la date de la retenue ne sont définitifs — et TEJ
    // n'accepte pas de correction sans annulation.
    if (frm.doc.docstatus !== 1 || frm.is_new()) return;
    frappe.call({ method: `${API}.etat`, args: { journal_entry: frm.doc.name } })
      .then((r) => poser_bouton(frm, r.message || {}));
  },
});

function poser_bouton(frm, etat) {
  if (!etat.concernee) return;
  const emis = etat.statut === "Émis";
  const libelle = emis
    ? __("Certificat émis ✓")
    : etat.peut_emettre
      ? __("Émettre le certificat de retenue")
      : __("Certificat de retenue — incomplet");
  const btn = frm.add_custom_button(libelle, () => fenetre(frm, etat));
  // Vert quand c'est fait, orange quand il manque quelque chose, rouge quand on peut partir :
  // la couleur dit l'état sans qu'on ait à ouvrir.
  btn.removeClass("btn-default")
     .addClass(emis ? "btn-success" : etat.peut_emettre ? "btn-danger" : "btn-warning");
}

function fenetre(frm, etat) {
  const M = (v) => format_currency(v || 0, "TND");
  const d = new frappe.ui.Dialog({
    title: __("Certificat de retenue à la source — {0}", [frm.doc.name]),
    fields: [
      { fieldtype: "HTML", fieldname: "resume" },
      { fieldtype: "Section Break", label: __("Le bénéficiaire") },
      {
        fieldtype: "Link", fieldname: "supplier", options: "Supplier",
        label: __("Fournisseur"), default: etat.supplier || "",
        description: __("Lu sur la pièce : {0}", [etat.fournisseur_lu || "—"]),
      },
      { fieldtype: "Column Break" },
      {
        fieldtype: "Data", fieldname: "numero_facture", label: __("N° de facture fournisseur"),
        default: etat.numero_facture || "",
        description: __("Ce que le portail attend comme « numéro chez le déclarant »."),
      },
      { fieldtype: "HTML", fieldname: "manques" },
      { fieldtype: "HTML", fieldname: "lecture" },
    ],
    primary_action_label: __("Enregistrer et vérifier"),
    primary_action: async (v) => {
      let e;
      try {
        e = (await frappe.call({
          method: `${API}.completer`, freeze: true,
          args: { journal_entry: frm.doc.name, supplier: v.supplier || "",
                  numero_facture: v.numero_facture || "" },
        })).message;
      } catch (err) { return; }
      d.hide();
      fenetre(frm, e);
    },
  });

  const info = `<table class="table table-bordered" style="font-size:12.5px;margin-bottom:0">
    <tr><td>${__("Montant TTC")}</td><td class="text-right">${M(etat.montant_ttc)}</td>
        <td>${__("Retenue prélevée")}</td>
        <td class="text-right"><b>${M(etat.retenue)}</b></td></tr>
    <tr><td>${__("Montant HT")}</td><td class="text-right">${M(etat.montant_ht)}</td>
        <td>${__("Taux de TVA")}</td>
        <td class="text-right">${etat.taux_tva == null ? "—" : etat.taux_tva + " %"}</td></tr>
    <tr><td>${__("Matricule fiscal")}</td>
        <td class="text-right" colspan="3">${frappe.utils.escape_html(etat.matricule || "—")}</td></tr>
    ${etat.certificat ? `<tr><td>${__("Certificat")}</td>
        <td class="text-right" colspan="3"><b>${frappe.utils.escape_html(etat.certificat)}</b>
        ${frappe.utils.escape_html(etat.emis_le || "")}</td></tr>` : ""}
  </table>`;
  d.fields_dict.resume.$wrapper.html(info);

  const manques = etat.manques || [];
  d.fields_dict.manques.$wrapper.html(
    manques.length
      ? `<div class="alert alert-warning" style="margin:10px 0 0">
           <b>${__("Il manque encore :")}</b><ul style="margin:6px 0 0 18px">${
             manques.map((m) => `<li>${frappe.utils.escape_html(m)}</li>`).join("")
           }</ul>
           <div style="font-size:12px;margin-top:6px">${__(
             "Le matricule se corrige sur la fiche du fournisseur, pas ici : deux endroits pour la même donnée finiraient par se contredire."
           )}</div></div>`
      : etat.statut === "Émis"
        ? `<div class="alert alert-success" style="margin:10px 0 0">${__(
            "Le certificat est déjà parti. Il se lit chez le fournisseur et chez l-administration ; le réémettre déclarerait deux fois."
          )}</div>`
        : `<div class="alert alert-info" style="margin:10px 0 0">${__(
            "Tout est réuni. Répétez d-abord à blanc : TEJ remplit le formulaire et rend les montants qu-IL calcule, sans rien valider."
          )}</div>`
  );

  // 🤖 Lire la facture : le scan est deja attache a l-ecriture, autant lui demander le
  // fournisseur ET son matricule fiscal plutot que de les retaper.
  if (etat.statut !== "Émis" && !etat.supplier) {
    d.fields_dict.lecture.$wrapper.html(
      `<button class="btn btn-sm btn-default" data-lire style="margin-top:8px">🤖 ${__(
        "Lire la facture (fournisseur et matricule)")}</button>
       <div data-resultat style="margin-top:8px"></div>`);
    d.fields_dict.lecture.$wrapper.find("[data-lire]").on("click", () => lire(frm, d));
  }

  if (!manques.length && etat.statut !== "Émis") {
    d.set_secondary_action_label(__("Répéter à blanc"));
    d.set_secondary_action(() => lancer(frm, d, true));
    d.set_primary_action(__("Émettre pour de bon"), () => confirmer(frm, d));
  }
  d.show();
}

// La lecture PROPOSE, elle ne crée rien : un doublon de fournisseur éparpille ses factures sur
// deux fiches et aucun solde ne veut plus rien dire. La création est un second bouton.
async function lire(frm, d, fichier) {
  const $z = d.fields_dict.lecture.$wrapper.find("[data-resultat]");
  $z.html(`<span class="text-muted">${__("Lecture en cours…")}</span>`);
  let r;
  try {
    r = (await frappe.call({ method: `${API}.lire_facture`, freeze: true,
                             freeze_message: __("Lecture de la facture…"),
                             args: { journal_entry: frm.doc.name, fichier: fichier || null },
                           })).message;
  } catch (e) { $z.empty(); return; }
  // ⚠️ « LA PREMIERE PIECE JOINTE » N-EST PAS « LA FACTURE ». Une ecriture en porte souvent
  // plusieurs : des pages d-une meme facture, mais aussi des avis de virement et des bons de
  // paiement. Le serveur classe et choisit le plus probable — l-ecran montre lequel, et permet
  // d-en essayer un autre.
  const autres = (r.pieces || []).filter((p) => p.nom !== r.fichier && p.lisible);
  const choix = autres.length
    ? `<div style="margin-top:6px;font-size:12px">${__("Lu sur")} :
         <b>${frappe.utils.escape_html(r.fichier || "—")}</b> —
         ${__("essayer plutôt")} ${autres.map(
           (p) => `<button class="btn btn-xs btn-default" data-piece="${
             frappe.utils.escape_html(p.fichier)}" style="margin:2px 4px 0 0">${
             frappe.utils.escape_html(p.nom)}</button>`).join("")}</div>`
    : "";
  if (!r.lu) {
    const lisibles = (r.pieces || []).filter((p) => p.lisible);
    $z.html(`<div class="alert alert-warning" style="margin:0">
      ${frappe.utils.escape_html(r.raison || "")}
      ${lisibles.length > 1 ? `<div style="margin-top:6px">${__("Autres pièces jointes")} :
        ${lisibles.map((p) => `<button class="btn btn-xs btn-default" data-piece="${
          frappe.utils.escape_html(p.fichier)}" style="margin:2px 4px 0 0">${
          frappe.utils.escape_html(p.nom)}</button>`).join("")}</div>` : ""}</div>`);
    $z.find("[data-piece]").on("click", function () {
      lire(frm, d, $(this).attr("data-piece"));
    });
    return;
  }
  const esc = frappe.utils.escape_html;
  const cands = (r.candidats || []).map(
    (c) => `<button class="btn btn-xs btn-default" data-choisir="${esc(c.name)}"
              style="margin:2px 4px 0 0">${esc(c.supplier_name)}</button>`).join("");
  $z.html(`<div class="alert alert-info" style="margin:0">
      <div><b>${__("Fournisseur lu")} :</b> ${esc(r.fournisseur || "—")}</div>
      <div><b>${__("Matricule fiscal lu")} :</b> ${esc(r.matricule || "—")}</div>
      ${cands ? `<div style="margin-top:6px">${__("Fiches proches — choisissez plutôt :")}
                 ${cands}</div>` : ""}
      ${choix}
      ${r.matricule
        ? `<button class="btn btn-sm btn-primary" data-creer style="margin-top:8px">${__(
            "Créer la fiche fournisseur avec ce matricule")}</button>`
        : `<div style="margin-top:6px">${__(
            "Sans matricule lu, la fiche ne servirait pas : le portail l-exige. Complétez-le à la main sur la fiche fournisseur."
          )}</div>`}
    </div>`);
  $z.find("[data-piece]").on("click", function () {
    lire(frm, d, $(this).attr("data-piece"));
  });
  $z.find("[data-creer]").on("click", async () => {
    let e;
    try {
      e = (await frappe.call({
        method: `${API}.creer_fournisseur`, freeze: true,
        args: { journal_entry: frm.doc.name, nom: r.fournisseur, matricule: r.matricule },
      })).message;
    } catch (err) { return; }
    d.hide();
    fenetre(frm, e);
  });
  $z.find("[data-choisir]").on("click", async function () {
    const e = (await frappe.call({
      method: `${API}.completer`, freeze: true,
      args: { journal_entry: frm.doc.name, supplier: $(this).attr("data-choisir") },
    })).message;
    d.hide();
    fenetre(frm, e);
  });
}

// ⚠️ DEUX GESTES, JAMAIS UN. La répétition ne déclare rien ; la soumission, si — et elle ne
// s-annule pas. La confirmation nomme la pièce pour qu-on ne la donne pas machinalement.
function confirmer(frm, d) {
  frappe.confirm(
    __("Déclarer définitivement cette retenue sur TEJ pour {0} ? Un certificat soumis se lit chez le fournisseur et chez l-administration ; l-annuler laisse une trace.",
       [frm.doc.name]),
    () => lancer(frm, d, false)
  );
}

async function lancer(frm, d, dry_run) {
  let res;
  try {
    res = (await frappe.call({
      method: `${API}.emettre`, freeze: true,
      freeze_message: dry_run ? __("Répétition sur TEJ…") : __("Émission sur TEJ…"),
      args: { ligne: frm.doc.name, dry_run: dry_run ? 1 : 0 },
    })).message;
  } catch (e) { return; }
  d.hide();
  frappe.msgprint({
    title: dry_run ? __("Répétition") : __("Émission"),
    indicator: res && (res.reference || res.statut === "ok") ? "green" : "orange",
    message: `<pre style="white-space:pre-wrap;font-size:11.5px">${
      frappe.utils.escape_html(JSON.stringify(res, null, 2))}</pre>`,
  });
  frm.reload_doc();
}
