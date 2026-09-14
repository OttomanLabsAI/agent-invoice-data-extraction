// D1 storage: settings, app state, invoices, runs and classifications.

export const STATUSES = ["review", "approved", "posted", "queried", "not_invoice", "error"];

export const STATUS_LABELS = {
  review: "Needs review",
  approved: "Approved",
  posted: "Posted to Sage",
  queried: "Queried",
  not_invoice: "Not an invoice",
  error: "Failed",
};

const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'gmail',
    gmail_message_id TEXT,
    gmail_thread_id TEXT,
    from_addr TEXT,
    subject TEXT,
    received_at TEXT,
    attachment_name TEXT,
    attachment_key TEXT,
    mime_type TEXT,
    status TEXT NOT NULL DEFAULT 'review',
    extracted_json TEXT,
    sage_json TEXT,
    issues_json TEXT,
    notes TEXT DEFAULT '',
    error TEXT DEFAULT '',
    intacct_recordno TEXT DEFAULT '',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    UNIQUE(gmail_message_id, attachment_name)
  )`,
  `CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL DEFAULT 'inbox',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT,
    ok INTEGER DEFAULT 1
  )`,
  `CREATE TABLE IF NOT EXISTS files (
    key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    content_type TEXT,
    size INTEGER,
    data BLOB,
    PRIMARY KEY (key, seq)
  )`,
  `CREATE TABLE IF NOT EXISTS classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    gmail_message_id TEXT UNIQUE,
    from_addr TEXT,
    subject TEXT,
    received_at TEXT,
    attachments TEXT DEFAULT '',
    verdict TEXT NOT NULL,
    document_kind TEXT DEFAULT '',
    confidence REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    label_applied TEXT DEFAULT '',
    model TEXT DEFAULT '',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0
  )`,
];

let schemaReady = null;

/** Create the tables on first use in this isolate. Cheap after the first call. */
export function ensureSchema(db) {
  if (!schemaReady) {
    schemaReady = db.batch(SCHEMA.map((sql) => db.prepare(sql))).catch((err) => {
      schemaReady = null;
      throw err;
    });
  }
  return schemaReady;
}

export function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

// --------------------------------------------------------------------------- state (key/value)

export async function getState(db, key) {
  const row = await db.prepare("SELECT value FROM state WHERE key = ?").bind(key).first();
  return row ? row.value : null;
}

export async function setState(db, key, value) {
  await db
    .prepare("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    .bind(key, value)
    .run();
}

export async function deleteState(db, key) {
  await db.prepare("DELETE FROM state WHERE key = ?").bind(key).run();
}

/** Take the run lock unless another run started less than `staleMinutes` ago. Returns true when taken. */
export async function acquireRunLock(db, staleMinutes = 10) {
  const now = Date.now();
  const stale = String(now - staleMinutes * 60 * 1000);
  await db.prepare("INSERT OR IGNORE INTO state (key, value) VALUES ('run_lock', '0')").run();
  const result = await db
    .prepare("UPDATE state SET value = ? WHERE key = 'run_lock' AND CAST(value AS INTEGER) < CAST(? AS INTEGER)")
    .bind(String(now), stale)
    .run();
  return (result.meta && result.meta.changes) === 1;
}

export async function releaseRunLock(db) {
  await setState(db, "run_lock", "0");
}

// --------------------------------------------------------------------------- invoices

function rowToInvoice(row) {
  if (!row) return null;
  const d = { ...row };
  for (const key of ["extracted_json", "sage_json", "issues_json"]) {
    const raw = d[key];
    try {
      d[key.slice(0, -5)] = raw ? JSON.parse(raw) : null;
    } catch {
      d[key.slice(0, -5)] = null;
    }
  }
  d.status_label = STATUS_LABELS[d.status] || d.status;
  return d;
}

export async function alreadyProcessed(db, gmailMessageId, attachmentName) {
  const row = await db
    .prepare("SELECT 1 AS x FROM invoices WHERE gmail_message_id = ? AND attachment_name = ?")
    .bind(gmailMessageId, attachmentName)
    .first();
  return Boolean(row);
}

export async function insertInvoice(db, record) {
  const fields = {
    created_at: nowIso(),
    source: record.source || "gmail",
    gmail_message_id: record.gmail_message_id ?? null,
    gmail_thread_id: record.gmail_thread_id ?? null,
    from_addr: record.from_addr ?? null,
    subject: record.subject ?? null,
    received_at: record.received_at ?? null,
    attachment_name: record.attachment_name ?? null,
    attachment_key: record.attachment_key ?? null,
    mime_type: record.mime_type ?? null,
    status: record.status || "review",
    extracted_json: record.extracted != null ? JSON.stringify(record.extracted) : null,
    sage_json: record.sage != null ? JSON.stringify(record.sage) : null,
    issues_json: JSON.stringify(record.issues || []),
    notes: record.notes || "",
    error: record.error || "",
    input_tokens: record.input_tokens || 0,
    output_tokens: record.output_tokens || 0,
  };
  const cols = Object.keys(fields);
  const result = await db
    .prepare(`INSERT INTO invoices (${cols.join(", ")}) VALUES (${cols.map(() => "?").join(", ")})`)
    .bind(...Object.values(fields))
    .run();
  return Number(result.meta.last_row_id);
}

export async function updateInvoice(db, invoiceId, changes) {
  const payload = {};
  for (const [key, value] of Object.entries(changes)) {
    if (["extracted", "sage", "issues"].includes(key)) payload[key + "_json"] = JSON.stringify(value);
    else payload[key] = value;
  }
  const keys = Object.keys(payload);
  if (!keys.length) return;
  await db
    .prepare(`UPDATE invoices SET ${keys.map((k) => `${k} = ?`).join(", ")} WHERE id = ?`)
    .bind(...Object.values(payload), invoiceId)
    .run();
}

export async function getInvoice(db, invoiceId) {
  const row = await db.prepare("SELECT * FROM invoices WHERE id = ?").bind(invoiceId).first();
  return rowToInvoice(row);
}

export async function listInvoices(db, status) {
  const order = "ORDER BY COALESCE(received_at, created_at) DESC, id DESC";
  const stmt = status && STATUSES.includes(status)
    ? db.prepare(`SELECT * FROM invoices WHERE status = ? ${order}`).bind(status)
    : db.prepare(`SELECT * FROM invoices ${order}`);
  const { results } = await stmt.all();
  return (results || []).map(rowToInvoice);
}

export async function statusCounts(db) {
  const { results } = await db.prepare("SELECT status, COUNT(*) AS n FROM invoices GROUP BY status").all();
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0]));
  for (const r of results || []) counts[r.status] = r.n;
  counts.total = STATUSES.reduce((sum, s) => sum + counts[s], 0);
  return counts;
}

export async function deleteInvoice(db, invoiceId) {
  await db.prepare("DELETE FROM invoices WHERE id = ?").bind(invoiceId).run();
}

// --------------------------------------------------------------------------- runs

export async function startRun(db, stage = "inbox") {
  const result = await db.prepare("INSERT INTO runs (stage, started_at) VALUES (?, ?)").bind(stage, nowIso()).run();
  return Number(result.meta.last_row_id);
}

export async function finishRun(db, runId, summary, ok = true) {
  await db
    .prepare("UPDATE runs SET finished_at = ?, summary = ?, ok = ? WHERE id = ?")
    .bind(nowIso(), summary, ok ? 1 : 0, runId)
    .run();
}

export async function lastRun(db, stage) {
  const stmt = stage
    ? db.prepare("SELECT * FROM runs WHERE stage = ? ORDER BY id DESC LIMIT 1").bind(stage)
    : db.prepare("SELECT * FROM runs ORDER BY id DESC LIMIT 1");
  const row = await stmt.first();
  return row || null;
}

// --------------------------------------------------------------------------- classifications

export async function recordClassification(db, entry) {
  await db
    .prepare(
      `INSERT INTO classifications (created_at, gmail_message_id, from_addr, subject, received_at, attachments, verdict,
         document_kind, confidence, reason, label_applied, model, input_tokens, output_tokens)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(gmail_message_id) DO UPDATE SET created_at = excluded.created_at, verdict = excluded.verdict,
         document_kind = excluded.document_kind, confidence = excluded.confidence, reason = excluded.reason,
         label_applied = excluded.label_applied, model = excluded.model, input_tokens = excluded.input_tokens,
         output_tokens = excluded.output_tokens`,
    )
    .bind(
      nowIso(), entry.gmail_message_id, entry.from_addr || "", entry.subject || "", entry.received_at || "",
      entry.attachments || "", entry.verdict, entry.document_kind || "", entry.confidence || 0, entry.reason || "",
      entry.label_applied || "", entry.model || "", entry.input_tokens || 0, entry.output_tokens || 0,
    )
    .run();
}

export async function listClassifications(db, limit = 50) {
  const { results } = await db.prepare("SELECT * FROM classifications ORDER BY id DESC LIMIT ?").bind(limit).all();
  return results || [];
}

export async function classificationCounts(db) {
  const { results } = await db.prepare("SELECT verdict, COUNT(*) AS n FROM classifications GROUP BY verdict").all();
  const counts = { invoice: 0, not_invoice: 0 };
  for (const r of results || []) counts[r.verdict] = r.n;
  counts.total = counts.invoice + counts.not_invoice;
  return counts;
}
