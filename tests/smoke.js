// End-to-end smoke test: starts `wrangler dev` with the fake mailbox and fake
// Claude answers (TEST_FIXTURES=1), then drives every route over HTTP.
//   npm run smoke
// Must end with "All checks passed."

import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PORT = Number(process.env.SMOKE_PORT || 8797);
const BASE = `http://127.0.0.1:${PORT}`;
const PASSWORD = "smoke-test-password";
const SAMPLE_PDF = readFileSync(path.join(ROOT, "samples", "sample-invoice.pdf"));

const persist = mkdtempSync(path.join(tmpdir(), "invoice-agent-smoke-"));
const wrangler = spawn(
  process.execPath,
  [
    path.join(ROOT, "node_modules", "wrangler", "bin", "wrangler.js"), "dev", "--local", "--ip", "127.0.0.1", "--port", String(PORT),
    "--var", `APP_PASSWORD:${PASSWORD}`, "--var", "TEST_FIXTURES:1", "--persist-to", persist, "--log-level", "error", "--test-scheduled",
  ],
  { cwd: ROOT, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, CI: "1", WRANGLER_SEND_METRICS: "false" } },
);
let wranglerOutput = "";
wrangler.stdout.on("data", (d) => (wranglerOutput += d));
wrangler.stderr.on("data", (d) => (wranglerOutput += d));

const jar = new Map();
function storeCookies(response) {
  const set = typeof response.headers.getSetCookie === "function" ? response.headers.getSetCookie() : [];
  for (const line of set) {
    const [pair, ...attrs] = line.split(";");
    const idx = pair.indexOf("=");
    const name = pair.slice(0, idx).trim();
    const value = pair.slice(idx + 1).trim();
    const gone = attrs.some((a) => /max-age=0/i.test(a.trim()));
    if (gone) jar.delete(name);
    else jar.set(name, value);
  }
}
const cookieHeader = () => [...jar].map(([k, v]) => `${k}=${v}`).join("; ");

/** fetch with the cookie jar, following 303s by hand so flash cookies are seen. */
async function req(pathname, { method = "GET", form, json, body, headers = {} } = {}) {
  const init = { method, redirect: "manual", headers: { cookie: cookieHeader(), ...headers } };
  if (form) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(form)) (Array.isArray(v) ? v : [v]).forEach((x) => params.append(k, x));
    init.body = params;
  } else if (json) {
    init.body = JSON.stringify(json);
    init.headers["content-type"] = "application/json";
  } else if (body) {
    init.body = body;
  }
  let response = await fetch(BASE + pathname, init);
  storeCookies(response);
  let hops = 0;
  while ([301, 302, 303].includes(response.status) && hops < 5) {
    const location = new URL(response.headers.get("location"), BASE);
    response = await fetch(location, { redirect: "manual", headers: { cookie: cookieHeader() } });
    storeCookies(response);
    hops += 1;
  }
  const text = await response.text();
  return { status: response.status, text, headers: response.headers, path: pathname };
}

let failed = 0;
function check(cond, message) {
  console.log((cond ? "ok  - " : "FAIL - ") + message);
  if (!cond) failed += 1;
}

async function waitForServer() {
  for (let i = 0; i < 120; i++) {
    try {
      const r = await fetch(`${BASE}/signin`, { redirect: "manual" });
      if (r.status === 200) return;
    } catch {
      /* not up yet */
    }
    if (wrangler.exitCode !== null) throw new Error("wrangler dev exited early:\n" + wranglerOutput);
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("wrangler dev did not come up:\n" + wranglerOutput);
}

const SETTINGS_FORM = {
  anthropic_api_key: "sk-ant-test", google_client_id: "cid", google_client_secret: "csec",
  company_name: "Glent Group", default_currency: "GBP",
  sage_endpoint: "https://api.intacct.com/ia/xml/xmlgw.phtml", sage_sender_id: "", sage_sender_password: "", sage_company_id: "",
  sage_user_id: "", sage_user_password: "", sage_action: "Draft", sage_location_id: "LON", sage_department_id: "MEP",
  sage_tax_solution_id: "United Kingdom - VAT", sage_default_gl: "5000", sage_cis_gl: "2210", sage_retention_gl: "2220",
  gl_materials: "5100", gl_labour: "5200", gl_plant: "5300", gl_subcontract: "5400", gl_services: "5500", gl_expenses: "7000", gl_other: "5000",
  vat_20: "UK Purchase Goods Standard Rate", vat_5: "UK Purchase Goods Reduced Rate", vat_0: "UK Purchase Goods Zero Rate",
  vat_rc: "UK Purchase Services Reverse Charge Standard Rate",
  terms_map: "30 = Net 30\n14 = Net 14", vendor_map: "Northbank Mechanical Services Ltd = V0088\n# comment\nSpeedy Hire = V0117", project_map: "HEL18 = P-HEL18",
};

async function run() {
  await waitForServer();

  // ---- Sign-in gate
  let r = await fetch(`${BASE}/`, { redirect: "manual" });
  check(r.status === 303 && r.headers.get("location").endsWith("/signin"), "unauthenticated visit is sent to sign-in");
  r = await fetch(`${BASE}/run`, { method: "POST", redirect: "manual" });
  check(r.status === 401, "unauthenticated post is refused");
  let page = await req("/signin", { method: "POST", form: { password: "wrong" } });
  check(page.status === 401 && page.text.includes("not right"), "wrong password is refused");
  page = await req("/signin", { method: "POST", form: { password: PASSWORD } });
  check(page.status === 200 && jar.has("session") && page.text.includes("<h1>Inbox</h1>"), "right password signs in and lands on the inbox");
  check(page.headers.get("x-frame-options") === "SAMEORIGIN" && page.headers.get("cache-control") === "no-store", "security headers on pages");
  check(page.text.includes("Nothing is connected yet") === false && page.text.includes("Inbox"), "inbox renders");
  r = await fetch(`${BASE}/run`, { method: "POST", redirect: "manual", headers: { cookie: cookieHeader(), origin: "https://evil.example", "sec-fetch-site": "cross-site" } });
  check(r.status === 403, "cross-site post is refused");
  page = await req("/assets/css/style.css");
  check(page.status === 200 && page.text.includes("--paper"), "stylesheet served as a static asset");
  page = await req("/robots.txt");
  check(page.text.includes("Disallow: /"), "robots.txt keeps crawlers out");

  // ---- Settings
  page = await req("/settings", { method: "POST", form: SETTINGS_FORM });
  check(page.status === 200 && page.text.includes("Settings saved.") && page.text.includes('value="sk-ant-test"'), "settings saved and the key comes back into the masked input");
  check((page.text.match(/type="password"/g) || []).length >= 8 && (page.text.match(/class="reveal"/g) || []).length >= 8, "every secret field is masked with a reveal button");
  check(page.text.includes("/oauth/callback") && page.text.includes("Connected</span> as invoices@glent.example"), "settings show the redirect URI and the (fixture) Gmail connection");
  page = await req("/settings/test/sage", { method: "POST", json: { sender_id: "" } });
  check(page.status === 200 && JSON.parse(page.text).ok === false, "sage test refuses without credentials");
  page = await req("/settings/test/claude", { method: "POST", json: { key: "sk-ant-test" } });
  check(JSON.parse(page.text).ok === true, "claude key test answers");
  page = await req("/settings/test/claude", { method: "POST", json: { key: "" } });
  check(JSON.parse(page.text).message === "Paste a key first.", "claude key test needs a key");

  // ---- Agent - Classification
  page = await req("/agents/classification");
  check(page.status === 200 && page.text.includes("Agent - Classification") && page.text.includes("Nothing classified yet."), "classification tab renders");
  page = await req("/agents/classification", {
    method: "POST",
    form: { classify_model: "claude-haiku-4-5-20251001", classify_reference_text: "Northbank send applications for payment monthly.", classify_invoice_label: "Invoice Incoming", classify_other_label: "Not an invoice", gmail_query: "is:unread", gmail_allowed_senders: "", gmail_max_messages: "5", action: "run" },
  });
  check(page.status === 200 && page.text.includes("Looked at 3 email(s): 1 labelled Invoice Incoming, 2 Not an invoice, 0 failed."), "classification run labels the fake mailbox");
  check((page.text.match(/class="status approved">Invoice<\/span>/g) || []).length === 1 && (page.text.match(/class="status not_invoice">Not an invoice<\/span>/g) || []).length === 2, "recent decisions table lists one invoice and two others");
  check(page.text.includes("Subject and attachment name say invoice.") && page.text.includes("label: Invoice Incoming"), "decision shows the reason and the label applied");
  check(page.text.includes("3 emails classified so far: 1 invoice, 2 not."), "classification counts");
  check(page.text.includes("Northbank send applications for payment monthly.") && page.text.includes("sent whole with every email"), "reference text saved and described");
  page = await req("/agents/classification/run", { method: "POST" });
  check(page.text.includes("Looked at 0 email(s)"), "second classification run finds nothing new");

  // ---- Agent - Invoice Extraction
  page = await req("/agents/extraction");
  check(page.status === 200 && page.text.includes("Agent - Invoice Extraction") && page.text.includes("<code>supplier.vat_number</code>"), "extraction tab renders the schema");
  page = await req("/agents/extraction", {
    method: "POST",
    form: { extract_model: "claude-sonnet-5", extract_reference_text: "HEL18 is the Helsinki data centre.", gmail_processed_label: "Invoices/Processed", poll_minutes: "0", action: "run" },
  });
  check(page.status === 200 && page.text.includes("Checked 1 email(s): 1 processed, 0 skipped, 0 failed."), "extraction run processes the labelled invoice");
  check(page.text.includes("1 document in the Inbox: 1 needing review"), "extraction tab counts the result");
  page = await req("/agents/extraction/run", { method: "POST" });
  check(page.text.includes("Checked 0 email(s)"), "processed mail is not read twice");

  // ---- Inbox and the combined check
  page = await req("/");
  check(page.text.includes("Northbank Mechanical Services Ltd") && page.text.includes("NMS-2026-0417") && page.text.includes("Needs review"), "inbox lists the extracted invoice");
  page = await req("/run", { method: "POST" });
  check(page.text.includes("Classification: Looked at 0 email(s)") && page.text.includes("Extraction: Checked 0 email(s)"), "check inbox now runs both agents");

  // ---- Review page
  page = await req("/invoice/1");
  check(page.status === 200 && page.text.includes("Key into Sage") && page.text.includes("V0088") && page.text.includes("P-HEL18"), "invoice page renders the entry sheet with vendor and project mapped");
  check(page.text.includes("UK Purchase Services Reverse Charge Standard Rate"), "invoice page shows the tax detail");
  check(!page.text.includes('<ul class="issues">'), "no blocking checks on the fully mapped sample");
  check((page.text.match(/name="line_desc"/g) || []).length === 6, "line editor shows the five extracted lines plus the template row");
  page = await req("/invoice/1/sage.xml");
  check(page.status === 200 && page.text.includes("<APBILL>") && (page.text.match(/<APBILLITEM>/g) || []).length === 7, "XML download has seven lines");
  page = await req("/invoice/1/sage.json");
  check(JSON.parse(page.text).APBILL.RECORDID === "NMS-2026-0417", "JSON download has the bill");
  page = await req("/invoice/1/file");
  check(page.status === 200 && page.text.startsWith("%PDF") && page.headers.get("content-type").startsWith("application/pdf"), "attachment served from R2 for the preview");
  page = await req("/export.csv");
  check(page.status === 200 && page.text.includes("NMS-2026-0417") && page.text.includes("Sage vendor ID") && page.text.includes("Needs review"), "CSV export has the log row");

  // ---- Edit and save through the form
  page = await req("/invoice/1", {
    method: "POST",
    form: {
      document_type: "invoice", supplier_name: "Northbank Mechanical Services Ltd", supplier_vat: "GB452889107",
      vendor_id: "V0088", invoice_number: "NMS-2026-0417", invoice_date: "2026-09-08", due_date: "2026-10-08",
      po_number: "GG-HEL18-00231", project_reference: "HEL18", project_id: "P-HEL18",
      description: "Edited description", currency: "GBP", vat_treatment: "reverse_charge",
      line_desc: ["Labour", "Materials"], line_qty: ["1", "1"], line_unit: ["lot", "lot"], line_unit_price: ["", ""],
      line_net: ["4914.00", "7735.00"], line_vat_rate: ["20", "20"], line_vat: ["0", "0"], line_category: ["labour", "materials"],
      net_total: "12649.00", vat_total: "0.00", gross_total: "12649.00",
      cis_applicable: "on", cis_rate: "20", cis_amount: "982.80", retention_pct: "3", retention_amount: "379.47",
      notes: "checked against AFP-07",
    },
  });
  check(page.status === 200 && page.text.includes("Saved. Sage payload rebuilt.") && page.text.includes('value="Edited description"'), "form edits saved");
  check(page.text.includes("checked against AFP-07"), "notes saved");
  page = await req("/invoice/1/sage.json");
  check(JSON.parse(page.text).APBILL.APBILLITEMS.length === 4, "payload rebuilt from the edited lines (2 + CIS + retention)");
  page = await req("/invoice/1/remap", { method: "POST" });
  check(page.text.includes("rebuilt with the current settings"), "rebuild from settings works");

  // ---- Status flow
  page = await req("/invoice/1/status", { method: "POST", form: { action: "approve" } });
  check(page.text.includes("Marked as approved.") && page.text.includes("Post to Sage"), "approve works when there are no issues");
  page = await req("/invoice/1/push", { method: "POST" });
  check(page.text.includes("Fill in the Sage Intacct connection"), "push refuses politely without Sage credentials");
  page = await req("/invoice/1/status", { method: "POST", form: { action: "posted" } });
  check(page.text.includes("Marked as posted to sage."), "manual 'keyed in' status works");
  page = await req("/invoice/1/status", { method: "POST", form: { action: "bogus" } });
  check(page.status === 400, "unknown status action is a 400");

  // ---- Uploads
  const good = new FormData();
  good.append("files", new Blob([SAMPLE_PDF], { type: "application/pdf" }), "sample-invoice.pdf");
  page = await req("/upload", { method: "POST", body: good });
  check(page.text.includes("Processed 1 file(s).") && (page.text.match(/Northbank Mechanical Services Ltd/g) || []).length >= 2, "manual upload processed and listed");
  const broken = new FormData();
  broken.append("files", new Blob([Buffer.from("%PDF-1.4 broken")], { type: "application/pdf" }), "broken.pdf");
  page = await req("/upload", { method: "POST", body: broken });
  check(page.text.includes("Failed"), "extraction failure recorded as an error row");
  page = await req("/invoice/3");
  check(page.status === 200 && page.text.includes("Extraction failed: Error: boom"), "error row page renders");
  const bad = new FormData();
  bad.append("files", new Blob([Buffer.from("hello")], { type: "text/plain" }), "notes.txt");
  page = await req("/upload", { method: "POST", body: bad });
  check(page.text.includes("only PDF, PNG, JPG and WEBP are supported"), "unsupported upload is refused");
  page = await req("/?status=error");
  check(page.text.includes("broken.pdf") && !page.text.includes("NMS-2026-0417"), "status filter works");
  page = await req("/invoice/3/status", { method: "POST", form: { action: "delete" } });
  check(page.text.includes("Deleted.") && !page.text.includes("broken.pdf"), "delete removes the row");

  // ---- Cron trigger honours the automatic-checks setting
  page = await req("/agents/extraction", { method: "POST", form: { poll_minutes: "5" } });
  check(page.text.includes("Extraction agent saved."), "poll minutes saved");
  r = await fetch(`${BASE}/__scheduled?cron=*%2F5+*+*+*+*`, { redirect: "manual" });
  check(r.status === 200, "scheduled handler runs");
  await new Promise((res) => setTimeout(res, 1500));
  page = await req("/agents/classification");
  check(page.text.includes("Last run"), "scheduled run recorded");

  // ---- Gmail disconnect, 404, sign out
  page = await req("/gmail/disconnect", { method: "POST" });
  check(page.status === 200 && page.text.includes("Gmail disconnected."), "disconnect route works");
  page = await req("/nothing-here");
  check(page.status === 404 && page.text.includes("Nothing filed under that address"), "404 page is themed");
  page = await req("/signout", { method: "POST" });
  check(page.status === 200 && page.text.includes("Sign in") && !jar.has("session"), "sign out clears the session");
  r = await fetch(`${BASE}/`, { redirect: "manual" });
  check(r.status === 303, "signed-out visit is sent back to sign-in");
}

const stop = () =>
  new Promise((resolve) => {
    if (wrangler.exitCode !== null) return resolve();
    wrangler.once("exit", resolve);
    wrangler.kill("SIGTERM");
    setTimeout(() => wrangler.kill("SIGKILL"), 5000).unref();
  });

try {
  await run();
} catch (err) {
  failed += 1;
  console.error("smoke test crashed:", err && err.stack ? err.stack : err);
} finally {
  await stop();
  rmSync(persist, { recursive: true, force: true });
}
if (failed) {
  console.log(`\n${failed} check(s) failed.`);
  if (wranglerOutput.trim()) console.log("\nwrangler output:\n" + wranglerOutput.slice(-4000));
  process.exit(1);
}
console.log("\nAll checks passed.");
