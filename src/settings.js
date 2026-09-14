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
  // Sage Intacct coding defaults. Generic starting points for a UK contractor
  // paying subcontractors and suppliers: Sage-style nominal codes and Intacct's
  // standard UK VAT tax detail names. Every ID must be replaced with, or checked
  // against, the lists in the company's own Intacct before the first real post.
  sage_location_id: "",
  sage_department_id: "",
  sage_tax_solution_id: "United Kingdom - VAT",
  sage_default_gl: "5002", // Miscellaneous purchases
  sage_cis_gl: "2214", // CIS deductions withheld, payable to HMRC
  sage_retention_gl: "2215", // Retentions held against subcontractors
  // Lookup maps (edited as "key = value" lines)
  gl_map: {
    materials: "5000", // Materials purchased
    labour: "6000", // Direct labour
    plant: "7700", // Plant and equipment hire
    subcontract: "6002", // Subcontractors
    services: "7603", // Professional and site services
    expenses: "7400", // Travel and subsistence
    other: "5002", // Miscellaneous purchases
  },
  // The fictional supplier and project on the bundled sample invoice, so it maps end to end out of the box.
  vendor_map: { "Northbank Mechanical Services Ltd": "V0088" },
  project_map: { HEL18: "P-HEL18" },
  vat_detail_map: {
    "20": "UK Purchase Goods Standard Rate",
    "5": "UK Purchase Goods Reduced Rate",
    "0": "UK Purchase Goods Zero Rate",
    reverse_charge: "UK Purchase Services Reverse Charge Standard Rate",
  },
  terms_map: { "0": "Due on receipt", "7": "Net 7", "14": "Net 14", "30": "Net 30", "45": "Net 45", "60": "Net 60", "90": "Net 90" },
};

/** What each generic code stands for, shown on the Settings page. */
export const GENERIC_CODES = [
  ["5000", "materials purchased"], ["6000", "direct labour"], ["7700", "plant and equipment hire"], ["6002", "subcontractors"],
  ["7603", "professional and site services"], ["7400", "travel and subsistence"], ["5002", "miscellaneous purchases (fallback)"],
  ["2214", "CIS deductions withheld"], ["2215", "retentions held"],
];

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
