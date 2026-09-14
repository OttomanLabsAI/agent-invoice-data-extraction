// Route handlers. Each takes a context { request, env, url, settings, params, flash }
// and returns a Response. Business logic lives in pipeline.js and friends.

import * as db from "./db.js";
import * as gmail from "./gmail.js";
import * as auth from "./auth.js";
import { checkApiKey } from "./claude.js";
import { normalise } from "./normalise.js";
import { billToXml, publicBill, LOG_COLUMNS } from "./sage_mapper.js";
import * as sage from "./sage_client.js";
import { toCsv } from "./csv.js";
import { describe as ragDescribe } from "./rag.js";
import { INVOICE_TOOL, LINE_CATEGORIES, DOCUMENT_TYPES, VAT_TREATMENTS } from "./schema.js";
import * as pipeline from "./pipeline.js";
import { resolveType, applyTypeDefaults, normaliseType, newTypeId, TYPE_SIGNALS } from "./invoice_types.js";
import { getFile, deleteFile } from "./files.js";
import {
  CLAUDE_MODELS, TEXT_FIELDS, INT_FIELDS, formatMap, parseMap, hasClaude, hasGoogleClient, hasSage, saveSettings,
} from "./settings.js";
import { page } from "./views/layout.js";
import * as pages from "./views/pages.js";

// --------------------------------------------------------------------------- Response helpers

export function htmlResponse(body, { status = 200, headers = {} } = {}) {
  return new Response(body, { status, headers: { "content-type": "text/html; charset=utf-8", ...headers } });
}

export function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8" } });
}

/** Redirect with an optional flash message carried in a short-lived cookie. */
export function redirect(ctx, location, flash) {
  const headers = { location };
  if (flash) {
    const value = btoa(unescape(encodeURIComponent(JSON.stringify(flash))));
    headers["set-cookie"] = auth.cookie("flash", value, { maxAge: 60, secure: auth.isSecure(ctx.request) });
  }
  return new Response(null, { status: 303, headers });
}

export function readFlash(ctx) {
  const raw = auth.readCookies(ctx.request).flash;
  if (!raw) return { flashes: [], clear: null };
  try {
    const parsed = JSON.parse(decodeURIComponent(escape(atob(raw))));
    return { flashes: [parsed], clear: auth.cookie("flash", "", { maxAge: 0, secure: auth.isSecure(ctx.request) }) };
  } catch {
    return { flashes: [], clear: auth.cookie("flash", "", { maxAge: 0, secure: auth.isSecure(ctx.request) }) };
  }
}

function render(ctx, { title, active, body, signedIn = true }) {
  const { flashes, clear } = readFlash(ctx);
  const companyName = (ctx.settings && ctx.settings.company_name) || "Glent Group";
  const headers = clear ? { "set-cookie": clear } : {};
  return htmlResponse(page({ title, companyName, active, flashes, body, signedIn }), { headers });
}

const field = (form, name) => String(form.get(name) ?? "").trim();
const numOrNull = (value) => {
  const text = String(value || "").trim().replace(/,/g, "");
  if (text === "") return null;
  const n = parseFloat(text);
  return Number.isFinite(n) ? Math.round(n * 100) / 100 : null;
};

async function invoiceOr404(ctx) {
  const invoice = await db.getInvoice(ctx.env.DB, Number(ctx.params.id));
  return invoice || null;
}

export function notFound(ctx) {
  const response = render(ctx, { title: "Not found", active: "", body: pages.notFoundPage() });
  return new Response(response.body, { status: 404, headers: response.headers });
}

// --------------------------------------------------------------------------- Sign-in

export async function signInForm(ctx, error = "") {
  const companyName = ctx.settings.company_name || "Glent Group";
  return htmlResponse(page({ title: "Sign in", companyName, active: "", flashes: [], body: pages.signInPage({ error, companyName }), signedIn: false }), {
    status: error ? 401 : 200,
  });
}

export async function signIn(ctx) {
  const form = await ctx.request.formData();
  if (!(await auth.passwordMatches(ctx.env, form.get("password")))) return signInForm(ctx, "That password is not right.");
  const session = await auth.newSession(ctx.env);
  return new Response(null, {
    status: 303,
    headers: { location: "/", "set-cookie": auth.cookie("session", session, { maxAge: auth.SESSION_MAX_AGE, secure: auth.isSecure(ctx.request) }) },
  });
}

export function signOut(ctx) {
  return new Response(null, {
    status: 303,
    headers: { location: "/signin", "set-cookie": auth.cookie("session", "", { maxAge: 0, secure: auth.isSecure(ctx.request) }) },
  });
}

export function setupNeeded(ctx) {
  return htmlResponse(page({ title: "Set up", companyName: "Glent Group", active: "", flashes: [], body: pages.setupPage(), signedIn: false }), { status: 503 });
}

// --------------------------------------------------------------------------- Inbox

export async function dashboard(ctx) {
  const status = ctx.url.searchParams.get("status") || null;
  const [invoices, counts, lastRun, gmailConnected] = await Promise.all([
    db.listInvoices(ctx.env.DB, status),
    db.statusCounts(ctx.env.DB),
    db.lastRun(ctx.env.DB, "inbox"),
    gmail.hasToken(ctx.env),
  ]);
  return render(ctx, {
    title: "Inbox",
    active: "inbox",
    body: pages.dashboardPage({
      invoices, counts, activeStatus: status, lastRun, gmailConnected,
      claudeReady: hasClaude(ctx.settings),
      continueRun: ctx.url.searchParams.get("continue") === "1",
    }),
  });
}

export async function runNow(ctx) {
  const result = await pipeline.checkInboxStep(ctx.env, ctx.settings);
  return redirect(ctx, result.ok && result.remaining ? "/?continue=1" : "/", [result.ok ? "ok" : "error", result.message]);
}

export async function upload(ctx) {
  if (!hasClaude(ctx.settings)) return redirect(ctx, "/settings", ["error", "Add your Claude API key in Settings before processing invoices."]);
  const form = await ctx.request.formData();
  const files = form.getAll("files").filter((f) => f && typeof f === "object" && f.name);
  if (!files.length) return redirect(ctx, "/", ["error", "Choose at least one PDF or image."]);
  let done = 0;
  let firstError = "";
  for (const file of files) {
    const ext = file.name.includes(".") ? file.name.slice(file.name.lastIndexOf(".")).toLowerCase() : "";
    const mime = gmail.EXT_TO_MIME[ext];
    if (!mime) {
      firstError = firstError || `Skipped ${file.name}: only PDF, PNG, JPG and WEBP are supported.`;
      continue;
    }
    const data = new Uint8Array(await file.arrayBuffer());
    const meta = { from: "manual upload", subject: file.name, received_at: db.nowIso() };
    await pipeline.processAttachment(ctx.env, ctx.settings, { data, filename: file.name, mimeType: mime, emailMeta: meta, source: "upload" });
    done += 1;
  }
  if (firstError && !done) return redirect(ctx, "/", ["error", firstError]);
  return redirect(ctx, "/", ["ok", `Processed ${done} file(s).${firstError ? " " + firstError : ""}`]);
}

// --------------------------------------------------------------------------- Invoice review

export async function invoiceDetail(ctx) {
  const invoice = await invoiceOr404(ctx);
  if (!invoice) return notFound(ctx);
  const sageData = invoice.sage || {};
  const bill = sageData.bill || {};
  const overrides = (invoice.extracted || {}).sage_overrides || {};
  const items = bill.ITEMS || [];
  return render(ctx, {
    title: (invoice.extracted || {}).invoice_number || invoice.attachment_name || "Invoice",
    active: "inbox",
    body: pages.invoicePage({
      inv: invoice,
      rec: invoice.extracted || {},
      sage: sageData,
      vendorId: overrides.vendor_id || bill.VENDORID || "",
      projectId: overrides.project_id || (items[0] && items[0].PROJECTID) || "",
      billJson: Object.keys(bill).length ? JSON.stringify(publicBill(bill), null, 2) : "",
      billXml: Object.keys(bill).length ? billToXml(bill) : "",
      categories: LINE_CATEGORIES,
      vatTreatments: VAT_TREATMENTS,
      documentTypes: DOCUMENT_TYPES,
      sageReady: hasSage(ctx.settings),
      invoiceTypes: ctx.settings.invoice_types || [],
      chosenType: overrides.invoice_type || "",
    }),
  });
}

export async function invoiceSave(ctx) {
  const invoice = await invoiceOr404(ctx);
  if (!invoice) return notFound(ctx);
  const form = await ctx.request.formData();
  const rec = { ...(invoice.extracted || {}) };

  rec.document_type = field(form, "document_type") || rec.document_type || "invoice";
  rec.supplier = { ...(rec.supplier || {}), name: field(form, "supplier_name"), vat_number: field(form, "supplier_vat") || null };
  rec.invoice_number = field(form, "invoice_number");
  rec.invoice_date = field(form, "invoice_date") || null;
  rec.due_date = field(form, "due_date") || null;
  rec.po_number = field(form, "po_number") || null;
  rec.project_reference = field(form, "project_reference") || null;
  rec.description = field(form, "description") || null;
  rec.currency = (field(form, "currency") || "GBP").toUpperCase();
  rec.vat_treatment = field(form, "vat_treatment") || "unknown";

  const col = (name, i) => {
    const values = form.getAll(name);
    return i < values.length ? String(values[i]) : "";
  };
  const lines = [];
  form.getAll("line_desc").forEach((desc, i) => {
    if (!String(desc).trim() && !col("line_net", i).trim()) return;
    lines.push({
      description: String(desc).trim(),
      quantity: numOrNull(col("line_qty", i)),
      unit: col("line_unit", i).trim() || null,
      unit_price: numOrNull(col("line_unit_price", i)),
      net_amount: numOrNull(col("line_net", i)),
      vat_rate: numOrNull(col("line_vat_rate", i)),
      vat_amount: numOrNull(col("line_vat", i)),
      category: col("line_category", i) || "other",
    });
  });
  rec.line_items = lines;
  rec.net_total = numOrNull(form.get("net_total"));
  rec.vat_total = numOrNull(form.get("vat_total"));
  rec.gross_total = numOrNull(form.get("gross_total"));
  rec.cis = { ...(rec.cis || {}), applicable: form.get("cis_applicable") === "on", deduction_rate: numOrNull(form.get("cis_rate")), deduction_amount: numOrNull(form.get("cis_amount")) };
  rec.retention = { ...(rec.retention || {}), applicable: Boolean(numOrNull(form.get("retention_amount"))), percentage: numOrNull(form.get("retention_pct")), amount: numOrNull(form.get("retention_amount")) };

  let normalised = normalise(rec);
  const overrides = { vendor_id: field(form, "vendor_id"), project_id: field(form, "project_id"), invoice_type: field(form, "invoice_type") };
  normalised = applyTypeDefaults(normalised, resolveType(normalised, ctx.settings, overrides).type).rec;
  normalised.sage_overrides = overrides;
  const mapped = pipeline.remap({ ...invoice, extracted: normalised }, ctx.settings, overrides);
  await db.updateInvoice(ctx.env.DB, invoice.id, { extracted: normalised, sage: mapped, issues: mapped.issues, notes: field(form, "notes") });
  return redirect(ctx, `/invoice/${invoice.id}`, ["ok", "Saved. Sage payload rebuilt."]);
}

export async function invoiceStatus(ctx) {
  const invoice = await invoiceOr404(ctx);
  if (!invoice) return notFound(ctx);
  const form = await ctx.request.formData();
  const action = field(form, "action");
  if (action === "delete") {
    if (invoice.attachment_key) await deleteFile(ctx.env.DB, invoice.attachment_key);
    await db.deleteInvoice(ctx.env.DB, invoice.id);
    return redirect(ctx, "/", ["ok", "Deleted."]);
  }
  const newStatus = { approve: "approved", query: "queried", reopen: "review", posted: "posted" }[action];
  if (!newStatus) return new Response("Bad request", { status: 400 });
  if (action === "approve" && (invoice.issues || []).length) {
    return redirect(ctx, `/invoice/${invoice.id}`, ["error", "Fix the outstanding checks before approving."]);
  }
  await db.updateInvoice(ctx.env.DB, invoice.id, { status: newStatus });
  return redirect(ctx, `/invoice/${invoice.id}`, ["ok", `Marked as ${db.STATUS_LABELS[newStatus].toLowerCase()}.`]);
}

export async function invoiceRemap(ctx) {
  const invoice = await invoiceOr404(ctx);
  if (!invoice) return notFound(ctx);
  if (!invoice.extracted) return new Response("Bad request", { status: 400 });
  const overrides = invoice.extracted.sage_overrides || {};
  const mapped = pipeline.remap(invoice, ctx.settings, overrides);
  await db.updateInvoice(ctx.env.DB, invoice.id, { sage: mapped, issues: mapped.issues });
  return redirect(ctx, `/invoice/${invoice.id}`, ["ok", "Sage payload rebuilt with the current settings."]);
}

export async function invoicePush(ctx) {
  const invoice = await invoiceOr404(ctx);
  if (!invoice) return notFound(ctx);
  if (!hasSage(ctx.settings)) return redirect(ctx, `/invoice/${invoice.id}`, ["error", "Fill in the Sage Intacct connection in Settings first."]);
  if (invoice.status !== "approved") return redirect(ctx, `/invoice/${invoice.id}`, ["error", "Approve the invoice before posting it to Sage."]);
  const bill = (invoice.sage || {}).bill;
  if (!bill) return redirect(ctx, `/invoice/${invoice.id}`, ["error", "Nothing to post - no Sage payload on this invoice."]);
  let result;
  try {
    result = await sage.postBill(ctx.settings, bill);
  } catch (err) {
    return redirect(ctx, `/invoice/${invoice.id}`, ["error", `Sage did not accept the bill: ${err.message || err}`]);
  }
  await db.updateInvoice(ctx.env.DB, invoice.id, { status: "posted", intacct_recordno: result.recordno || "" });
  return redirect(ctx, `/invoice/${invoice.id}`, ["ok", `Posted to Sage as ${result.object} record ${result.recordno || "(no record number returned)"}.`]);
}

export async function invoiceFile(ctx) {
  const invoice = await invoiceOr404(ctx);
  if (!invoice || !invoice.attachment_key) return notFound(ctx);
  const file = await getFile(ctx.env.DB, invoice.attachment_key);
  if (!file) return notFound(ctx);
  const name = String(invoice.attachment_name || "attachment").replace(/["\r\n]/g, "");
  return new Response(file.bytes, {
    headers: {
      "content-type": invoice.mime_type || file.contentType || "application/octet-stream",
      "content-disposition": `inline; filename="${name}"`,
      "cache-control": "private, no-store",
    },
  });
}

export async function invoiceSageJson(ctx) {
  const invoice = await invoiceOr404(ctx);
  if (!invoice) return notFound(ctx);
  const bill = (invoice.sage || {}).bill || {};
  return new Response(JSON.stringify(publicBill(bill), null, 2), {
    headers: { "content-type": "application/json", "content-disposition": `attachment; filename=invoice-${invoice.id}-intacct.json` },
  });
}

export async function invoiceSageXml(ctx) {
  const invoice = await invoiceOr404(ctx);
  if (!invoice) return notFound(ctx);
  const bill = (invoice.sage || {}).bill || {};
  return new Response(billToXml(bill), {
    headers: { "content-type": "application/xml", "content-disposition": `attachment; filename=invoice-${invoice.id}-intacct.xml` },
  });
}

export async function exportCsv(ctx) {
  const status = ctx.url.searchParams.get("status") || null;
  const rows = [];
  for (const inv of await db.listInvoices(ctx.env.DB, status)) {
    const logRow = { ...(((inv.sage || {}).log_row) || {}) };
    if (!Object.keys(logRow).length) continue;
    logRow.Status = inv.status_label;
    logRow["Sage record no"] = inv.intacct_recordno || "";
    rows.push(logRow);
  }
  const name = `invoice-log-${status || "all"}-${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.csv`;
  return new Response(toCsv(LOG_COLUMNS, rows), { headers: { "content-type": "text/csv", "content-disposition": `attachment; filename=${name}` } });
}

// --------------------------------------------------------------------------- Agent tabs

function applyFields(form, settings, textKeys, intKeys = []) {
  for (const key of textKeys) if (form.has(key)) settings[key] = field(form, key);
  for (const key of intKeys) {
    if (!form.has(key)) continue;
    const n = parseInt(field(form, key), 10);
    settings[key] = Number.isFinite(n) ? Math.max(0, n) : INT_FIELDS[key];
  }
}

const CLASSIFY_TEXT = ["classify_model", "classify_reference_text", "classify_invoice_label", "classify_other_label", "gmail_query", "gmail_allowed_senders"];
const EXTRACT_TEXT = ["extract_model", "extract_reference_text", "gmail_processed_label"];

export async function classificationTab(ctx) {
  const s = ctx.settings;
  const [connected, recent, counts, lastRun] = await Promise.all([
    gmail.hasToken(ctx.env), db.listClassifications(ctx.env.DB, 50), db.classificationCounts(ctx.env.DB), db.lastRun(ctx.env.DB, "classify"),
  ]);
  return render(ctx, {
    title: "Agent - Classification",
    active: "classification",
    body: pages.classificationPage({
      s, models: CLAUDE_MODELS, connected, claudeReady: hasClaude(s), recent, counts, lastRun,
      ragInfo: ragDescribe(s.classify_reference_text), continueRun: ctx.url.searchParams.get("continue") === "1",
    }),
  });
}

export async function classificationSave(ctx) {
  const form = await ctx.request.formData();
  const settings = ctx.settings;
  applyFields(form, settings, CLASSIFY_TEXT, ["gmail_max_messages"]);
  if (!settings.classify_invoice_label) settings.classify_invoice_label = "Invoice Incoming";
  if (!settings.gmail_max_messages) settings.gmail_max_messages = 1;
  await saveSettings(ctx.env.DB, settings);
  if (field(form, "action") === "run") return classificationRun(ctx);
  return redirect(ctx, "/agents/classification", ["ok", "Classification agent saved."]);
}

export async function classificationRun(ctx) {
  const result = await pipeline.classifyStep(ctx.env, ctx.settings);
  return redirect(ctx, result.ok && result.remaining ? "/agents/classification?continue=1" : "/agents/classification", [result.ok ? "ok" : "error", result.message]);
}

export async function extractionTab(ctx) {
  const s = ctx.settings;
  const [connected, lastRun, counts] = await Promise.all([gmail.hasToken(ctx.env), db.lastRun(ctx.env.DB, "extract"), db.statusCounts(ctx.env.DB)]);
  return render(ctx, {
    title: "Agent - Invoice Extraction",
    active: "extraction",
    body: pages.extractionPage({
      s, models: CLAUDE_MODELS, connected, claudeReady: hasClaude(s), lastRun, counts,
      ragInfo: ragDescribe(s.extract_reference_text), continueRun: ctx.url.searchParams.get("continue") === "1",
      schema: INVOICE_TOOL.input_schema,
    }),
  });
}

export async function agentTree(ctx) {
  const s = ctx.settings;
  const hasTok = await gmail.hasToken(ctx.env);
  const [status, classifyRun, extractRun, classCounts, invoiceCounts] = await Promise.all([
    hasTok ? gmail.connectionStatus(ctx.env, s) : { connected: false, email: null, error: null },
    db.lastRun(ctx.env.DB, "classify"),
    db.lastRun(ctx.env.DB, "extract"),
    db.classificationCounts(ctx.env.DB),
    db.statusCounts(ctx.env.DB),
  ]);
  return render(ctx, {
    title: "Agent tree",
    active: "tree",
    body: pages.agentTreePage({ s, gmail: status, claudeReady: hasClaude(s), sageReady: hasSage(s), classifyRun, extractRun, classCounts, invoiceCounts }),
  });
}

export async function extractionSave(ctx) {
  const form = await ctx.request.formData();
  const settings = ctx.settings;
  applyFields(form, settings, EXTRACT_TEXT, ["poll_minutes"]);
  await saveSettings(ctx.env.DB, settings);
  if (field(form, "action") === "run") return extractionRun(ctx);
  return redirect(ctx, "/agents/extraction", ["ok", "Extraction agent saved."]);
}

export async function extractionRun(ctx) {
  const result = await pipeline.extractStep(ctx.env, ctx.settings);
  return redirect(ctx, result.ok && result.remaining ? "/agents/extraction?continue=1" : "/agents/extraction", [result.ok ? "ok" : "error", result.message]);
}

// --------------------------------------------------------------------------- Invoice types

export async function typesTab(ctx) {
  return render(ctx, {
    title: "Invoice types",
    active: "types",
    body: pages.typesPage({ types: ctx.settings.invoice_types || [], categories: LINE_CATEGORIES }),
  });
}

function readType(form, t) {
  const f = (name) => `${t.id}__${name}`;
  if (!form.has(f("name"))) return t;
  return normaliseType({
    id: t.id,
    name: field(form, f("name")) || t.name,
    description: field(form, f("description")),
    suppliers: String(form.get(f("suppliers")) || ""),
    categories: LINE_CATEGORIES.filter((c) => form.has(f("cat_" + c))),
    signals: TYPE_SIGNALS.map(([k]) => k).filter((k) => form.has(f("sig_" + k))),
    gl_map: Object.fromEntries(LINE_CATEGORIES.map((c) => [c, field(form, f("gl_" + c))])),
    vat_detail_map: { "20": field(form, f("vat_20")), "5": field(form, f("vat_5")), "0": field(form, f("vat_0")), reverse_charge: field(form, f("vat_rc")) },
    cis_applies: form.has(f("cis_applies")),
    cis_rate: field(form, f("cis_rate")),
    retention_percent: field(form, f("retention_percent")),
    terms_days: field(form, f("terms_days")),
    po_required: form.has(f("po_required")),
    project_required: form.has(f("project_required")),
    expected_vat: field(form, f("expected_vat")),
    location_id: field(form, f("location_id")),
    department_id: field(form, f("department_id")),
    sage_action: field(form, f("sage_action")),
  });
}

export async function typesSave(ctx) {
  const form = await ctx.request.formData();
  const settings = ctx.settings;
  const action = field(form, "action");
  let types = (settings.invoice_types || []).map((t) => readType(form, t));
  let anchor = "";
  let message = "Invoice types saved.";
  if (action === "add") {
    const id = newTypeId(types.map((t) => t.id));
    types.push(normaliseType({ id, name: "New type" }));
    anchor = `#type-${id}`;
    message = "Type added - name it and save.";
  } else if (action.startsWith("remove:")) {
    const id = action.slice(7);
    const gone = types.find((t) => t.id === id);
    types = types.filter((t) => t.id !== id);
    message = gone ? `Removed the ${gone.name} type.` : message;
  }
  settings.invoice_types = types;
  await saveSettings(ctx.env.DB, settings);
  return redirect(ctx, "/types" + anchor, ["ok", message]);
}

// --------------------------------------------------------------------------- Settings

const SETTINGS_TEXT = TEXT_FIELDS.filter((k) => !CLASSIFY_TEXT.includes(k) && !EXTRACT_TEXT.includes(k));

export async function settingsPage(ctx) {
  const s = ctx.settings;
  const hasTok = await gmail.hasToken(ctx.env);
  const status = hasTok ? await gmail.connectionStatus(ctx.env, s) : { connected: false, email: null, error: null };
  return render(ctx, {
    title: "Settings",
    active: "settings",
    body: pages.settingsPage({
      s,
      gmail: status,
      redirectUri: `${ctx.url.origin}/oauth/callback`,
      vendorMapText: formatMap(s.vendor_map),
      projectMapText: formatMap(s.project_map),
      termsMapText: formatMap(s.terms_map),
      categories: LINE_CATEGORIES,
    }),
  });
}

export async function settingsSave(ctx) {
  const form = await ctx.request.formData();
  const settings = ctx.settings;
  applyFields(form, settings, SETTINGS_TEXT);
  if (form.has("gl_labour")) settings.gl_map = Object.fromEntries(LINE_CATEGORIES.map((cat) => [cat, field(form, `gl_${cat}`)]));
  if (form.has("vat_20")) settings.vat_detail_map = { "20": field(form, "vat_20"), "5": field(form, "vat_5"), "0": field(form, "vat_0"), reverse_charge: field(form, "vat_rc") };
  if (form.has("vendor_map")) settings.vendor_map = parseMap(form.get("vendor_map"));
  if (form.has("project_map")) settings.project_map = parseMap(form.get("project_map"));
  if (form.has("terms_map")) settings.terms_map = parseMap(form.get("terms_map"));
  await saveSettings(ctx.env.DB, settings);
  if (field(form, "action") === "connect_gmail") return gmailConnect(ctx);
  return redirect(ctx, "/settings", ["ok", "Settings saved."]);
}

export async function testClaude(ctx) {
  const body = await ctx.request.json().catch(() => ({}));
  const key = String(body.key || "").trim();
  if (!key) return jsonResponse({ ok: false, message: "Paste a key first." });
  const [ok, message] = await checkApiKey(ctx.env, key);
  return jsonResponse({ ok, message });
}

export async function testSage(ctx) {
  const body = await ctx.request.json().catch(() => ({}));
  const probe = {
    sage_endpoint: body.endpoint || sage.DEFAULT_ENDPOINT,
    sage_sender_id: body.sender_id || "",
    sage_sender_password: body.sender_password || "",
    sage_company_id: body.company_id || "",
    sage_user_id: body.user_id || "",
    sage_user_password: body.user_password || "",
  };
  if (!hasSage(probe)) return jsonResponse({ ok: false, message: "Fill in all five Intacct fields first." });
  const [ok, message] = await sage.testConnection(probe);
  return jsonResponse({ ok, message });
}

// --------------------------------------------------------------------------- Gmail OAuth

export async function gmailConnect(ctx) {
  if (!hasGoogleClient(ctx.settings)) return redirect(ctx, "/settings", ["error", "Paste the Google OAuth client ID and secret, save, then connect."]);
  const state = auth.randomToken();
  const location = gmail.authUrl(ctx.settings, `${ctx.url.origin}/oauth/callback`, state);
  return new Response(null, {
    status: 303,
    headers: { location, "set-cookie": auth.cookie("oauth_state", state, { maxAge: 600, secure: auth.isSecure(ctx.request) }) },
  });
}

export async function gmailCallback(ctx) {
  const params = ctx.url.searchParams;
  if (params.get("error")) return redirect(ctx, "/settings", ["error", `Google said: ${params.get("error")}`]);
  const expected = auth.readCookies(ctx.request).oauth_state || "";
  if (!expected || params.get("state") !== expected) return redirect(ctx, "/settings", ["error", "Gmail connection failed: the sign-in state did not match. Try Connect Gmail again."]);
  try {
    await gmail.exchangeCode(ctx.env.DB, ctx.settings, params.get("code") || "", `${ctx.url.origin}/oauth/callback`);
  } catch (err) {
    return redirect(ctx, "/settings", ["error", `Gmail connection failed: ${err.message || err}`]);
  }
  const status = await gmail.connectionStatus(ctx.env, ctx.settings);
  return redirect(ctx, "/settings", ["ok", `Gmail connected as ${status.email || "unknown account"}.`]);
}

export async function gmailDisconnect(ctx) {
  await gmail.clearToken(ctx.env.DB);
  return redirect(ctx, "/settings", ["ok", "Gmail disconnected. The stored token was deleted."]);
}
