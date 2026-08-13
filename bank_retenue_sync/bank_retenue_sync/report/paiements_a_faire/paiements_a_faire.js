// Filtres du rapport « Paiements à faire ».
// Trois filtres, pas plus : la liste doit se lire sans être réglée. Aucune borne de date par
// défaut — une liste de reste-à-faire qui commencerait au 1er du mois cacherait justement les
// virements les plus anciens, ceux qui traînent.
frappe.query_reports["Paiements a faire"] = {
  filters: [
    {
      fieldname: "type",
      label: __("Type"),
      fieldtype: "Select",
      options: ["", "Aramex", "Salaire", "Loyer", "Honoraire", "Carte technologique"].join("\n"),
    },
    { fieldname: "date_from", label: __("Du"), fieldtype: "Date" },
    { fieldname: "date_to", label: __("Au"), fieldtype: "Date" },
  ],
  formatter(value, row, column, data, default_formatter) {
    value = default_formatter(value, row, column, data);
    // Au-delà d'une semaine, le virement traîne : c'est la seule alerte de ce tableau.
    if (data && column.fieldname === "retard" && flt(data.retard) > 7) {
      value = `<span style="color:#a93226;font-weight:600">${value}</span>`;
    }
    // Une écriture encore en brouillon n'est pas engagée : le signaler évite de virer sur la
    // foi d'une ligne que personne n'a validée.
    if (data && column.fieldname === "reference" && data.brouillon) {
      value = `${value} <span style="color:#b9770e;font-size:11px">(brouillon)</span>`;
    }
    return value;
  },
};
