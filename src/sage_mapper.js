// Turn an extracted invoice into what Sage Intacct needs.
//
//   bill        - APBILL (or APADJUSTMENT for credit notes) create payload, field order as Intacct documents it
//   issues      - things that block a clean post (missing vendor ID, GL code, ...)
//   entry_sheet - the fields in the order they appear on Intacct's bill entry screen, for keying in by hand
//   log_row     - flat row for the purchase invoice log / CSV export
//   xml         - the <create> function body for the Intacct XML gateway (billToXml)

const LEGAL_SUFFIXES = /\b(ltd|limited|plc|llp|llc|inc|co|company|uk|group|holdings|services|the)\b/g;

function norm(name) {
  let text = String(name || "").toLowerCase();
  text = text.replace(/[^a-z0-9 ]+/g, " ");
  text = text.replace(LEGAL_SUFFIXES, " ");
  return text.replace(/\s+/g, " ").trim();
}

/** Returns [value, matchedKey]. Exact normalised match first, then containment either way. */
function lookup(mapping, ...candidates) {
  if (!mapping || !Object.keys(mapping).length) return ["", ""];
  const normalised = new Map();
  for (const [k, v] of Object.entries(mapping)) if (k && v) normalised.set(norm(k), [k, v]);
  const cands = candidates.filter(Boolean).map(norm);
  for (const cand of cands) {
    if (normalised.has(cand)) {
      const [key, value] = normalised.get(cand);
      return [value, key];
    }
  }
  for (const cand of cands) {
    for (const [nkey, [key, value]] of normalised) {
      if (nkey.length >= 3 && (cand.includes(nkey) || nkey.includes(cand))) return [value, key];
    }
  }
  return ["", ""];
}

export function money(value) {
  const n = parseFloat(value);
  return Number.isFinite(n) ? Math.round(n * 100) / 100 : 0;
}

export const fmt = (value) => money(value).toFixed(2);

const MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };

function pad(n) {
  return String(n).padStart(2, "0");
}

/** Accepts YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, "8 Sep 2026" and "8 September 2026"; anything else is passed through. */
export function iso(value) {
  if (!value) return "";
  const text = String(value).trim();
  let m = text.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) return `${m[1]}-${pad(m[2])}-${pad(m[3])}`;
  m = text.match(/^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$/);
  if (m) return `${m[3]}-${pad(m[2])}-${pad(m[1])}`;
  m = text.match(/^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$/);
  if (m) {
    const month = MONTHS[m[2].slice(0, 3).toLowerCase()];
    if (month) return `${m[3]}-${pad(month)}-${pad(m[1])}`;
  }
  return text;
}

function addDays(isoDate, days) {
  const m = isoDate.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return "";
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]) + Number(days)));
  return d.toISOString().slice(0, 10);
}

function rateKey(rate) {
  if (rate === null || rate === undefined) return "0";
  const r = Number(rate);
  return Number.isInteger(r) ? String(r) : String(r);
}

function deductionLine(account, amount, description, ctx, category, lineNo) {
  return {
    ACCOUNTNO: account,
    TRX_AMOUNT: fmt(amount),
    ENTRYDESCRIPTION: description,
    LOCATIONID: ctx.locationId,
    DEPARTMENTID: ctx.departmentId,
    PROJECTID: ctx.projectId,
    TAXENTRIES: [],
    _line_no: lineNo,
    _category: category,
    _vat_rate: "",
    _tax_label: "",
    _quantity: null,
    _unit: null,
    _unit_price: null,
  };
}

export function build(rec, settings, { emailMeta = {}, overrides = {} } = {}) {
  overrides = overrides || {};
  emailMeta = emailMeta || {};
  const issues = [];
  const notes = [];

  const docType = rec.document_type || "invoice";
  const isCredit = docType === "credit_note";
  const supplier = rec.supplier || {};
  const supplierName = supplier.name || "";
  const currency = String(rec.currency || settings.default_currency || "GBP").toUpperCase();

  // ---- Vendor
  let vendorId = overrides.vendor_id || "";
  let matchedVendorKey = "";
  if (!vendorId) [vendorId, matchedVendorKey] = lookup(settings.vendor_map || {}, supplierName, supplier.trading_name);
  if (!vendorId) {
    issues.push(`No Sage vendor ID for '${supplierName || "unknown supplier"}'. Add it on the invoice or in Settings > Vendor map.`);
  }

  // ---- Dates and terms
  const invoiceDate = iso(overrides.invoice_date || rec.invoice_date);
  let dueDate = iso(overrides.due_date || rec.due_date);
  const termsDays = rec.payment_terms_days;
  if (!dueDate && invoiceDate && termsDays !== null && termsDays !== undefined) {
    const derived = addDays(invoiceDate, parseInt(termsDays, 10));
    if (derived) {
      dueDate = derived;
      notes.push(`Due date derived from ${termsDays}-day terms.`);
    }
  }
  let termName = "";
  if (termsDays !== null && termsDays !== undefined) {
    termName = (settings.terms_map || {})[String(parseInt(termsDays, 10))] || "";
  }
  if (!invoiceDate) issues.push("Invoice date missing.");
  if (!dueDate && !termName) issues.push("No due date and no matching payment term. Set one before posting.");

  // ---- Project / job
  let projectId = overrides.project_id || "";
  let matchedProjectKey = "";
  if (!projectId) {
    [projectId, matchedProjectKey] = lookup(
      settings.project_map || {},
      rec.project_reference, rec.po_number, rec.order_reference, rec.site_address, emailMeta.subject,
    );
  }
  if (!projectId && Object.keys(settings.project_map || {}).length) {
    notes.push("No project matched - bill will post without a project dimension.");
  }

  // ---- Lines
  const glMap = settings.gl_map || {};
  const vatMap = settings.vat_detail_map || {};
  const reverseCharge = rec.vat_treatment === "reverse_charge";
  const sign = isCredit ? -1 : 1;
  const ctx = { locationId: settings.sage_location_id || "", departmentId: settings.sage_department_id || "", projectId };

  const items = [];
  const missingGl = new Set();
  const missingTax = new Set();
  (rec.line_items || []).forEach((line, idx) => {
    const category = line.category || "other";
    const account = glMap[category] || settings.sage_default_gl || "";
    if (!account) missingGl.add(category);
    const net = money(line.net_amount);
    const vatAmount = reverseCharge ? 0 : money(line.vat_amount);
    const key = reverseCharge ? "reverse_charge" : rateKey(line.vat_rate);
    const detailId = vatMap[key] || "";
    if (!detailId) missingTax.add(key);
    items.push({
      ACCOUNTNO: account,
      TRX_AMOUNT: fmt(sign * net),
      ENTRYDESCRIPTION: String(line.description || "").slice(0, 200),
      LOCATIONID: ctx.locationId,
      DEPARTMENTID: ctx.departmentId,
      PROJECTID: projectId,
      TAXENTRIES: [{ DETAILID: detailId, TRX_TAX: fmt(sign * vatAmount) }],
      _line_no: idx + 1,
      _category: category,
      _vat_rate: key,
      _tax_label: reverseCharge ? "reverse charge" : `${key}%`,
      _quantity: line.quantity ?? null,
      _unit: line.unit ?? null,
      _unit_price: line.unit_price ?? null,
    });
  });

  if (!items.length) issues.push("No line items were extracted - add at least one line.");
  for (const cat of [...missingGl].sort()) issues.push(`No GL account for '${cat}' lines. Set it in Settings > GL accounts by category.`);
  for (const key of [...missingTax].sort()) {
    const label = key === "reverse_charge" ? "reverse charge" : `${key}% VAT`;
    issues.push(`No Sage tax detail for ${label}. Set it in Settings > VAT tax details.`);
  }

  // ---- Construction-specific deductions
  const cis = rec.cis || {};
  const cisDeduction = money(cis.deduction_amount);
  if (cis.applicable || cisDeduction) {
    if (cisDeduction && settings.sage_cis_gl) {
      const rate = cis.deduction_rate ? ` @ ${fmt(cis.deduction_rate)}%` : "";
      items.push(deductionLine(settings.sage_cis_gl, -sign * cisDeduction, `CIS deduction${rate}`, ctx, "cis", items.length + 1));
    } else if (cisDeduction) {
      issues.push(`CIS deduction of ${currency} ${fmt(cisDeduction)} shown. Set a CIS control account in Settings or post it by hand.`);
    } else {
      notes.push("Subcontractor invoice - confirm the CIS status and deduction rate before payment.");
    }
  }

  const retention = rec.retention || {};
  const retentionAmount = money(retention.amount);
  if (retentionAmount) {
    if (settings.sage_retention_gl) {
      const pct = retention.percentage ? ` @ ${fmt(retention.percentage)}%` : "";
      items.push(deductionLine(settings.sage_retention_gl, -sign * retentionAmount, `Retention withheld${pct}`, ctx, "retention", items.length + 1));
    } else {
      issues.push(`Retention of ${currency} ${fmt(retentionAmount)} withheld. Set a retention account in Settings or post it by hand.`);
    }
  }

  if (reverseCharge) notes.push("Domestic reverse charge - no VAT is paid to the supplier; Sage posts the input and output VAT.");
  if (rec.totals_reconcile === false) issues.push("Totals on the document do not add up - check the lines against the PDF.");
  if (!rec.po_number) notes.push("No purchase order number on the invoice.");
  if (!["invoice", "credit_note"].includes(docType)) issues.push(`Document looks like a ${docType.replace(/_/g, " ")}, not an invoice.`);
  for (const flag of rec.flags || []) notes.push(flag);

  const description = String(overrides.description || rec.description || `${supplierName} ${rec.invoice_number || ""}`).trim().slice(0, 200);
  const poNumber = overrides.po_number || rec.po_number || "";
  const recordId = String(overrides.invoice_number || rec.invoice_number || "").slice(0, 30);

  const objectName = isCredit ? "APADJUSTMENT" : "APBILL";
  const bill = {
    _object: objectName,
    WHENCREATED: invoiceDate,
    WHENPOSTED: invoiceDate,
    WHENDUE: dueDate,
    VENDORID: vendorId,
    RECORDID: recordId,
    DOCNUMBER: poNumber,
    DESCRIPTION: description,
    TERMNAME: termName,
    BASECURR: settings.default_currency || "GBP",
    CURRENCY: currency,
    ACTION: settings.sage_action || "Draft",
    TAXSOLUTIONID: settings.sage_tax_solution_id || "",
    ITEMS: items,
  };
  if (currency !== bill.BASECURR) {
    bill.EXCH_RATE_DATE = invoiceDate;
    bill.EXCH_RATE_TYPE_ID = "Intacct Daily Rate";
    notes.push(`Foreign currency (${currency}) - Intacct will apply its daily rate on ${invoiceDate}.`);
  }
  if (isCredit) notes.push("Credit note - mapped to an AP adjustment with negative amounts.");

  const netTotal = money(rec.net_total);
  const vatTotal = reverseCharge ? 0 : money(rec.vat_total);
  const grossTotal = reverseCharge ? netTotal : money(rec.gross_total);
  const amountDue = money(rec.amount_due) || Math.round((grossTotal - cisDeduction - retentionAmount) * 100) / 100;
  const bank = rec.bank_details || {};

  const totals = [
    ["Net", fmt(netTotal)],
    ["VAT", fmt(vatTotal)],
    ["Gross", fmt(grossTotal)],
  ];
  if (cisDeduction) totals.push(["CIS deduction", "-" + fmt(cisDeduction)]);
  if (retentionAmount) totals.push(["Retention", "-" + fmt(retentionAmount)]);
  if (cisDeduction || retentionAmount || money(rec.amount_due)) totals.push(["Amount payable", fmt(amountDue)]);

  const entrySheet = {
    header: [
      ["Vendor", vendorId || "(not mapped)", supplierName],
      ["Bill date", invoiceDate, ""],
      ["Due date", dueDate, termName ? `terms: ${termName}` : ""],
      ["Bill number", recordId, "supplier's invoice number"],
      ["Reference / PO", poNumber, rec.order_reference || ""],
      ["Description", description, ""],
      ["Currency", currency, ""],
      ["Tax solution", bill.TAXSOLUTIONID, reverseCharge ? "reverse charge" : rec.vat_treatment || ""],
      ["Project", projectId || "(none)", rec.project_reference || ""],
      ["Location / department", [ctx.locationId, ctx.departmentId].filter(Boolean).join(" / ") || "(default)", ""],
    ],
    totals,
    payment: [
      ["Account name", bank.account_name || ""],
      ["Sort code", bank.sort_code || ""],
      ["Account number", bank.account_number || ""],
      ["IBAN", bank.iban || ""],
      ["Supplier VAT no", supplier.vat_number || ""],
    ],
  };

  const logRow = {
    "Date received": String(emailMeta.received_at || "").slice(0, 10),
    Supplier: supplierName,
    "Sage vendor ID": vendorId,
    "Invoice no": recordId,
    Type: docType.replace(/_/g, " "),
    "Invoice date": invoiceDate,
    "Due date": dueDate,
    "PO no": poNumber,
    Project: projectId || rec.project_reference || "",
    Description: description,
    Currency: currency,
    Net: fmt(netTotal),
    VAT: fmt(vatTotal),
    Gross: fmt(grossTotal),
    "VAT treatment": rec.vat_treatment || "",
    "CIS deduction": cisDeduction ? fmt(cisDeduction) : "",
    Retention: retentionAmount ? fmt(retentionAmount) : "",
    "Amount payable": fmt(amountDue),
    "Supplier VAT no": supplier.vat_number || "",
    "Email from": emailMeta.from || "",
    "Email subject": emailMeta.subject || "",
    Attachment: emailMeta.attachment_name || "",
    Confidence: (parseFloat(rec.confidence) || 0).toFixed(2),
    Checks: [...issues, ...notes].join("; "),
  };

  return {
    bill,
    issues,
    notes,
    entry_sheet: entrySheet,
    log_row: logRow,
    matched: { vendor_key: matchedVendorKey, project_key: matchedProjectKey },
  };
}

// --------------------------------------------------------------------------- XML

export function xmlEscape(value) {
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

const tag = (name, value) => `<${name}>${xmlEscape(value)}</${name}>`;

const HEADER_ORDER = ["WHENCREATED", "WHENPOSTED", "WHENDUE", "VENDORID", "RECORDID", "DOCNUMBER", "DESCRIPTION",
  "TERMNAME", "BASECURR", "CURRENCY", "EXCH_RATE_DATE", "EXCH_RATE_TYPE_ID", "ACTION", "TAXSOLUTIONID"];
const ITEM_ORDER = ["ACCOUNTNO", "TRX_AMOUNT", "ENTRYDESCRIPTION", "LOCATIONID", "DEPARTMENTID", "PROJECTID"];

const present = (v) => v !== null && v !== undefined && v !== "";

/** The <create> function body for Intacct's XML gateway (no control/authentication wrapper). */
export function billToXml(bill, indent = "    ") {
  const obj = bill._object || "APBILL";
  const itemsTag = obj === "APADJUSTMENT" ? "APADJUSTMENTITEMS" : "APBILLITEMS";
  const itemTag = obj === "APADJUSTMENT" ? "APADJUSTMENTITEM" : "APBILLITEM";
  const out = ["<create>", `${indent}<${obj}>`];
  for (const key of HEADER_ORDER) if (present(bill[key])) out.push(`${indent.repeat(2)}${tag(key, bill[key])}`);
  out.push(`${indent.repeat(2)}<${itemsTag}>`);
  for (const item of bill.ITEMS || []) {
    out.push(`${indent.repeat(3)}<${itemTag}>`);
    for (const key of ITEM_ORDER) if (present(item[key])) out.push(`${indent.repeat(4)}${tag(key, item[key])}`);
    const taxes = (item.TAXENTRIES || []).filter((t) => t.DETAILID);
    if (taxes.length) {
      out.push(`${indent.repeat(4)}<TAXENTRIES>`);
      for (const t of taxes) {
        out.push(`${indent.repeat(5)}<TAXENTRY>`);
        out.push(`${indent.repeat(6)}${tag("DETAILID", t.DETAILID)}`);
        out.push(`${indent.repeat(6)}${tag("TRX_TAX", t.TRX_TAX ?? "0.00")}`);
        out.push(`${indent.repeat(5)}</TAXENTRY>`);
      }
      out.push(`${indent.repeat(4)}</TAXENTRIES>`);
    }
    out.push(`${indent.repeat(3)}</${itemTag}>`);
  }
  out.push(`${indent.repeat(2)}</${itemsTag}>`);
  out.push(`${indent}</${obj}>`);
  out.push("</create>");
  return out.join("\n");
}

/** The payload without the underscore-prefixed helper keys, for JSON display/download. */
export function publicBill(bill) {
  const clean = {};
  for (const [k, v] of Object.entries(bill)) if (!k.startsWith("_") && k !== "ITEMS") clean[k] = v;
  const items = (bill.ITEMS || []).map((item) => {
    const pub = {};
    for (const [k, v] of Object.entries(item)) if (!k.startsWith("_")) pub[k] = v;
    pub.TAXENTRIES = (item.TAXENTRIES || []).filter((t) => t.DETAILID);
    return pub;
  });
  const itemsKey = bill._object === "APADJUSTMENT" ? "APADJUSTMENTITEMS" : "APBILLITEMS";
  return { [bill._object || "APBILL"]: { ...clean, [itemsKey]: items } };
}

export const LOG_COLUMNS = [
  "Date received", "Supplier", "Sage vendor ID", "Invoice no", "Type", "Invoice date", "Due date", "PO no",
  "Project", "Description", "Currency", "Net", "VAT", "Gross", "VAT treatment", "CIS deduction", "Retention",
  "Amount payable", "Supplier VAT no", "Status", "Sage record no", "Email from", "Email subject", "Attachment",
  "Confidence", "Checks",
];
