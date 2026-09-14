// Invoice types: different kinds of invoice need different Sage coding and
// different checks. A type carries GL and VAT overrides (blank = the Sage coding
// in Settings), CIS and retention defaults, payment terms, and PO / project
// rules. Every invoice is matched to one type; the review page can override it.

import { LINE_CATEGORIES } from "./schema.js";
import { nameListed } from "./match.js";

export const TYPE_SIGNALS = [
  ["cis", "CIS shown on the invoice"],
  ["reverse_charge", "Domestic reverse charge"],
  ["application_for_payment", "Application for payment"],
];
const SIGNAL_HOW = {
  cis: "CIS is shown on the invoice",
  reverse_charge: "the invoice is under the domestic reverse charge",
  application_for_payment: "it is an application for payment",
};

export const EXPECTED_VAT = [
  ["", "Anything"],
  ["standard", "Standard VAT on goods or services"],
  ["reverse_charge", "Domestic reverse charge"],
];

const SERVICES_VAT = {
  "20": "UK Purchase Services Standard Rate",
  "5": "UK Purchase Services Reduced Rate",
  "0": "UK Purchase Services Zero Rate",
  reverse_charge: "UK Purchase Services Reverse Charge Standard Rate",
};
const blankGl = () => Object.fromEntries(LINE_CATEGORIES.map((c) => [c, ""]));
const blankVat = () => ({ "20": "", "5": "", "0": "", reverse_charge: "" });
const allGl = (code) => Object.fromEntries(LINE_CATEGORIES.map((c) => [c, code]));

const base = {
  suppliers: "", categories: [], signals: [], gl_map: blankGl(), vat_detail_map: blankVat(),
  cis_applies: false, cis_rate: 0, retention_percent: 0, terms_days: 30, po_required: false, project_required: false,
  expected_vat: "", location_id: "", department_id: "", sage_action: "",
};

/** Generic types for a UK contractor. Every code is a starting point to check against the company's Intacct. */
export const DEFAULT_TYPES = [
  {
    ...base, id: "subcontractor", name: "Subcontractor",
    description: "Labour-only and supply-and-fit subcontractors, including applications for payment. The whole invoice is coded to the subcontractor cost account; CIS and the domestic reverse charge usually apply and retention may be held.",
    categories: ["labour", "subcontract"], signals: ["cis", "reverse_charge", "application_for_payment"],
    gl_map: allGl("6002"), vat_detail_map: { ...SERVICES_VAT },
    cis_applies: true, cis_rate: 20, project_required: true, expected_vat: "reverse_charge",
  },
  {
    ...base, id: "materials", name: "Materials supplier",
    description: "Builders' merchants and suppliers delivering goods to site. Standard VAT on goods, no CIS, and a purchase order is expected.",
    categories: ["materials"], gl_map: { ...blankGl(), materials: "5000", expenses: "5100" },
    po_required: true, project_required: true, expected_vat: "standard",
  },
  {
    ...base, id: "plant", name: "Plant hire",
    description: "Hire of plant and equipment, with or without an operator. Operated hire is a construction service and may be reverse charged.",
    categories: ["plant"], gl_map: { ...blankGl(), plant: "7700", labour: "7700" }, vat_detail_map: { ...SERVICES_VAT },
    po_required: true, project_required: true,
  },
  {
    ...base, id: "professional", name: "Professional services",
    description: "Consultants, designers, surveyors, testing and commissioning. Standard VAT on services, no CIS.",
    categories: ["services"], gl_map: { ...blankGl(), services: "7603", labour: "7603" }, vat_detail_map: { ...SERVICES_VAT },
    expected_vat: "standard",
  },
  {
    ...base, id: "overheads", name: "Overheads",
    description: "Utilities, office, travel, insurance and everything else that is not a project cost. The fallback when nothing else matches.",
    categories: ["expenses", "other"],
  },
];

export function slug(text) {
  return String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40) || "type";
}

export function newTypeId(existingIds) {
  let id;
  do id = "type_" + Math.random().toString(36).slice(2, 8);
  while (existingIds.includes(id));
  return id;
}

const numOr = (value, fallback) => {
  const n = parseFloat(value);
  return Number.isFinite(n) ? n : fallback;
};

/** Fill in any missing fields so older stored types keep working. */
export function normaliseType(t, index = 0) {
  const raw = t || {};
  const id = slug(raw.id || raw.name || `type_${index}`);
  const valid = new Set(TYPE_SIGNALS.map(([k]) => k));
  const terms = raw.terms_days === "" || raw.terms_days === null || raw.terms_days === undefined ? null : Math.max(0, Math.round(numOr(raw.terms_days, 0)));
  return {
    id,
    name: String(raw.name || id).trim() || id,
    description: String(raw.description || "").trim(),
    suppliers: String(raw.suppliers || "").trim(),
    categories: (raw.categories || []).filter((c) => LINE_CATEGORIES.includes(c)),
    signals: (raw.signals || []).filter((s) => valid.has(s)),
    gl_map: { ...blankGl(), ...(raw.gl_map || {}) },
    vat_detail_map: { ...blankVat(), ...(raw.vat_detail_map || {}) },
    cis_applies: Boolean(raw.cis_applies),
    cis_rate: Math.max(0, numOr(raw.cis_rate, 0)),
    retention_percent: Math.max(0, numOr(raw.retention_percent, 0)),
    terms_days: terms,
    po_required: Boolean(raw.po_required),
    project_required: Boolean(raw.project_required),
    expected_vat: ["standard", "reverse_charge"].includes(raw.expected_vat) ? raw.expected_vat : "",
    location_id: String(raw.location_id || "").trim(),
    department_id: String(raw.department_id || "").trim(),
    sage_action: ["Draft", "Submit"].includes(raw.sage_action) ? raw.sage_action : "",
  };
}

export const typeById = (settings, id) => (settings.invoice_types || []).find((t) => t.id === id) || null;

const money = (v) => {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : 0;
};
const round2 = (x) => Math.round(x * 100) / 100;

/** Which type an invoice is: chosen by hand, then by supplier, then by CIS / reverse charge / AFP, then by what the lines mostly are. */
export function resolveType(rec, settings, overrides = {}) {
  const types = settings.invoice_types || [];
  if (!types.length) return { type: null, how: "" };
  if (overrides && overrides.invoice_type) {
    const chosen = typeById(settings, overrides.invoice_type);
    if (chosen) return { type: chosen, how: "chosen on the invoice" };
  }
  const supplierName = (rec.supplier || {}).name || "";
  for (const t of types) {
    const names = t.suppliers.split(/\r?\n|,/).map((s) => s.trim()).filter(Boolean);
    if (names.length && supplierName && nameListed(supplierName, names)) return { type: t, how: `the supplier is listed under ${t.name}` };
  }
  const cis = rec.cis || {};
  const present = new Set();
  if (cis.applicable || money(cis.deduction_amount)) present.add("cis");
  if (rec.vat_treatment === "reverse_charge") present.add("reverse_charge");
  if (rec.document_type === "application_for_payment") present.add("application_for_payment");
  for (const t of types) {
    const hit = t.signals.find((s) => present.has(s));
    if (hit) return { type: t, how: SIGNAL_HOW[hit] };
  }
  const totals = {};
  for (const line of rec.line_items || []) {
    const category = LINE_CATEGORIES.includes(line.category) ? line.category : "other";
    totals[category] = (totals[category] || 0) + Math.abs(money(line.net_amount));
  }
  const dominant = Object.entries(totals).sort((a, b) => b[1] - a[1])[0];
  if (dominant) {
    for (const t of types) if (t.categories.includes(dominant[0])) return { type: t, how: `the lines are mostly ${dominant[0]}` };
  }
  const fallback = types.find((t) => t.categories.includes("other")) || types[types.length - 1];
  return { type: fallback, how: "nothing else matched" };
}

/** The Sage coding to use for this type: its overrides on top of the company-wide settings. */
export function effectiveCoding(settings, type) {
  const compact = (m) => Object.fromEntries(Object.entries(m || {}).filter(([, v]) => v));
  return {
    gl_map: { ...(settings.gl_map || {}), ...compact(type && type.gl_map) },
    vat_detail_map: { ...(settings.vat_detail_map || {}), ...compact(type && type.vat_detail_map) },
    location_id: (type && type.location_id) || settings.sage_location_id || "",
    department_id: (type && type.department_id) || settings.sage_department_id || "",
    sage_action: (type && type.sage_action) || settings.sage_action || "Draft",
  };
}

const labourOnLines = (rec) =>
  (rec.line_items || []).filter((l) => l.category === "labour").reduce((sum, l) => sum + money(l.net_amount), 0);

/** Fill blanks on the record from the type's defaults: CIS deduction, retention, payment terms. Only blanks are touched. */
export function applyTypeDefaults(rec, type) {
  if (!type) return { rec, notes: [] };
  const out = { ...rec, cis: { ...(rec.cis || {}) }, retention: { ...(rec.retention || {}) }, flags: [...(rec.flags || [])] };
  const notes = [];
  if (type.cis_applies) {
    const rate = out.cis.deduction_rate != null ? money(out.cis.deduction_rate) : type.cis_rate;
    const labour = out.cis.labour_amount != null ? money(out.cis.labour_amount) : round2(labourOnLines(out));
    if (out.cis.deduction_amount == null && rate > 0 && labour > 0) {
      out.cis.applicable = true;
      out.cis.deduction_rate = rate;
      out.cis.labour_amount = labour;
      out.cis.deduction_amount = round2((labour * rate) / 100);
      notes.push(`CIS deduction of ${out.cis.deduction_amount.toFixed(2)} computed at ${rate}% on labour of ${labour.toFixed(2)} - not shown on the invoice; check the subcontractor's verification status.`);
    } else if (!out.cis.applicable) {
      out.cis.applicable = true;
    }
  }
  if (type.retention_percent > 0 && out.retention.amount == null && money(out.net_total) > 0) {
    out.retention.applicable = true;
    out.retention.percentage = type.retention_percent;
    out.retention.amount = round2((money(out.net_total) * type.retention_percent) / 100);
    notes.push(`Retention of ${out.retention.amount.toFixed(2)} computed at ${type.retention_percent}% - not shown on the invoice; check the subcontract.`);
  }
  if ((out.payment_terms_days === null || out.payment_terms_days === undefined) && type.terms_days !== null) {
    out.payment_terms_days = type.terms_days;
    notes.push(`Payment terms not on the invoice - ${type.terms_days}-day default for ${type.name}.`);
  }
  for (const note of notes) if (!out.flags.includes(note)) out.flags.push(note);
  return { rec: out, notes };
}
