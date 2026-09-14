// Tiny HTML helpers: a tagged template that escapes every interpolation unless
// it is marked raw, plus the formatting filters the pages use.

export class Raw {
  constructor(s) {
    this.s = String(s);
  }
  toString() {
    return this.s;
  }
}

export const raw = (s) => new Raw(s);

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function render(value) {
  if (value instanceof Raw) return value.s;
  if (Array.isArray(value)) return value.map(render).join("");
  if (value === null || value === undefined || value === false) return "";
  return esc(value);
}

export function html(strings, ...values) {
  let out = "";
  for (let i = 0; i < strings.length; i++) {
    out += strings[i];
    if (i < values.length) out += render(values[i]);
  }
  return new Raw(out);
}

export const when = (cond, value) => (cond ? value : "");
export const selected = (a, b) => (String(a) === String(b) ? raw(" selected") : "");
export const checked = (cond) => (cond ? raw(" checked") : "");
export const disabled = (cond) => (cond ? raw(" disabled") : "");

/** 12,649.00 style, or a dash for blanks. */
export function money(value) {
  const n = parseFloat(value);
  if (!Number.isFinite(n)) return "-";
  const [int, dec] = Math.abs(n).toFixed(2).split(".");
  return (n < 0 ? "-" : "") + int.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + "." + dec;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function shortDate(value) {
  if (!value) return "";
  const text = String(value);
  const m = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (m) return `${m[3]} ${MONTHS[Number(m[2]) - 1]} ${m[1]}`;
  return text.slice(0, 10);
}

export const timeOf = (iso) => (iso ? String(iso).slice(11, 16) : "");

/** Two-decimal text for form inputs, blank for null. */
export const fmt2 = (value) => (value === null || value === undefined || value === "" ? "" : Number(value).toFixed(2));

export const plain = (value) => (value === null || value === undefined ? "" : value);

export const humanise = (value) => String(value || "").replace(/_/g, " ");
