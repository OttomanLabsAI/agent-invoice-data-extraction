"""Invoice intake agent - local web app.

Run:  python app.py
Then open http://localhost:8765
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from datetime import datetime

from flask import (
    Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for,
)

from agent import config, extractor, gmail_client, invoice_types, pipeline, prompts, rag, sage_client, sage_mapper, store
from agent.extractor import DOCUMENT_TYPES, LINE_CATEGORIES, VAT_TREATMENTS
from agent.invoice_types import EXPECTED_VAT, TYPE_SIGNALS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

HOST = os.environ.get("INVOICE_AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("INVOICE_AGENT_PORT", "8765"))

app = Flask(__name__)
app.secret_key = config.flask_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024

store.init_db()
pipeline.start_poller(config.load_settings)


@app.context_processor
def inject_globals():
    settings = config.load_settings()
    return {
        "company_name": settings.get("company_name") or "Glent Group",
        "setup_done": config.has_claude(settings) and gmail_client.GOOGLE_TOKEN_PATH.exists(),
        "sage_ready": config.has_sage(settings),
        "status_labels": store.STATUS_LABELS,
    }


@app.template_filter("money")
def money_filter(value):
    try:
        return f"{float(value):,.2f}"
    except Exception:  # noqa: BLE001 - None, blanks and Jinja Undefined all render as a dash
        return "-"


@app.template_filter("model_label")
def model_label_filter(value):
    return config.model_label(value)


@app.template_filter("shortdate")
def shortdate_filter(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%d %b %Y")
    except Exception:  # noqa: BLE001
        return str(value)[:10]


# --------------------------------------------------------------------------- Dashboard


@app.route("/")
def dashboard():
    status = request.args.get("status") or None
    settings = config.load_settings()
    return render_template(
        "dashboard.html",
        invoices=store.list_invoices(status),
        counts=store.status_counts(),
        active_status=status,
        last_run=store.last_run("inbox"),
        gmail_connected=gmail_client.GOOGLE_TOKEN_PATH.exists(),
        claude_ready=config.has_claude(settings),
    )


@app.post("/run")
def run_now():
    settings = config.load_settings()
    result = pipeline.run_once(settings)
    flash(result["message"], "ok" if result["ok"] else "error")
    return redirect(url_for("dashboard"))


@app.post("/upload")
def upload():
    settings = config.load_settings()
    if not config.has_claude(settings):
        flash("Add your Claude API key in Settings before processing invoices.", "error")
        return redirect(url_for("settings_page"))
    files = request.files.getlist("files")
    if not files or all(not f.filename for f in files):
        flash("Choose at least one PDF or image.", "error")
        return redirect(url_for("dashboard"))
    done = 0
    for f in files:
        if not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        mime = gmail_client.EXT_TO_MIME.get(ext)
        if not mime:
            flash(f"Skipped {f.filename}: only PDF, PNG, JPG and WEBP are supported.", "error")
            continue
        data = f.read()
        meta = {"from": "manual upload", "subject": f.filename, "received_at": store.now_iso()}
        pipeline.process_attachment(settings, data, f.filename, mime, email_meta=meta, source="upload")
        done += 1
    if done:
        flash(f"Processed {done} file(s).", "ok")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------- Invoice review


def _get_or_404(invoice_id: int) -> dict:
    invoice = store.get_invoice(invoice_id)
    if invoice is None:
        abort(404)
    return invoice


@app.route("/invoice/<int:invoice_id>")
def invoice_detail(invoice_id: int):
    invoice = _get_or_404(invoice_id)
    settings = config.load_settings()
    sage = invoice.get("sage") or {}
    bill = sage.get("bill") or {}
    overrides = (invoice.get("extracted") or {}).get("sage_overrides") or {}
    items = bill.get("ITEMS") or []
    return render_template(
        "invoice.html",
        inv=invoice,
        rec=invoice.get("extracted") or {},
        sage=sage,
        vendor_id=overrides.get("vendor_id") or bill.get("VENDORID") or "",
        project_id=overrides.get("project_id") or (items[0].get("PROJECTID") if items else "") or "",
        bill_json=json.dumps(sage_mapper.public_bill(bill), indent=2) if bill else "",
        bill_xml=sage_mapper.bill_to_xml(bill) if bill else "",
        overrides=overrides,
        categories=LINE_CATEGORIES,
        vat_treatments=VAT_TREATMENTS,
        document_types=DOCUMENT_TYPES,
        invoice_types=settings.get("invoice_types") or [],
        chosen_type=overrides.get("invoice_type") or "",
    )


def _float_or_none(value: str):
    value = (value or "").strip().replace(",", "")
    if value == "":
        return None
    try:
        return round(float(value), 2)
    except ValueError:
        return None


@app.post("/invoice/<int:invoice_id>")
def invoice_save(invoice_id: int):
    invoice = _get_or_404(invoice_id)
    rec = dict(invoice.get("extracted") or {})
    form = request.form

    rec["document_type"] = form.get("document_type") or rec.get("document_type") or "invoice"
    rec["supplier"] = dict(rec.get("supplier") or {})
    rec["supplier"]["name"] = form.get("supplier_name", "").strip()
    rec["supplier"]["vat_number"] = form.get("supplier_vat", "").strip() or None
    rec["invoice_number"] = form.get("invoice_number", "").strip()
    rec["invoice_date"] = form.get("invoice_date", "").strip() or None
    rec["due_date"] = form.get("due_date", "").strip() or None
    rec["po_number"] = form.get("po_number", "").strip() or None
    rec["project_reference"] = form.get("project_reference", "").strip() or None
    rec["description"] = form.get("description", "").strip() or None
    rec["currency"] = (form.get("currency", "GBP").strip() or "GBP").upper()
    rec["vat_treatment"] = form.get("vat_treatment") or "unknown"

    def col(name: str, i: int) -> str:
        values = form.getlist(name)
        return values[i] if i < len(values) else ""

    lines = []
    for i, desc in enumerate(form.getlist("line_desc")):
        if not desc.strip() and not col("line_net", i).strip():
            continue
        lines.append({
            "description": desc.strip(),
            "quantity": _float_or_none(col("line_qty", i)),
            "unit": col("line_unit", i).strip() or None,
            "unit_price": _float_or_none(col("line_unit_price", i)),
            "net_amount": _float_or_none(col("line_net", i)),
            "vat_rate": _float_or_none(col("line_vat_rate", i)),
            "vat_amount": _float_or_none(col("line_vat", i)),
            "category": col("line_category", i) or "other",
        })
    rec["line_items"] = lines
    rec["net_total"] = _float_or_none(form.get("net_total"))
    rec["vat_total"] = _float_or_none(form.get("vat_total"))
    rec["gross_total"] = _float_or_none(form.get("gross_total"))

    cis = dict(rec.get("cis") or {})
    cis["applicable"] = form.get("cis_applicable") == "on"
    cis["deduction_rate"] = _float_or_none(form.get("cis_rate"))
    cis["deduction_amount"] = _float_or_none(form.get("cis_amount"))
    rec["cis"] = cis
    retention = dict(rec.get("retention") or {})
    retention["applicable"] = bool(_float_or_none(form.get("retention_amount")))
    retention["percentage"] = _float_or_none(form.get("retention_pct"))
    retention["amount"] = _float_or_none(form.get("retention_amount"))
    rec["retention"] = retention

    rec = extractor.normalise(rec)
    overrides = {
        "vendor_id": form.get("vendor_id", "").strip(),
        "project_id": form.get("project_id", "").strip(),
        "invoice_type": form.get("invoice_type", "").strip(),
    }
    settings = config.load_settings()
    kind, _ = invoice_types.resolve_type(rec, settings, overrides)
    rec, _ = invoice_types.apply_type_defaults(rec, kind)
    rec["sage_overrides"] = overrides

    mapped = pipeline.remap({**invoice, "extracted": rec}, settings, overrides=overrides)
    store.update_invoice(invoice_id, extracted=rec, sage=mapped, issues=mapped["issues"], notes=form.get("notes", ""))
    flash("Saved. Sage payload rebuilt.", "ok")
    return redirect(url_for("invoice_detail", invoice_id=invoice_id))


@app.post("/invoice/<int:invoice_id>/status")
def invoice_status(invoice_id: int):
    invoice = _get_or_404(invoice_id)
    action = request.form.get("action")
    if action == "delete":
        store.delete_invoice(invoice_id)
        flash("Deleted.", "ok")
        return redirect(url_for("dashboard"))
    new_status = {"approve": "approved", "query": "queried", "reopen": "review", "posted": "posted"}.get(action)
    if new_status is None:
        abort(400)
    if action == "approve" and (invoice.get("issues") or []):
        flash("Fix the outstanding checks before approving.", "error")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))
    store.update_invoice(invoice_id, status=new_status)
    flash(f"Marked as {store.STATUS_LABELS[new_status].lower()}.", "ok")
    return redirect(url_for("invoice_detail", invoice_id=invoice_id))


@app.post("/invoice/<int:invoice_id>/remap")
def invoice_remap(invoice_id: int):
    invoice = _get_or_404(invoice_id)
    if not invoice.get("extracted"):
        abort(400)
    settings = config.load_settings()
    overrides = (invoice["extracted"] or {}).get("sage_overrides") or {}
    mapped = pipeline.remap(invoice, settings, overrides=overrides)
    store.update_invoice(invoice_id, sage=mapped, issues=mapped["issues"])
    flash("Sage payload rebuilt with the current settings.", "ok")
    return redirect(url_for("invoice_detail", invoice_id=invoice_id))


@app.post("/invoice/<int:invoice_id>/push")
def invoice_push(invoice_id: int):
    invoice = _get_or_404(invoice_id)
    settings = config.load_settings()
    if not config.has_sage(settings):
        flash("Fill in the Sage Intacct connection in Settings first.", "error")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))
    if invoice.get("status") != "approved":
        flash("Approve the invoice before posting it to Sage.", "error")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))
    bill = (invoice.get("sage") or {}).get("bill")
    if not bill:
        flash("Nothing to post - no Sage payload on this invoice.", "error")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))
    try:
        result = sage_client.post_bill(settings, bill)
    except (sage_client.SageError, Exception) as exc:  # noqa: BLE001
        flash(f"Sage did not accept the bill: {exc}", "error")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))
    store.update_invoice(invoice_id, status="posted", intacct_recordno=result.get("recordno", ""))
    flash(f"Posted to Sage as {result['object']} record {result.get('recordno') or '(no record number returned)'}.", "ok")
    return redirect(url_for("invoice_detail", invoice_id=invoice_id))


@app.route("/invoice/<int:invoice_id>/file")
def invoice_file(invoice_id: int):
    invoice = _get_or_404(invoice_id)
    path = invoice.get("attachment_path")
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype=invoice.get("mime_type") or "application/octet-stream", as_attachment=False,
                     download_name=invoice.get("attachment_name") or "attachment")


@app.route("/invoice/<int:invoice_id>/sage.json")
def invoice_sage_json(invoice_id: int):
    invoice = _get_or_404(invoice_id)
    bill = (invoice.get("sage") or {}).get("bill") or {}
    payload = json.dumps(sage_mapper.public_bill(bill), indent=2)
    return Response(payload, mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename=invoice-{invoice_id}-intacct.json"})


@app.route("/invoice/<int:invoice_id>/sage.xml")
def invoice_sage_xml(invoice_id: int):
    invoice = _get_or_404(invoice_id)
    bill = (invoice.get("sage") or {}).get("bill") or {}
    return Response(sage_mapper.bill_to_xml(bill), mimetype="application/xml",
                    headers={"Content-Disposition": f"attachment; filename=invoice-{invoice_id}-intacct.xml"})


@app.route("/export.csv")
def export_csv():
    status = request.args.get("status") or None
    rows = store.list_invoices(status)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=sage_mapper.LOG_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for inv in rows:
        log_row = dict(((inv.get("sage") or {}).get("log_row")) or {})
        if not log_row:
            continue
        log_row["Status"] = inv.get("status_label")
        log_row["Sage record no"] = inv.get("intacct_recordno") or ""
        writer.writerow(log_row)
    name = f"invoice-log-{status or 'all'}-{datetime.now():%Y%m%d}.csv"
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


# --------------------------------------------------------------------------- Settings


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    settings = config.load_settings()
    if request.method == "POST":
        form = request.form
        for key in (
            "anthropic_api_key", "google_client_id", "google_client_secret", "company_name", "default_currency", "sage_endpoint",
            "sage_sender_id", "sage_sender_password", "sage_company_id", "sage_user_id", "sage_user_password",
            "sage_action", "sage_location_id", "sage_department_id", "sage_tax_solution_id", "sage_default_gl",
            "sage_cis_gl", "sage_retention_gl",
        ):
            if key in form:
                settings[key] = form.get(key, "").strip()
        if "gl_labour" in form:
            settings["gl_map"] = {cat: form.get(f"gl_{cat}", "").strip() for cat in LINE_CATEGORIES}
        if "vat_20" in form:
            settings["vat_detail_map"] = {
                "20": form.get("vat_20", "").strip(),
                "5": form.get("vat_5", "").strip(),
                "0": form.get("vat_0", "").strip(),
                "reverse_charge": form.get("vat_rc", "").strip(),
            }
        for key in ("vendor_map", "project_map", "terms_map"):
            if key in form:
                settings[key] = config.parse_map(form.get(key, ""))
        config.save_settings(settings)
        if form.get("action") == "connect_gmail":
            return redirect(url_for("gmail_connect"))
        flash("Settings saved.", "ok")
        return redirect(url_for("settings_page"))

    return render_template(
        "settings.html",
        s=settings,
        models=config.CLAUDE_MODELS,
        gmail=gmail_client.connection_status() if gmail_client.GOOGLE_TOKEN_PATH.exists() else {"connected": False},
        categories=LINE_CATEGORIES,
        generic_codes=config.GENERIC_CODES,
        vendor_map_text=config.format_map(settings.get("vendor_map")),
        project_map_text=config.format_map(settings.get("project_map")),
        terms_map_text=config.format_map(settings.get("terms_map")),
        redirect_uri=url_for("gmail_callback", _external=True),
    )


@app.post("/settings/test/claude")
def test_claude():
    key = (request.json or {}).get("key", "").strip()
    if not key:
        return jsonify(ok=False, message="Paste a key first.")
    ok, message = extractor.check_api_key(key)
    return jsonify(ok=ok, message=message)


@app.post("/settings/test/sage")
def test_sage():
    body = request.json or {}
    probe = {
        "sage_endpoint": body.get("endpoint") or sage_client.DEFAULT_ENDPOINT,
        "sage_sender_id": body.get("sender_id", ""),
        "sage_sender_password": body.get("sender_password", ""),
        "sage_company_id": body.get("company_id", ""),
        "sage_user_id": body.get("user_id", ""),
        "sage_user_password": body.get("user_password", ""),
    }
    if not all(probe[k] for k in ("sage_sender_id", "sage_sender_password", "sage_company_id", "sage_user_id", "sage_user_password")):
        return jsonify(ok=False, message="Fill in all five Intacct fields first.")
    ok, message = sage_client.test_connection(probe)
    return jsonify(ok=ok, message=message)


# --------------------------------------------------------------------------- Agents


def _int_field(form, key: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(form.get(key, default) or 0))
    except ValueError:
        return default


# The sample invoice email, used on the agent tabs to show the note that goes with each document.
SAMPLE_EMAIL = {
    "from": "accounts@northbankmech.co.uk", "to": "(the connected mailbox)", "subject": "Invoice NMS-2026-0417 - HEL18 Hall 2",
    "date": "Mon, 8 Sep 2026 09:00:00 +0000", "attachment_name": "NMS-2026-0417.pdf",
    "body_text": "Please find attached our application for payment no. 7 for August.",
}
SAMPLE_ATTACHMENTS = [{"filename": "NMS-2026-0417.pdf", "mime_type": "application/pdf", "size": 48 * 1024}]


def _gmail_status() -> dict:
    if not gmail_client.GOOGLE_TOKEN_PATH.exists():
        return {"connected": False, "email": None, "error": None}
    return gmail_client.connection_status()


@app.route("/agents")
def agent_tree():
    settings = config.load_settings()
    return render_template(
        "agent_tree.html",
        s=settings,
        gmail=_gmail_status(),
        claude_ready=config.has_claude(settings),
        sage_ready=config.has_sage(settings),
        classify_run=store.last_run("classify"),
        extract_run=store.last_run("extract"),
        class_counts=store.classification_counts(),
        invoice_counts=store.status_counts(),
    )


@app.route("/agents/classification", methods=["GET", "POST"])
def agent_classification():
    settings = config.load_settings()
    if request.method == "POST":
        form = request.form
        for key in ("classify_model", "classify_system_prompt", "classify_reference_text", "classify_invoice_label", "classify_other_label", "gmail_query", "gmail_allowed_senders"):
            if key in form:
                settings[key] = prompts.clean(form.get(key))
        if "gmail_max_messages" in form:
            settings["gmail_max_messages"] = _int_field(form, "gmail_max_messages", 10, minimum=1)
        if not settings["classify_invoice_label"]:
            settings["classify_invoice_label"] = "Invoice Incoming"
        emptied = "classify_system_prompt" in form and not settings["classify_system_prompt"]
        if form.get("action") == "reset_prompt" or emptied:
            settings["classify_system_prompt"] = prompts.CLASSIFY_SYSTEM
        config.save_settings(settings)
        if form.get("action") == "run":
            result = pipeline.classify_run(settings)
            flash(result["message"], "ok" if result["ok"] else "error")
        elif form.get("action") == "reset_prompt":
            flash("Instructions restored to the default.", "ok")
            return redirect(url_for("agent_classification") + "#prompt")
        elif emptied:
            flash("Saved. The instructions cannot be empty, so the default text was put back.", "error")
            return redirect(url_for("agent_classification") + "#prompt")
        else:
            flash("Classification agent saved.", "ok")
        return redirect(url_for("agent_classification"))
    return render_template(
        "agent_classification.html",
        s=settings,
        models=config.CLAUDE_MODELS,
        connected=gmail_client.GOOGLE_TOKEN_PATH.exists(),
        claude_ready=config.has_claude(settings),
        recent=store.list_classifications(50),
        counts=store.classification_counts(),
        last_run=store.last_run("classify"),
        rag_info=rag.describe(settings.get("classify_reference_text")),
        prompt_is_default=prompts.is_default(settings.get("classify_system_prompt"), prompts.CLASSIFY_SYSTEM),
        placeholders=prompts.PLACEHOLDERS,
        preview_system=extractor.classification_system_prompt(
            settings.get("company_name") or "Glent Group", settings.get("classify_reference_text") or "", settings.get("classify_system_prompt") or ""),
        preview_note=extractor.classification_context(SAMPLE_EMAIL, SAMPLE_ATTACHMENTS),
        tool=extractor.CLASSIFY_TOOL,
    )


def _schema_rows(schema: dict, prefix: str = "") -> list[tuple]:
    rows: list[tuple] = []
    for name, definition in (schema.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        items = definition.get("items") if isinstance(definition.get("items"), dict) else {}
        if definition.get("type") == "object" and definition.get("properties"):
            rows.append((path, definition.get("description", ""), True))
            rows += _schema_rows(definition, path)
        elif definition.get("type") == "array" and items.get("properties"):
            rows.append((path + "[]", definition.get("description", ""), True))
            rows += _schema_rows(items, path + "[]")
        else:
            kind = definition.get("type")
            kind = "/".join(t for t in kind if t != "null") if isinstance(kind, list) else (kind or "")
            choice = f" one of: {', '.join(definition['enum'])}" if definition.get("enum") else ""
            rows.append((path, (f"{definition.get('description', '')}{choice}").strip() or kind, False))
    return rows


@app.route("/agents/extraction", methods=["GET", "POST"])
def agent_extraction():
    settings = config.load_settings()
    if request.method == "POST":
        form = request.form
        for key in ("extract_model", "extract_system_prompt", "extract_reference_text", "gmail_processed_label"):
            if key in form:
                settings[key] = prompts.clean(form.get(key))
        if "poll_minutes" in form:
            settings["poll_minutes"] = _int_field(form, "poll_minutes", 0)
        emptied = "extract_system_prompt" in form and not settings["extract_system_prompt"]
        if form.get("action") == "reset_prompt" or emptied:
            settings["extract_system_prompt"] = prompts.EXTRACT_SYSTEM
        config.save_settings(settings)
        if form.get("action") == "run":
            result = pipeline.extract_run(settings)
            flash(result["message"], "ok" if result["ok"] else "error")
        elif form.get("action") == "reset_prompt":
            flash("Instructions restored to the default.", "ok")
            return redirect(url_for("agent_extraction") + "#prompt")
        elif emptied:
            flash("Saved. The instructions cannot be empty, so the default text was put back.", "error")
            return redirect(url_for("agent_extraction") + "#prompt")
        else:
            flash("Extraction agent saved.", "ok")
        return redirect(url_for("agent_extraction"))
    return render_template(
        "agent_extraction.html",
        s=settings,
        models=config.CLAUDE_MODELS,
        connected=gmail_client.GOOGLE_TOKEN_PATH.exists(),
        claude_ready=config.has_claude(settings),
        last_run=store.last_run("extract"),
        counts=store.status_counts(),
        rag_info=rag.describe(settings.get("extract_reference_text")),
        prompt_is_default=prompts.is_default(settings.get("extract_system_prompt"), prompts.EXTRACT_SYSTEM),
        placeholders=prompts.PLACEHOLDERS,
        preview_system=extractor.system_prompt(
            settings.get("company_name") or "Glent Group", settings.get("default_currency") or "GBP",
            settings.get("extract_reference_text") or "", settings.get("extract_system_prompt") or ""),
        preview_note=extractor.email_context(SAMPLE_EMAIL),
        schema_rows=_schema_rows(extractor.INVOICE_TOOL["input_schema"]),
    )


# --------------------------------------------------------------------------- Invoice types


def _read_type(form, t: dict) -> dict:
    f = f"{t['id']}__"
    if f + "name" not in form:
        return t
    return invoice_types.normalise_type({
        "id": t["id"],
        "name": form.get(f + "name", "").strip() or t["name"],
        "description": form.get(f + "description", ""),
        "suppliers": form.get(f + "suppliers", ""),
        "categories": [c for c in LINE_CATEGORIES if f + "cat_" + c in form],
        "signals": [k for k, _ in TYPE_SIGNALS if f + "sig_" + k in form],
        "gl_map": {c: form.get(f + "gl_" + c, "").strip() for c in LINE_CATEGORIES},
        "vat_detail_map": {"20": form.get(f + "vat_20", "").strip(), "5": form.get(f + "vat_5", "").strip(),
                           "0": form.get(f + "vat_0", "").strip(), "reverse_charge": form.get(f + "vat_rc", "").strip()},
        "cis_applies": f + "cis_applies" in form,
        "cis_rate": form.get(f + "cis_rate", ""),
        "retention_percent": form.get(f + "retention_percent", ""),
        "terms_days": form.get(f + "terms_days", ""),
        "po_required": f + "po_required" in form,
        "project_required": f + "project_required" in form,
        "expected_vat": form.get(f + "expected_vat", ""),
        "location_id": form.get(f + "location_id", ""),
        "department_id": form.get(f + "department_id", ""),
        "sage_action": form.get(f + "sage_action", ""),
    })


@app.route("/types", methods=["GET", "POST"])
def invoice_types_page():
    settings = config.load_settings()
    if request.method == "POST":
        form = request.form
        action = form.get("action", "")
        types = [_read_type(form, t) for t in settings.get("invoice_types") or []]
        anchor = ""
        message = "Invoice types saved."
        if action == "add":
            new_id = invoice_types.new_type_id([t["id"] for t in types])
            types.append(invoice_types.normalise_type({"id": new_id, "name": "New type"}))
            anchor = f"#type-{new_id}"
            message = "Type added - name it and save."
        elif action.startswith("remove:"):
            gone = next((t for t in types if t["id"] == action[7:]), None)
            types = [t for t in types if t["id"] != action[7:]]
            if gone:
                message = f"Removed the {gone['name']} type."
        settings["invoice_types"] = types
        config.save_settings(settings)
        flash(message, "ok")
        return redirect(url_for("invoice_types_page") + anchor)
    return render_template(
        "invoice_types.html",
        types=settings.get("invoice_types") or [],
        categories=LINE_CATEGORIES,
        signals=TYPE_SIGNALS,
        expected_vat=EXPECTED_VAT,
    )


# --------------------------------------------------------------------------- Gmail OAuth


@app.route("/gmail/connect")
def gmail_connect():
    settings = config.load_settings()
    if not config.has_google_client(settings):
        flash("Paste the Google OAuth client ID and secret, save, then connect.", "error")
        return redirect(url_for("settings_page"))
    flow = gmail_client.make_flow(settings, redirect_uri=url_for("gmail_callback", _external=True))
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    session["oauth_state"] = state
    session["oauth_code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/oauth/callback")
def gmail_callback():
    settings = config.load_settings()
    if request.args.get("error"):
        flash(f"Google said: {request.args['error']}", "error")
        return redirect(url_for("settings_page"))
    state = session.pop("oauth_state", None)
    code_verifier = session.pop("oauth_code_verifier", None)
    try:
        flow = gmail_client.make_flow(
            settings, redirect_uri=url_for("gmail_callback", _external=True), state=state, code_verifier=code_verifier
        )
        flow.fetch_token(authorization_response=request.url)
        gmail_client.save_credentials(flow.credentials)
    except Exception as exc:  # noqa: BLE001
        flash(f"Gmail connection failed: {exc}", "error")
        return redirect(url_for("settings_page"))
    status = gmail_client.connection_status()
    flash(f"Gmail connected as {status.get('email') or 'unknown account'}.", "ok")
    return redirect(url_for("settings_page"))


@app.post("/gmail/disconnect")
def gmail_disconnect():
    gmail_client.clear_credentials()
    flash("Gmail disconnected. The stored token was deleted.", "ok")
    return redirect(url_for("settings_page"))


if __name__ == "__main__":
    print(f"\n  Invoice agent running at http://localhost:{PORT}\n")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
