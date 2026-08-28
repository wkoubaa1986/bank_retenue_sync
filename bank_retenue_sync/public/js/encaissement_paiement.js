// Écarts Aramex sur le brouillon d'Encaissement Paiement.
//
// Le brouillon est créé par bank_retenue_sync même quand l'advice Aramex ne colle pas
// parfaitement aux pièces (delta de paiement, ligne sans PE) : ces écarts sont enregistrés en
// « BRS Ecart Encaissement » et BLOQUENT la soumission (hook before_submit). Ce fichier ajoute
// les boutons de résolution — l'intervention humaine voulue. Les gestes serveur reprennent les
// mécanismes maison de customization_app (dette « Dette non payée », avoir flottant + ligne
// d'échéancier « Avoir client », perte liée à la commande) — cf. encaissement/ecarts.py.
//
// Règles d'affichage (décisions utilisateur 2026-08-18) :
//   Delta paiement : Perte / Ajustement toujours ; Avoir SEULEMENT si le client a encore un
//                    avoir flottant suffisant (`avoir_disponible` >= delta).
//   Sans pièce     : Ajustement (reclasser une PE) ; Ignorer si montant <= 1 DT.
// Chaque action ouvre un champ « Note » libre, posé dans les références des pièces créées.

frappe.ui.form.on("Encaissement Paiement", {
	refresh(frm) {
		if (frm.is_new()) return;
		frappe.call({
			method: "bank_retenue_sync.encaissement.ecarts.liste",
			args: { encaissement: frm.doc.name },
			callback(r) {
				const ecarts = r.message || [];
				if (!ecarts.length) return;
				const bloquants = ecarts.filter(
					(e) => e.bloquant && e.statut === "À traiter"
				);
				const btn = frm.add_custom_button(
					__("Écarts Aramex ({0})", [ecarts.length]),
					() => brs_show_ecarts(frm, ecarts)
				);
				if (btn && bloquants.length) {
					btn.removeClass("btn-default").addClass("btn-danger");
				}
				if (bloquants.length && frm.doc.docstatus === 0) {
					// Le bandeau est CLIQUABLE : c'est l'entree principale — le bouton de la
					// barre d'outils peut etre replie dans « ⋯ » selon la largeur d'ecran.
					frm.dashboard.set_headline(
						__("{0} écart(s) Aramex bloquent la soumission — ", [bloquants.length]) +
							'<a class="brs-voir-ecarts" style="text-decoration:underline;cursor:pointer;font-weight:bold">' +
							__("voir / résoudre les écarts") +
							"</a>"
					);
					frm.$wrapper
						.find(".brs-voir-ecarts")
						.on("click", () => brs_show_ecarts(frm, ecarts));
				}
			},
		});
	},
});

function brs_show_ecarts(frm, ecarts) {
	const fmt = (v) => format_currency(v, "TND", 3);
	const rows = ecarts
		.map((e) => {
			const badge =
				e.statut === "À traiter" && e.bloquant
					? '<span class="indicator-pill red">' + __("Bloquant") + "</span>"
					: '<span class="indicator-pill ' +
					  (e.statut === "Résolu" ? "green" : "gray") +
					  '">' +
					  __(e.statut) +
					  "</span>";
			let actions;
			if (e.statut === "À traiter" && e.bloquant) {
				const b = [];
				if (e.type_ecart === "Delta paiement" && e.ecart < 0) {
					// Advice > pièce : Aramex verse PLUS que le paiement enregistré.
					// La PE a été saisie trop bas — une seule issue, la porter au
					// montant de l'advice (décision 28/08/2026).
					b.push(
						`<button class="btn btn-xs btn-primary brs-act" data-action="regularisation" data-ecart="${e.name}"
							title="${__("Le client a bien payé le montant de l'advice")}">${__("Régulariser")}</button>`
					);
				} else if (e.type_ecart === "Delta paiement") {
					b.push(
						`<button class="btn btn-xs btn-danger brs-act" data-action="perte" data-ecart="${e.name}">${__("Perte")}</button>`,
						`<button class="btn btn-xs btn-warning brs-act" data-action="ajust-delta" data-ecart="${e.name}">${__("Ajustement")}</button>`
					);
					if (e.avoir_disponible >= e.ecart) {
						b.push(
							`<button class="btn btn-xs btn-info brs-act" data-action="avoir" data-ecart="${e.name}" title="${__(
								"Avoir flottant disponible : {0}",
								[fmt(e.avoir_disponible)]
							)}">${__("Avoir")}</button>`
						);
					}
				} else if (e.type_ecart === "Sans pièce") {
					b.push(
						`<button class="btn btn-xs btn-warning brs-act" data-action="ajust-piece" data-ecart="${e.name}">${__("Ajustement")}</button>`
					);
					if (e.ignorable) {
						b.push(
							`<button class="btn btn-xs btn-secondary brs-act" data-action="ignorer" data-ecart="${e.name}">${__("Ignorer")}</button>`
						);
					}
				}
				actions = b.join(" ");
			} else {
				actions = frappe.utils.escape_html(
					(e.resolution || "") +
						(e.piece_resolution ? " → " + e.piece_resolution : "")
				);
			}
			return `<tr>
				<td>${frappe.utils.escape_html(e.type_ecart)} <span style="color:var(--text-muted);font-size:10px">${frappe.utils.escape_html(
				e.flux || ""
			)}</span> ${badge}</td>
				<td>${frappe.utils.escape_html(e.suivi || "")}</td>
				<td>${frappe.utils.escape_html(e.client || "")}</td>
				<td class="text-right">${fmt(e.montant_advice)}</td>
				<td class="text-right">${fmt(e.montant_piece)}</td>
				<td class="text-right">${fmt(e.ecart)}</td>
				<td>${actions}</td>
			</tr>`;
		})
		.join("");
	const d = new frappe.ui.Dialog({
		title: __("Écarts Aramex — {0}", [frm.doc.name]),
		size: "extra-large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "tbl",
				options: `<table class="table table-bordered" style="font-size:12px">
					<thead><tr>
						<th>${__("Type")}</th><th>${__("Suivi")}</th><th>${__("Client")}</th>
						<th class="text-right">${__("Advice")}</th>
						<th class="text-right">${__("Pièce")}</th>
						<th class="text-right">${__("Écart")}</th><th>${__("Résolution")}</th>
					</tr></thead><tbody>${rows}</tbody></table>`,
			},
		],
		// Les écarts sont des constats FIGÉS au rapprochement : une Payment Entry
		// corrigée à la main ensuite ne les change pas. Ce bouton confronte les
		// écarts encore à traiter à l'état actuel de la base.
		primary_action_label: __("Recalculer les écarts"),
		primary_action() {
			frappe.call({
				method: "bank_retenue_sync.encaissement.ecarts.recalculer",
				args: { encaissement: frm.doc.name },
				freeze: true,
				freeze_message: __("Confrontation aux paiements actuels…"),
				callback: (r) => {
					const m = r.message || {};
					d.hide();
					frappe.msgprint({
						title: __("Recalcul terminé"),
						indicator: (m.orphelins || []).length ? "orange" : "green",
						message:
							__("{0} écart(s) fermé(s), {1} mis à jour, {2} inchangé(s).", [
								(m.fermes || []).length,
								(m.maj || []).length,
								m.inchanges || 0,
							]) +
							((m.orphelins || []).length
								? "<br>" +
								  __("⚠️ Pièce introuvable pour : {0}", [
										frappe.utils.escape_html((m.orphelins || []).join(", ")),
								  ])
								: ""),
					});
					frm.reload_doc();
				},
			});
		},
	});
	d.show();
	const done = () => {
		d.hide();
		frm.reload_doc();
	};
	const NOTE_FIELD = {
		fieldtype: "Small Text",
		fieldname: "note",
		label: __("Note (traçabilité — posée dans la référence des pièces créées)"),
	};
	const ACTIONS = {
		perte: {
			titre: __("Perte de non paiement"),
			fields: [NOTE_FIELD],
			args: (e, v) => ({ ecart: e, note: v.note }),
			method: "resoudre_perte",
			description: __(
				"La PE sera remplacée au montant réellement versé ; la différence part en Perte de non paiement, liée à la commande."
			),
		},
		"ajust-delta": {
			titre: __("Ajustement — le delta redevient une dette"),
			fields: [NOTE_FIELD],
			args: (e, v) => ({ ecart: e, note: v.note }),
			method: "resoudre_ajustement",
			description: __(
				"La PE sera remplacée au montant réellement versé ; le delta est recréé en dette (« Dette non payée », compte Dettes) liée à la commande."
			),
		},
		"ajust-piece": {
			titre: __("Ajustement — reclasser une PE existante"),
			fields: [
				{
					fieldtype: "Link",
					options: "Payment Entry",
					fieldname: "pe_source",
					reqd: 1,
					label: __("Payment Entry à reclasser (erreur de saisie)"),
					get_query: () => ({ filters: { docstatus: 1, payment_type: "Receive" } }),
				},
				NOTE_FIELD,
			],
			args: (e, v) => ({ ecart: e, pe_source: v.pe_source, note: v.note }),
			method: "resoudre_ajustement",
			description: __(
				"La PE choisie sera reclassée sur Livraison Aramex avec le n° de suivi de l'advice."
			),
		},
		avoir: {
			titre: __("Paiement par avoir"),
			fields: [NOTE_FIELD],
			args: (e, v) => ({ ecart: e, note: v.note }),
			method: "resoudre_avoir",
			description: __(
				"La PE sera remplacée au montant réellement versé ; une ligne d'échéancier « Avoir client » du delta est ajoutée à la commande (l'avoir flottant existant s'y applique)."
			),
		},
		regularisation: {
			titre: __("Régularisation — la pièce était trop basse"),
			fields: [NOTE_FIELD],
			args: (e, v) => ({ ecart: e, note: v.note }),
			method: "resoudre_regularisation",
			description: __(
				"La PE sera portée au montant de l'advice (le client a bien payé ce montant). Le supplément est affecté aux pièces de la commande tant qu'elles peuvent l'absorber ; le reste demeure au crédit du client."
			),
		},
		ignorer: {
			titre: __("Ignorer cet écart"),
			fields: [NOTE_FIELD],
			args: (e, v) => ({ ecart: e, note: v.note }),
			method: "resoudre_ignorer",
			description: __(
				"Aucune pièce ne sera créée ; le montant sera constaté par l'écart de paiement mensuel."
			),
		},
	};
	d.$wrapper.find(".brs-act").on("click", function () {
		const action = ACTIONS[this.dataset.action];
		const ecart = this.dataset.ecart;
		const dd = new frappe.ui.Dialog({
			title: action.titre,
			fields: [
				{ fieldtype: "HTML", options: `<p>${action.description}</p>` },
				...action.fields,
			],
			primary_action_label: __("Confirmer"),
			primary_action(v) {
				dd.hide();
				frappe
					.call({
						method: "bank_retenue_sync.encaissement.ecarts." + action.method,
						args: action.args(ecart, v),
						freeze: true,
					})
					.then(done);
			},
		});
		dd.show();
	});
}
