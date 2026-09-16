// Paiements à venir : traites émises en attente de leur échéance (cf. .py).
frappe.query_reports["Paiements a venir"] = {
  filters: [
    { fieldname: "date_from", label: __("Échéance depuis"), fieldtype: "Date" },
    { fieldname: "date_to", label: __("Échéance jusqu'au"), fieldtype: "Date" },
  ],
  formatter(value, row, column, data, default_formatter) {
    value = default_formatter(value, row, column, data);
    if (column.fieldname === "dans" && data && data.dans < 0) {
      value = `<span style="color:#a8071a;font-weight:600">${value}</span>`;
    }
    return value;
  },
};
