"""End-to-end smoke test without touching Gmail or the Claude API.

Run from the project root:  python tests/smoke_test.py
It uses a throwaway data/ directory (INVOICE_AGENT_DATA) so it never touches real settings.
Gmail is replaced by a fake mailbox and both Claude calls are mocked.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="invoice-agent-test-"))
os.environ["INVOICE_AGENT_DATA"] = str(TMP)

from agent import config, extractor, gmail_client, invoice_types, pipeline, prompts, rag, sage_mapper, store  # noqa: E402

SAMPLE_PDF = ROOT / "samples" / "sample-invoice.pdf"

FAKE_RECORD = {
    "document_type": "invoice",
    "supplier": {
        "name": "Northbank Mechanical Services Ltd",
        "address": "Unit 7, Riverside Industrial Estate, Dartford, Kent DA1 5QP",
        "postcode": "DA1 5QP", "country": "United Kingdom",
        "vat_number": "GB 452 8891 07", "company_number": "09876543",
        "email": "accounts@northbankmech.co.uk", "phone": "01322 555 0192",
    },
    "bill_to": {"name": "Glent Group Ltd", "address": "Accounts Payable, London"},
    "invoice_number": "NMS-2026-0417",
    "invoice_date": "2026-09-08",
    "due_date": "2026-10-08",
    "payment_terms_days": 30,
    "payment_terms_text": "30 days from invoice date",
    "po_number": "GG-HEL18-00231",
    "order_reference": "HEL18 Mechanical Package",
    "delivery_note_number": None,
    "project_reference": "HEL18 Data Centre, Hall 2",
    "site_address": "HEL18 Data Centre, Hall 2, Helsinki Region, Finland",
    "description": "Chilled water pipework install Hall 2 - AFP-07 period ending 31/08/2026",
    "currency": "GBP",
    "line_items": [
        {"description": "Labour - chilled water pipework install, Hall 2 corr. C", "quantity": 84, "unit": "hrs", "unit_price": 48.5, "net_amount": 4074.0, "vat_rate": 20, "vat_amount": 0, "category": "labour"},
        {"description": "Labour - supervision, site manager", "quantity": 2, "unit": "days", "unit_price": 420.0, "net_amount": 840.0, "vat_rate": 20, "vat_amount": 0, "category": "labour"},
        {"description": "Materials - 150mm carbon steel pipe & fittings (per DN-2251)", "quantity": 1, "unit": "lot", "unit_price": 6180.0, "net_amount": 6180.0, "vat_rate": 20, "vat_amount": 0, "category": "materials"},
        {"description": "Materials - pipe supports and brackets", "quantity": 1, "unit": "lot", "unit_price": 935.0, "net_amount": 935.0, "vat_rate": 20, "vat_amount": 0, "category": "materials"},
        {"description": "Hire - 3.5t telehandler, week 34-35", "quantity": 2, "unit": "wk", "unit_price": 310.0, "net_amount": 620.0, "vat_rate": 20, "vat_amount": 0, "category": "plant"},
    ],
    "net_total": 12649.0, "vat_total": 0.0, "gross_total": 12649.0,
    "discount_amount": None, "amount_due": 11286.73,
    "vat_breakdown": [{"rate": 20, "net": 12649.0, "vat": 0.0}],
    "vat_treatment": "reverse_charge",
    "cis": {"applicable": True, "labour_amount": 4914.0, "materials_amount": 7735.0, "deduction_rate": 20, "deduction_amount": 982.8},
    "retention": {"applicable": True, "percentage": 3, "amount": 379.47},
    "bank_details": {"account_name": "Northbank Mechanical Services Ltd", "bank_name": "Barclays Bank plc", "sort_code": "20-45-77", "account_number": "30918824", "iban": "GB29 BARC 2045 7730 9188 24", "bic": "BARCGB22"},
    "totals_reconcile": True,
    "confidence": 0.94,
    "flags": ["Supplier asks for gross payment status to be verified before CIS deduction."],
}

# A plain materials invoice with standard VAT: no CIS, no reverse charge, so the type comes from the lines.
MATERIALS_RECORD = {
    "document_type": "invoice",
    "supplier": {"name": "Riverside Builders Merchants Ltd", "vat_number": "GB 111 2222 33"},
    "invoice_number": "RBM-55012", "invoice_date": "2026-09-10", "due_date": None, "payment_terms_days": None,
    "po_number": None, "project_reference": None, "description": "Cable containment delivered to site", "currency": "GBP",
    "line_items": [
        {"description": "150mm cable tray", "quantity": 40, "unit": "m", "unit_price": 12.5, "net_amount": 500.0, "vat_rate": 20, "vat_amount": 100.0, "category": "materials"},
        {"description": "Delivery", "quantity": 1, "unit": "each", "unit_price": 45.0, "net_amount": 45.0, "vat_rate": 20, "vat_amount": 9.0, "category": "expenses"},
    ],
    "net_total": 545.0, "vat_total": 109.0, "gross_total": 654.0, "amount_due": 654.0,
    "vat_treatment": "standard", "cis": {"applicable": False}, "retention": {"applicable": False},
    "totals_reconcile": True, "confidence": 0.9, "flags": [],
}

SETTINGS = {
    **config.with_defaults({}),
    "anthropic_api_key": "sk-ant-test",
    "company_name": "Glent Group",
    "sage_location_id": "LON",
    "sage_department_id": "MEP",
    "sage_default_gl": "5000",
    "sage_cis_gl": "2210",
    "sage_retention_gl": "2220",
    "gl_map": {"materials": "5100", "labour": "5200", "plant": "5300", "subcontract": "5400", "services": "5500", "expenses": "7000", "other": "5000"},
    "vendor_map": {"Northbank Mechanical Services Ltd": "V0088"},
    "project_map": {"HEL18": "P-HEL18"},
    "vat_detail_map": {"20": "UK Purchase Goods Standard Rate", "5": "UK Purchase Goods Reduced Rate", "0": "UK Purchase Goods Zero Rate", "reverse_charge": "UK Purchase Services Reverse Charge Standard Rate"},
}
NO_TYPES = {**SETTINGS, "invoice_types": []}


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("ok  -", msg)


def test_mapper():
    rec = extractor.normalise(FAKE_RECORD)
    meta = {"subject": "Invoice NMS-2026-0417", "from": "accounts@northbankmech.co.uk", "received_at": "2026-09-13T09:00:00+00:00"}

    # Company-wide coding only (no invoice types)
    mapped = sage_mapper.build(rec, NO_TYPES, email_meta=meta)
    bill = mapped["bill"]
    check(bill["VENDORID"] == "V0088", "vendor matched through the vendor map")
    check(bill["ITEMS"][0]["PROJECTID"] == "P-HEL18", "project matched from the PO / references")
    check(bill["ITEMS"][0]["ACCOUNTNO"] == "5200", "labour line coded to the labour GL")
    check(bill["ITEMS"][4]["ACCOUNTNO"] == "5300", "plant hire coded to the plant GL")
    check(bill["ITEMS"][0]["TAXENTRIES"][0]["DETAILID"].endswith("Reverse Charge Standard Rate"), "reverse charge tax detail applied")
    check(bill["TERMNAME"] == "Net 30", "30-day terms mapped to Intacct term")
    cis_lines = [i for i in bill["ITEMS"] if i["_category"] == "cis"]
    ret_lines = [i for i in bill["ITEMS"] if i["_category"] == "retention"]
    check(cis_lines and cis_lines[0]["TRX_AMOUNT"] == "-982.80", "CIS deduction added as a negative line")
    check(ret_lines and ret_lines[0]["TRX_AMOUNT"] == "-379.47", "retention added as a negative line")
    check(mapped["issues"] == [], f"no blocking issues when fully mapped ({mapped['issues']})")
    check(mapped["invoice_type"] is None and mapped["log_row"]["Invoice type"] == "", "no invoice type when none are defined")
    xml_text = sage_mapper.bill_to_xml(bill)
    root = ET.fromstring(xml_text)
    check(root.tag == "create" and root.find("APBILL") is not None, "XML gateway payload parses as <create><APBILL>")
    check(len(root.findall(".//APBILLITEM")) == 7, "seven bill lines in the XML (5 + CIS + retention)")
    net_sum = sum(float(i["TRX_AMOUNT"]) for i in bill["ITEMS"])
    check(abs(net_sum - 11286.73) < 0.01, f"line amounts sum to the amount payable ({net_sum:.2f})")
    check(mapped["log_row"]["Amount payable"] == "11286.73", "log row carries the amount payable")

    # With the default invoice types the CIS / reverse charge signals make it a subcontractor invoice
    typed = sage_mapper.build(rec, SETTINGS, email_meta=meta)
    check(typed["invoice_type"]["id"] == "subcontractor" and "CIS" in typed["invoice_type"]["how"], f"CIS signal picks the subcontractor type ({typed['invoice_type']})")
    check(all(i["ACCOUNTNO"] == "6002" for i in typed["bill"]["ITEMS"] if i["_category"] not in ("cis", "retention")), "subcontractor type codes every line to its GL override")
    check(typed["bill"]["ITEMS"][0]["TAXENTRIES"][0]["DETAILID"] == "UK Purchase Services Reverse Charge Standard Rate", "type's VAT detail override used")
    check(typed["issues"] == [], f"typed mapping has no issues ({typed['issues']})")
    check(typed["entry_sheet"]["header"][1][0] == "Invoice type" and typed["entry_sheet"]["header"][1][1] == "Subcontractor", "entry sheet shows the invoice type")
    check(typed["log_row"]["Invoice type"] == "Subcontractor" and "Invoice type" in sage_mapper.LOG_COLUMNS, "log row and CSV columns carry the invoice type")

    chosen = sage_mapper.build(rec, SETTINGS, email_meta=meta, overrides={"invoice_type": "plant"})
    check(chosen["invoice_type"]["id"] == "plant" and chosen["invoice_type"]["how"] == "chosen on the invoice", "type chosen by hand wins")
    check(chosen["bill"]["ITEMS"][0]["ACCOUNTNO"] == "7700" and chosen["bill"]["ITEMS"][2]["ACCOUNTNO"] == "5100", "chosen type overrides only the GLs it sets, the rest fall back to Settings")
    check(any("not usually reverse charged" in n for n in sage_mapper.build(rec, SETTINGS, overrides={"invoice_type": "materials"})["notes"]), "unexpected VAT treatment for the type is noted")

    # Generic contractor defaults map the bundled sample end to end
    generic = sage_mapper.build(rec, config.with_defaults({}), email_meta=meta)
    check(generic["issues"] == [] and generic["bill"]["VENDORID"] == "V0088", f"generic defaults map the sample supplier with no issues ({generic['issues']})")
    check(generic["bill"]["ITEMS"][-2]["ACCOUNTNO"] == "2214" and generic["bill"]["ITEMS"][-1]["ACCOUNTNO"] == "2215", "generic CIS and retention control accounts used")

    # Materials invoice: type from the lines, PO check from the type
    mat = extractor.normalise(MATERIALS_RECORD)
    mat_map = sage_mapper.build(mat, config.with_defaults({}), email_meta={})
    check(mat_map["invoice_type"]["id"] == "materials" and "mostly materials" in mat_map["invoice_type"]["how"], "materials type picked from the dominant line category")
    check(mat_map["bill"]["ITEMS"][0]["ACCOUNTNO"] == "5000" and mat_map["bill"]["ITEMS"][1]["ACCOUNTNO"] == "5100", "materials type GL overrides applied")
    check(any("purchase order" in i for i in mat_map["issues"]), "missing PO is an issue for a type that requires one")
    check(any("No Sage project" in i for i in mat_map["issues"]), "missing project is an issue for a type that requires one")
    check(any("vendor ID" in i for i in mat_map["issues"]), "unknown supplier flagged for a vendor ID")

    bare = {**config.with_defaults({}), "vendor_map": {}, "gl_map": {}, "sage_default_gl": "", "invoice_types": []}
    unmapped = sage_mapper.build(rec, bare, email_meta={})
    check(any("vendor ID" in i for i in unmapped["issues"]), "missing vendor flagged when the map is empty")
    check(any("GL account" in i for i in unmapped["issues"]), "missing GL flagged when the map is empty")

    credit = extractor.normalise({**FAKE_RECORD, "document_type": "credit_note", "cis": {"applicable": False}, "retention": {"applicable": False}})
    credit_map = sage_mapper.build(credit, SETTINGS, email_meta={})
    check(credit_map["bill"]["_object"] == "APADJUSTMENT", "credit note maps to an AP adjustment")
    check(credit_map["bill"]["ITEMS"][0]["TRX_AMOUNT"].startswith("-"), "credit note amounts are negative")
    check("<APADJUSTMENTITEMS>" in sage_mapper.bill_to_xml(credit_map["bill"]), "credit note XML uses APADJUSTMENTITEMS")


def test_types_rag_and_config():
    settings = config.with_defaults({})
    ids = [t["id"] for t in settings["invoice_types"]]
    check(ids == ["subcontractor", "materials", "plant", "professional", "overheads"], f"five generic invoice types by default ({ids})")

    # Resolution order: chosen > supplier list > signals > dominant category > fallback
    rec = extractor.normalise(MATERIALS_RECORD)
    listed = {**settings, "invoice_types": [dict(t, suppliers="Riverside Builders Merchants" if t["id"] == "professional" else "") for t in settings["invoice_types"]]}
    kind, how = invoice_types.resolve_type(rec, listed)
    check(kind["id"] == "professional" and "listed under" in how, "a listed supplier beats the line categories")
    kind, how = invoice_types.resolve_type({**rec, "document_type": "application_for_payment"}, settings)
    check(kind["id"] == "subcontractor" and "application for payment" in how, "an application for payment is a subcontractor signal")
    kind, how = invoice_types.resolve_type({**rec, "line_items": [{"category": "services", "net_amount": 100}]}, settings)
    check(kind["id"] == "professional" and "mostly services" in how, "services lines pick professional services")
    kind, how = invoice_types.resolve_type({**rec, "line_items": []}, settings)
    check(kind["id"] == "overheads" and how == "nothing else matched", "overheads is the fallback")
    kind, how = invoice_types.resolve_type(rec, settings, {"invoice_type": "plant"})
    check(kind["id"] == "plant" and how == "chosen on the invoice", "override picks the type by id")
    check(invoice_types.resolve_type(rec, {**settings, "invoice_types": []}) == (None, ""), "no types means no type")

    # Type defaults fill only blanks
    sub = invoice_types.type_by_id(settings, "subcontractor")
    silent = extractor.normalise({**FAKE_RECORD, "cis": {"applicable": None}, "retention": {"applicable": None}, "payment_terms_days": None, "due_date": None, "flags": []})
    filled, notes = invoice_types.apply_type_defaults(silent, dict(sub, retention_percent=5))
    check(filled["cis"]["deduction_amount"] == 982.8 and filled["cis"]["deduction_rate"] == 20, f"CIS computed at the type's rate on the labour lines ({filled['cis']})")
    check(filled["retention"]["amount"] == round(12649.0 * 0.05, 2), "retention computed from the type's percentage")
    check(filled["payment_terms_days"] == 30 and len(notes) == 3 and all(n in filled["flags"] for n in notes), "terms defaulted and every default noted as a flag")
    untouched, notes = invoice_types.apply_type_defaults(extractor.normalise(FAKE_RECORD), sub)
    check(untouched["cis"]["deduction_amount"] == 982.8 and untouched["retention"]["amount"] == 379.47 and notes == [], "values printed on the invoice are left alone")
    check(invoice_types.apply_type_defaults(silent, None) == (silent, []), "no type, no defaults")

    coding = invoice_types.effective_coding(SETTINGS, invoice_types.type_by_id(settings, "plant"))
    check(coding["gl_map"]["plant"] == "7700" and coding["gl_map"]["materials"] == "5100" and coding["location_id"] == "LON", "effective coding layers the type over Settings")
    coding = invoice_types.effective_coding(SETTINGS, dict(invoice_types.type_by_id(settings, "plant"), location_id="SITE", sage_action="Submit"))
    check(coding["location_id"] == "SITE" and coding["sage_action"] == "Submit", "type location and action override Settings")

    t = invoice_types.normalise_type({"name": "Site cabins", "cis_rate": "30", "terms_days": "", "signals": ["cis", "bogus"], "categories": ["plant", "nope"], "expected_vat": "weird"})
    check(t["id"] == "site_cabins" and t["cis_rate"] == 30 and t["terms_days"] is None and t["signals"] == ["cis"] and t["categories"] == ["plant"] and t["expected_vat"] == "", "normalise_type fills and cleans a partial type")
    check(len(set(invoice_types.new_type_id([]) for _ in range(5))) == 5, "new type ids are unique")

    # Settings compatibility
    old = config.with_defaults({"claude_model": "claude-sonnet-5", "vendor_map": {"Acme": "V1"}, "invoice_types": [{"name": "Only one"}], "unknown": 1})
    check(old["extract_model"] == "claude-sonnet-5" and old["classify_model"] == "claude-opus-5", "old single model becomes the extraction model; classification keeps the default")
    check(old["vendor_map"] == {"Northbank Mechanical Services Ltd": "V0088", "Acme": "V1"}, "stored maps merge over the defaults")
    check([t["id"] for t in old["invoice_types"]] == ["only_one"] and "unknown" not in old, "stored types are normalised and unknown keys dropped")
    check(config.DEFAULT_MODEL == "claude-opus-5" and config.CLAUDE_MODELS[0][0] == "claude-opus-5", "Opus 5 is the default model")
    check(config.model_label("claude-opus-5").startswith("Claude Opus 5") and config.model_label("x") == "x", "model labels resolve")

    # Reference text retrieval
    short = "Northbank send applications for payment monthly.\n\nSpeedy Hire invoices are plant."
    check(rag.retrieve(short, "anything") == short, "short reference text is sent whole")
    blocks = [f"Paragraph {i} about supplier number {i} and its paperwork. " * 8 for i in range(60)]
    blocks[7] = "Northbank Mechanical send an application for payment each month; treat it as an invoice. " * 5
    blocks[41] = "Riverside Builders Merchants deliver cable tray and containment to the HEL18 site. " * 5
    long_text = "\n\n".join(blocks)
    picked = rag.retrieve(long_text, "Invoice from Northbank Mechanical Services - application for payment", max_chars=1500)
    check("Northbank Mechanical send an application" in picked and "Riverside" not in picked and len(picked) <= 1500, "long text: only the paragraphs matching the email are sent, within budget")
    both = rag.retrieve(long_text, "Northbank Riverside containment HEL18", max_chars=1500)
    check(both.index("Northbank Mechanical send") < both.index("Riverside Builders"), "retrieved paragraphs keep their original order")
    info = rag.describe(long_text)
    check(info["chars"] == len(long_text.strip()) and info["paragraphs"] == 60 and info["whole"] is False, "describe reports size, paragraphs and whether it fits")
    check(rag.describe("")["chars"] == 0 and rag.retrieve("", "x") == "", "empty reference text is fine")
    check(config.DEFAULTS["classify_reference_text"] == "" and config.DEFAULTS["extract_reference_text"] == "", "reference text starts empty")
    check(config.DEFAULTS["classify_system_prompt"] == prompts.CLASSIFY_SYSTEM and config.DEFAULTS["extract_system_prompt"] == prompts.EXTRACT_SYSTEM and "{company_name}" in prompts.CLASSIFY_SYSTEM and "{default_currency}" in prompts.EXTRACT_SYSTEM, "the shipped prompts are the defaults, with placeholders")
    check(extractor.classification_system_prompt("Glent Group", "", "Hi {company_name} {x}") == "Hi Glent Group {x}", "an edited prompt is used with the placeholder filled and other braces kept")
    check(extractor.system_prompt("Glent Group", "GBP", "notes", "Pay in {default_currency}").startswith("Pay in GBP\n\nReference notes"), "an edited extraction prompt gets the currency and the reference notes")
    check(extractor.classification_system_prompt("Glent Group", "", "") == prompts.fill(prompts.CLASSIFY_SYSTEM, company_name="Glent Group") and "classifier for Glent Group" in extractor.classification_system_prompt("Glent Group"), "a blank template falls back to the shipped prompt")
    check(prompts.is_default(" " + prompts.CLASSIFY_SYSTEM.replace("\n", "\r\n") + "\n", prompts.CLASSIFY_SYSTEM) and not prompts.is_default("x", prompts.CLASSIFY_SYSTEM), "is_default ignores surrounding whitespace and CRLF line endings only")
    check("NMS-2026-0417.pdf (application/pdf, 48 KB)" in extractor.classification_context({"from": "a", "subject": "s"}, [{"filename": "NMS-2026-0417.pdf", "mime_type": "application/pdf", "size": 48 * 1024}]), "the per-email note lists the attachments")
    check("Northbank" in extractor.system_prompt("Glent Group", "GBP", "Northbank notes") and "Reference notes" in extractor.classification_system_prompt("Glent Group", "x"), "reference notes land in both system prompts")
    check("Reference notes" not in extractor.system_prompt("Glent Group", "GBP", ""), "no reference section when the text is blank")
    check(extractor.CLASSIFY_TOOL["name"] == "label_email" and extractor.INVOICE_TOOL["name"] == "record_invoice", "both tools defined")


class FakeMailbox:
    """Stands in for gmail_client.connect(): a handful of messages, labels kept in memory."""

    def __init__(self, messages: dict):
        self.messages = messages
        self.labels: dict[str, set] = {mid: set() for mid in messages}
        self.read: set = set()
        self.queries: list[str] = []

    def list(self, query: str, max_messages: int = 10) -> list[str]:
        self.queries.append(query)
        wanted = [w[6:] for w in query.split() if w.startswith("label:")]
        unwanted = [w[7:] for w in query.split() if w.startswith("-label:")]
        out = []
        for mid, labels in self.labels.items():
            have = {gmail_client.search_label(l) for l in labels}
            if all(w in have for w in wanted) and not any(u in have for u in unwanted):
                out.append(mid)
        return out[:max_messages]

    def fetch(self, msg_id: str) -> dict:
        if msg_id == "broken":
            raise RuntimeError("Gmail hiccup")
        return dict(self.messages[msg_id], id=msg_id)

    def add_labels(self, msg_id: str, label_names: list[str]) -> None:
        self.labels[msg_id].update(l for l in label_names if l)

    def mark_processed(self, msg_id: str, label_name: str) -> None:
        self.read.add(msg_id)
        if label_name:
            self.labels[msg_id].add(label_name)


def fake_classifier(api_key, model, email_meta, attachments, company_name="", reference_text="", system_template=None):
    usage = {"input_tokens": 900, "output_tokens": 60, "model": model}
    if any(a["mime_type"] == "application/pdf" for a in attachments):
        return {"verdict": "invoice", "document_kind": "invoice", "confidence": 0.97, "reason": "PDF invoice attached.", "usage": usage}
    return {"verdict": "not_invoice", "document_kind": "remittance advice", "confidence": 0.9, "reason": "Remittance, nothing to pay.", "usage": usage}


def test_agents():
    """Classification then extraction over a fake mailbox, with both Claude calls mocked."""
    store.init_db()
    pdf = SAMPLE_PDF.read_bytes()
    box = FakeMailbox({
        "m-inv": {"thread_id": "t1", "from": "accounts@northbankmech.co.uk", "subject": "Invoice NMS-2026-0417", "date": "Mon, 8 Sep 2026",
                  "received_at": "2026-09-08T09:00:00+00:00", "body_text": "Please find attached.", "snippet": "",
                  "attachments": [{"filename": "NMS-2026-0417.pdf", "mime_type": "application/pdf", "data": pdf, "size": len(pdf)}]},
        "m-rem": {"thread_id": "t2", "from": "payments@client.example", "subject": "Remittance advice", "date": "", "received_at": "2026-09-08T10:00:00+00:00",
                  "body_text": "We have paid you.", "snippet": "", "attachments": []},
        "m-spam": {"thread_id": "t3", "from": "deals@marketing.example", "subject": "Big sale", "date": "", "received_at": "", "body_text": "", "snippet": "",
                   "attachments": [{"filename": "offer.pdf", "mime_type": "application/pdf", "data": pdf, "size": len(pdf)}]},
        "broken": {"attachments": []},
    })
    settings = {**SETTINGS, "gmail_allowed_senders": "northbankmech.co.uk, client.example"}
    fake_usage = {"input_tokens": 3210, "output_tokens": 640, "model": "test"}

    with mock.patch.object(gmail_client, "connect", return_value=box), \
         mock.patch.object(extractor, "classify_email", side_effect=fake_classifier) as classify, \
         mock.patch.object(extractor, "extract_invoice", return_value=(extractor.normalise(FAKE_RECORD), fake_usage)) as extract:
        result = pipeline.classify_run(settings)
        check(result["ok"] is True and result["looked"] == 3 and result["invoices"] == 1 and result["others"] == 1 and result["skipped"] == 1 and result["errors"] == 1,
              f"classification run counts looked / invoices / others / skipped / errors ({result})")
        check(box.queries[0] == "has:attachment is:unread -label:Invoice-Incoming -label:Not-an-invoice -label:Invoices-Processed", f"classification search excludes both agents' labels ({box.queries[0]})")
        check(box.labels["m-inv"] == {"Invoice Incoming"} and box.labels["m-rem"] == {"Not an invoice"} and box.labels["m-spam"] == {"Not an invoice"}, f"labels applied per verdict ({box.labels})")
        check(classify.call_count == 2, "the disallowed sender is labelled without a Claude call")
        check(classify.call_args_list[0].kwargs["model"] == "claude-opus-5" and classify.call_args_list[0].kwargs["reference_text"] == "" and classify.call_args_list[0].kwargs["system_template"] == prompts.CLASSIFY_SYSTEM, "classification uses its model, its (empty) reference text and its prompt")
        rows = store.list_classifications()
        check(len(rows) == 3 and {r["verdict"] for r in rows} == {"invoice", "not_invoice"}, "every decision recorded")
        spam = next(r for r in rows if r["gmail_message_id"] == "m-spam")
        check(spam["document_kind"] == "sender not allowed" and spam["label_applied"] == "Not an invoice", "sender filter decision recorded")
        counts = store.classification_counts()
        check(counts == {"invoice": 1, "not_invoice": 2, "total": 3}, f"classification counts ({counts})")
        check(store.last_run("classify")["ok"] == 0 and "Looked at 3" in store.last_run("classify")["summary"], "classification run logged with its summary")

        result = pipeline.extract_run(settings)
        check(result["ok"] and result["messages"] == 1 and result["processed"] == 1 and result["errors"] == 0, f"extraction run reads only the labelled email ({result})")
        check(box.queries[-1] == "label:Invoice-Incoming -label:Invoices-Processed", f"extraction search uses the labels ({box.queries[-1]})")
        check("Invoices/Processed" in box.labels["m-inv"] and "m-inv" in box.read, "processed email labelled and marked read")
        check(extract.call_args.kwargs["model"] == "claude-opus-5" and extract.call_args.kwargs["reference_text"] == "" and extract.call_args.kwargs["system_template"] == prompts.EXTRACT_SYSTEM, "extraction uses its model, its (empty) reference text and its prompt")
        inv = next(i for i in store.list_invoices() if i["gmail_message_id"] == "m-inv")
        check(inv["status"] == "review" and inv["attachment_name"] == "NMS-2026-0417.pdf" and inv["sage"]["invoice_type"]["id"] == "subcontractor", "labelled invoice extracted into the Inbox with its type")

        again = pipeline.extract_run(settings)
        check(again["messages"] == 0 and again["processed"] == 0, "nothing left to extract once labelled as processed")
        box.labels["m-inv"].discard("Invoices/Processed")
        again = pipeline.extract_run(settings)
        check(again["skipped"] == 1 and again["processed"] == 0 and extract.call_count == 1, "an attachment already in the Inbox is skipped, not re-read")

        both = pipeline.run_once(settings)
        check(both["ok"] and both["stage"] == "inbox" and "Classification:" in both["message"] and "Extraction:" in both["message"], f"check inbox runs both agents ({both})")
        check(store.last_run("inbox")["summary"].startswith("Classification:"), "inbox run recorded")

    with mock.patch.object(gmail_client, "connect", side_effect=gmail_client.GmailNotConnected("Gmail is not connected.")):
        result = pipeline.classify_run(settings)
        check(result["ok"] is False and "not connected" in result["message"].lower(), "classification reports Gmail not connected")
        result = pipeline.extract_run(settings)
        check(result["ok"] is False and "not connected" in result["message"].lower(), "extraction reports Gmail not connected")
    check(pipeline.classify_run({**settings, "anthropic_api_key": ""})["message"].startswith("Add your Claude API key"), "classification refuses without a Claude key")


def test_pipeline_and_routes():
    store.init_db()
    fake_usage = {"input_tokens": 3210, "output_tokens": 640, "model": "test"}
    with mock.patch.object(extractor, "extract_invoice", return_value=(extractor.normalise(FAKE_RECORD), fake_usage)):
        invoice_id = pipeline.process_attachment(
            SETTINGS, SAMPLE_PDF.read_bytes(), "sample-invoice.pdf", "application/pdf",
            email_meta={"id": "msg001", "from": "accounts@northbankmech.co.uk", "subject": "Invoice NMS-2026-0417", "received_at": "2026-09-13T09:00:00+00:00", "body_text": "Please find attached."},
        )
    inv = store.get_invoice(invoice_id)
    check(inv["status"] == "review", "processed invoice lands in review")
    check(Path(inv["attachment_path"]).exists(), "attachment saved to disk")
    check(inv["input_tokens"] == 3210, "token usage recorded")
    check(inv["sage"]["invoice_type"]["id"] == "subcontractor", "invoice type resolved on processing")

    with mock.patch.object(extractor, "extract_invoice", return_value=(extractor.normalise({**MATERIALS_RECORD, "payment_terms_days": None}), fake_usage)):
        mat_id = pipeline.process_attachment(SETTINGS, b"%PDF-1.4 materials", "rbm.pdf", "application/pdf", email_meta={"id": "msg003", "subject": "RBM invoice"})
    mat = store.get_invoice(mat_id)
    check(mat["sage"]["invoice_type"]["id"] == "materials" and mat["extracted"]["payment_terms_days"] == 30 and any("30-day default" in f for f in mat["extracted"]["flags"]),
          "type defaults (terms) applied on processing and flagged")

    with mock.patch.object(extractor, "extract_invoice", side_effect=RuntimeError("boom")):
        failed_id = pipeline.process_attachment(SETTINGS, b"%PDF-1.4 broken", "broken.pdf", "application/pdf", email_meta={"id": "msg002"})
    check(store.get_invoice(failed_id)["status"] == "error", "extraction failure recorded as error row")

    # Settings on disk so the app's context processor sees them
    config.save_settings(SETTINGS)

    import app as webapp  # noqa: E402
    client = webapp.app.test_client()

    r = client.get("/")
    check(r.status_code == 200 and b"Northbank Mechanical" in r.data, "dashboard lists the invoice")
    check(b"Check inbox now" in r.data and b"Classification:" in r.data, "dashboard shows the last inbox run")
    for path, marker in (("/agents", b"Agent tree"), ("/agents/classification", b"Reference text"), ("/agents/extraction", b"Reference text"), ("/types", b"Invoice types"), ("/settings", b"Settings")):
        r = client.get(path)
        check(r.status_code == 200 and marker in r.data and r.data.count(b'class="active"') == 1, f"{path} renders with its tab active")
    r = client.get(f"/invoice/{invoice_id}")
    check(r.status_code == 200 and b"Key into Sage" in r.data and b"V0088" in r.data, "invoice page renders the entry sheet")
    check(b"UK Purchase Services Reverse Charge Standard Rate" in r.data, "invoice page shows the tax detail")
    check(b'name="invoice_type"' in r.data and b"Automatic - Subcontractor" in r.data and b"#type-subcontractor" in r.data, "invoice page has the type selector with the matched type")
    r = client.get(f"/invoice/{failed_id}")
    check(r.status_code == 200 and b"Extraction failed" in r.data, "error row page renders")
    r = client.get("/settings")
    check(r.status_code == 200 and b'type="password"' in r.data and b"sk-ant-test" in r.data, "settings page renders masked key fields")
    check(r.data.count(b'class="reveal"') >= 8, "every secret field has a reveal button")
    check(b"5002" in r.data and b"miscellaneous purchases" in r.data, "settings page explains the generic codes")
    r = client.get("/export.csv")
    check(r.status_code == 200 and b"NMS-2026-0417" in r.data and b"Sage vendor ID" in r.data and b"Invoice type" in r.data and b"Subcontractor" in r.data, "CSV export has the log row with the invoice type")
    r = client.get(f"/invoice/{invoice_id}/sage.xml")
    check(r.status_code == 200 and b"<APBILL>" in r.data, "XML download works")
    r = client.get(f"/invoice/{invoice_id}/sage.json")
    payload = json.loads(r.data)
    check("APBILL" in payload and payload["APBILL"]["RECORDID"] == "NMS-2026-0417", "JSON download has the bill")
    r = client.get(f"/invoice/{invoice_id}/file")
    check(r.status_code == 200 and r.data.startswith(b"%PDF"), "attachment served for preview")

    # Edit and save through the form
    form = {
        "document_type": "invoice", "supplier_name": "Northbank Mechanical Services Ltd", "supplier_vat": "GB452889107",
        "vendor_id": "V0088", "invoice_type": "", "invoice_number": "NMS-2026-0417", "invoice_date": "2026-09-08", "due_date": "2026-10-08",
        "po_number": "GG-HEL18-00231", "project_reference": "HEL18", "project_id": "P-HEL18",
        "description": "Edited description", "currency": "GBP", "vat_treatment": "reverse_charge",
        "line_desc": ["Labour", "Materials"], "line_qty": ["1", "1"], "line_unit": ["lot", "lot"], "line_unit_price": ["", ""],
        "line_net": ["4914.00", "7735.00"], "line_vat_rate": ["20", "20"], "line_vat": ["0", "0"], "line_category": ["labour", "materials"],
        "net_total": "12649.00", "vat_total": "0.00", "gross_total": "12649.00",
        "cis_applicable": "on", "cis_rate": "20", "cis_amount": "982.80", "retention_pct": "3", "retention_amount": "379.47",
        "notes": "checked against AFP-07",
    }
    r = client.post(f"/invoice/{invoice_id}", data=form, follow_redirects=True)
    inv = store.get_invoice(invoice_id)
    check(r.status_code == 200 and inv["extracted"]["description"] == "Edited description", "form edits saved")
    check(len(inv["sage"]["bill"]["ITEMS"]) == 4, "payload rebuilt from the edited lines (2 + CIS + retention)")
    check(inv["notes"] == "checked against AFP-07", "notes saved")
    check(inv["sage"]["invoice_type"]["id"] == "subcontractor" and inv["sage"]["bill"]["ITEMS"][1]["ACCOUNTNO"] == "6002", "automatic type kept on save")

    r = client.post(f"/invoice/{invoice_id}", data={**form, "invoice_type": "materials"}, follow_redirects=True)
    inv = store.get_invoice(invoice_id)
    check(inv["extracted"]["sage_overrides"]["invoice_type"] == "materials" and inv["sage"]["invoice_type"]["how"] == "chosen on the invoice", "type chosen on the review page is stored")
    check(inv["sage"]["bill"]["ITEMS"][0]["ACCOUNTNO"] == "5200" and inv["sage"]["bill"]["ITEMS"][1]["ACCOUNTNO"] == "5000", "chosen type recodes the lines (labour from Settings, materials from the type)")
    check(any("not usually reverse charged" in n for n in inv["sage"]["notes"]), "VAT treatment note for the chosen type")
    r = client.get(f"/invoice/{invoice_id}")
    check(b'value="materials" selected' in r.data, "review page shows the chosen type selected")
    check(inv["issues"] == [], f"chosen type leaves no issues ({inv['issues']})")

    r = client.post(f"/invoice/{invoice_id}/status", data={"action": "approve"}, follow_redirects=True)
    check(store.get_invoice(invoice_id)["status"] == "approved", "approve works when there are no issues")
    r = client.post(f"/invoice/{mat_id}/status", data={"action": "approve"}, follow_redirects=True)
    check(store.get_invoice(mat_id)["status"] == "review" and b"outstanding checks" in r.data, "approve refused while the type's checks are outstanding")
    r = client.post(f"/invoice/{invoice_id}/push", follow_redirects=True)
    check(b"Sage Intacct connection" in r.data, "push refuses politely without Sage credentials")
    r = client.post(f"/invoice/{invoice_id}/status", data={"action": "posted"}, follow_redirects=True)
    check(store.get_invoice(invoice_id)["status"] == "posted", "manual 'keyed in' status works")

    # Settings round trip (company, Gmail client, Sage coding)
    r = client.post("/settings", data={
        "anthropic_api_key": "sk-ant-new", "google_client_id": "cid", "google_client_secret": "csec",
        "company_name": "Glent Group", "default_currency": "GBP",
        "sage_endpoint": SETTINGS["sage_endpoint"], "sage_sender_id": "", "sage_sender_password": "", "sage_company_id": "",
        "sage_user_id": "", "sage_user_password": "", "sage_action": "Draft", "sage_location_id": "LON", "sage_department_id": "",
        "sage_tax_solution_id": "United Kingdom - VAT", "sage_default_gl": "5000", "sage_cis_gl": "", "sage_retention_gl": "",
        "gl_materials": "5100", "gl_labour": "5200", "gl_plant": "", "gl_subcontract": "", "gl_services": "", "gl_expenses": "", "gl_other": "",
        "vat_20": "STD", "vat_5": "", "vat_0": "", "vat_rc": "RC",
        "terms_map": "30 = Net 30", "vendor_map": "Northbank Mechanical Services Ltd = V0088\n# comment\nSpeedy Hire = V0117", "project_map": "HEL18 = P-HEL18",
    }, follow_redirects=True)
    saved = config.load_settings()
    check(saved["anthropic_api_key"] == "sk-ant-new" and saved["vendor_map"] == {"Northbank Mechanical Services Ltd": "V0088", "Speedy Hire": "V0117"}, "settings saved incl. maps")
    check(saved["gl_map"]["labour"] == "5200" and saved["gl_map"]["plant"] == "" and saved["vat_detail_map"]["reverse_charge"] == "RC", "coding maps saved as typed")
    check(saved["classify_model"] == "claude-opus-5" and saved["gmail_max_messages"] == 10 and len(saved["invoice_types"]) == 5, "settings save leaves the agents and types alone")

    r = client.get("/settings")
    check(b"sk-ant-new" in r.data and b"csec" in r.data, "saved secrets come back into the masked inputs")

    # Agent tabs: the prompt is shown and editable, the reference text starts empty with examples
    r = client.get("/agents/classification")
    check(b"Instructions to the AI" in r.data and b"You are the mailbox classifier for {company_name}" in r.data and b"This is the shipped wording" in r.data and b'value="reset_prompt" formnovalidate disabled' in r.data, "classification tab shows the shipped prompt with a disabled restore button")
    check(b"System prompt as sent" in r.data and b"mailbox classifier for Glent Group, a UK" in r.data and b"Subject: Invoice NMS-2026-0417 - HEL18 Hall 2" in r.data and b"NMS-2026-0417.pdf (application/pdf, 48 KB)" in r.data, "classification tab previews the prompt as sent and the per-email note")
    check(b"Examples of what to add" in r.data and b"Nothing here yet" in r.data and b"Empty - nothing is added to the prompt yet" in r.data and b"Speedy Hire send a statement" in r.data, "classification reference text is empty with examples")
    r = client.post("/agents/classification", data={"classify_system_prompt": prompts.CLASSIFY_SYSTEM.replace("\n", "\r\n"), "classify_reference_text": "Line one\r\n\r\nLine two", "classify_model": "claude-opus-5"}, follow_redirects=True)
    saved = config.load_settings()
    check(saved["classify_system_prompt"] == prompts.CLASSIFY_SYSTEM and saved["classify_reference_text"] == "Line one\n\nLine two" and b"This is the shipped wording" in r.data and b"2 paragraphs" in r.data, "a browser save with CRLF line endings still counts as the shipped wording and keeps paragraphs")
    r = client.post("/agents/classification", data={"classify_system_prompt": "Only {company_name} invoices count. {keep}", "classify_model": "claude-opus-5"}, follow_redirects=True)
    saved = config.load_settings()
    check(saved["classify_system_prompt"] == "Only {company_name} invoices count. {keep}" and b"Edited from the shipped wording" in r.data and b"Only Glent Group invoices count. {keep}" in r.data and b'value="reset_prompt" formnovalidate onclick' in r.data, "edited prompt saved, previewed with the placeholder filled, restore enabled")
    r = client.post("/agents/classification", data={"classify_system_prompt": "", "classify_model": "claude-opus-5"}, follow_redirects=True)
    saved = config.load_settings()
    check(saved["classify_system_prompt"] == prompts.CLASSIFY_SYSTEM and b"cannot be empty" in r.data, "an emptied prompt goes back to the default with a warning")
    client.post("/agents/classification", data={"classify_system_prompt": "Short.", "classify_model": "claude-sonnet-5"})
    r = client.post("/agents/classification", data={"classify_system_prompt": "Short.", "classify_model": "claude-haiku-4-5-20251001", "gmail_max_messages": "9", "action": "reset_prompt"}, follow_redirects=True)
    saved = config.load_settings()
    check(saved["classify_system_prompt"] == prompts.CLASSIFY_SYSTEM and saved["classify_model"] == "claude-haiku-4-5-20251001" and saved["gmail_max_messages"] == 9 and b"Instructions restored to the default" in r.data and b"This is the shipped wording" in r.data, "restore default puts the shipped prompt back and still saves the other fields")
    r = client.get("/agents/extraction")
    check(b"You are the accounts payable assistant for {company_name}" in r.data and b"Default currency GBP unless" in r.data and b"This is the shipped wording" in r.data and b"Examples of what to add" in r.data and b"Attachment: NMS-2026-0417.pdf" in r.data, "extraction tab shows the shipped prompt, the preview with the currency filled, and the examples")
    r = client.post("/agents/extraction", data={"extract_system_prompt": "Read it in {default_currency}.", "extract_model": "claude-opus-5"}, follow_redirects=True)
    saved = config.load_settings()
    check(saved["extract_system_prompt"] == "Read it in {default_currency}." and b"Read it in GBP." in r.data and b"Edited from the shipped wording" in r.data, "edited extraction prompt saved and previewed")
    r = client.post("/agents/extraction", data={"extract_system_prompt": "x", "extract_model": "claude-opus-5", "action": "reset_prompt"}, follow_redirects=True)
    saved = config.load_settings()
    check(saved["extract_system_prompt"] == prompts.EXTRACT_SYSTEM and b"Instructions restored to the default" in r.data, "extraction restore default works")
    r = client.post("/agents/classification", data={
        "classify_model": "claude-haiku-4-5-20251001", "classify_reference_text": "Northbank send AFPs.\n\nStatements from Speedy are not invoices.",
        "classify_invoice_label": "Invoice Incoming", "classify_other_label": "", "gmail_query": "has:attachment", "gmail_allowed_senders": "", "gmail_max_messages": "7",
    }, follow_redirects=True)
    saved = config.load_settings()
    check(r.status_code == 200 and saved["classify_model"] == "claude-haiku-4-5-20251001" and saved["gmail_max_messages"] == 7 and saved["classify_other_label"] == "" and "AFPs" in saved["classify_reference_text"], "classification agent settings saved")
    r = client.get("/agents/classification")
    check(b"2 paragraphs" in r.data and b"sent whole" in r.data and b'value="claude-haiku-4-5-20251001" selected' in r.data, "classification tab shows the reference text size and the model")
    check(b"then the reference text." in r.data and b"Statements from Speedy are not invoices." in r.data, "the as-sent preview includes the reference text once there is some")
    check(b"Remittance advice" in r.data and b"Not an invoice" in r.data and b"97%" in r.data, "classification tab lists the recorded decisions")
    r = client.post("/agents/classification", data={"classify_model": "claude-opus-5", "classify_invoice_label": "", "action": "run"}, follow_redirects=True)
    saved = config.load_settings()
    check(b"not connected" in r.data.lower() and saved["classify_invoice_label"] == "Invoice Incoming" and saved["gmail_max_messages"] == 7, "classify now without Gmail explains; blank label falls back; other fields untouched")

    r = client.post("/agents/extraction", data={"extract_model": "claude-sonnet-5", "extract_reference_text": "HEL18 is the Helsinki job.", "gmail_processed_label": "Done/Invoices", "poll_minutes": "15"}, follow_redirects=True)
    saved = config.load_settings()
    check(saved["extract_model"] == "claude-sonnet-5" and saved["poll_minutes"] == 15 and saved["gmail_processed_label"] == "Done/Invoices", "extraction agent settings saved")
    r = client.get("/agents/extraction")
    check(b"1 paragraph -" in r.data and b"HEL18 is the Helsinki job." in r.data and r.data.count(b"HEL18 is the Helsinki job.") == 2, "extraction tab shows the reference text in the box and in the as-sent preview")
    r = client.get("/agents/extraction")
    check(b'value="claude-sonnet-5" selected' in r.data and b"invoice_number" in r.data and b"line_items[]" in r.data and b"Done/Invoices" in r.data, "extraction tab shows the model, the schema and the label")
    r = client.post("/agents/extraction", data={"action": "run"}, follow_redirects=True)
    check(b"not connected" in r.data.lower(), "extract now without Gmail explains")

    r = client.get("/agents")
    check(b"Agent - Classification" in r.data and b"Agent - Invoice Extraction" in r.data and b"Claude Sonnet 5" in r.data and b"Claude Opus 5" in r.data, "agent tree shows both agents with their models")
    check(b"3 emails classified" in r.data and b"Not connected" in r.data and b"Done/Invoices" in r.data and b'href="/agents/classification"' in r.data, "agent tree carries counts, status, labels and links")

    # Invoice types: edit, add, remove
    r = client.get("/types")
    check(b"Subcontractor" in r.data and b"Materials supplier" in r.data and b'name="subcontractor__gl_labour"' in r.data and b'value="6002"' in r.data, "invoice types page lists the generic types with their coding")
    types_form = {}
    for t in saved["invoice_types"]:
        f = t["id"] + "__"
        types_form.update({f + "name": t["name"], f + "description": t["description"], f + "suppliers": t["suppliers"], f + "cis_rate": str(t["cis_rate"]),
                           f + "retention_percent": str(t["retention_percent"]), f + "terms_days": "" if t["terms_days"] is None else str(t["terms_days"]),
                           f + "expected_vat": t["expected_vat"], f + "location_id": t["location_id"], f + "department_id": t["department_id"], f + "sage_action": t["sage_action"]})
        types_form.update({f + "gl_" + c: v for c, v in t["gl_map"].items()})
        types_form.update({f + "vat_20": t["vat_detail_map"]["20"], f + "vat_5": t["vat_detail_map"]["5"], f + "vat_0": t["vat_detail_map"]["0"], f + "vat_rc": t["vat_detail_map"]["reverse_charge"]})
        for c in t["categories"]:
            types_form[f + "cat_" + c] = "on"
        for s in t["signals"]:
            types_form[f + "sig_" + s] = "on"
        for flag in ("cis_applies", "po_required", "project_required"):
            if t[flag]:
                types_form[f + flag] = "on"
    edited = {**types_form, "subcontractor__name": "Subbies", "subcontractor__retention_percent": "5", "subcontractor__suppliers": "Northbank Mechanical", "plant__po_required": ""}
    edited.pop("plant__po_required")
    r = client.post("/types", data=edited, follow_redirects=True)
    saved = config.load_settings()
    sub = invoice_types.type_by_id(saved, "subcontractor")
    check(r.status_code == 200 and sub["name"] == "Subbies" and sub["retention_percent"] == 5 and sub["suppliers"] == "Northbank Mechanical" and sub["cis_applies"] and sub["signals"] == ["cis", "reverse_charge", "application_for_payment"], f"type edits saved ({sub['name']}, {sub['retention_percent']}, {sub['signals']})")
    check(invoice_types.type_by_id(saved, "plant")["po_required"] is False and invoice_types.type_by_id(saved, "materials")["po_required"] is True, "unticked check saved as off, others kept")
    r = client.post("/types", data={**edited, "action": "add"}, follow_redirects=True)
    saved = config.load_settings()
    added = [t for t in saved["invoice_types"] if t["id"].startswith("type_")]
    check(len(saved["invoice_types"]) == 6 and len(added) == 1 and added[0]["name"] == "New type" and b"Type added" in r.data, "a type can be added")
    r = client.post("/types", data={**edited, "action": "remove:" + added[0]["id"]}, follow_redirects=True)
    saved = config.load_settings()
    check(len(saved["invoice_types"]) == 5 and b"Removed the New type type" in r.data, "a type can be removed")
    r = client.get(f"/invoice/{invoice_id}")
    check(b"Subbies" in r.data, "renamed type shows on the review page")
    kind, how = invoice_types.resolve_type(extractor.normalise(FAKE_RECORD), saved)
    check(kind["id"] == "subcontractor" and "listed under Subbies" in how, "supplier listed on the edited type now matches by name")

    # Run without Gmail connected
    result = pipeline.run_once(saved)
    check(result["ok"] is False and "not connected" in result["message"].lower(), "inbox run reports Gmail not connected")
    r = client.post("/run", follow_redirects=True)
    check(b"not connected" in r.data.lower(), "check inbox button explains when Gmail is not connected")

    r = client.post("/gmail/disconnect", follow_redirects=True)
    check(r.status_code == 200, "disconnect route works with no token")

    r = client.post("/settings/test/sage", json={"sender_id": ""})
    check(r.get_json()["ok"] is False, "sage test refuses without credentials")


if __name__ == "__main__":
    try:
        test_mapper()
        test_types_rag_and_config()
        test_agents()
        test_pipeline_and_routes()
        print("\nAll checks passed.")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
