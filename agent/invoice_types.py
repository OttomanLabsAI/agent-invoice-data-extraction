"""Invoice types: different kinds of invoice need different Sage coding and
different checks. A type carries GL and VAT overrides (blank = the Sage coding
in Settings), CIS and retention defaults, payment terms, and PO / project
rules. Every invoice is matched to one type; the review page can override it.
"""

from __future__ import annotations

import random
import re
import string

from .match import name_listed

LINE_CATEGORIES = ["labour", "materials", "plant", "subcontract", "services", "expenses", "other"]

TYPE_SIGNALS = [
    ("cis", "CIS shown on the invoice"),
    ("reverse_charge", "Domestic reverse charge"),
    ("application_for_payment", "Application for payment"),
]
SIGNAL_HOW = {
    "cis": "CIS is shown on the invoice",
    "reverse_charge": "the invoice is under the domestic reverse charge",
    "application_for_payment": "it is an application for payment",
}
EXPECTED_VAT = [
    ("", "Anything"),
    ("standard", "Standard VAT on goods or services"),
    ("reverse_charge", "Domestic reverse charge"),
]

SERVICES_VAT = {
    "20": "UK Purchase Services Standard Rate",
    "5": "UK Purchase Services Reduced Rate",
    "0": "UK Purchase Services Zero Rate",
    "reverse_charge": "UK Purchase Services Reverse Charge Standard Rate",
}


def _blank_gl() -> dict:
    return {c: "" for c in LINE_CATEGORIES}


def _blank_vat() -> dict:
    return {"20": "", "5": "", "0": "", "reverse_charge": ""}


def _all_gl(code: str) -> dict:
    return {c: code for c in LINE_CATEGORIES}


_BASE = {
    "suppliers": "", "categories": [], "signals": [], "gl_map": _blank_gl(), "vat_detail_map": _blank_vat(),
    "cis_applies": False, "cis_rate": 0.0, "retention_percent": 0.0, "terms_days": 30, "po_required": False,
    "project_required": False, "expected_vat": "", "location_id": "", "department_id": "", "sage_action": "",
}

# Generic types for a UK contractor. Every code is a starting point to check against the company's Intacct.
DEFAULT_TYPES = [
    {
        **_BASE, "id": "subcontractor", "name": "Subcontractor",
        "description": "Labour-only and supply-and-fit subcontractors, including applications for payment. The whole invoice is coded to the subcontractor cost account; CIS and the domestic reverse charge usually apply and retention may be held.",
        "categories": ["labour", "subcontract"], "signals": ["cis", "reverse_charge", "application_for_payment"],
        "gl_map": _all_gl("6002"), "vat_detail_map": dict(SERVICES_VAT),
        "cis_applies": True, "cis_rate": 20.0, "project_required": True, "expected_vat": "reverse_charge",
    },
    {
        **_BASE, "id": "materials", "name": "Materials supplier",
        "description": "Builders' merchants and suppliers delivering goods to site. Standard VAT on goods, no CIS, and a purchase order is expected.",
        "categories": ["materials"], "gl_map": {**_blank_gl(), "materials": "5000", "expenses": "5100"},
        "po_required": True, "project_required": True, "expected_vat": "standard",
    },
    {
        **_BASE, "id": "plant", "name": "Plant hire",
        "description": "Hire of plant and equipment, with or without an operator. Operated hire is a construction service and may be reverse charged.",
        "categories": ["plant"], "gl_map": {**_blank_gl(), "plant": "7700", "labour": "7700"}, "vat_detail_map": dict(SERVICES_VAT),
        "po_required": True, "project_required": True,
    },
    {
        **_BASE, "id": "professional", "name": "Professional services",
        "description": "Consultants, designers, surveyors, testing and commissioning. Standard VAT on services, no CIS.",
        "categories": ["services"], "gl_map": {**_blank_gl(), "services": "7603", "labour": "7603"}, "vat_detail_map": dict(SERVICES_VAT),
        "expected_vat": "standard",
    },
    {
        **_BASE, "id": "overheads", "name": "Overheads",
        "description": "Utilities, office, travel, insurance and everything else that is not a project cost. The fallback when nothing else matches.",
        "categories": ["expenses", "other"],
    },
]


def slug(text: str | None) -> str:
    return (re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40]) or "type"


def new_type_id(existing: list[str]) -> str:
    while True:
        candidate = "type_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        if candidate not in existing:
            return candidate


def _num(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def normalise_type(raw: dict | None, index: int = 0) -> dict:
    """Fill in any missing fields so older stored types keep working."""
    raw = raw or {}
    type_id = slug(raw.get("id") or raw.get("name") or f"type_{index}")
    valid = {k for k, _ in TYPE_SIGNALS}
    terms = raw.get("terms_days")
    terms_days = None if terms in ("", None) else max(0, int(round(_num(terms, 0))))
    return {
        "id": type_id,
        "name": (str(raw.get("name") or type_id)).strip() or type_id,
        "description": str(raw.get("description") or "").strip(),
        "suppliers": str(raw.get("suppliers") or "").strip(),
        "categories": [c for c in (raw.get("categories") or []) if c in LINE_CATEGORIES],
        "signals": [s for s in (raw.get("signals") or []) if s in valid],
        "gl_map": {**_blank_gl(), **(raw.get("gl_map") or {})},
        "vat_detail_map": {**_blank_vat(), **(raw.get("vat_detail_map") or {})},
        "cis_applies": bool(raw.get("cis_applies")),
        "cis_rate": max(0.0, _num(raw.get("cis_rate"), 0.0)),
        "retention_percent": max(0.0, _num(raw.get("retention_percent"), 0.0)),
        "terms_days": terms_days,
        "po_required": bool(raw.get("po_required")),
        "project_required": bool(raw.get("project_required")),
        "expected_vat": raw.get("expected_vat") if raw.get("expected_vat") in ("standard", "reverse_charge") else "",
        "location_id": str(raw.get("location_id") or "").strip(),
        "department_id": str(raw.get("department_id") or "").strip(),
        "sage_action": raw.get("sage_action") if raw.get("sage_action") in ("Draft", "Submit") else "",
    }


def type_by_id(settings: dict, type_id: str | None) -> dict | None:
    for t in settings.get("invoice_types") or []:
        if t.get("id") == type_id:
            return t
    return None


def _money(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def resolve_type(rec: dict, settings: dict, overrides: dict | None = None) -> tuple[dict | None, str]:
    """Which type an invoice is: chosen by hand, then by supplier, then by CIS / reverse charge / AFP, then by what the lines mostly are."""
    types = settings.get("invoice_types") or []
    if not types:
        return None, ""
    overrides = overrides or {}
    chosen = type_by_id(settings, overrides.get("invoice_type"))
    if chosen:
        return chosen, "chosen on the invoice"
    supplier_name = (rec.get("supplier") or {}).get("name") or ""
    for t in types:
        names = [n.strip() for n in re.split(r"\r?\n|,", t.get("suppliers") or "") if n.strip()]
        if names and supplier_name and name_listed(supplier_name, names):
            return t, f"the supplier is listed under {t['name']}"
    cis = rec.get("cis") or {}
    present = set()
    if cis.get("applicable") or _money(cis.get("deduction_amount")):
        present.add("cis")
    if rec.get("vat_treatment") == "reverse_charge":
        present.add("reverse_charge")
    if rec.get("document_type") == "application_for_payment":
        present.add("application_for_payment")
    for t in types:
        hit = next((s for s in t.get("signals") or [] if s in present), None)
        if hit:
            return t, SIGNAL_HOW[hit]
    totals: dict = {}
    for line in rec.get("line_items") or []:
        category = line.get("category") if line.get("category") in LINE_CATEGORIES else "other"
        totals[category] = totals.get(category, 0.0) + abs(_money(line.get("net_amount")))
    if totals:
        dominant = max(totals.items(), key=lambda kv: kv[1])[0]
        for t in types:
            if dominant in (t.get("categories") or []):
                return t, f"the lines are mostly {dominant}"
    fallback = next((t for t in types if "other" in (t.get("categories") or [])), types[-1])
    return fallback, "nothing else matched"


def effective_coding(settings: dict, kind: dict | None) -> dict:
    """The Sage coding to use for this type: its overrides on top of the company-wide settings."""
    def compact(mapping):
        return {k: v for k, v in (mapping or {}).items() if v}
    kind = kind or {}
    return {
        "gl_map": {**(settings.get("gl_map") or {}), **compact(kind.get("gl_map"))},
        "vat_detail_map": {**(settings.get("vat_detail_map") or {}), **compact(kind.get("vat_detail_map"))},
        "location_id": kind.get("location_id") or settings.get("sage_location_id") or "",
        "department_id": kind.get("department_id") or settings.get("sage_department_id") or "",
        "sage_action": kind.get("sage_action") or settings.get("sage_action") or "Draft",
    }


def _labour_on_lines(rec: dict) -> float:
    return round(sum(_money(l.get("net_amount")) for l in rec.get("line_items") or [] if l.get("category") == "labour"), 2)


def apply_type_defaults(rec: dict, kind: dict | None) -> tuple[dict, list[str]]:
    """Fill blanks on the record from the type's defaults: CIS deduction, retention, payment terms. Only blanks are touched."""
    if not kind:
        return rec, []
    out = dict(rec)
    out["cis"] = dict(rec.get("cis") or {})
    out["retention"] = dict(rec.get("retention") or {})
    out["flags"] = list(rec.get("flags") or [])
    notes: list[str] = []
    if kind.get("cis_applies"):
        rate = _money(out["cis"].get("deduction_rate")) if out["cis"].get("deduction_rate") is not None else float(kind.get("cis_rate") or 0)
        labour = _money(out["cis"].get("labour_amount")) if out["cis"].get("labour_amount") is not None else _labour_on_lines(out)
        if out["cis"].get("deduction_amount") is None and rate > 0 and labour > 0:
            out["cis"]["applicable"] = True
            out["cis"]["deduction_rate"] = rate
            out["cis"]["labour_amount"] = labour
            out["cis"]["deduction_amount"] = round(labour * rate / 100, 2)
            notes.append(
                f"CIS deduction of {out['cis']['deduction_amount']:.2f} computed at {rate:g}% on labour of {labour:.2f} - "
                "not shown on the invoice; check the subcontractor's verification status."
            )
        elif not out["cis"].get("applicable"):
            out["cis"]["applicable"] = True
    pct = float(kind.get("retention_percent") or 0)
    if pct > 0 and out["retention"].get("amount") is None and _money(out.get("net_total")) > 0:
        out["retention"]["applicable"] = True
        out["retention"]["percentage"] = pct
        out["retention"]["amount"] = round(_money(out.get("net_total")) * pct / 100, 2)
        notes.append(f"Retention of {out['retention']['amount']:.2f} computed at {pct:g}% - not shown on the invoice; check the subcontract.")
    if out.get("payment_terms_days") is None and kind.get("terms_days") is not None:
        out["payment_terms_days"] = kind["terms_days"]
        notes.append(f"Payment terms not on the invoice - {kind['terms_days']}-day default for {kind['name']}.")
    for note in notes:
        if note not in out["flags"]:
            out["flags"].append(note)
    return out, notes
