// Pure-logic checks: normalisation, Sage mapping, XML/JSON payloads, retrieval, CSV.
//   node --test tests/

import test from "node:test";
import assert from "node:assert/strict";

import { normalise } from "../src/normalise.js";
import { build, billToXml, publicBill, iso, LOG_COLUMNS } from "../src/sage_mapper.js";
import { retrieve, describe, RAG_MAX_CHARS } from "../src/rag.js";
import { toCsv } from "../src/csv.js";
import { parseMap, formatMap, withDefaults, DEFAULTS, DEFAULT_MODEL, CLAUDE_MODELS } from "../src/settings.js";
import { FAKE_RECORD } from "../src/fixtures.js";
import { parseTextJson, emailContext } from "../src/claude.js";
import { searchLabel, senderAllowed, stripHtml, b64urlDecode } from "../src/gmail.js";

export const SETTINGS = {
  ...structuredClone(DEFAULTS),
  anthropic_api_key: "sk-ant-test",
  company_name: "Glent Group",
  sage_location_id: "LON",
  sage_department_id: "MEP",
  sage_default_gl: "5000",
  sage_cis_gl: "2210",
  sage_retention_gl: "2220",
  gl_map: { materials: "5100", labour: "5200", plant: "5300", subcontract: "5400", services: "5500", expenses: "7000", other: "5000" },
  vendor_map: { "Northbank Mechanical Services Ltd": "V0088" },
  project_map: { HEL18: "P-HEL18" },
  vat_detail_map: { "20": "UK Purchase Goods Standard Rate", "5": "UK Purchase Goods Reduced Rate", "0": "UK Purchase Goods Zero Rate", reverse_charge: "UK Purchase Services Reverse Charge Standard Rate" },
};

const META = { subject: "Invoice NMS-2026-0417", from: "accounts@northbankmech.co.uk", received_at: "2026-09-13T09:00:00+00:00" };

test("mapper: fully mapped subcontractor invoice", () => {
  const rec = normalise(FAKE_RECORD);
  const mapped = build(rec, SETTINGS, { emailMeta: META });
  const bill = mapped.bill;
  assert.equal(bill.VENDORID, "V0088", "vendor matched through the vendor map");
  assert.equal(bill.ITEMS[0].PROJECTID, "P-HEL18", "project matched from the PO / references");
  assert.equal(bill.ITEMS[0].ACCOUNTNO, "5200", "labour line coded to the labour GL");
  assert.equal(bill.ITEMS[4].ACCOUNTNO, "5300", "plant hire coded to the plant GL");
  assert.ok(bill.ITEMS[0].TAXENTRIES[0].DETAILID.endsWith("Reverse Charge Standard Rate"), "reverse charge tax detail applied");
  assert.equal(bill.TERMNAME, "Net 30", "30-day terms mapped to Intacct term");
  const cis = bill.ITEMS.filter((i) => i._category === "cis");
  const ret = bill.ITEMS.filter((i) => i._category === "retention");
  assert.equal(cis[0].TRX_AMOUNT, "-982.80", "CIS deduction added as a negative line");
  assert.equal(ret[0].TRX_AMOUNT, "-379.47", "retention added as a negative line");
  assert.deepEqual(mapped.issues, [], "no blocking issues when fully mapped");
  const netSum = bill.ITEMS.reduce((s, i) => s + parseFloat(i.TRX_AMOUNT), 0);
  assert.ok(Math.abs(netSum - 11286.73) < 0.01, "line amounts sum to the amount payable");
  assert.equal(mapped.log_row["Amount payable"], "11286.73", "log row carries the amount payable");
  assert.equal(mapped.entry_sheet.header[0][1], "V0088");
  assert.equal(mapped.entry_sheet.totals.at(-1)[0], "Amount payable");
});

test("mapper: XML gateway payload", () => {
  const rec = normalise(FAKE_RECORD);
  const xml = billToXml(build(rec, SETTINGS, { emailMeta: META }).bill);
  assert.ok(xml.startsWith("<create>\n    <APBILL>"), "XML payload is <create><APBILL>");
  assert.equal((xml.match(/<APBILLITEM>/g) || []).length, 7, "seven bill lines in the XML (5 + CIS + retention)");
  assert.equal((xml.match(/<APBILLITEM>/g) || []).length, (xml.match(/<\/APBILLITEM>/g) || []).length, "tags balanced");
  assert.ok(xml.includes("<ENTRYDESCRIPTION>Materials - 150mm carbon steel pipe &amp; fittings (per DN-2251)</ENTRYDESCRIPTION>"), "ampersand escaped");
  const json = publicBill(build(rec, SETTINGS, { emailMeta: META }).bill);
  assert.equal(json.APBILL.RECORDID, "NMS-2026-0417");
  assert.equal(json.APBILL.APBILLITEMS.length, 7);
  assert.ok(!("_object" in json.APBILL), "helper keys stripped from the public payload");
});

test("mapper: unmapped settings surface as issues", () => {
  const unmapped = build(normalise(FAKE_RECORD), structuredClone(DEFAULTS), {});
  assert.ok(unmapped.issues.some((i) => i.includes("vendor ID")), "missing vendor flagged when the map is empty");
  assert.ok(unmapped.issues.some((i) => i.includes("GL account")), "missing GL flagged when the map is empty");
  assert.ok(unmapped.issues.some((i) => i.includes("CIS control")), "CIS without a control account is an issue");
});

test("mapper: credit note maps to an AP adjustment", () => {
  const credit = normalise({ ...FAKE_RECORD, document_type: "credit_note", cis: { applicable: false }, retention: { applicable: false } });
  const mapped = build(credit, SETTINGS, {});
  assert.equal(mapped.bill._object, "APADJUSTMENT");
  assert.ok(mapped.bill.ITEMS[0].TRX_AMOUNT.startsWith("-"), "credit note amounts are negative");
  assert.ok(billToXml(mapped.bill).includes("<APADJUSTMENTITEMS>"), "credit note XML uses APADJUSTMENTITEMS");
});

test("mapper: due date derived from terms, dates parsed", () => {
  const rec = normalise({ ...FAKE_RECORD, due_date: null, invoice_date: "08/09/2026" });
  const mapped = build(rec, SETTINGS, {});
  assert.equal(mapped.bill.WHENCREATED, "2026-09-08");
  assert.equal(mapped.bill.WHENDUE, "2026-10-08");
  assert.ok(mapped.notes.some((n) => n.includes("derived from 30-day terms")));
  assert.equal(iso("8 Sep 2026"), "2026-09-08");
  assert.equal(iso("8 September 2026"), "2026-09-08");
  assert.equal(iso("2026-9-8"), "2026-09-08");
});

test("normalise: coerces numbers, fills totals and checks arithmetic", () => {
  const rec = normalise({
    supplier: { name: "X" },
    line_items: [{ description: "a", net_amount: "1,000.00", vat_rate: "20", category: "widgets" }],
    confidence: "1.7",
  });
  assert.equal(rec.line_items[0].net_amount, 1000);
  assert.equal(rec.line_items[0].vat_amount, 200);
  assert.equal(rec.line_items[0].category, "other");
  assert.equal(rec.net_total, 1000);
  assert.equal(rec.vat_total, 200);
  assert.equal(rec.gross_total, 1200);
  assert.equal(rec.totals_reconcile, true);
  assert.equal(rec.confidence, 1);
  assert.deepEqual(rec.vat_breakdown, [{ rate: 20, net: 1000, vat: 200 }]);
  assert.equal(normalise({ ...FAKE_RECORD, gross_total: 999 }).totals_reconcile, false);
});

test("rag: short text is sent whole, long text is retrieved by paragraph", () => {
  assert.equal(retrieve("Northbank are a subcontractor.", "anything"), "Northbank are a subcontractor.");
  const filler = Array.from({ length: 400 }, (_, i) => `Paragraph ${i} about site logistics, welfare cabins and parking permits.`).join("\n\n");
  const long = `${filler}\n\nNorthbank Mechanical Services are a CIS subcontractor on HEL18; their applications for payment count as invoices.\n\nSpeedy Hire send plant hire invoices weekly.`;
  assert.ok(long.length > RAG_MAX_CHARS);
  const picked = retrieve(long, "Invoice NMS-2026-0417 from Northbank Mechanical Services, HEL18");
  assert.ok(picked.includes("Northbank Mechanical Services are a CIS subcontractor"), "matching paragraph retrieved");
  assert.ok(picked.length <= RAG_MAX_CHARS, "budget respected");
  assert.ok(!picked.includes("Paragraph 399"), "filler paragraphs left out");
  const d = describe(long);
  assert.equal(d.whole, false);
  assert.equal(d.paragraphs, 402);
});

test("csv: quotes commas, quotes and newlines", () => {
  const csv = toCsv(["A", "B"], [{ A: 'say "hi", ok', B: "line1\nline2" }]);
  assert.equal(csv, 'A,B\r\n"say ""hi"", ok","line1\nline2"\r\n');
  assert.equal(LOG_COLUMNS.length, 26);
});

test("settings: map parsing and defaults merge", () => {
  assert.deepEqual(parseMap("Northbank Mechanical Services Ltd = V0088\n# comment\nSpeedy Hire = V0117\n"), {
    "Northbank Mechanical Services Ltd": "V0088",
    "Speedy Hire": "V0117",
  });
  assert.equal(formatMap({ "30": "Net 30" }), "30 = Net 30");
  const merged = withDefaults({ gl_map: { labour: "5200" }, unknown_key: "x", classify_model: "claude-opus-5" });
  assert.equal(merged.gl_map.labour, "5200");
  assert.equal(merged.gl_map.materials, "");
  assert.equal(merged.classify_model, "claude-opus-5");
  assert.ok(!("unknown_key" in merged));
  assert.equal(DEFAULT_MODEL, "claude-opus-5");
  assert.equal(DEFAULTS.classify_model, "claude-opus-5");
  assert.equal(DEFAULTS.extract_model, "claude-opus-5");
  assert.equal(CLAUDE_MODELS[0][0], "claude-opus-5", "Opus 5 is first in the model list");
});

test("claude helpers: JSON fallback and email context", () => {
  assert.deepEqual(parseTextJson('```json\n{"a": 1}\n```'), { a: 1 });
  assert.deepEqual(parseTextJson('Here you go: {"a": 2} thanks'), { a: 2 });
  assert.equal(parseTextJson("nothing"), null);
  assert.ok(emailContext({ from: "x@y", subject: "S", body_text: "hello" }).includes("Email body:\nhello"));
});

test("gmail helpers", () => {
  assert.equal(searchLabel("Invoices/Processed"), "Invoices-Processed");
  assert.equal(searchLabel("Invoice Incoming"), "Invoice-Incoming");
  assert.equal(senderAllowed("Bob <bob@supplier.co.uk>", "@supplier.co.uk, accounts@other.com"), true);
  assert.equal(senderAllowed("eve@elsewhere.com", "@supplier.co.uk"), false);
  assert.equal(senderAllowed("anyone@x", ""), true);
  assert.equal(stripHtml("<p>Hello <b>there</b></p><style>x{}</style>&amp; more").replace(/\s+/g, " "), "Hello there & more");
  assert.equal(new TextDecoder().decode(b64urlDecode("aGVsbG8_")), "hello?");
});
