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

from agent import config, extractor, gmail_client, pipeline, sage_client, sage_mapper, store
from agent.extractor import DOCUMENT_TYPES, LINE_CATEGORIES, VAT_TREATMENTS

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
        last_run=store.last_run(),
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
    }
    rec["sage_overrides"] = overrides

    settings = config.load_settings()
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
            "anthropic_api_key", "claude_model", "google_client_id", "google_client_secret", "gmail_query",
            "gmail_allowed_senders", "gmail_processed_label", "company_name", "default_currency", "sage_endpoint",
            "sage_sender_id", "sage_sender_password", "sage_company_id", "sage_user_id", "sage_user_password",
            "sage_action", "sage_location_id", "sage_department_id", "sage_tax_solution_id", "sage_default_gl",
            "sage_cis_gl", "sage_retention_gl",
        ):
            if key in form:
                settings[key] = form.get(key, "").strip()
        for key, default in (("gmail_max_messages", 10), ("poll_minutes", 0)):
            try:
                settings[key] = max(0, int(form.get(key, default) or 0))
            except ValueError:
                settings[key] = default
        settings["gl_map"] = {cat: form.get(f"gl_{cat}", "").strip() for cat in LINE_CATEGORIES}
        settings["vat_detail_map"] = {
            "20": form.get("vat_20", "").strip(),
            "5": form.get("vat_5", "").strip(),
            "0": form.get("vat_0", "").strip(),
            "reverse_charge": form.get("vat_rc", "").strip(),
        }
        settings["vendor_map"] = config.parse_map(form.get("vendor_map", ""))
        settings["project_map"] = config.parse_map(form.get("project_map", ""))
        settings["terms_map"] = config.parse_map(form.get("terms_map", ""))
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
