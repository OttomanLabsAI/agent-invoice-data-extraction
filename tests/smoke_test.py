"""End-to-end smoke test without touching Gmail or the Claude API.

Run from the project root:  python tests/smoke_test.py
It uses a throwaway data/ directory (INVOICE_AGENT_DATA) so it never touches real settings.
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

from agent import config, extractor, pipeline, sage_mapper, store  # noqa: E402

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

SETTINGS = {
    **config.DEFAULTS,
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


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("ok  -", msg)


def test_mapper():
    rec = extractor.normalise(FAKE_RECORD)
    mapped = sage_mapper.build(rec, SETTINGS, email_meta={"subject": "Invoice NMS-2026-0417", "from": "accounts@northbankmech.co.uk", "received_at": "2026-09-13T09:00:00+00:00"})
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
    xml_text = sage_mapper.bill_to_xml(bill)
    root = ET.fromstring(xml_text)
    check(root.tag == "create" and root.find("APBILL") is not None, "XML gateway payload parses as <create><APBILL>")
    check(len(root.findall(".//APBILLITEM")) == 7, "seven bill lines in the XML (5 + CIS + retention)")
    net_sum = sum(float(i["TRX_AMOUNT"]) for i in bill["ITEMS"])
    check(abs(net_sum - 11286.73) < 0.01, f"line amounts sum to the amount payable ({net_sum:.2f})")
    check(mapped["log_row"]["Amount payable"] == "11286.73", "log row carries the amount payable")

    unmapped = sage_mapper.build(rec, {**config.DEFAULTS}, email_meta={})
    check(any("vendor ID" in i for i in unmapped["issues"]), "missing vendor flagged when the map is empty")
    check(any("GL account" in i for i in unmapped["issues"]), "missing GL flagged when the map is empty")

    credit = extractor.normalise({**FAKE_RECORD, "document_type": "credit_note", "cis": {"applicable": False}, "retention": {"applicable": False}})
    credit_map = sage_mapper.build(credit, SETTINGS, email_meta={})
    check(credit_map["bill"]["_object"] == "APADJUSTMENT", "credit note maps to an AP adjustment")
    check(credit_map["bill"]["ITEMS"][0]["TRX_AMOUNT"].startswith("-"), "credit note amounts are negative")
    check("<APADJUSTMENTITEMS>" in sage_mapper.bill_to_xml(credit_map["bill"]), "credit note XML uses APADJUSTMENTITEMS")


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

    with mock.patch.object(extractor, "extract_invoice", side_effect=RuntimeError("boom")):
        failed_id = pipeline.process_attachment(SETTINGS, b"%PDF-1.4 broken", "broken.pdf", "application/pdf", email_meta={"id": "msg002"})
    check(store.get_invoice(failed_id)["status"] == "error", "extraction failure recorded as error row")

    # Settings on disk so the app's context processor sees them
    config.save_settings(SETTINGS)

    import app as webapp  # noqa: E402
    client = webapp.app.test_client()

    r = client.get("/")
    check(r.status_code == 200 and b"Northbank Mechanical" in r.data, "dashboard lists the invoice")
    r = client.get(f"/invoice/{invoice_id}")
    check(r.status_code == 200 and b"Key into Sage" in r.data and b"V0088" in r.data, "invoice page renders the entry sheet")
    check(b"UK Purchase Services Reverse Charge Standard Rate" in r.data, "invoice page shows the tax detail")
    r = client.get(f"/invoice/{failed_id}")
    check(r.status_code == 200 and b"Extraction failed" in r.data, "error row page renders")
    r = client.get("/settings")
    check(r.status_code == 200 and b'type="password"' in r.data and b"sk-ant-test" in r.data, "settings page renders masked key fields")
    check(r.data.count(b'class="reveal"') >= 8, "every secret field has a reveal button")
    r = client.get("/export.csv")
    check(r.status_code == 200 and b"NMS-2026-0417" in r.data and b"Sage vendor ID" in r.data, "CSV export has the log row")
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
        "vendor_id": "V0088", "invoice_number": "NMS-2026-0417", "invoice_date": "2026-09-08", "due_date": "2026-10-08",
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

    r = client.post(f"/invoice/{invoice_id}/status", data={"action": "approve"}, follow_redirects=True)
    check(store.get_invoice(invoice_id)["status"] == "approved", "approve works when there are no issues")
    r = client.post(f"/invoice/{invoice_id}/push", follow_redirects=True)
    check(b"Sage Intacct connection" in r.data, "push refuses politely without Sage credentials")
    r = client.post(f"/invoice/{invoice_id}/status", data={"action": "posted"}, follow_redirects=True)
    check(store.get_invoice(invoice_id)["status"] == "posted", "manual 'keyed in' status works")

    # Settings round trip
    r = client.post("/settings", data={
        "anthropic_api_key": "sk-ant-new", "claude_model": "claude-sonnet-5", "google_client_id": "cid", "google_client_secret": "csec",
        "gmail_query": "has:attachment", "gmail_allowed_senders": "", "gmail_processed_label": "Invoices/Processed",
        "gmail_max_messages": "5", "poll_minutes": "0", "company_name": "Glent Group", "default_currency": "GBP",
        "sage_endpoint": SETTINGS["sage_endpoint"], "sage_sender_id": "", "sage_sender_password": "", "sage_company_id": "",
        "sage_user_id": "", "sage_user_password": "", "sage_action": "Draft", "sage_location_id": "LON", "sage_department_id": "",
        "sage_tax_solution_id": "United Kingdom - VAT", "sage_default_gl": "5000", "sage_cis_gl": "", "sage_retention_gl": "",
        "gl_materials": "5100", "gl_labour": "5200", "gl_plant": "", "gl_subcontract": "", "gl_services": "", "gl_expenses": "", "gl_other": "",
        "vat_20": "STD", "vat_5": "", "vat_0": "", "vat_rc": "RC",
        "terms_map": "30 = Net 30", "vendor_map": "Northbank Mechanical Services Ltd = V0088\n# comment\nSpeedy Hire = V0117", "project_map": "HEL18 = P-HEL18",
    }, follow_redirects=True)
    saved = config.load_settings()
    check(saved["anthropic_api_key"] == "sk-ant-new" and saved["vendor_map"] == {"Northbank Mechanical Services Ltd": "V0088", "Speedy Hire": "V0117"}, "settings saved incl. maps")
    check(saved["gmail_max_messages"] == 5, "numeric settings parsed")

    r = client.get("/settings")
    check(b"sk-ant-new" in r.data and b"csec" in r.data, "saved secrets come back into the masked inputs")

    # Run without Gmail connected
    result = pipeline.run_once(saved)
    check(result["ok"] is False and "not connected" in result["message"].lower(), "inbox run reports Gmail not connected")

    r = client.post("/gmail/disconnect", follow_redirects=True)
    check(r.status_code == 200, "disconnect route works with no token")

    r = client.post("/settings/test/sage", json={"sender_id": ""})
    check(r.get_json()["ok"] is False, "sage test refuses without credentials")


if __name__ == "__main__":
    try:
        test_mapper()
        test_pipeline_and_routes()
        print("\nAll checks passed.")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
