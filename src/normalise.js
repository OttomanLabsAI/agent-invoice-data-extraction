// Coerce the model's record into clean numbers and complete blocks, recompute
// totals where the model left gaps, and check the arithmetic.

import { LINE_CATEGORIES } from "./schema.js";

export const round2 = (x) => Math.round(x * 100) / 100;

export function num(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return Number.isFinite(value) ? round2(value) : null;
  const cleaned = String(value).replace(/[^0-9.\-]/g, "");
  const n = parseFloat(cleaned);
  return Number.isFinite(n) ? round2(n) : null;
}

const sum = (items, key) => items.reduce((acc, item) => acc + (item[key] || 0), 0);

export function normalise(record) {
  const rec = { ...record };
  rec.document_type = rec.document_type || "invoice";
  rec.supplier = { ...(rec.supplier || {}) };
  rec.bill_to = { ...(rec.bill_to || {}) };
  rec.bank_details = { ...(rec.bank_details || {}) };
  rec.cis = { ...(rec.cis || { applicable: false }) };
  rec.retention = { ...(rec.retention || { applicable: false }) };
  rec.flags = (rec.flags || []).map((f) => String(f));
  rec.currency = String(rec.currency || "GBP").toUpperCase();
  rec.vat_treatment = rec.vat_treatment || "unknown";

  const lines = [];
  for (const raw of rec.line_items || []) {
    const item = { ...raw };
    for (const key of ["quantity", "unit_price", "net_amount", "vat_rate", "vat_amount"]) item[key] = num(item[key]);
    if (!LINE_CATEGORIES.includes(item.category)) item.category = "other";
    if (item.vat_amount == null && item.net_amount != null && item.vat_rate != null) {
      item.vat_amount = round2((item.net_amount * item.vat_rate) / 100);
    }
    lines.push(item);
  }
  rec.line_items = lines;

  for (const key of ["net_total", "vat_total", "gross_total", "discount_amount", "amount_due"]) rec[key] = num(rec[key]);
  for (const key of ["labour_amount", "materials_amount", "deduction_rate", "deduction_amount"]) rec.cis[key] = num(rec.cis[key]);
  for (const key of ["percentage", "amount"]) rec.retention[key] = num(rec.retention[key]);
  rec.cis.applicable = Boolean(rec.cis.applicable);
  rec.retention.applicable = Boolean(rec.retention.applicable);

  if (rec.net_total == null && lines.length) rec.net_total = round2(sum(lines, "net_amount"));
  if (rec.vat_total == null && lines.length) rec.vat_total = round2(sum(lines, "vat_amount"));
  if (rec.gross_total == null && rec.net_total != null) rec.gross_total = round2(rec.net_total + (rec.vat_total || 0));

  let breakdown = (rec.vat_breakdown || []).map((entry) => ({
    ...entry,
    rate: num(entry.rate),
    net: num(entry.net),
    vat: num(entry.vat),
  }));
  if (!breakdown.length && lines.length) {
    const byRate = new Map();
    for (const line of lines) {
      const rate = line.vat_rate != null ? line.vat_rate : 0;
      const slot = byRate.get(rate) || { rate, net: 0, vat: 0 };
      slot.net = round2(slot.net + (line.net_amount || 0));
      slot.vat = round2(slot.vat + (line.vat_amount || 0));
      byRate.set(rate, slot);
    }
    breakdown = [...byRate.values()];
  }
  rec.vat_breakdown = breakdown;

  rec.totals_reconcile = totalsReconcile(rec);
  const confidence = parseFloat(rec.confidence);
  rec.confidence = Number.isFinite(confidence) ? Math.max(0, Math.min(1, confidence)) : 0;
  return rec;
}

export function totalsReconcile(rec, tolerance = 0.02) {
  const { net_total: net, vat_total: vat, gross_total: gross } = rec;
  if (net == null || gross == null) return false;
  if (Math.abs(net + (vat || 0) - gross) > tolerance) return false;
  const lines = rec.line_items || [];
  if (lines.length && Math.abs(sum(lines, "net_amount") - net) > tolerance) return false;
  return true;
}
