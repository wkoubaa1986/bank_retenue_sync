// Bordereaux de remise (demande du 25/11/2026) — deux choses, sur toutes les pages du Desk :
//
// 1. LA BARRE DES BORDEREAUX MANQUANTS, pour ceux qui ont le rôle « Relance bordereaux » (Salma et Nejib) : la BIAT
//    a encaissé une remise de chèques sans que son bordereau soit photographié, a pris la commission d'une remise de
//    traites, ou un numéro de bordereau a sauté. La barre dit lequel ; un clic ouvre la liste, et chaque ligne a son
//    bouton « 📷 Prendre la photo » (l'IA lit le bordereau et la remise s'encaisse toute seule) ou « Pas de bordereau »
//    (avec un motif). Données : bank_retenue_sync.api.bordereaux.manquants_barre ; rafraîchie toutes les 5 minutes et
//    à chaque bordereau lu (temps réel `brs_bordereau_lu`, `brs_bordereaux_manquants`).
//
//    LES REMISES À CORRIGER (demande du 27/11/2026), pour ceux qui ont le rôle « Alerte remises à corriger » (Nejib
//    et Wassim) : un chèque ou une traite d'un bordereau introuvable dans ERPNext, une remise carte « à signaler »,
//    un écart bloquant d'encaissement (la forme que prend le même cas sur le compte Zitouna).
//    Même barre, même liste : chaque ligne dit ce qui cloche, donne une piste quand elle est sûre, et ouvre la fiche
//    à corriger. Elle disparaît d'elle-même quand le contrôle ne signale plus rien (`bank/remises_a_corriger`).
//
// 2. window.brs_bordereau_depot() : la même photo, hors de la caisse — le bouton de « Ma journée », pour celui qui
//    va à la banque et prend le bordereau en photo au guichet.
//
// L'envoi suit le même chemin que la caisse (Caisse journalière › Bordereaux de remise) : photo allégée, téléversée,
// lue par l'IA en arrière-plan (`deposer` puis `etat_depot`). L'employé voit un verdict, pas un tableau.
(() => {
  const API = "bank_retenue_sync.api.bordereaux";
  const esc = (s) => frappe.utils.escape_html(String(s == null ? "" : s));
  const TEINTES = { vert: ["#eaf7ee", "#9bd3a8", "#1a7f37"], orange: ["#fff6e9", "#f0c58a", "#b35900"],
                    rouge: ["#fdecea", "#f1a9a0", "#c0392b"], bleu: ["#eef6ff", "#b6d4fe", "#1f4e8c"] };
  const boite = (teinte, html) => {
    const [fond, bord, texte] = TEINTES[teinte] || TEINTES.bleu;
    return `<div style="background:${fond};border:1px solid ${bord};color:${texte};border-radius:8px;padding:9px 11px;font-size:13.5px">${html}</div>`;
  };

  // ── Envoi d'une photo ──────────────────────────────────────────────────────────────────────────────────────────
  // Une photo de téléphone pèse 5 à 10 Mo : ramenée à 2600 px (de quoi lire l'écriture), comme dans la caisse.
  const alleger = (fichier) => new Promise((ok) => {
    if (!/^image\//.test(fichier.type) || fichier.size < 1500000) return ok(fichier);
    const adresse = URL.createObjectURL(fichier);
    const image = new Image();
    image.onerror = () => { URL.revokeObjectURL(adresse); ok(fichier); };
    image.onload = () => {
      const echelle = Math.min(1, 2600 / Math.max(image.width, image.height));
      const toile = document.createElement("canvas");
      toile.width = Math.round(image.width * echelle); toile.height = Math.round(image.height * echelle);
      toile.getContext("2d").drawImage(image, 0, 0, toile.width, toile.height);
      URL.revokeObjectURL(adresse);
      toile.toBlob((blob) => ok(blob ? new File([blob], fichier.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" }) : fichier),
                   "image/jpeg", 0.9);
    };
    image.src = adresse;
  });
  const televerser = (fichier) => {
    const envoi = new FormData();
    envoi.append("file", fichier, fichier.name);
    envoi.append("is_private", "1");
    envoi.append("folder", "Home/Attachments");
    return fetch("/api/method/upload_file", { method: "POST", body: envoi,
        headers: { "X-Frappe-CSRF-Token": frappe.csrf_token, Accept: "application/json" } })
      .then((r) => r.json().catch(() => ({})).then((j) => {
        if (!r.ok || !j.message || !j.message.file_url) throw new Error(__("Le fichier n’a pas pu être envoyé."));
        return j.message.file_url;
      }));
  };
  // Attend la fin de la lecture (l'IA met une vingtaine de secondes) -> {etat, fichier, name?, detail?, message?, propose?}
  const attendre = (fichier) => new Promise((ok) => {
    let essais = 0;
    const tour = () => {
      frappe.xcall(`${API}.etat_depot`, { fichier }).then((m) => {
        m = m || {};
        if ((m.etat === "en_cours" || m.etat === "inconnu" || !m.etat) && essais++ < 60) { setTimeout(tour, 4000); return; }
        if (m.etat === "collecte" || m.etat === "deja") {
          frappe.xcall(`${API}.detail`, { name: m.name }).then((d) => ok({ ...m, fichier, detail: d || {} }))
            .catch(() => ok({ ...m, fichier, detail: {} }));
        } else ok({ ...m, fichier });
      }).catch(() => { if (essais++ < 60) setTimeout(tour, 4000); else ok({ etat: "echec", fichier }); });
    };
    setTimeout(tour, 3000);
  });
  // montrer(teinte, html) suit l'envoi ; la promesse rend le résultat d'`attendre`.
  const envoyer = (fichier, attendu, montrer) => {
    montrer("bleu", `⏫ ${__("Envoi du bordereau…")}`);
    return alleger(fichier).then(televerser).then((adresse) => {
      montrer("bleu", `⏳ ${__("Vérification du bordereau… (une vingtaine de secondes)")}`);
      const args = { fichier: adresse };
      if (attendu) args.attendu = JSON.stringify(attendu);
      return frappe.xcall(`${API}.deposer`, args).then(() => attendre(adresse));
    });
  };

  // Le verdict, comme dans la caisse : tout est bon, ou ce qu'il faut signaler.
  const verdict = (b) => {
    const signale = (b.a_signaler || "").split("\n").filter(Boolean);
    const quoi = `${__("Bordereau")} ${esc(b.banque || "")} n° ${esc(b.numero || "")}`;
    if (b.nature === "Espèces") return ["vert", `✔ <b>${quoi} ${__("enregistré.")}</b>`];
    if (b.lecture_statut === "En attente") return ["bleu", `⏳ <b>${quoi} ${__("enregistré.")}</b> ${__("Vérification en cours…")}`];
    if (b.lecture_statut !== "Lue") {
      return ["orange", `⚠ <b>${quoi} ${__("enregistré, mais pas vérifié.")}</b> ${__("L’IA n’a pas pu le lire : signalez-le à votre responsable.")}`];
    }
    if (!signale.length) return ["vert", `✔ <b>${__("Tout est bon.")}</b> ${quoi} ${__("enregistré, tout est retrouvé dans ERPNext.")}`];
    return ["orange", `⚠ <b>${quoi} ${__("enregistré. À signaler à votre responsable :")}</b>
      <ul style="margin:4px 0 0;padding-left:18px">${signale.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`];
  };
  const autre_numero = (m, attendu) => {
    const lu = String((m.detail || {}).numero || "").replace(/\D/g, ""), voulu = String((attendu || {}).numero || "").replace(/\D/g, "");
    return lu && voulu && lu !== voulu
      ? `<div style="margin-top:6px">${__("Ce bordereau est le n° {0}, pas le n° {1} demandé : il est enregistré quand même, et le n° {1} reste à photographier.", [esc(lu), esc(voulu)])}</div>`
      : "";
  };

  // Ce que l'IA n'a pas pu identifier : trois champs, préremplis, et « Enregistrer » (saisie à la main de la caisse).
  const completer = ($zone, m, attendu, fini) => {
    const p = Object.assign({}, attendu || {}, Object.fromEntries(Object.entries(m.propose || {}).filter(([, v]) => v)));
    const opt = (liste, v) => liste.map((x) => `<option ${x === v ? "selected" : ""}>${esc(x)}</option>`).join("");
    $zone.html(boite(m.etat === "echec" ? "rouge" : "orange", `⚠ ${esc(m.message || __("Le bordereau n’a pas pu être identifié."))}
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">
        <select class="form-control" data-c="banque" style="flex:1 1 90px">${opt(["BIAT", "Zitouna"], p.banque || "BIAT")}</select>
        <input class="form-control" data-c="numero" inputmode="numeric" placeholder="${__("N° du bordereau")}" value="${esc(p.numero || "")}" style="flex:2 1 140px">
        <select class="form-control" data-c="nature" style="flex:1 1 110px">${opt(["Chèques", "Traites", "Espèces"], p.nature || "Chèques")}</select>
        <button class="btn btn-primary" data-c="ok" style="flex:1 1 100%">${__("Enregistrer ce bordereau")}</button>
      </div>`));
    $zone.find("[data-c=ok]").on("click", () => {
      const v = (c) => $zone.find(`[data-c=${c}]`).val();
      if (!String(v("numero") || "").replace(/\D/g, "")) { frappe.show_alert({ message: __("Indiquez le numéro imprimé sur le bordereau."), indicator: "orange" }); return; }
      frappe.call({ method: `${API}.ajouter`, freeze: true, freeze_message: __("Enregistrement…"),
        args: { banque: v("banque"), numero: v("numero"), nature: v("nature"), fichier: m.fichier,
                date_remise: p.date_remise || frappe.datetime.get_today() } })
        .then((r) => {
          const x = r.message || {};
          $zone.html(boite(x.deja ? "orange" : "vert", x.deja ? __("Ce bordereau était déjà enregistré.")
            : `✔ <b>${__("Bordereau enregistré.")}</b> ${__("La vérification se fait en arrière-plan.")}`));
          if (fini) fini();
        });
    });
  };
  // Le résultat d'un envoi, affiché dans $zone. fini() : la liste à recharger.
  const conclure = ($zone, m, attendu, fini) => {
    if (m.etat === "collecte" || m.etat === "deja") {
      const [teinte, html] = verdict(m.detail || {});
      $zone.html(boite(teinte, (m.etat === "deja" ? `<div style="margin-bottom:4px">${__("Ce bordereau était déjà enregistré.")}</div>` : "")
        + html + autre_numero(m, attendu)));
      if (fini) fini();
    } else completer($zone, m, attendu, fini);
  };
  // Les deux gros boutons ; choisir(fichier) est appelé avec la photo prise ou le fichier choisi.
  const boutons = (cle) => `
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn btn-primary" data-photo="${esc(cle)}" style="flex:1 1 180px;min-height:44px;font-weight:600">📷 ${__("Prendre la photo")}</button>
      <button class="btn btn-default" data-fichier="${esc(cle)}" style="flex:1 1 140px;min-height:44px">📎 ${__("Choisir un fichier")}</button>
    </div>`;
  // Les champs fichier cachés vont dans $hote, qui n'est jamais redessiné (la liste, elle, l'est après chaque envoi).
  const brancher = ($racine, choisir, $hote) => {
    const entree = (accept, capture) =>
      $(`<input type="file" accept="${accept}" ${capture ? 'capture="environment"' : ""} style="display:none">`).appendTo($hote || $racine);
    const $photo = entree("image/*", true), $fichier = entree("image/jpeg,image/png,application/pdf", false);
    let cle = null;
    $racine.on("click", "[data-photo]", (e) => { e.preventDefault(); cle = $(e.currentTarget).attr("data-photo"); $photo.val("").trigger("click"); });
    $racine.on("click", "[data-fichier]", (e) => { e.preventDefault(); cle = $(e.currentTarget).attr("data-fichier"); $fichier.val("").trigger("click"); });
    [$photo, $fichier].forEach(($i) => $i.on("change", (e) => {
      const f = e.currentTarget.files && e.currentTarget.files[0];
      if (f) choisir(f, cle);
    }));
  };

  // ── 2. Le dépôt hors de la caisse (bouton de Ma journée) ───────────────────────────────────────────────────────────
  window.brs_bordereau_depot = (opts) => {
    opts = opts || {};
    const d = new frappe.ui.Dialog({ title: __("Bordereau de remise"), fields: [{ fieldtype: "HTML", fieldname: "zone" }] });
    const $w = d.fields_dict.zone.$wrapper;
    $w.html(`
      <div style="font-size:13.5px;margin-bottom:10px">${__("Au guichet, juste après la remise : prenez le bordereau en photo (chèques, traites ou espèces). Il est lu et vérifié tout seul.")}</div>
      ${boutons("depot")}
      <div data-zone="message" style="margin-top:10px"></div>`);
    const $m = $w.find("[data-zone=message]");
    brancher($w, (f) => {
      envoyer(f, opts.attendu, (t, h) => $m.html(boite(t, h)))
        .then((m) => conclure($m, m, opts.attendu, opts.apres))
        .catch((e) => $m.html(boite("rouge", `✖ ${esc((e && e.message) || __("Envoi impossible."))}`)));
    });
    d.show();
    return d;
  };

  // ── 1. La barre et la liste des bordereaux manquants ───────────────────────────────────────────────────────────────
  let etat = null, $b = null, actif = true, dernier = -1, dialogue = null;
  const style = `<style id="bm-style">
    .bm-bandeau { position: fixed; top: 0; left: 0; right: 0; z-index: 1033; background: #4338ca; color: #fff; height: 40px;
      box-sizing: border-box; padding: 0 14px; font-weight: 700; font-size: 14px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,.2);
      display: flex; justify-content: center; align-items: center; gap: 10px; white-space: nowrap; overflow: hidden; }
    .bm-bandeau .bm-txt { overflow: hidden; text-overflow: ellipsis; }
    .bm-bandeau .bm-bt { background: #fff; color: #3730a3; border-radius: 999px; padding: 3px 12px; font-size: 13px; }
    @media (max-width: 768px) { .bm-bandeau { font-size: 13px; } .bm-bandeau .bm-detail { display: none; } }
    .bm-carte { border: 1px solid var(--border-color, #e2e6e9); border-radius: 10px; padding: 10px 12px; margin-bottom: 10px; }
    .bm-carte .bm-titre { font-weight: 700; font-size: 14.5px; }
    .bm-carte .bm-texte { font-size: 13px; color: var(--text-muted, #555); margin: 3px 0 9px; }
    .bm-carte .bm-non { font-size: 12.5px; display: inline-block; margin-top: 8px; }
  </style>`;
  const pluriel = (n) => (n > 1 ? `${n} ${__("bordereaux de remise à photographier")}` : __("1 bordereau de remise à photographier"));
  const pluriel_c = (n) => (n > 1 ? `${n} ${__("remises à corriger")}` : __("1 remise à corriger"));
  const corriger = () => (etat && etat.a_corriger) || [];
  // Le texte de la barre : ce qui bloque de l'argent d'abord (à corriger), puis les photos.
  const resume = () => {
    const n = etat ? etat.n || 0 : 0, c = corriger().length;
    return [c ? pluriel_c(c) : "", n ? pluriel(n) : ""].filter(Boolean).join(" · ");
  };
  const notifier = (txt) => {
    try {
      if (!("Notification" in window)) return;
      if (Notification.permission === "default") Notification.requestPermission();
      if (Notification.permission === "granted") {
        const n = new Notification("🏦 " + __("Bordereaux de remise"), { body: txt, tag: "bm-bordereaux", renotify: true });
        n.onclick = () => { window.focus(); ouvrir(); n.close(); };
      }
    } catch (e) { /* ignoré */ }
  };
  const peindre = () => {
    const n = (etat ? etat.n || 0 : 0) + corriger().length;
    if (!n) { if ($b) { $b.remove(); $b = null; } dernier = 0; return; }
    if (!document.getElementById("bm-style")) $("head").append(style);
    if (!$b) {
      $b = $(`<div class="bm-bandeau" title="${__("Voir ce qu’il y a à faire sur les remises")}"><span>🏦</span><span class="bm-txt"><span class="bm-court"></span><span class="bm-detail"></span></span><span class="bm-bt">${__("Voir")}</span></div>`).appendTo("body");
      $b.on("click", ouvrir);
    }
    const premier = corriger()[0] || (etat.lignes || [])[0];
    $b.find(".bm-court").text(resume());
    $b.find(".bm-detail").text(premier ? ` — ${premier.titre}${n > 1 ? " …" : ""}` : "");
    if (dernier >= 0 && n > dernier) notifier(resume() + (premier ? " — " + premier.titre : ""));
    dernier = n;
  };
  const carte = (l) => `
    <div class="bm-carte" data-cle="${esc(l.cle)}">
      <div class="bm-titre">${esc(l.titre)}</div>
      <div class="bm-texte">${esc(l.detail)}</div>
      ${etat.photo ? boutons(l.cle) : ""}
      <a href="#" class="bm-non" data-non="${esc(l.cle)}">${__("Pas de bordereau…")}</a>
      <div data-zone="non" style="display:none;margin-top:8px">
        <select class="form-control" data-motif>${(etat.motifs || []).map((x) => `<option>${esc(x)}</option>`).join("")}</select>
        <input class="form-control" data-note placeholder="${__("Précision (obligatoire pour « Autre »)")}" style="margin-top:6px">
        <button class="btn btn-default btn-sm" data-confirmer="${esc(l.cle)}" style="margin-top:6px">${__("Confirmer : pas de bordereau")}</button>
      </div>
    </div>`;
  // Une remise à corriger : ce qui cloche, la piste, la fiche à ouvrir. Pas de photo à prendre ici.
  const carte_corriger = (l) => `
    <div class="bm-carte" data-cle="${esc(l.cle)}" style="border-color:#f0c58a;background:#fffaf2">
      <div class="bm-titre">⚠ ${esc(l.titre)}</div>
      <div class="bm-texte" style="margin-bottom:5px">${esc(l.detail)}</div>
      <ul style="margin:0 0 8px;padding-left:18px;font-size:13.5px;font-weight:600">${(l.points || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      ${l.piste ? `<div style="margin-bottom:9px">${boite("bleu", "💡 " + esc(l.piste))}</div>` : ""}
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-primary" data-ouvrir="${esc(l.cle)}" style="flex:1 1 180px;min-height:44px;font-weight:600">${__("Ouvrir la fiche à corriger")}</button>
        ${l.verifier ? `<button class="btn btn-default" data-verifier="${esc(l.cle)}" style="flex:1 1 140px;min-height:44px">🔄 ${__("C’est corrigé : vérifier")}</button>` : ""}
      </div>
      <a href="#" class="bm-non" data-non="${esc(l.cle)}">${__("Ne plus signaler…")}</a>
      <div data-zone="non" style="display:none;margin-top:8px">
        <input class="form-control" data-note placeholder="${__("Pourquoi il n’y a rien à corriger (obligatoire)")}">
        <button class="btn btn-default btn-sm" data-confirmer="${esc(l.cle)}" style="margin-top:6px">${__("Confirmer : ne plus signaler")}</button>
      </div>
    </div>`;
  const intro_photos = `<div style="font-size:13px;color:var(--text-muted,#555);margin-bottom:8px">${
    __("La BIAT ne dit pas quels chèques ou quelles traites composent une remise : seul le bordereau le dit. Prenez-le en photo, ou demandez-le à celui qui a fait la remise.")}</div>`;
  const section = (titre) => `<div style="font-weight:700;font-size:13px;text-transform:uppercase;letter-spacing:.03em;color:var(--text-muted,#555);margin:4px 0 8px">${esc(titre)}</div>`;
  const remplir = () => {
    if (!dialogue) return;
    const $l = dialogue.fields_dict.liste.$wrapper;
    const lignes = (etat && etat.lignes) || [], c = corriger();
    if (!lignes.length && !c.length) { $l.html(boite("vert", `✔ <b>${__("Rien à faire : aucune remise à corriger, aucun bordereau ne manque.")}</b>`)); return; }
    $l.html((c.length ? section(pluriel_c(c.length)) + c.map(carte_corriger).join("") : "")
      + (lignes.length ? (c.length ? section(pluriel(lignes.length)) : "") + intro_photos + lignes.map(carte).join("") : ""));
  };
  const ouvrir = () => {
    if (dialogue) { dialogue.fields_dict.message.$wrapper.html(""); dialogue.show(); remplir(); return; }
    dialogue = new frappe.ui.Dialog({ title: __("Remises en banque : à faire"), fields: [
      { fieldtype: "HTML", fieldname: "intro" }, { fieldtype: "HTML", fieldname: "message" }, { fieldtype: "HTML", fieldname: "liste" }] });
    // Le champ « intro » ne porte plus de texte : il garde les champs fichier cachés (jamais redessiné).
    const $m = dialogue.fields_dict.message.$wrapper, $l = dialogue.fields_dict.liste.$wrapper;
    brancher($l, (f, cle) => {
      const l = ((etat && etat.lignes) || []).find((x) => x.cle === cle) || {};
      const titre = `<div style="font-weight:700;margin-bottom:3px">${esc(l.titre || "")}</div>`;
      $m.html(""); $m[0].scrollIntoView({ block: "nearest" });
      envoyer(f, l.attendu, (t, h) => $m.html(boite(t, titre + h)))
        .then((m) => conclure($m, m, l.attendu, charger))
        .catch((e) => $m.html(boite("rouge", `✖ ${esc((e && e.message) || __("Envoi impossible."))}`)));
    }, dialogue.fields_dict.intro.$wrapper);
    $l.on("click", "[data-non]", (e) => { e.preventDefault(); $(e.currentTarget).siblings("[data-zone=non]").toggle(); });
    // Remise à corriger : ouvrir sa fiche (bordereau ou remise carte), ou relancer la recherche après correction.
    $l.on("click", "[data-ouvrir]", (e) => {
      const l = corriger().find((x) => x.cle === $(e.currentTarget).attr("data-ouvrir"));
      if (!l) return;
      dialogue.hide();
      frappe.set_route(...(l.route || ["Form", l.doctype, l.name]));
    });
    $l.on("click", "[data-verifier]", (e) => {
      const l = corriger().find((x) => x.cle === $(e.currentTarget).attr("data-verifier"));
      if (!l) return;
      frappe.call({ method: `${API}.verifier`, args: { name: l.name }, freeze: true, freeze_message: __("Recherche dans ERPNext…") })
        .then((r) => {
          const signale = ((r.message || {}).a_signaler || "").split("\n").filter(Boolean);
          $m.html(signale.length
            ? boite("orange", `⚠ <b>${esc(l.titre)}</b> — ${__("toujours à corriger :")}<ul style="margin:4px 0 0;padding-left:18px">${signale.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`)
            : boite("vert", `✔ <b>${esc(l.titre)}</b> — ${__("tout est retrouvé dans ERPNext. La remise s’encaisse au prochain passage.")}`));
          $m[0].scrollIntoView({ block: "nearest" });
          charger();
        });
    });
    $l.on("click", "[data-confirmer]", (e) => {
      const $c = $(e.currentTarget).closest(".bm-carte");
      frappe.call({ method: `${API}.ecarter_manquant`, freeze: true,
        args: { cle: $(e.currentTarget).attr("data-confirmer"), motif: $c.find("[data-motif]").val() || "Autre", note: $c.find("[data-note]").val() } })
        .then((r) => { if (r.message) { etat = r.message; peindre(); remplir(); } });
    });
    dialogue.show();
    remplir();
  };
  const charger = () => {
    if (!actif) return;
    frappe.xcall(`${API}.manquants_barre`).then((c) => {
      if (c === null || c === undefined) { actif = false; return; }
      etat = c;
      peindre();
      if (dialogue && dialogue.display) remplir();
    }).catch(() => {});
  };

  $(document).on("app_ready", () => {
    if (window.frameElement) return;   // fiche ouverte en fenêtre (ouvrir_document.js) : l'alerte est déjà dessous
    if (!frappe.session || frappe.session.user === "Guest") return;
    charger();
    setInterval(charger, 300000);
    frappe.realtime.on("brs_bordereau_lu", () => setTimeout(charger, 1500));
    frappe.realtime.on("brs_bordereaux_manquants", charger);
  });
})();
