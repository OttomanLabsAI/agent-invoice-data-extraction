"""Turn an extracted invoice into what Sage Intacct needs.

Outputs:
  bill        - APBILL (or APADJUSTMENT for credit notes) create payload, field order
                as Intacct documents it
  issues      - things that block a clean post (missing vendor ID, GL code, ...)
  entry_sheet - the fields in the order they appear on Intacct's bill entry screen,
                for anyone keying the bill in by hand
  log_row     - flat row for the purchase invoice log / CSV export
  xml         - the <create> function body for the Intacct XML gateway
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape

LEGAL_SUFFIXES = r"\b(ltd|limited|plc|llp|llc|inc|co|company|uk|group|holdings|services|the)\b"


def _norm(name: str | None) -> str:
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(LEGAL_SUFFIXES, " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _lookup(mapping: dict, *candidates: str | None) -> tuple[str, str]:
    """Return (value, matched_key). Exact normalised match first, then containment either way."""
    if not mapping:
        return "", ""
    normalised = {_norm(k): (k, v) for k, v in mapping.items() if k and v}
    cands = [_norm(c) for c in candidates if c]
    for cand in cands:
        if cand in normalised:
            key, value = normalised[cand]
            return value, key
    for cand in cands:
        for nkey, (key, value) in normalised.items():
            if len(nkey) >= 3 and (nkey in cand or cand in nkey):
                return value, key
    return "", ""


def _money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _fmt(value) -> str:
    return f"{_money(value):.2f}"


def _iso(value: str | None) -> str:
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return value


def _rate_key(rate) -> str:
    if rate is None:
        return "0"
    r = float(rate)
    return str(int(r)) if r.is_integer() else str(r)


def build(rec: dict, settings: dict, email_meta: dict | None = None, overrides: dict | None = None) -> dict:
    overrides = overrides or {}
    email_meta = email_meta or {}
    issues: list[str] = []
    notes: list[str] = []

    doc_type = rec.get("document_type") or "invoice"
    is_credit = doc_type == "credit_note"
    supplier = rec.get("supplier") or {}
    supplier_name = supplier.get("name") or ""
    currency = (rec.get("currency") or settings.get("default_currency") or "GBP").upper()

    # ---- Vendor
    vendor_id = overrides.get("vendor_id") or ""
    matched_vendor_key = ""
    if not vendor_id:
        vendor_id, matched_vendor_key = _lookup(settings.get("vendor_map") or {}, supplier_name, supplier.get("trading_name"))
    if not vendor_id:
        issues.append(f"No Sage vendor ID for '{supplier_name or 'unknown supplier'}'. Add it on the invoice or in Settings > Vendor map.")

    # ---- Dates and terms
    invoice_date = _iso(overrides.get("invoice_date") or rec.get("invoice_date"))
    due_date = _iso(overrides.get("due_date") or rec.get("due_date"))
    terms_days = rec.get("payment_terms_days")
    if not due_date and invoice_date and terms_days is not None:
        try:
            due_date = (date.fromisoformat(invoice_date) + timedelta(days=int(terms_days))).isoformat()
            notes.append(f"Due date derived from {terms_days}-day terms.")
        except ValueError:
            pass
    term_name = ""
    if terms_days is not None:
        term_name = (settings.get("terms_map") or {}).get(str(int(terms_days)), "")
    if not invoice_date:
        issues.append("Invoice date missing.")
    if not due_date and not term_name:
        issues.append("No due date and no matching payment term. Set one before posting.")

    # ---- Project / job
    project_id = overrides.get("project_id") or ""
    matched_project_key = ""
    if not project_id:
        project_id, matched_project_key = _lookup(
            settings.get("project_map") or {},
            rec.get("project_reference"), rec.get("po_number"), rec.get("order_reference"),
            rec.get("site_address"), email_meta.get("subject"),
        )
    if not project_id and (settings.get("project_map") or {}):
        notes.append("No project matched - bill will post without a project dimension.")

    # ---- Lines
    gl_map = settings.get("gl_map") or {}
    vat_map = settings.get("vat_detail_map") or {}
    reverse_charge = (rec.get("vat_treatment") == "reverse_charge")
    sign = -1 if is_credit else 1
    location_id = settings.get("sage_location_id") or ""
    department_id = settings.get("sage_department_id") or ""

    items = []
    missing_gl = set()
    missing_tax = set()
    for idx, line in enumerate(rec.get("line_items") or [], start=1):
        category = line.get("category") or "other"
        account = gl_map.get(category) or settings.get("sage_default_gl") or ""
        if not account:
            missing_gl.add(category)
        net = _money(line.get("net_amount"))
        vat_amount = 0.0 if reverse_charge else _money(line.get("vat_amount"))
        rate_key = "reverse_charge" if reverse_charge else _rate_key(line.get("vat_rate"))
        detail_id = vat_map.get(rate_key, "")
        if not detail_id:
            missing_tax.add(rate_key)
        item = {
            "ACCOUNTNO": account,
            "TRX_AMOUNT": _fmt(sign * net),
            "ENTRYDESCRIPTION": (line.get("description") or "")[:200],
            "LOCATIONID": location_id,
            "DEPARTMENTID": department_id,
            "PROJECTID": project_id,
            "TAXENTRIES": [{"DETAILID": detail_id, "TRX_TAX": _fmt(sign * vat_amount)}],
            "_line_no": idx,
            "_category": category,
            "_vat_rate": rate_key,
            "_tax_label": "reverse charge" if reverse_charge else f"{rate_key}%",
            "_quantity": line.get("quantity"),
            "_unit": line.get("unit"),
            "_unit_price": line.get("unit_price"),
        }
        items.append(item)

    if not items:
        issues.append("No line items were extracted - add at least one line.")
    for cat in sorted(missing_gl):
        issues.append(f"No GL account for '{cat}' lines. Set it in Settings > GL accounts by category.")
    for key in sorted(missing_tax):
        label = "reverse charge" if key == "reverse_charge" else f"{key}% VAT"
        issues.append(f"No Sage tax detail for {label}. Set it in Settings > VAT tax details.")

    # ---- Construction-specific deductions
    cis = rec.get("cis") or {}
    cis_deduction = _money(cis.get("deduction_amount"))
    if cis.get("applicable") or cis_deduction:
        if cis_deduction and settings.get("sage_cis_gl"):
            items.append({
                "ACCOUNTNO": settings["sage_cis_gl"],
                "TRX_AMOUNT": _fmt(-sign * cis_deduction),
                "ENTRYDESCRIPTION": f"CIS deduction{(' @ ' + _fmt(cis.get('deduction_rate')) + '%') if cis.get('deduction_rate') else ''}",
                "LOCATIONID": location_id, "DEPARTMENTID": department_id, "PROJECTID": project_id,
                "TAXENTRIES": [], "_line_no": len(items) + 1, "_category": "cis", "_vat_rate": "", "_tax_label": "",
                "_quantity": None, "_unit": None, "_unit_price": None,
            })
        elif cis_deduction:
            issues.append(f"CIS deduction of {currency} {_fmt(cis_deduction)} shown. Set a CIS control account in Settings or post it by hand.")
        else:
            notes.append("Subcontractor invoice - confirm the CIS status and deduction rate before payment.")

    retention = rec.get("retention") or {}
    retention_amount = _money(retention.get("amount"))
    if retention_amount:
        if settings.get("sage_retention_gl"):
            items.append({
                "ACCOUNTNO": settings["sage_retention_gl"],
                "TRX_AMOUNT": _fmt(-sign * retention_amount),
                "ENTRYDESCRIPTION": f"Retention withheld{(' @ ' + _fmt(retention.get('percentage')) + '%') if retention.get('percentage') else ''}",
                "LOCATIONID": location_id, "DEPARTMENTID": department_id, "PROJECTID": project_id,
                "TAXENTRIES": [], "_line_no": len(items) + 1, "_category": "retention", "_vat_rate": "", "_tax_label": "",
                "_quantity": None, "_unit": None, "_unit_price": None,
            })
        else:
            issues.append(f"Retention of {currency} {_fmt(retention_amount)} withheld. Set a retention account in Settings or post it by hand.")

    if reverse_charge:
        notes.append("Domestic reverse charge - no VAT is paid to the supplier; Sage posts the input and output VAT.")
    if not rec.get("totals_reconcile", True):
        issues.append("Totals on the document do not add up - check the lines against the PDF.")
    if not rec.get("po_number"):
        notes.append("No purchase order number on the invoice.")
    if doc_type not in ("invoice", "credit_note"):
        issues.append(f"Document looks like a {doc_type.replace('_', ' ')}, not an invoice.")
    for flag in rec.get("flags") or []:
        notes.append(flag)

    description = (overrides.get("description") or rec.get("description") or f"{supplier_name} {rec.get('invoice_number') or ''}").strip()[:200]
    po_number = overrides.get("po_number") or rec.get("po_number") or ""
    record_id = (overrides.get("invoice_number") or rec.get("invoice_number") or "")[:30]

    object_name = "APADJUSTMENT" if is_credit else "APBILL"
    bill = {
        "_object": object_name,
        "WHENCREATED": invoice_date,
        "WHENPOSTED": invoice_date,
        "WHENDUE": due_date,
        "VENDORID": vendor_id,
        "RECORDID": record_id,
        "DOCNUMBER": po_number,
        "DESCRIPTION": description,
        "TERMNAME": term_name,
        "BASECURR": settings.get("default_currency") or "GBP",
        "CURRENCY": currency,
        "ACTION": settings.get("sage_action") or "Draft",
        "TAXSOLUTIONID": settings.get("sage_tax_solution_id") or "",
        "ITEMS": items,
    }
    if currency != bill["BASECURR"]:
        bill["EXCH_RATE_DATE"] = invoice_date
        bill["EXCH_RATE_TYPE_ID"] = "Intacct Daily Rate"
        notes.append(f"Foreign currency ({currency}) - Intacct will apply its daily rate on {invoice_date}.")
    if is_credit:
        notes.append("Credit note - mapped to an AP adjustment with negative amounts.")

    net_total = _money(rec.get("net_total"))
    vat_total = 0.0 if reverse_charge else _money(rec.get("vat_total"))
    gross_total = _money(rec.get("gross_total")) if not reverse_charge else net_total
    amount_due = _money(rec.get("amount_due")) or round(gross_total - cis_deduction - retention_amount, 2)

    entry_sheet = {
        "header": [
            ("Vendor", vendor_id or "(not mapped)", supplier_name),
            ("Bill date", invoice_date, ""),
            ("Due date", due_date, term_name and f"terms: {term_name}"),
            ("Bill number", record_id, "supplier's invoice number"),
            ("Reference / PO", po_number, rec.get("order_reference") or ""),
            ("Description", description, ""),
            ("Currency", currency, ""),
            ("Tax solution", bill["TAXSOLUTIONID"], "reverse charge" if reverse_charge else (rec.get("vat_treatment") or "")),
            ("Project", project_id or "(none)", rec.get("project_reference") or ""),
            ("Location / department", " / ".join(x for x in (location_id, department_id) if x) or "(default)", ""),
        ],
        "totals": [
            ("Net", _fmt(net_total)),
            ("VAT", _fmt(vat_total)),
            ("Gross", _fmt(gross_total)),
        ] + ([("CIS deduction", "-" + _fmt(cis_deduction))] if cis_deduction else [])
          + ([("Retention", "-" + _fmt(retention_amount))] if retention_amount else [])
          + ([("Amount payable", _fmt(amount_due))] if (cis_deduction or retention_amount or _money(rec.get("amount_due"))) else []),
        "payment": [
            ("Account name", (rec.get("bank_details") or {}).get("account_name") or ""),
            ("Sort code", (rec.get("bank_details") or {}).get("sort_code") or ""),
            ("Account number", (rec.get("bank_details") or {}).get("account_number") or ""),
            ("IBAN", (rec.get("bank_details") or {}).get("iban") or ""),
            ("Supplier VAT no", supplier.get("vat_number") or ""),
        ],
    }

    log_row = {
        "Date received": (email_meta.get("received_at") or "")[:10],
        "Supplier": supplier_name,
        "Sage vendor ID": vendor_id,
        "Invoice no": record_id,
        "Type": doc_type.replace("_", " "),
        "Invoice date": invoice_date,
        "Due date": due_date,
        "PO no": po_number,
        "Project": project_id or (rec.get("project_reference") or ""),
        "Description": description,
        "Currency": currency,
        "Net": _fmt(net_total),
        "VAT": _fmt(vat_total),
        "Gross": _fmt(gross_total),
        "VAT treatment": rec.get("vat_treatment") or "",
        "CIS deduction": _fmt(cis_deduction) if cis_deduction else "",
        "Retention": _fmt(retention_amount) if retention_amount else "",
        "Amount payable": _fmt(amount_due),
        "Supplier VAT no": supplier.get("vat_number") or "",
        "Email from": email_meta.get("from") or "",
        "Email subject": email_meta.get("subject") or "",
        "Attachment": email_meta.get("attachment_name") or "",
        "Confidence": f"{float(rec.get('confidence') or 0):.2f}",
        "Checks": "; ".join(issues + notes),
    }

    return {
        "bill": bill,
        "issues": issues,
        "notes": notes,
        "entry_sheet": entry_sheet,
        "log_row": log_row,
        "matched": {"vendor_key": matched_vendor_key, "project_key": matched_project_key},
    }


# --------------------------------------------------------------------------- XML


def _tag(name: str, value) -> str:
    return f"<{name}>{escape(str(value))}</{name}>"


def bill_to_xml(bill: dict, indent: str = "    ") -> str:
    """The <create> function body for Intacct's XML gateway (no control/authentication wrapper)."""
    obj = bill.get("_object", "APBILL")
    items_tag = "APADJUSTMENTITEMS" if obj == "APADJUSTMENT" else "APBILLITEMS"
    item_tag = "APADJUSTMENTITEM" if obj == "APADJUSTMENT" else "APBILLITEM"

    header_order = ["WHENCREATED", "WHENPOSTED", "WHENDUE", "VENDORID", "RECORDID", "DOCNUMBER", "DESCRIPTION",
                    "TERMNAME", "BASECURR", "CURRENCY", "EXCH_RATE_DATE", "EXCH_RATE_TYPE_ID", "ACTION", "TAXSOLUTIONID"]
    item_order = ["ACCOUNTNO", "TRX_AMOUNT", "ENTRYDESCRIPTION", "LOCATIONID", "DEPARTMENTID", "PROJECTID"]

    out = [f"<create>", f"{indent}<{obj}>"]
    for key in header_order:
        value = bill.get(key)
        if value not in (None, ""):
            out.append(f"{indent * 2}{_tag(key, value)}")
    out.append(f"{indent * 2}<{items_tag}>")
    for item in bill.get("ITEMS", []):
        out.append(f"{indent * 3}<{item_tag}>")
        for key in item_order:
            value = item.get(key)
            if value not in (None, ""):
                out.append(f"{indent * 4}{_tag(key, value)}")
        taxes = [t for t in item.get("TAXENTRIES", []) if t.get("DETAILID")]
        if taxes:
            out.append(f"{indent * 4}<TAXENTRIES>")
            for tax in taxes:
                out.append(f"{indent * 5}<TAXENTRY>")
                out.append(f"{indent * 6}{_tag('DETAILID', tax['DETAILID'])}")
                out.append(f"{indent * 6}{_tag('TRX_TAX', tax.get('TRX_TAX', '0.00'))}")
                out.append(f"{indent * 5}</TAXENTRY>")
            out.append(f"{indent * 4}</TAXENTRIES>")
        out.append(f"{indent * 3}</{item_tag}>")
    out.append(f"{indent * 2}</{items_tag}>")
    out.append(f"{indent}</{obj}>")
    out.append("</create>")
    return "\n".join(out)


def public_bill(bill: dict) -> dict:
    """The payload without the underscore-prefixed helper keys, for JSON display/download."""
    clean = {k: v for k, v in bill.items() if not k.startswith("_") and k != "ITEMS"}
    items = []
    for item in bill.get("ITEMS", []):
        pub = {k: v for k, v in item.items() if not k.startswith("_")}
        pub["TAXENTRIES"] = [t for t in item.get("TAXENTRIES", []) if t.get("DETAILID")]
        items.append(pub)
    items_key = "APADJUSTMENTITEMS" if bill.get("_object") == "APADJUSTMENT" else "APBILLITEMS"
    return {bill.get("_object", "APBILL"): {**clean, items_key: items}}


LOG_COLUMNS = [
    "Date received", "Supplier", "Sage vendor ID", "Invoice no", "Type", "Invoice date", "Due date", "PO no",
    "Project", "Description", "Currency", "Net", "VAT", "Gross", "VAT treatment", "CIS deduction", "Retention",
    "Amount payable", "Supplier VAT no", "Status", "Sage record no", "Email from", "Email subject", "Attachment",
    "Confidence", "Checks",
]
