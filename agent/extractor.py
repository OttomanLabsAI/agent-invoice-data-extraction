"""Invoice extraction with the Claude API.

The attachment (PDF or image) goes to the Messages API together with a tool
definition whose input schema is the invoice record we want. The model fills
the schema; if it answers in plain JSON instead, that is parsed as a fallback.
"""

from __future__ import annotations

import base64
import json
import re

import anthropic

MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024

LINE_CATEGORIES = ["labour", "materials", "plant", "subcontract", "services", "expenses", "other"]
DOCUMENT_TYPES = ["invoice", "credit_note", "application_for_payment", "proforma", "statement", "remittance", "other"]
VAT_TREATMENTS = ["standard", "reduced", "zero", "exempt", "reverse_charge", "mixed", "unknown"]


def _money(desc: str) -> dict:
    return {"type": ["number", "null"], "description": desc + " Plain number, no currency symbol, 2dp."}


def _text(desc: str) -> dict:
    return {"type": ["string", "null"], "description": desc}


INVOICE_TOOL = {
    "name": "record_invoice",
    "description": (
        "Record the contents of one supplier document (invoice, credit note, application for payment) "
        "as structured data ready to be posted as an accounts payable bill in Sage Intacct."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "document_type": {"type": "string", "enum": DOCUMENT_TYPES},
            "supplier": {
                "type": "object",
                "properties": {
                    "name": _text("Legal / trading name of the supplier exactly as printed."),
                    "address": _text("Supplier postal address on one line."),
                    "postcode": _text("Supplier postcode."),
                    "country": _text("Supplier country, default United Kingdom if a UK address."),
                    "vat_number": _text("Supplier VAT registration number, e.g. GB123456789."),
                    "company_number": _text("Companies House registration number if printed."),
                    "email": _text("Accounts / remittance email address if printed."),
                    "phone": _text("Phone number if printed."),
                },
            },
            "bill_to": {
                "type": "object",
                "properties": {
                    "name": _text("Customer name the invoice is addressed to."),
                    "address": _text("Customer address on one line."),
                },
            },
            "invoice_number": _text("Supplier's invoice / credit note number exactly as printed."),
            "invoice_date": _text("Invoice date as YYYY-MM-DD."),
            "due_date": _text("Payment due date as YYYY-MM-DD if printed or derivable from terms."),
            "payment_terms_days": {"type": ["integer", "null"], "description": "Payment terms in days, e.g. 30."},
            "payment_terms_text": _text("Payment terms exactly as printed, e.g. '30 days from invoice date'."),
            "po_number": _text("Customer purchase order number quoted on the invoice."),
            "order_reference": _text("Any other order / contract / quote reference quoted."),
            "delivery_note_number": _text("Delivery note or ticket number(s) if quoted."),
            "project_reference": _text("Project, job, site or contract name/code the work relates to (e.g. a data centre site code)."),
            "site_address": _text("Site / delivery address if different from the bill-to address."),
            "description": _text("One-line summary of what was supplied, suitable as a bill description in Sage."),
            "currency": {"type": "string", "description": "ISO 4217 code, e.g. GBP, EUR."},
            "line_items": {
                "type": "array",
                "description": "Every billable line. Keep the supplier's order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": _text("Line description."),
                        "quantity": {"type": ["number", "null"]},
                        "unit": _text("Unit of measure, e.g. each, m, hrs, day, tonne."),
                        "unit_price": _money("Unit price ex VAT."),
                        "net_amount": _money("Line total ex VAT."),
                        "vat_rate": {"type": ["number", "null"], "description": "VAT rate percent for the line, e.g. 20, 5, 0."},
                        "vat_amount": _money("VAT on the line."),
                        "category": {"type": "string", "enum": LINE_CATEGORIES},
                    },
                    "required": ["description", "net_amount", "category"],
                },
            },
            "net_total": _money("Total ex VAT."),
            "vat_total": _money("Total VAT charged."),
            "gross_total": _money("Total inc VAT."),
            "discount_amount": _money("Settlement or other discount shown, if any."),
            "amount_due": _money("Amount payable after any CIS deduction, retention or discount, if shown separately from the gross total."),
            "vat_breakdown": {
                "type": "array",
                "description": "One entry per VAT rate used.",
                "items": {
                    "type": "object",
                    "properties": {
                        "rate": {"type": ["number", "null"]},
                        "net": _money("Net at this rate."),
                        "vat": _money("VAT at this rate."),
                    },
                },
            },
            "vat_treatment": {
                "type": "string",
                "enum": VAT_TREATMENTS,
                "description": "reverse_charge when the invoice says the customer must account for VAT under the domestic reverse charge for construction services.",
            },
            "cis": {
                "type": "object",
                "description": "Construction Industry Scheme details if the invoice is from a subcontractor.",
                "properties": {
                    "applicable": {"type": "boolean"},
                    "labour_amount": _money("Labour element subject to CIS deduction."),
                    "materials_amount": _money("Materials element (not deductible)."),
                    "deduction_rate": {"type": ["number", "null"], "description": "CIS deduction rate percent (0, 20 or 30) if stated."},
                    "deduction_amount": _money("CIS deduction shown, if any."),
                },
                "required": ["applicable"],
            },
            "retention": {
                "type": "object",
                "properties": {
                    "applicable": {"type": "boolean"},
                    "percentage": {"type": ["number", "null"]},
                    "amount": _money("Retention withheld on this invoice."),
                },
                "required": ["applicable"],
            },
            "bank_details": {
                "type": "object",
                "properties": {
                    "account_name": _text("Account name."),
                    "bank_name": _text("Bank name."),
                    "sort_code": _text("Sort code as printed, e.g. 12-34-56."),
                    "account_number": _text("Account number."),
                    "iban": _text("IBAN if printed."),
                    "bic": _text("BIC / SWIFT if printed."),
                },
            },
            "totals_reconcile": {"type": "boolean", "description": "true if net_total + vat_total = gross_total and the lines add up to net_total (within 0.02)."},
            "confidence": {"type": "number", "description": "0 to 1. Your confidence that the key fields (supplier, number, dates, totals) are correct."},
            "flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Anything the accounts team should check by hand: unreadable text, missing PO, totals that do not add up, duplicate risk, handwritten amendments, multiple invoices in one file, etc.",
            },
        },
        "required": [
            "document_type", "supplier", "invoice_number", "invoice_date", "currency",
            "line_items", "net_total", "vat_total", "gross_total", "vat_treatment",
            "cis", "retention", "totals_reconcile", "confidence", "flags",
        ],
    },
}


def system_prompt(company_name: str, default_currency: str) -> str:
    return f"""You are the accounts payable assistant for {company_name}, a UK construction contractor delivering MEP and civils packages on data-centre and infrastructure projects. Suppliers and subcontractors email their invoices to a shared mailbox; your job is to read each document and capture exactly what the accounts team keys into Sage Intacct when posting an AP bill.

Extract every field you can read directly from the document. Never invent a value: if something is not printed, leave it null and add a flag. Specifically:
- Dates: output YYYY-MM-DD. If only payment terms are printed (e.g. "30 days"), derive due_date from invoice_date and say so in a flag.
- Amounts: plain numbers, no currency symbols or thousands separators. Default currency {default_currency} unless the document clearly shows another.
- Lines: capture every billable line with its net amount and VAT rate. Classify each line as labour, materials, plant, subcontract, services, expenses or other. A subcontractor's "supply and fit" line is subcontract; hire of equipment is plant; consumables and deliveries are materials.
- UK VAT: identify the treatment. If the invoice says "reverse charge", "domestic reverse charge applies", "customer to pay VAT to HMRC" or shows VAT at 0 with a reverse-charge note, set vat_treatment to reverse_charge and vat_total to 0 (the VAT is accounted for by the customer).
- CIS: if the invoice separates labour and materials, or shows a CIS deduction, or the supplier is clearly a subcontractor doing site work, fill the cis block. Materials are never subject to CIS deduction.
- Retention: capture any retention percentage or amount withheld.
- References: purchase order numbers, delivery notes, project or site codes and contract references matter a lot for coding the bill - capture every one you can see.
- Check the arithmetic: lines should sum to net_total and net_total + vat_total should equal gross_total. If they do not, still record what is printed and set totals_reconcile to false with a flag explaining the difference.
- If the file is not an invoice or credit note (a statement, remittance advice, quote, delivery note, marketing), set document_type accordingly and keep the rest minimal.
- If a file contains more than one invoice, extract the first and flag that others exist.

Use the record_invoice tool for your answer."""


def build_content(attachment: bytes, mime_type: str, context_text: str) -> list[dict]:
    data = base64.standard_b64encode(attachment).decode("ascii")
    if mime_type == "application/pdf":
        media_block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    else:
        media_block = {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": data}}
    return [
        media_block,
        {"type": "text", "text": context_text},
    ]


def email_context(meta: dict | None) -> str:
    if not meta:
        return "Read the attached document and record it with the record_invoice tool."
    lines = [
        "The attached document arrived by email. Use the email details for context (they often carry the PO or site reference) but the document itself is the source of truth.",
        f"From: {meta.get('from', '')}",
        f"Subject: {meta.get('subject', '')}",
        f"Date: {meta.get('date', '')}",
        f"Attachment: {meta.get('attachment_name', '')}",
    ]
    body = (meta.get("body_text") or "").strip()
    if body:
        lines.append("Email body:\n" + body[:2500])
    lines.append("Record the document with the record_invoice tool.")
    return "\n".join(lines)


def _parse_text_json(text: str) -> dict | None:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    try:
        return json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except ValueError:
                return None
    return None


def extract_invoice(
    api_key: str,
    model: str,
    attachment: bytes,
    mime_type: str,
    company_name: str = "Glent Group",
    default_currency: str = "GBP",
    email_meta: dict | None = None,
) -> tuple[dict, dict]:
    """Return (extracted_record, usage). Raises anthropic errors on API failure."""
    if len(attachment) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Attachment is larger than 30 MB; split it or compress it first.")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=8000,
        system=system_prompt(company_name, default_currency),
        tools=[INVOICE_TOOL],
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": build_content(attachment, mime_type, email_context(email_meta))}],
    )

    record: dict | None = None
    text_parts: list[str] = []
    for block in response.content:
        if block.type == "tool_use" and block.name == INVOICE_TOOL["name"]:
            record = dict(block.input)
            break
        if block.type == "text":
            text_parts.append(block.text)
    if record is None:
        record = _parse_text_json("\n".join(text_parts))
    if record is None:
        raise ValueError("The model did not return a structured invoice record.")

    usage = {
        "input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(response.usage, "output_tokens", 0) or 0,
        "model": response.model,
    }
    return normalise(record), usage


# --------------------------------------------------------------------------- Normalisation


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def normalise(record: dict) -> dict:
    """Coerce numbers, fill missing blocks, recompute totals where the model left gaps."""
    rec = dict(record)
    rec.setdefault("document_type", "invoice")
    rec["supplier"] = dict(rec.get("supplier") or {})
    rec["bill_to"] = dict(rec.get("bill_to") or {})
    rec["bank_details"] = dict(rec.get("bank_details") or {})
    rec["cis"] = dict(rec.get("cis") or {"applicable": False})
    rec["retention"] = dict(rec.get("retention") or {"applicable": False})
    rec["flags"] = [str(f) for f in (rec.get("flags") or [])]
    rec["currency"] = (rec.get("currency") or "GBP").upper()
    rec.setdefault("vat_treatment", "unknown")

    lines = []
    for item in rec.get("line_items") or []:
        item = dict(item)
        for key in ("quantity", "unit_price", "net_amount", "vat_rate", "vat_amount"):
            item[key] = _num(item.get(key))
        if item.get("category") not in LINE_CATEGORIES:
            item["category"] = "other"
        if item.get("vat_amount") is None and item.get("net_amount") is not None and item.get("vat_rate") is not None:
            item["vat_amount"] = round(item["net_amount"] * item["vat_rate"] / 100, 2)
        lines.append(item)
    rec["line_items"] = lines

    for key in ("net_total", "vat_total", "gross_total", "discount_amount", "amount_due"):
        rec[key] = _num(rec.get(key))
    for key in ("labour_amount", "materials_amount", "deduction_rate", "deduction_amount"):
        rec["cis"][key] = _num(rec["cis"].get(key))
    for key in ("percentage", "amount"):
        rec["retention"][key] = _num(rec["retention"].get(key))

    if rec["net_total"] is None and lines:
        rec["net_total"] = round(sum(l["net_amount"] or 0 for l in lines), 2)
    if rec["vat_total"] is None and lines:
        rec["vat_total"] = round(sum(l["vat_amount"] or 0 for l in lines), 2)
    if rec["gross_total"] is None and rec["net_total"] is not None:
        rec["gross_total"] = round(rec["net_total"] + (rec["vat_total"] or 0), 2)

    breakdown = []
    for entry in rec.get("vat_breakdown") or []:
        entry = dict(entry)
        for key in ("rate", "net", "vat"):
            entry[key] = _num(entry.get(key))
        breakdown.append(entry)
    if not breakdown and lines:
        by_rate: dict = {}
        for l in lines:
            rate = l.get("vat_rate") if l.get("vat_rate") is not None else 0
            slot = by_rate.setdefault(rate, {"rate": rate, "net": 0.0, "vat": 0.0})
            slot["net"] = round(slot["net"] + (l.get("net_amount") or 0), 2)
            slot["vat"] = round(slot["vat"] + (l.get("vat_amount") or 0), 2)
        breakdown = list(by_rate.values())
    rec["vat_breakdown"] = breakdown

    rec["totals_reconcile"] = totals_reconcile(rec)
    try:
        rec["confidence"] = max(0.0, min(1.0, float(rec.get("confidence") or 0)))
    except (TypeError, ValueError):
        rec["confidence"] = 0.0
    return rec


def totals_reconcile(rec: dict, tolerance: float = 0.02) -> bool:
    net, vat, gross = rec.get("net_total"), rec.get("vat_total"), rec.get("gross_total")
    if net is None or gross is None:
        return False
    if abs(net + (vat or 0) - gross) > tolerance:
        return False
    lines = rec.get("line_items") or []
    if lines and abs(sum(l.get("net_amount") or 0 for l in lines) - net) > tolerance:
        return False
    return True


def check_api_key(api_key: str) -> tuple[bool, str]:
    """Validate a key without spending tokens: list models."""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        page = client.models.list(limit=5)
        names = [m.id for m in page.data]
        return True, "Key works. Models visible: " + ", ".join(names[:5])
    except anthropic.AuthenticationError:
        return False, "Anthropic rejected the key (401). Check it was pasted in full."
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not reach the Claude API: {exc}"
