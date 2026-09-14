// Fixture mode for the smoke test: a fake mailbox and fake Claude answers.
// Only active when the TEST_FIXTURES variable is "1" (wrangler dev --var
// TEST_FIXTURES:1). Never set it on the deployed Worker.

import { getState, setState } from "./db.js";
import { normalise } from "./normalise.js";

export const enabled = (env) => Boolean(env) && String(env.TEST_FIXTURES) === "1";

export const FAKE_RECORD = {
  document_type: "invoice",
  supplier: {
    name: "Northbank Mechanical Services Ltd",
    address: "Unit 7, Riverside Industrial Estate, Dartford, Kent DA1 5QP",
    postcode: "DA1 5QP", country: "United Kingdom",
    vat_number: "GB 452 8891 07", company_number: "09876543",
    email: "accounts@northbankmech.co.uk", phone: "01322 555 0192",
  },
  bill_to: { name: "Glent Group Ltd", address: "Accounts Payable, London" },
  invoice_number: "NMS-2026-0417",
  invoice_date: "2026-09-08",
  due_date: "2026-10-08",
  payment_terms_days: 30,
  payment_terms_text: "30 days from invoice date",
  po_number: "GG-HEL18-00231",
  order_reference: "HEL18 Mechanical Package",
  delivery_note_number: null,
  project_reference: "HEL18 Data Centre, Hall 2",
  site_address: "HEL18 Data Centre, Hall 2, Helsinki Region, Finland",
  description: "Chilled water pipework install Hall 2 - AFP-07 period ending 31/08/2026",
  currency: "GBP",
  line_items: [
    { description: "Labour - chilled water pipework install, Hall 2 corr. C", quantity: 84, unit: "hrs", unit_price: 48.5, net_amount: 4074.0, vat_rate: 20, vat_amount: 0, category: "labour" },
    { description: "Labour - supervision, site manager", quantity: 2, unit: "days", unit_price: 420.0, net_amount: 840.0, vat_rate: 20, vat_amount: 0, category: "labour" },
    { description: "Materials - 150mm carbon steel pipe & fittings (per DN-2251)", quantity: 1, unit: "lot", unit_price: 6180.0, net_amount: 6180.0, vat_rate: 20, vat_amount: 0, category: "materials" },
    { description: "Materials - pipe supports and brackets", quantity: 1, unit: "lot", unit_price: 935.0, net_amount: 935.0, vat_rate: 20, vat_amount: 0, category: "materials" },
    { description: "Hire - 3.5t telehandler, week 34-35", quantity: 2, unit: "wk", unit_price: 310.0, net_amount: 620.0, vat_rate: 20, vat_amount: 0, category: "plant" },
  ],
  net_total: 12649.0, vat_total: 0.0, gross_total: 12649.0,
  discount_amount: null, amount_due: 11286.73,
  vat_breakdown: [{ rate: 20, net: 12649.0, vat: 0.0 }],
  vat_treatment: "reverse_charge",
  cis: { applicable: true, labour_amount: 4914.0, materials_amount: 7735.0, deduction_rate: 20, deduction_amount: 982.8 },
  retention: { applicable: true, percentage: 3, amount: 379.47 },
  bank_details: { account_name: "Northbank Mechanical Services Ltd", bank_name: "Barclays Bank plc", sort_code: "20-45-77", account_number: "30918824", iban: "GB29 BARC 2045 7730 9188 24", bic: "BARCGB22" },
  totals_reconcile: true,
  confidence: 0.94,
  flags: ["Supplier asks for gross payment status to be verified before CIS deduction."],
};

/** A one-page PDF that any viewer opens; the fixture extractor never reads it. */
export function tinyPdf(title) {
  const stream = `BT /F1 18 Tf 72 770 Td (${title.replace(/[()\\]/g, "")}) Tj ET`;
  const body =
    "%PDF-1.4\n" +
    "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n" +
    "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n" +
    "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n" +
    `4 0 obj << /Length ${stream.length} >> stream\n${stream}\nendstream endobj\n` +
    "5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n" +
    "trailer << /Root 1 0 R >>\n%%EOF\n";
  return new TextEncoder().encode(body);
}

export function extractInvoice({ emailMeta }) {
  const name = String((emailMeta && emailMeta.attachment_name) || "");
  if (/broken/i.test(name)) throw new Error("boom");
  return { record: normalise(FAKE_RECORD), usage: { input_tokens: 3210, output_tokens: 640, model: "fixture" } };
}

export function classifyEmail({ emailMeta, attachments }) {
  const haystack = [emailMeta.subject, ...(attachments || []).map((a) => a.filename)].join(" ");
  const usage = { input_tokens: 900, output_tokens: 60, model: "fixture" };
  if (/invoice|application for payment|credit note/i.test(haystack) && (attachments || []).length) {
    return { verdict: "invoice", document_kind: "invoice", confidence: 0.97, reason: "Subject and attachment name say invoice.", usage };
  }
  if (/statement/i.test(haystack)) {
    return { verdict: "not_invoice", document_kind: "statement", confidence: 0.9, reason: "A statement of account, not an invoice.", usage };
  }
  return { verdict: "not_invoice", document_kind: "other", confidence: 0.8, reason: "No invoice document attached.", usage };
}

const MESSAGES = [
  {
    id: "msg-invoice-1", thread_id: "t1", from: "Northbank Accounts <accounts@northbankmech.co.uk>", to: "invoices@glent.example",
    subject: "Invoice NMS-2026-0417", date: "Sat, 13 Sep 2026 09:00:00 +0000", received_at: "2026-09-13T09:00:00+00:00",
    snippet: "Please find attached.", body_text: "Please find attached our invoice for AFP-07.",
    attachments: [{ filename: "sample-invoice.pdf", mime_type: "application/pdf" }],
  },
  {
    id: "msg-statement", thread_id: "t2", from: "Northbank Accounts <accounts@northbankmech.co.uk>", to: "invoices@glent.example",
    subject: "Statement of account - August", date: "Fri, 12 Sep 2026 15:00:00 +0000", received_at: "2026-09-12T15:00:00+00:00",
    snippet: "Your statement.", body_text: "Your monthly statement is attached.",
    attachments: [{ filename: "statement-august.pdf", mime_type: "application/pdf" }],
  },
  {
    id: "msg-lunch", thread_id: "t3", from: "Canteen <canteen@example.com>", to: "invoices@glent.example",
    subject: "Lunch menu this week", date: "Thu, 11 Sep 2026 08:00:00 +0000", received_at: "2026-09-11T08:00:00+00:00",
    snippet: "Menu.", body_text: "This week's menu is attached as an image.",
    attachments: [],
  },
];

const search = (name) => String(name || "").trim().replace(/[\s/]+/g, "-").toLowerCase();

async function loadBox(db) {
  const raw = await getState(db, "fixture_mailbox");
  if (raw) {
    try {
      return JSON.parse(raw);
    } catch {
      /* fall through */
    }
  }
  return { labels: {}, unread: Object.fromEntries(MESSAGES.map((m) => [m.id, true])), names: ["INBOX", "UNREAD"] };
}

const saveBox = (db, box) => setState(db, "fixture_mailbox", JSON.stringify(box));

export function gmailClient(env) {
  const db = env.DB;
  return {
    async profile() {
      return { emailAddress: "invoices@glent.example" };
    },
    async listMessages(query, maxMessages = 10) {
      const box = await loadBox(db);
      const terms = String(query || "").split(/\s+/).filter(Boolean);
      const ids = [];
      for (const m of MESSAGES) {
        const labels = (box.labels[m.id] || []).map(search);
        let ok = true;
        for (const term of terms) {
          const t = term.toLowerCase();
          if (t.startsWith("-label:")) ok = ok && !labels.includes(search(t.slice(7)));
          else if (t.startsWith("label:")) ok = ok && labels.includes(search(t.slice(6)));
          else if (t === "is:unread") ok = ok && Boolean(box.unread[m.id]);
          else if (t === "has:attachment") ok = ok && m.attachments.length > 0;
        }
        if (ok) ids.push(m.id);
      }
      return ids.slice(0, maxMessages);
    },
    async fetchMessage(msgId) {
      const m = MESSAGES.find((x) => x.id === msgId);
      if (!m) throw new Error(`fixture message ${msgId} not found`);
      const attachments = m.attachments.map((a) => {
        const data = tinyPdf(a.filename);
        return { ...a, data, size: data.length };
      });
      return { ...m, attachments, label_ids: [] };
    },
    async labelId(name) {
      const box = await loadBox(db);
      if (!box.names.includes(name)) {
        box.names.push(name);
        await saveBox(db, box);
      }
      return "Label_" + search(name);
    },
    async modify(msgId, { addLabels = [], markRead = false } = {}) {
      const box = await loadBox(db);
      const current = new Set(box.labels[msgId] || []);
      for (const name of addLabels) current.add(name);
      box.labels[msgId] = [...current];
      if (markRead) box.unread[msgId] = false;
      await saveBox(db, box);
    },
  };
}
