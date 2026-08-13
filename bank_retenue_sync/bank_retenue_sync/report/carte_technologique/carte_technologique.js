// Suivi de la carte technologique. Aucune borne de date par défaut : le solde disponible est la
// question du jour, et il se lit sur toute l'histoire du compte.
//
// Deux actions viennent s'ajouter au tableau, parce que le tableau seul ne répond pas à la
// question qu'on se pose devant lui :
//   « Contrôle du relevé »   — mes livres sont-ils à jour avec la carte ? (lecture seule)
//   « Générer les écritures » — créer celles qui manquent, avec un essai à blanc d'abord.
// Le tableau lit le GRAND LIVRE ; le contrôle lit le RELEVÉ DE CARTE. Ce que le premier ne peut
// pas montrer, par construction, c'est ce qui n'a jamais été saisi — d'où le second.
frappe.query_reports["Carte technologique"] = {
  filters: [
    {
      fieldname: "compte",
      label: __("Compte"),
      fieldtype: "Link",
      options: "Account",
      default: "Carte technologique - A&S",
      reqd: 1,
    },
    { fieldname: "date_from", label: __("Du"), fieldtype: "Date" },
    { fieldname: "date_to", label: __("Au"), fieldtype: "Date" },
  ],

  onload(report) {
    report.page.add_inner_button(__("Contrôle du relevé"), () => controle(report, 0));
    // Rafraîchir déclenche un nouveau scraping chez la banque : c'est long et ça sollicite le
    // service, donc jamais implicite — le contrôle courant lit le dernier export disponible.
    report.page.add_inner_button(__("Contrôle (relevé rafraîchi)"), () => controle(report, 1));
    if (frappe.user.has_role("System Manager")) {
      report.page.add_inner_button(__("Générer les écritures"), () => generer(report));
    }
    // Le verdict « à jour / en retard » est posé sur la page dès l'ouverture : c'est la première
    // chose qu'on veut savoir, et elle ne se déduit d'aucune colonne du tableau.
    etat(0).then((d) => d && indicateur(report, d));
  },

  formatter(value, row, column, data, default_formatter) {
    value = default_formatter(value, row, column, data);
    // Une recharge non rapprochée au relevé est la seule anomalie que ce tableau peut détecter.
    if (data && column.fieldname === "rapproche" && data.rapproche === "non") {
      value = `<span style="color:#a93226;font-weight:600">${value}</span>`;
    }
    // Le solde qui descend est l'information opérationnelle : recharger avant la panne.
    if (data && column.fieldname === "solde" && flt(data.solde) < 500) {
      value = `<span style="color:#b9770e;font-weight:600">${value}</span>`;
    }
    return value;
  },
};

// Lecture seule : ne crée rien, ne modifie rien. Échoue en silence si le service bancaire est
// muet — le suivi comptable doit rester consultable sans lui.
function etat(refresh) {
  return frappe
    .call({
      method: "bank_retenue_sync.bank.cartes.controle_releve",
      args: { refresh: refresh || 0 },
      freeze: !!refresh,
      freeze_message: __("Lecture du relevé de carte chez la banque…"),
    })
    .then((r) => r.message)
    .catch(() => null);
}

function indicateur(report, d) {
  if (d.erreur) {
    report.page.set_indicator(__("Relevé de carte illisible"), "grey");
    return;
  }
  const n = d.resume.a_comptabiliser;
  if (n) {
    report.page.set_indicator(__("{0} paiement(s) non comptabilisé(s)", [n]), "red");
  } else if (d.a_jour) {
    report.page.set_indicator(__("Comptabilité à jour"), "green");
  } else {
    // Rien à saisir, mais les soldes ne concordent pas (ou le service n'a pas répondu) : ni
    // alerte ni feu vert — dire « à jour » ici serait affirmer un contrôle qu'on n'a pas fait.
    report.page.set_indicator(__("Écart à vérifier"), "orange");
  }
}

function controle(report, refresh) {
  etat(refresh).then((d) => {
    if (!d || d.erreur) {
      frappe.msgprint({
        title: __("Contrôle impossible"),
        message:
          __("Le relevé de carte n'a pas pu être lu (service bancaire injoignable).") +
          ((d && d.erreur && `<br><span class="text-muted">${frappe.utils.escape_html(d.erreur)}</span>`) || ""),
        indicator: "orange",
      });
      return;
    }
    indicateur(report, d);
    const dialog = new frappe.ui.Dialog({
      title: __("Contrôle du relevé de carte"),
      size: "extra-large",
      fields: [{ fieldtype: "HTML", fieldname: "corps" }],
    });
    dialog.fields_dict.corps.$wrapper.html(html_controle(d));
    // Le bouton de création n'apparaît que s'il y a quelque chose à créer : proposer « générer »
    // devant un contrôle vert invite à cliquer pour rien.
    if (d.resume.a_comptabiliser && frappe.user.has_role("System Manager")) {
      dialog.set_primary_action(
        __("Générer les {0} écriture(s) manquante(s)", [d.resume.a_comptabiliser]),
        () => {
          dialog.hide();
          generer(report);
        }
      );
    }
    dialog.show();
  });
}

// Le service rend un horodatage ISO (« 2026-08-11T19:12:24 ») que `str_to_user` ne sait pas lire :
// le « T » lui fait rendre une date invalide. La date de lecture du relevé est justement ce qui
// dit si le contrôle porte sur un état récent — l'afficher faux serait pire que ne rien afficher.
function lu_le(d) {
  if (!d.lu_le) return "—";
  return frappe.datetime.str_to_user(String(d.lu_le).replace("T", " ")) || "—";
}

function html_controle(d) {
  const r = d.resume;
  const dt = (v) => format_currency(v, "TND");
  const verdict = r.a_comptabiliser
    ? `<div style="padding:10px;border-radius:4px;background:#fbe9e7;color:#a93226">
         <b>${__("Les livres sont en retard sur la carte.")}</b><br>
         ${__("{0} paiement(s) du relevé, soit {1}, n'ont aucune écriture.", [
           r.a_comptabiliser,
           dt(r.montant_a_comptabiliser),
         ])}
       </div>`
    : d.a_jour
      ? `<div style="padding:10px;border-radius:4px;background:#e8f5e9;color:#1e7e34">
           <b>${__("Comptabilité à jour.")}</b><br>
           ${__("Tous les paiements approuvés du relevé sont comptabilisés, et le solde des livres égale le solde réel de la carte.")}
         </div>`
      : `<div style="padding:10px;border-radius:4px;background:#fff4e5;color:#b9770e">
           <b>${__("Rien à saisir, mais les soldes ne concordent pas.")}</b><br>
           ${__("Aucun paiement du relevé ne manque : l'écart restant est un frais prélevé par la banque, régularisé par la tâche quotidienne.")}
         </div>`;

  const soldes = `
    <table class="table table-bordered" style="margin-top:12px">
      <tr>
        <td>${__("Solde comptable (ERPNext)")}</td><td align="right"><b>${dt(d.solde_comptable)}</b></td>
        <td>${__("Solde réel (carte)")}</td>
        <td align="right"><b>${d.solde_reel === null ? "—" : dt(d.solde_reel)}</b></td>
        <td>${__("Écart")}</td>
        <td align="right"><b style="color:${Math.abs(d.ecart || 0) < 0.5 ? "#1e7e34" : "#a93226"}">
          ${d.ecart === null ? "—" : dt(d.ecart)}</b></td>
      </tr>
      <tr>
        <td>${__("Relevé lu le")}</td><td align="right">${lu_le(d)}</td>
        <td>${__("Plafond annuel restant")}</td>
        <td align="right">${d.plafond_restant === null ? "—" : dt(d.plafond_restant)}</td>
        <td>${__("Seuil de recharge")}</td><td align="right">${dt(d.seuil_recharge)}</td>
      </tr>
    </table>`;

  // Les refus figurent dans la liste et n'en sortiront pas : c'est en les voyant écartés qu'on
  // vérifie qu'ils l'ont été. Sur l'export du 21/07, 8 lignes sur 17 étaient des refus — les
  // comptabiliser aurait inventé 3 438 DT de charges.
  const etats = {
    comptabilisee: [__("comptabilisée"), "#1e7e34"],
    a_comptabiliser: [__("à comptabiliser"), "#a93226"],
    refusee: [__("refusée par la carte"), "#8d8d8d"],
  };
  const lignes = d.lignes
    .map((l) => {
      const [libelle, couleur] = etats[l.etat];
      const je = l.journal_entry
        ? `<a href="/app/journal-entry/${encodeURIComponent(l.journal_entry)}">${l.journal_entry}</a>
           ${l.brouillon ? `<span style="color:#b9770e;font-size:11px"> (${__("brouillon")})</span>` : ""}`
        : "";
      return `<tr style="${l.etat === "refusee" ? "opacity:.6" : ""}">
          <td>${frappe.datetime.str_to_user(l.date)}</td>
          <td>${frappe.utils.escape_html(l.detail || "")}</td>
          <td>${frappe.utils.escape_html(l.statut || "")}</td>
          <td align="right">${dt(l.montant)}</td>
          <td>${frappe.utils.escape_html(l.reference || "")}</td>
          <td style="color:${couleur};font-weight:600">${libelle}</td>
          <td>${je}</td>
        </tr>`;
    })
    .join("");

  return `${verdict}${soldes}
    <table class="table table-bordered" style="font-size:12px">
      <thead><tr>
        <th>${__("Date")}</th><th>${__("Détail")}</th><th>${__("Statut carte")}</th>
        <th align="right">${__("Montant")}</th><th>${__("Référence")}</th>
        <th>${__("État")}</th><th>${__("Écriture")}</th>
      </tr></thead>
      <tbody>${lignes}</tbody>
    </table>
    <div class="text-muted" style="font-size:11px">
      ${__("{0} ligne(s) au relevé : {1} comptabilisée(s), {2} à comptabiliser, {3} refusée(s) par la carte (aucun mouvement d'argent).", [
        r.total,
        r.comptabilisees,
        r.a_comptabiliser,
        r.refusees,
      ])}
    </div>`;
}

// Essai à blanc d'abord, création ensuite. Deux temps volontairement : une écriture de journal
// soumise ne se retire pas d'un clic, et l'utilisateur doit voir CE QUI SERA CRÉÉ avant de le
// créer — le montant, la date et la référence de chaque ligne.
function generer(report) {
  const dialog = new frappe.ui.Dialog({
    title: __("Générer les écritures de la carte"),
    fields: [
      {
        fieldtype: "Check",
        fieldname: "refresh",
        label: __("Rafraîchir le relevé chez la banque avant (plus lent)"),
        default: 0,
        description: __(
          "Sans cette option, le dernier relevé exporté est utilisé. Les paiements du jour même peuvent en être absents."
        ),
      },
      { fieldtype: "HTML", fieldname: "apercu" },
    ],
    primary_action_label: __("Essai à blanc"),
    primary_action: () => {
      const refresh = dialog.get_value("refresh");
      frappe
        .call({
          method: "bank_retenue_sync.orchestrator.run_cartes",
          args: { insert: 0, refresh: refresh ? 1 : 0 },
          freeze: true,
          freeze_message: __("Lecture du relevé et simulation…"),
        })
        .then((r) => {
          const res = r.message || {};
          const a_creer = (res.ecritures || []).filter((e) => e.status === "created");
          dialog.fields_dict.apercu.$wrapper.html(html_apercu(res, a_creer));
          if (!a_creer.length) {
            dialog.set_primary_action(__("Fermer"), () => dialog.hide());
            return;
          }
          // Le relevé a déjà été rafraîchi à l'essai : le refaire à la création rallongerait
          // l'attente et, surtout, ferait porter la création sur un relevé DIFFÉRENT de celui
          // qui vient d'être validé à l'écran.
          dialog.set_primary_action(
            __("Créer les {0} écriture(s)", [a_creer.length]),
            () => {
              frappe
                .call({
                  method: "bank_retenue_sync.orchestrator.run_cartes",
                  args: { insert: 1, refresh: 0 },
                  freeze: true,
                  freeze_message: __("Création des écritures…"),
                })
                .then((r2) => {
                  dialog.hide();
                  const cree = (r2.message.ecritures || []).filter((e) => e.status === "created");
                  frappe.show_alert(
                    { message: __("{0} écriture(s) créée(s)", [cree.length]), indicator: "green" },
                    7
                  );
                  frappe.msgprint({
                    title: __("Écritures créées"),
                    message: html_apercu(r2.message || {}, cree, true),
                    indicator: "green",
                  });
                  report.refresh();
                  etat(0).then((d) => d && indicateur(report, d));
                });
            }
          );
        });
    },
  });
  dialog.fields_dict.apercu.$wrapper.html(
    `<div class="text-muted">${__("Lancez l'essai à blanc pour voir ce qui serait créé. Rien n'est écrit à cette étape.")}</div>`
  );
  dialog.show();
}

function html_apercu(res, ecritures, cree) {
  const dt = (v) => format_currency(v, "TND");
  const erreurs = (res.ecritures || []).filter((e) => e.status === "error");
  if (!ecritures.length && !erreurs.length) {
    return `<div style="padding:10px;border-radius:4px;background:#e8f5e9;color:#1e7e34">
      <b>${__("Aucune écriture à créer.")}</b><br>
      ${__("Tous les paiements approuvés du relevé sont déjà comptabilisés.")}
      ${html_frais(res)}</div>`;
  }
  const lignes = ecritures
    .map(
      (e) => `<tr>
        <td>${frappe.datetime.str_to_user(e.date)}</td>
        <td>${frappe.utils.escape_html(e.ref || "")}</td>
        <td align="right">${dt(e.montant)}</td>
        <td>${cree && e.je !== "(dry-run)" ? `<a href="/app/journal-entry/${encodeURIComponent(e.je)}">${e.je}</a>` : ""}</td>
      </tr>`
    )
    .join("");
  const erreurs_html = erreurs.length
    ? `<div style="color:#a93226;margin-top:8px">${erreurs
        .map((e) => `${frappe.utils.escape_html(e.ref)} : ${frappe.utils.escape_html(e.error)}`)
        .join("<br>")}</div>`
    : "";
  return `
    <div style="margin-bottom:6px">${
      cree
        ? __("Écritures créées — Dr Frais de Marketing / Cr Carte technologique :")
        : __("Seraient créées — Dr Frais de Marketing / Cr Carte technologique :")
    }</div>
    <table class="table table-bordered" style="font-size:12px">
      <thead><tr><th>${__("Date")}</th><th>${__("Référence")}</th>
        <th align="right">${__("Montant")}</th><th>${__("Écriture")}</th></tr></thead>
      <tbody>${lignes}</tbody>
    </table>${erreurs_html}${html_frais(res)}`;
}

// La régularisation de frais n'est PAS un paiement : elle aligne le solde comptable sur le solde
// réel, et seulement une fois toutes les opérations reconnues. La montrer à part évite de la
// confondre avec une charge de publicité.
function html_frais(res) {
  const f = res.frais;
  if (!f || !f.statut) return "";
  const messages = {
    aligne: __("Soldes alignés : aucun frais à régulariser."),
    "operations non comptabilisees": __(
      "Frais non régularisés : des paiements du relevé restent à comptabiliser. L'écart n'est pas un frais tant qu'ils manquent."
    ),
    "deja regularise": __("Frais déjà régularisés pour cette lecture du solde."),
    "a creer": __("Un frais bancaire de {0} serait créé pour aligner les soldes.", [
      format_currency(f.montant, "TND"),
    ]),
    regularise: __("Frais bancaire de {0} créé ({1}).", [format_currency(f.montant, "TND"), f.je]),
  };
  const m = messages[f.statut] || f.statut;
  return `<div class="text-muted" style="font-size:11px;margin-top:8px">${m}</div>`;
}
