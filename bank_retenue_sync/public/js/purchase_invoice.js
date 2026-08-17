// Facture d'achat locale : lire le scan, voir ce qui bloque, voir la retenue due.
//
// L'écran ne décide rien : tout vient de bank_retenue_sync.achat.facture. Il rend visible AVANT la
// validation ce que le contrôle refuserait au moment de valider — sans quoi l'utilisateur découvre
// le problème au pire moment, après avoir tout saisi.

frappe.ui.form.on("Purchase Invoice", {
  refresh(frm) {
    // Le certificat ne se déclare qu'APRÈS validation : avant, ni le montant ni la date de la
    // retenue ne sont définitifs, et TEJ n'accepte pas de correction sans annulation.
    if (frm.doc.docstatus === 1) return bouton_certificat(frm);
    if (frm.doc.docstatus !== 0 || frm.is_new()) return;
    frm.add_custom_button(__("Lire le scan"), () => lire(frm), __("Facture fournisseur"));
    frm.add_custom_button(__("Vérifier avant validation"), () => verifier(frm),
                          __("Facture fournisseur"));
    frm.add_custom_button(__("Recalculer la retenue à la source"), () => poser(frm),
                          __("Facture fournisseur"));
  },
  onload_post_render: (frm) => frm.doc.docstatus === 1 && bouton_certificat(frm),
});

// ── Certificat de retenue à la source émis sur TEJ ─────────────────────────────
//
// Bouton rouge au bout de la barre d'onglets, après « Connexions » : c'est un geste qui parle à
// l'administration fiscale, il ne se range pas dans un menu déroulant avec le reste.
const CLASSE_ITEM = "brs-cert-nav-item";

function poser_css_certificat() {
  if (document.getElementById("brs-cert-css")) return;
  const style = document.createElement("style");
  style.id = "brs-cert-css";
  style.textContent = `
    .${CLASSE_ITEM} { margin-left: auto; display: flex; align-items: center; padding-right: 8px; }
    .brs-cert-btn { background: #dc2626; color: #fff; border: none; border-radius: 6px;
                    padding: 4px 12px; font-size: 12px; font-weight: 600; cursor: pointer;
                    white-space: nowrap; }
    .brs-cert-btn:hover { background: #b91c1c; }
    .brs-cert-btn.fait { background: #16a34a; }
    .brs-cert-btn.fait:hover { background: #15803d; }
    .brs-cert-btn.attente { background: #ea580c; }
    .brs-cert-btn.attente:hover { background: #c2410c; }
  `;
  document.head.appendChild(style);
}

function bouton_certificat(frm) {
  frappe.call({
    method: "bank_retenue_sync.tej.emis.preparer",
    args: { facture: frm.doc.name },
    callback: (r) => {
      const ctx = r.message || {};
      // Aucune retenue : rien à déclarer, et un bouton inerte ferait douter.
      if (!ctx.retenue_facture) return;
      poser_css_certificat();
      etat_certificat(frm, ctx);
      const $tabs = frm.$wrapper.find("ul.form-tabs").first();
      const fait = !!ctx.deja_emis;
      const dep = ctx.depot_en_cours;
      // ⚠️ UN BOUTON ROUGE SUR UNE DÉCLARATION DÉJÀ PARTIE INVITE AU CLIC DE TROP. Rouge veut dire
      // « à faire » ; tant qu'un dépôt est en analyse, il n'y a rien à faire qu'attendre, et
      // l'état devait se lire SANS ouvrir le dialogue.
      const classe = fait ? "fait" : dep ? "attente" : "";
      const libelle = fait
        ? __("Certificat émis ✓")
        : dep
          ? dep.statut === "en_envoi"
            ? __("Envoi en cours ⏳")
            : __("Dépôt en analyse ⏳")
          : __("Certificat de retenue (TEJ)");
      const action = () => (fait ? voir_certificat(ctx) : ouvrir_certificat(frm, ctx));
      if (!$tabs.length) {
        // Repli : sans barre d'onglets le bouton disparaîtrait en silence, et la fonction
        // passerait pour absente.
        frm.add_custom_button(libelle, action, __("Facture fournisseur"));
        return;
      }
      $tabs.find("." + CLASSE_ITEM).remove();
      const $btn = $(
        `<button type="button" class="brs-cert-btn ${classe}">${libelle}</button>`
      ).on("click", action);
      $(`<li class="nav-item ${CLASSE_ITEM}"></li>`).append($btn).appendTo($tabs);
    },
  });
}

/** L'état de l'émission, lisible à l'ouverture de la facture — sans un clic.
 *
 * ⚠️ SANS ÇA, UNE DÉCLARATION DÉJÀ CHEZ LE FISC NE SE VOIT NULLE PART. Le serveur renvoyait déjà
 * `depot_en_cours` à chaque ouverture ; l'écran ne s'en servait qu'APRÈS un clic, dans un modal.
 * Le formulaire affichait donc exactement la même chose pour « rien n'a été émis » et pour
 * « le dépôt IN260052 est chez l'administration fiscale ».
 */
function etat_certificat(frm, ctx) {
  if (ctx.deja_emis) {
    frm.dashboard.add_indicator(__("Certificat TEJ émis"), "green");
    return;
  }
  const dep = ctx.depot_en_cours;
  if (!dep) return;
  const envoi = dep.statut === "en_envoi";
  frm.dashboard.add_indicator(
    envoi
      ? __("Certificat TEJ : envoi en cours")
      : __("Certificat TEJ : dépôt {0} en analyse", [dep.numero || "—"]),
    envoi ? "blue" : "orange"
  );
  const lignes = [
    envoi
      ? __("La déclaration est en cours d'envoi sur le portail TEJ depuis {0}.", [
          frappe.utils.escape_html((dep.soumis_le || "").substring(0, 16)),
        ])
      : __(
          "Déclaration déposée chez TEJ le {0} — dépôt <b>{1}</b>. Le certificat et sa référence n'existeront qu'après l'analyse de ce dépôt.",
          [
            frappe.utils.escape_html((dep.soumis_le || "").substring(0, 16)),
            frappe.utils.escape_html(dep.numero || "—"),
          ]
        ),
  ];
  // Le message vient du service et s'adresse à un opérateur : il dit s'il faut attendre ou
  // surtout pas resoumettre. Il dormait dans un champ que personne n'ouvre.
  if (dep.message) {
    lignes.push(`<span class="text-muted">${frappe.utils.escape_html(dep.message)}</span>`);
  }
  if (!envoi) {
    lignes.push(
      __("Dernière vérification : {0} ({1}).", [
        frappe.utils.escape_html((dep.derniere_verification || "").substring(0, 16) || __("jamais")),
        dep.verifications || 0,
      ])
    );
  }
  lignes.push(`<b>${__("Ne resoumets pas.")}</b>`);
  frm.dashboard.add_comment(lignes.join("<br>"), envoi ? "blue" : "orange", true);
}

function voir_certificat(ctx) {
  frappe.msgprint({
    title: __("Certificat déjà émis"),
    indicator: "green",
    message: `<p>${__("Référence")} : <b>${frappe.utils.escape_html(ctx.deja_emis.reference)}</b></p>
      <p><a href="${frappe.utils.escape_html(ctx.deja_emis.file_url)}" target="_blank">${__(
        "Ouvrir le PDF joint à la facture"
      )} ↗</a></p>
      <p class="text-muted">${__(
        "Un certificat ne s'émet qu'une fois : l'administration et le fournisseur en verraient deux."
      )}</p>`,
  });
}

function ouvrir_certificat(frm, ctx) {
  const dev = frm.doc.currency || "TND";
  if (ctx.manques && ctx.manques.length) {
    frappe.msgprint({
      title: __("Impossible d'émettre le certificat"),
      indicator: "red",
      message:
        "<ul><li>" + ctx.manques.map(frappe.utils.escape_html).join("</li><li>") + "</li></ul>",
    });
    return;
  }
  // Doublon déjà visible dans le dernier export TEJ : inutile d'aller plus loin. L'inverse n'est
  // pas vrai — ne rien trouver ici ne prouve rien, l'export peut dater ; c'est la soumission qui
  // revérifie sur un export fraîchement régénéré.
  if (ctx.deja_chez_tej) {
    frappe.msgprint({
      title: __("Déjà déclaré chez TEJ"),
      indicator: "red",
      message: __(
        "Un certificat <b>{0}</b> existe déjà pour le n° <b>{1}</b> et le matricule <b>{2}</b> — référence {3}.<br><br>En émettre un second ferait apparaître deux retenues pour la même facture, chez le fournisseur comme à l'administration.",
        [
          frappe.utils.escape_html(ctx.deja_chez_tej.etat),
          frappe.utils.escape_html(ctx.bill_no),
          frappe.utils.escape_html(ctx.matricule),
          frappe.utils.escape_html(ctx.deja_chez_tej.reference),
        ]
      ),
    });
    return;
  }
  // ⚠️ UN DÉPÔT EN ANALYSE INTERDIT TOUT NOUVEAU GESTE, RÉPÉTITION COMPRISE. « Valider » ne crée
  // pas un certificat : TEJ enregistre un dépôt qu'il analyse quand il veut, et pendant ce temps
  // la déclaration EST DÉJÀ partie. Ni le PDF attaché, ni la clé d'idempotence, ni l'export des
  // certificats émis ne le voient — c'est le seul écran qui puisse le dire.
  if (ctx.depot_en_cours) {
    const dep = ctx.depot_en_cours;
    frappe.msgprint({
      title: __("Déjà déposé — en cours d'analyse"),
      indicator: "orange",
      message: __(
        "La déclaration <b>est partie</b> : TEJ a enregistré le dépôt <b>{0}</b> le {1}, et ne l'a pas encore analysé. Le certificat et sa référence n'existeront qu'ensuite.<br><br><b>Ne resoumets pas</b> : le dépôt existe déjà chez l'administration, le rejouer déclarerait deux fois.<br><br>Le suivi est automatique ({2} vérification(s) faite(s)). Tu peux aussi le relancer maintenant.",
        [
          frappe.utils.escape_html(dep.numero || "—"),
          frappe.utils.escape_html((dep.soumis_le || "").substring(0, 16)),
          dep.verifications || 0,
        ]
      ),
      primary_action: {
        label: __("Vérifier maintenant"),
        action() {
          frappe.msgprint.close_all && frappe.msgprint.close_all();
          suivre_depot(frm);
        },
      },
    });
    return;
  }
  const d = new frappe.ui.Dialog({
    title: __("Certificat de retenue à la source — TEJ"),
    size: "large",
    fields: [
      { fieldname: "recap", fieldtype: "HTML" },
      { fieldname: "date_paiement", fieldtype: "Date", label: __("Date de paiement déclarée"),
        default: ctx.date_paiement, reqd: 1,
        description: __("C'est la date que porte le certificat remis au fournisseur.") },
      { fieldname: "resultat", fieldtype: "HTML" },
    ],
    // ⚠️ LA RÉPÉTITION EST L'ACTION PAR DÉFAUT, LA SOUMISSION EST SECONDAIRE ET SE MÉRITE.
    primary_action_label: __("Répéter sans valider"),
    primary_action: () => repeter_certificat(frm, d),
  });
  d.fields_dict.recap.$wrapper.html(`
    <table class="table table-bordered" style="font-size:12px">
      <tr><td>${__("Bénéficiaire")}</td><td><b>${frappe.utils.escape_html(
        ctx.fournisseur_nom || ctx.fournisseur
      )}</b> — ${__("matricule")} ${frappe.utils.escape_html(ctx.matricule)}</td></tr>
      <tr><td>${__("N° chez le déclarant")}</td><td>${frappe.utils.escape_html(ctx.bill_no)}</td></tr>
      <tr><td>${__("Montant HT / TVA")}</td><td>${format_currency(ctx.montant_ht, dev)} · ${
    ctx.taux_tva
  } %</td></tr>
      <tr><td>${__("Retenue portée par la facture")}</td><td><b>${format_currency(
    ctx.retenue_facture,
    dev
  )}</b></td></tr>
    </table>
    <p class="text-muted">${__(
      "La répétition remplit le formulaire TEJ et relève les montants que le portail calcule, sans valider. Elle est là pour confronter ce que dit l'administration à ce que porte la facture."
    )}</p>`);
  d.show();
}

function repeter_certificat(frm, d) {
  frappe.call({
    method: "bank_retenue_sync.tej.emis.repeter",
    args: { facture: frm.doc.name },
    freeze: true,
    freeze_message: __("Remplissage du formulaire TEJ (sans valider)…"),
    callback: (r) => {
      const m = r.message || {};
      const dev = frm.doc.currency || "TND";
      const rep = m.reponse || {};
      // ⚠️ LE CALCUL EST FAIT PAR LE SERVEUR, ET C'EST UNE CORRECTION. L'écran allait chercher
      // `montant_rs` à la racine de la réponse ; le portail rend `cert_create.operations[].
      // computed.mantantRs`, et sous forme de CHAÎNE avec une espace insécable pour les milliers.
      // Résultat : le montant s'affichait dans le JSON brut pendant que le bandeau annonçait
      // « le portail n'a rien renvoyé ». Un garde-fou qui se trompe est pire que pas de garde-fou.
      const calc = m.calcule || {};
      const calcule = calc.retenue;
      const ecart = m.ecart;
      const accord = ecart !== null && ecart !== undefined && Math.abs(ecart) < 0.01;
      d.fields_dict.resultat.$wrapper.html(`
        <div style="border:1px solid ${accord ? "#16a34a" : "#dc2626"};border-radius:8px;padding:10px">
          <b>${__("Ce que TEJ a calculé")}</b>
          <div>${__("Retenue portail")} : <b>${
        calcule != null ? format_currency(calcule, dev) : "—"
      }</b>${calc.taux != null ? ` (${calc.taux} %)` : ""} · ${__(
        "facture"
      )} : ${format_currency(m.retenue_facture, dev)}</div>
          ${
            calc.ttc != null
              ? `<div class="text-muted">${__("Base TTC portail")} : ${format_currency(
                  calc.ttc,
                  dev
                )} · ${__("TVA")} : ${format_currency(calc.tva, dev)}</div>`
              : ""
          }
          ${
            ecart === null || ecart === undefined
              ? `<div style="color:var(--text-muted)">${__(
                  "Le portail n'a pas renvoyé de montant : relisez la réponse brute avant de soumettre."
                )}</div>`
              : accord
                ? `<div style="color:#16a34a">${__("Les deux concordent.")}</div>`
                : `<div style="color:#dc2626"><b>${__("Écart de {0} — ne soumettez pas sans comprendre pourquoi.", [
                    format_currency(ecart, dev),
                  ])}</b></div>`
          }
          ${
            // ⚠️ « PAS TROUVÉ » N'EST PAS « PAS DE DOUBLON ». Le serveur calcule jusqu'où
            // l'export TEJ couvre réellement, et le dit quand cette couverture s'arrête AVANT
            // la date de la facture : le contrôle anti-doublon n'a alors rien pu voir de la
            // période concernée. Ce constat existait côté serveur depuis le début et
            // n'atteignait aucun écran — un garde-fou muet ne garde rien.
            m.controle_aveugle
              ? `<div style="color:#b45309;margin-top:6px"><b>${__(
                  "Le contrôle anti-doublon repose sur un export daté."
                )}</b> ${__(
                  "Il a été généré le {0} : un certificat émis depuis n'y figure pas. Ne pas trouver de doublon ne prouve donc rien à cet instant — la soumission, elle, régénère l'export juste avant de vérifier.",
                  [frappe.utils.escape_html(m.export_genere_le || "date inconnue")]
                )}</div>`
              : ""
          }
          <pre style="max-height:160px;overflow:auto;font-size:11px;margin-top:8px">${frappe.utils.escape_html(
            JSON.stringify(rep, null, 1)
          )}</pre>
        </div>`);
      // La soumission n'apparaît qu'APRÈS une répétition : on ne déclare pas à l'aveugle.
      d.set_secondary_action_label(__("Soumettre pour de bon"));
      d.set_secondary_action(() => confirmer_soumission(frm, d, m, accord));
    },
  });
}

function confirmer_soumission(frm, d, m, accord) {
  frappe.confirm(
    __(
      "Le certificat sera <b>déclaré à l'administration</b> et remis au fournisseur. Un certificat soumis ne s'efface pas : il ne peut qu'être annulé, et l'annulation reste visible.{0}<br><br>Confirmer ?",
      [
        accord
          ? ""
          : `<br><br><span style="color:#dc2626">${__(
              "⚠️ La répétition ne concorde pas avec la facture."
            )}</span>`,
      ]
    ),
    () => {
      frappe.call({
        method: "bank_retenue_sync.tej.emis.soumettre",
        args: { facture: frm.doc.name, date_paiement: d.get_value("date_paiement") },
        freeze: true,
        freeze_message: __("Mise en file de la soumission…"),
        callback: (r) => {
          const res = r.message || {};
          // ⚠️ LA SOUMISSION EST PARTIE EN TÂCHE DE FOND, ELLE N'EST PAS FAITE. Le portail se
          // pilote au navigateur sur un service à worker unique : attendre dans la requête desk
          // bloquait l'écran plusieurs minutes et finissait coupé par le proxy — la déclaration
          // partait sans que personne ne le sache.
          if (res.statut === "en file") {
            d.hide();
            frappe.msgprint({
              title: __("Soumission lancée"),
              indicator: "blue",
              message: __(
                "La déclaration est en cours d'envoi sur le portail. Ça prend quelques minutes : tu peux fermer cette page.<br><br>Le dépôt, puis le certificat et son PDF, arriveront d'eux-mêmes sur la facture. <b>Ne relance pas</b> — la facture est déjà réservée, un second envoi déclarerait deux fois.",
              ),
              primary_action: {
                label: __("Où ça en est ?"),
                action() {
                  frappe.msgprint.close_all && frappe.msgprint.close_all();
                  suivre_depot(frm);
                },
              },
            });
            return;
          }
          // Le contrôle sur export FRAIS a trouvé un certificat que l'écran ne voyait pas : rien
          // n'a été soumis, et c'est exactement ce qu'on attend de lui.
          if (res.statut === "deja chez tej") {
            d.hide();
            frappe.msgprint({
              title: __("Rien n'a été soumis"),
              indicator: "orange",
              message: __(
                "Le portail détient déjà un certificat <b>{0}</b> pour cette facture (référence {1}). La vérification faite juste avant l'envoi l'a arrêté.",
                [
                  frappe.utils.escape_html(res.doublon.etat),
                  frappe.utils.escape_html(res.doublon.reference),
                ]
              ),
            });
            return;
          }
          // ⚠️ LE CAS NOMINAL, ET IL RESSEMBLAIT À UN ÉCHEC. TEJ enregistre un dépôt puis
          // l'analyse quand il veut : le certificat n'existe pas encore, mais LA DÉCLARATION EST
          // PARTIE. Annoncer ici « aucun certificat n'a été créé » — ce que faisait l'écran tant
          // qu'il déduisait tout de la référence — pousse au second clic, donc à la double
          // déclaration.
          if (res.statut === "depot en analyse") {
            d.hide();
            const dep = res.depot || {};
            frappe.msgprint({
              title: __("Déposé chez TEJ — analyse en cours"),
              indicator: "orange",
              message: __(
                "Le dépôt <b>{0}</b> est enregistré à l'administration fiscale. Le certificat et sa référence n'existeront que lorsque TEJ aura analysé ce dépôt.<br><br><b>Ne resoumets pas cette facture.</b> Le suivi est automatique — le certificat et son PDF arriveront d'eux-mêmes.",
                [frappe.utils.escape_html(dep.numero || "—")]
              ),
              primary_action: {
                label: __("Vérifier maintenant"),
                action() {
                  frappe.msgprint.close_all && frappe.msgprint.close_all();
                  suivre_depot(frm);
                },
              },
            });
            return;
          }
          // Ni référence, ni dépôt, ni statut connu du service : on ne SAIT pas. C'est différent
          // de « rien n'est parti », et le confondre avec un échec est précisément ce qui fait
          // recliquer.
          if (res.statut === "incertain") {
            d.hide();
            frappe.msgprint({
              title: __("Résultat incertain — ne resoumets pas"),
              indicator: "red",
              message:
                `<p>${__(
                  "Le service n'a rendu ni référence, ni numéro de dépôt, ni statut exploitable. <b>Impossible de dire si la déclaration est partie.</b> Vérifie sur le portail TEJ avant tout nouveau geste : une seconde soumission déclarerait deux fois."
                )}</p><p class="text-muted">${__("Réponse brute du service :")}</p>` +
                `<pre style="max-height:200px;overflow:auto;font-size:11px">${frappe.utils.escape_html(
                  JSON.stringify(res.reponse, null, 1)
                )}</pre>`,
            });
            return;
          }
          // ⚠️ SANS RÉFÉRENCE, RIEN N'A ÉTÉ DÉCLARÉ — et le dire autrement est un mensonge aux
          // conséquences fiscales. Le serveur ne rend « soumis » que si le portail confirme la
          // soumission ET rend une référence ; tout le reste atterrit ici.
          if (res.statut === "non soumis") {
            d.hide();
            frappe.msgprint({
              title: __("Rien n'a été déclaré"),
              indicator: "red",
              message:
                `<p>${__(
                  "Le portail n'a pas confirmé de soumission et n'a rendu aucune référence : <b>aucun certificat n'a été créé</b>."
                )}</p><p class="text-muted">${__("Réponse brute du service :")}</p>` +
                `<pre style="max-height:200px;overflow:auto;font-size:11px">${frappe.utils.escape_html(
                  JSON.stringify(res.reponse, null, 1)
                )}</pre>`,
            });
            return;
          }
          d.hide();
          const pj = res.pdf || {};
          frappe.msgprint({
            title: __("Certificat émis"),
            indicator: pj.statut === "attache" || pj.statut === "deja attache" ? "green" : "orange",
            message:
              `<p>${__("Référence")} : <b>${frappe.utils.escape_html(res.reference || "—")}</b></p>` +
              (pj.file_url
                ? `<p><a href="${frappe.utils.escape_html(pj.file_url)}" target="_blank">${__(
                    "PDF joint à la facture"
                  )} ↗</a></p>`
                : `<p style="color:#b45309">${__(
                    "Le certificat est bien émis, mais son PDF n'a pas pu être récupéré : {0}",
                    [frappe.utils.escape_html(pj.erreur || "—")]
                  )}</p>`),
          });
          frm.reload_doc();
        },
      });
    }
  );
}

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
        : `<p>${__("Scan PDF joint, totaux concordants avec le scan.")}</p>`) +
        `<p>${retenue}</p>` +
        `<p class="text-muted">${__("Le stock, le magasin, le n° et les dates du fournisseur ne bloquent plus rien : ils sont corrigés tout seuls à l'enregistrement.")}</p>`,
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
    const centre = m.centre || {};
    const fait = m.statut === "posee" || m.statut === "corrigee" || centre.statut === "pose";
    // Le centre de coûts n'est pas un détail d'écran : la ligne pointe un compte de résultat, et
    // ERPNext refuse la validation sans lui.
    const note = centre.statut === "pose"
      ? `<p>${__("Centre de coûts posé sur la ligne : {0}.", [frappe.utils.escape_html(centre.cost_center)])}</p>`
      : centre.statut === "aucun centre de couts"
        ? `<p style="color:var(--red-500)">${__("Aucun centre de coûts par défaut sur la société : la validation sera refusée tant qu'il manquera.")}</p>`
        : "";
    frappe.msgprint({
      title: __("Retenue à la source"),
      indicator: fait ? "green" : "orange",
      message: (m.statut === "posee"
        ? __("Ligne ajoutée : {0} — 1 % de {1} (TTC {2}, timbre exclu).",
             [format_currency(m.due), format_currency(m.assiette),
              format_currency(m.ttc_avant_retenue)])
        : m.statut === "corrigee"
          ? __("Ligne ramenée à {0} : {1} était saisi. Une retenue à la source se calcule, elle ne se négocie pas.",
               [format_currency(m.apres), format_currency(m.avant)])
          : __("Rien à faire ({0}).", [m.statut])) + note,
    });
  });
}

/** Où en est le dépôt de cette facture, maintenant.
 *
 * ⚠️ CET APPEL NE RESOUMET RIEN. La route de statut du service est en lecture seule au contrat —
 * c'est précisément ce qui permet de la rappeler autant qu'on veut. TEJ analyse ses dépôts quand
 * il veut, et rien de notre côté ne peut l'accélérer : ce bouton renseigne, il ne relance pas.
 */
function suivre_depot(frm) {
  frappe.call({
    method: "bank_retenue_sync.tej.emis.suivre",
    args: { facture: frm.doc.name },
    freeze: true,
    freeze_message: __("Consultation du dépôt chez TEJ…"),
    callback: (r) => {
      const res = r.message || {};
      if (res.statut === "aucun depot en analyse") {
        frappe.msgprint({
          title: __("Aucun dépôt en attente"),
          indicator: "blue",
          message: __("Cette facture n'a pas de dépôt en cours d'analyse."),
        });
        return;
      }
      if (res.statut === "genere") {
        const pj = res.pdf || {};
        frm.reload_doc();
        frappe.msgprint({
          title: __("Certificat généré"),
          indicator:
            pj.statut === "attache" || pj.statut === "deja attache" ? "green" : "orange",
          message:
            `<p>${__("Référence")} : <b>${frappe.utils.escape_html(res.reference || "—")}</b></p>` +
            (pj.file_url
              ? `<p><a href="${frappe.utils.escape_html(pj.file_url)}" target="_blank">${__(
                  "PDF joint à la facture"
                )} ↗</a></p>`
              : `<p style="color:#b45309">${__(
                  "Le PDF n'a pas pu être joint. La référence est connue : reprends-le depuis le bouton du certificat."
                )}</p>`),
        });
        return;
      }
      if (res.statut === "en_envoi") {
        // La progression vient du service lui-même (`progress.pct` / `progress.step`) : c'est la
        // seule réponse honnête à « où ça en est ? » pendant que le navigateur pilote le portail.
        const p = res.progression || {};
        const barre =
          p.pct != null
            ? `<div style="margin:10px 0"><div style="background:var(--gray-200);border-radius:4px;height:8px;overflow:hidden">
                 <div style="background:#2490ef;height:8px;width:${Math.max(
                   0,
                   Math.min(100, p.pct)
                 )}%"></div></div>
               <div class="text-muted" style="font-size:11px;margin-top:4px">${p.pct}% — ${frappe.utils.escape_html(
                 p.step || ""
               )}</div></div>`
            : "";
        frappe.msgprint({
          title: __("Envoi toujours en cours"),
          indicator: "blue",
          message:
            __(
              "La tâche lancée le {0} n'est pas encore terminée : le portail se pilote au navigateur, ça prend quelques minutes.",
              [frappe.utils.escape_html((res.soumis_le || "").substring(0, 16))]
            ) +
            barre +
            (p.erreur
              ? `<div style="color:#dc2626">${frappe.utils.escape_html(p.erreur)}</div>`
              : "") +
            `<p>${__("Rien à faire, et surtout <b>ne relance pas</b>.")}</p>`,
        });
        return;
      }
      if (res.statut === "erreur") {
        frappe.msgprint({
          title: __("Suivi impossible"),
          indicator: "red",
          message: __(
            "Le service n'a pas pu consulter le dépôt : {0}<br><br>Le dépôt reste enregistré ; rien n'est perdu et le suivi automatique réessaiera.",
            [frappe.utils.escape_html(res.erreur || "")]
          ),
        });
        return;
      }
      frappe.msgprint({
        title: __("Toujours en analyse"),
        indicator: "orange",
        message:
          __(
            "TEJ n'a pas encore analysé le dépôt. C'est normal — il le fait quand il veut. Le suivi automatique continue, et <b>il ne faut surtout pas resoumettre</b>."
          ) +
          // Ce que le service a répondu, mot pour mot : c'est lui qui sait où en est le dépôt.
          (res.message
            ? `<p class="text-muted">${frappe.utils.escape_html(res.message)}</p>`
            : ""),
      });
    },
  });
}
