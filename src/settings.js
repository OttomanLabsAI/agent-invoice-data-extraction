// Settings: defaults, storage in D1 (settings table, one row per key) and the
// "key = value" map parsing used by the Sage coding fields.

export const CLAUDE_MODELS = [
  ["claude-opus-5", "Claude Opus 5 (default)"],
  ["claude-sonnet-5", "Claude Sonnet 5 (faster, cheaper)"],
  ["claude-fable-5-1", "Claude Fable 5.1"],
  ["claude-haiku-4-5-20251001", "Claude Haiku 4.5 (cheapest)"],
];

export const DEFAULT_MODEL = "claude-opus-5";

export const SECRET_FIELDS = new Set([
  "anthropic_api_key", "google_client_id", "google_client_secret",
  "sage_sender_id", "sage_sender_password", "sage_company_id", "sage_user_id", "sage_user_password",
]);

export const MAP_FIELDS = new Set(["vendor_map", "project_map", "gl_map", "vat_detail_map", "terms_map"]);

export const TEXT_FIELDS = [
  "anthropic_api_key", "google_client_id", "google_client_secret",
  "classify_model", "classify_reference_text", "classify_invoice_label", "classify_other_label",
  "gmail_query", "gmail_allowed_senders",
  "extract_model", "extract_reference_text", "gmail_processed_label",
  "company_name", "default_currency",
  "sage_endpoint", "sage_sender_id", "sage_sender_password", "sage_company_id", "sage_user_id", "sage_user_password",
  "sage_action", "sage_location_id", "sage_department_id", "sage_tax_solution_id", "sage_default_gl",
  "sage_cis_gl", "sage_retention_gl",
];

export const INT_FIELDS = { gmail_max_messages: 10, poll_minutes: 0 };

export const DEFAULTS = {
  // Claude
  anthropic_api_key: "",
  // Agent - Classification: reads new mail and labels invoices
  classify_model: DEFAULT_MODEL,
  classify_reference_text: "",
  classify_invoice_label: "Invoice Incoming",
  classify_other_label: "Not an invoice",
  gmail_query: "has:attachment is:unread",
  gmail_allowed_senders: "",
  gmail_max_messages: 10,
  // Agent - Invoice Extraction: reads mail labelled as invoices and extracts the data
  extract_model: DEFAULT_MODEL,
  extract_reference_text: "",
  gmail_processed_label: "Invoices/Processed",
  poll_minutes: 0,
  // Gmail OAuth client
  google_client_id: "",
  google_client_secret: "",
  // Company
  company_name: "Glent Group",
  default_currency: "GBP",
  // Sage Intacct connection (optional - leave blank to work review-and-export only)
  sage_endpoint: "https://api.intacct.com/ia/xml/xmlgw.phtml",
  sage_sender_id: "",
  sage_sender_password: "",
  sage_company_id: "",
  sage_user_id: "",
  sage_user_password: "",
  sage_action: "Draft",
  // Sage Intacct coding defaults
  sage_location_id: "",
  sage_department_id: "",
  sage_tax_solution_id: "United Kingdom - VAT",
  sage_default_gl: "",
  sage_cis_gl: "",
  sage_retention_gl: "",
  // Lookup maps (edited as "key = value" lines)
  gl_map: { materials: "", labour: "", plant: "", subcontract: "", services: "", expenses: "", other: "" },
  vendor_map: {},
  project_map: {},
  vat_detail_map: { "20": "", "5": "", "0": "", reverse_charge: "" },
  terms_map: { "30": "Net 30", "14": "Net 14", "60": "Net 60", "0": "Due on receipt" },
};

export function withDefaults(stored) {
  const settings = structuredClone(DEFAULTS);
  for (const [key, value] of Object.entries(stored || {})) {
    if (MAP_FIELDS.has(key) && value && typeof value === "object") {
      settings[key] = { ...(DEFAULTS[key] || {}), ...value };
    } else if (key in DEFAULTS) {
      settings[key] = value;
    }
  }
  return settings;
}

export async function loadSettings(db) {
  const { results } = await db.prepare("SELECT key, value FROM settings").all();
  const stored = {};
  for (const row of results || []) {
    try {
      stored[row.key] = JSON.parse(row.value);
    } catch {
      stored[row.key] = row.value;
    }
  }
  return withDefaults(stored);
}

export async function saveSettings(db, settings) {
  const statements = [];
  for (const key of Object.keys(DEFAULTS)) {
    if (!(key in settings)) continue;
    statements.push(
      db.prepare("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value")
        .bind(key, JSON.stringify(settings[key])),
    );
  }
  if (statements.length) await db.batch(statements);
}

/** Turn "key = value" lines into an object. Blank lines and # comments are ignored. */
export function parseMap(text) {
  const result = {};
  for (const raw of String(text || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const idx = line.indexOf("=");
    const key = line.slice(0, idx).trim();
    if (key) result[key] = line.slice(idx + 1).trim();
  }
  return result;
}

export function formatMap(mapping) {
  return Object.entries(mapping || {}).map(([k, v]) => `${k} = ${v}`).join("\n");
}

export const hasClaude = (s) => Boolean(s.anthropic_api_key);
export const hasGoogleClient = (s) => Boolean(s.google_client_id && s.google_client_secret);
export const hasSage = (s) =>
  ["sage_sender_id", "sage_sender_password", "sage_company_id", "sage_user_id", "sage_user_password"].every((k) => Boolean(s[k]));

export function modelLabel(id) {
  const found = CLAUDE_MODELS.find(([value]) => value === id);
  return found ? found[1] : id;
}
