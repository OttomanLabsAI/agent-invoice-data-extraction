"""The processing loop: two agents over Gmail -> Claude -> Sage mapping -> local store.

Agent - Classification reads new mail and labels each email as an invoice or
not. Agent - Invoice Extraction reads the labelled mail, sends each attachment
to Claude and files the mapped result in the Inbox.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import traceback
from pathlib import Path

from . import config, extractor, gmail_client, invoice_types, sage_mapper, store

log = logging.getLogger("invoice-agent")

_run_lock = threading.Lock()

BUSY = {"ok": False, "message": "A run is already in progress."}


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "attachment")
    return cleaned[:120]


def save_attachment(data: bytes, filename: str, key: str) -> Path:
    config.ensure_dirs()
    path = config.ATTACHMENTS_DIR / f"{key}_{_safe_name(filename)}"
    path.write_bytes(data)
    return path


def process_attachment(
    settings: dict,
    data: bytes,
    filename: str,
    mime_type: str,
    email_meta: dict | None = None,
    source: str = "gmail",
) -> int:
    """Extract, map and store one attachment. Returns the new invoice id."""
    meta = dict(email_meta or {})
    meta["attachment_name"] = filename
    key = meta.get("id") or f"upload-{int(time.time())}"
    path = save_attachment(data, filename, key)

    base_record = {
        "source": source,
        "gmail_message_id": meta.get("id"),
        "gmail_thread_id": meta.get("thread_id"),
        "from_addr": meta.get("from"),
        "subject": meta.get("subject"),
        "received_at": meta.get("received_at"),
        "attachment_name": filename,
        "attachment_path": str(path),
        "mime_type": mime_type,
    }

    try:
        extracted, usage = extractor.extract_invoice(
            api_key=settings["anthropic_api_key"],
            model=settings.get("extract_model") or config.DEFAULT_MODEL,
            attachment=data,
            mime_type=mime_type,
            company_name=settings.get("company_name") or "Glent Group",
            default_currency=settings.get("default_currency") or "GBP",
            email_meta=meta,
            reference_text=settings.get("extract_reference_text") or "",
            system_template=settings.get("extract_system_prompt") or "",
        )
    except Exception as exc:  # noqa: BLE001 - recorded on the row
        log.error("Extraction failed for %s: %s", filename, exc)
        return store.insert_invoice({**base_record, "status": "error", "error": f"{type(exc).__name__}: {exc}"})

    kind, _ = invoice_types.resolve_type(extracted, settings)
    extracted, _ = invoice_types.apply_type_defaults(extracted, kind)
    mapped = sage_mapper.build(extracted, settings, email_meta=meta)
    status = "review"
    if extracted.get("document_type") not in ("invoice", "credit_note"):
        status = "not_invoice"
    return store.insert_invoice({
        **base_record,
        "status": status,
        "extracted": extracted,
        "sage": mapped,
        "issues": mapped["issues"],
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    })


def remap(invoice: dict, settings: dict, overrides: dict | None = None) -> dict:
    """Rebuild the Sage payload for a stored invoice (after edits or settings changes)."""
    meta = {
        "id": invoice.get("gmail_message_id"),
        "from": invoice.get("from_addr"),
        "subject": invoice.get("subject"),
        "received_at": invoice.get("received_at"),
        "attachment_name": invoice.get("attachment_name"),
    }
    return sage_mapper.build(invoice["extracted"], settings, email_meta=meta, overrides=overrides)


def _batch_size(settings: dict, limit: int | None) -> int:
    size = max(1, int(settings.get("gmail_max_messages") or 10))
    return min(size, limit) if limit else size


# --------------------------------------------------------------------------- Agent - Classification


def classify_run(settings: dict, limit: int | None = None) -> dict:
    """Look at new mail and label each email as an invoice or not. Returns a summary dict."""
    summary = {"ok": True, "stage": "classify", "looked": 0, "invoices": 0, "others": 0, "skipped": 0, "errors": 0, "message": ""}
    if not config.has_claude(settings):
        return {**summary, "ok": False, "message": "Add your Claude API key in Settings first."}
    if not _run_lock.acquire(blocking=False):
        return {**summary, **BUSY}
    run_id = store.start_run("classify")
    try:
        box = gmail_client.connect()
        invoice_label = settings.get("classify_invoice_label") or "Invoice Incoming"
        other_label = settings.get("classify_other_label") or ""
        query = settings.get("gmail_query") or "has:attachment is:unread"
        for label in (invoice_label, other_label, settings.get("gmail_processed_label") or ""):
            if label:
                query = f"{query} -label:{gmail_client.search_label(label)}"
        message_ids = box.list(query, _batch_size(settings, limit))

        for msg_id in message_ids:
            try:
                meta = box.fetch(msg_id)
            except Exception as exc:  # noqa: BLE001
                log.error("Could not read message %s: %s", msg_id, exc)
                summary["errors"] += 1
                continue
            summary["looked"] += 1
            base = {
                "gmail_message_id": msg_id, "from_addr": meta.get("from"), "subject": meta.get("subject"),
                "received_at": meta.get("received_at"), "attachments": ", ".join(a["filename"] for a in meta["attachments"]),
            }

            if not gmail_client.sender_allowed(meta.get("from", ""), settings.get("gmail_allowed_senders", "")):
                summary["skipped"] += 1
                applied = ""
                if other_label:
                    box.add_labels(msg_id, [other_label])
                    applied = other_label
                store.record_classification({**base, "verdict": "not_invoice", "document_kind": "sender not allowed", "confidence": 1,
                                             "reason": "Sender is not in the allowed list.", "label_applied": applied})
                continue

            try:
                result = extractor.classify_email(
                    api_key=settings["anthropic_api_key"],
                    model=settings.get("classify_model") or config.DEFAULT_MODEL,
                    email_meta=meta,
                    attachments=meta["attachments"],
                    company_name=settings.get("company_name") or "Glent Group",
                    reference_text=settings.get("classify_reference_text") or "",
                    system_template=settings.get("classify_system_prompt") or "",
                )
            except Exception as exc:  # noqa: BLE001 - left unlabelled so the next run tries again
                log.error("Classification failed for %s: %s", msg_id, exc)
                summary["errors"] += 1
                continue

            label = invoice_label if result["verdict"] == "invoice" else other_label
            applied = ""
            if label:
                try:
                    box.add_labels(msg_id, [label])
                    applied = label
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not label message %s: %s", msg_id, exc)
                    summary["errors"] += 1
            store.record_classification({
                **base, "verdict": result["verdict"], "document_kind": result["document_kind"], "confidence": result["confidence"],
                "reason": result["reason"], "label_applied": applied, "model": result["usage"].get("model", ""),
                "input_tokens": result["usage"].get("input_tokens", 0), "output_tokens": result["usage"].get("output_tokens", 0),
            })
            if result["verdict"] == "invoice":
                summary["invoices"] += 1
            else:
                summary["others"] += 1

        summary["message"] = (
            f"Looked at {summary['looked']} email(s): {summary['invoices']} labelled {invoice_label}, "
            f"{summary['others'] + summary['skipped']} {other_label or 'left unlabelled'}, {summary['errors']} failed."
        )
        store.finish_run(run_id, summary["message"], ok=summary["errors"] == 0)
    except gmail_client.GmailNotConnected as exc:
        summary.update(ok=False, message=str(exc))
        store.finish_run(run_id, summary["message"], ok=False)
    except Exception as exc:  # noqa: BLE001
        log.error("Classification run failed: %s\n%s", exc, traceback.format_exc())
        summary.update(ok=False, message=f"Classification run failed: {type(exc).__name__}: {exc}")
        store.finish_run(run_id, summary["message"], ok=False)
    finally:
        _run_lock.release()
    return summary


# --------------------------------------------------------------------------- Agent - Invoice Extraction


def extract_run(settings: dict, limit: int | None = None) -> dict:
    """Read the emails labelled as invoices and process every supported attachment. Returns a summary dict."""
    summary = {"ok": True, "stage": "extract", "messages": 0, "processed": 0, "skipped": 0, "errors": 0, "message": ""}
    if not config.has_claude(settings):
        return {**summary, "ok": False, "message": "Add your Claude API key in Settings first."}
    if not _run_lock.acquire(blocking=False):
        return {**summary, **BUSY}
    run_id = store.start_run("extract")
    try:
        box = gmail_client.connect()
        invoice_label = settings.get("classify_invoice_label") or "Invoice Incoming"
        processed_label = settings.get("gmail_processed_label") or ""
        query = f"label:{gmail_client.search_label(invoice_label)}"
        if processed_label:
            query = f"{query} -label:{gmail_client.search_label(processed_label)}"
        message_ids = box.list(query, _batch_size(settings, limit))

        for msg_id in message_ids:
            try:
                meta = box.fetch(msg_id)
            except Exception as exc:  # noqa: BLE001
                log.error("Could not read message %s: %s", msg_id, exc)
                summary["errors"] += 1
                continue
            summary["messages"] += 1

            for att in meta["attachments"]:
                if store.already_processed(msg_id, att["filename"]):
                    summary["skipped"] += 1
                    continue
                invoice_id = process_attachment(settings, att["data"], att["filename"], att["mime_type"], email_meta=meta, source="gmail")
                row = store.get_invoice(invoice_id)
                if row and row["status"] == "error":
                    summary["errors"] += 1
                else:
                    summary["processed"] += 1

            if not meta["attachments"]:
                summary["skipped"] += 1
                if not store.already_processed(msg_id, "(no supported attachment)"):
                    store.insert_invoice({
                        "source": "gmail",
                        "gmail_message_id": msg_id,
                        "gmail_thread_id": meta.get("thread_id"),
                        "from_addr": meta.get("from"),
                        "subject": meta.get("subject"),
                        "received_at": meta.get("received_at"),
                        "attachment_name": "(no supported attachment)",
                        "mime_type": "",
                        "status": "not_invoice",
                        "notes": "Email had no PDF or image attachment.",
                    })

            try:
                box.mark_processed(msg_id, processed_label)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not label message %s: %s", msg_id, exc)

        summary["message"] = (
            f"Checked {summary['messages']} email(s): {summary['processed']} processed, "
            f"{summary['skipped']} skipped, {summary['errors']} failed."
        )
        store.finish_run(run_id, summary["message"], ok=summary["errors"] == 0)
    except gmail_client.GmailNotConnected as exc:
        summary.update(ok=False, message=str(exc))
        store.finish_run(run_id, summary["message"], ok=False)
    except Exception as exc:  # noqa: BLE001
        log.error("Extraction run failed: %s\n%s", exc, traceback.format_exc())
        summary.update(ok=False, message=f"Extraction run failed: {type(exc).__name__}: {exc}")
        store.finish_run(run_id, summary["message"], ok=False)
    finally:
        _run_lock.release()
    return summary


def run_once(settings: dict) -> dict:
    """Check inbox now: classify new mail, then extract the labelled invoices. Returns a summary dict."""
    classified = classify_run(settings)
    if not classified["ok"]:
        return classified
    extracted = extract_run(settings)
    if not extracted["ok"]:
        return extracted
    message = f"Classification: {classified['message']} Extraction: {extracted['message']}"
    run_id = store.start_run("inbox")
    store.finish_run(run_id, message, ok=(classified["errors"] + extracted["errors"]) == 0)
    return {"ok": True, "stage": "inbox", "errors": classified["errors"] + extracted["errors"], "message": message}


# --------------------------------------------------------------------------- Background polling

_poller: threading.Thread | None = None
_stop = threading.Event()


def start_poller(get_settings) -> None:
    """Run both agents every `poll_minutes` (0 = manual only). Safe to call repeatedly."""
    global _poller
    if _poller and _poller.is_alive():
        return

    def loop():
        while not _stop.is_set():
            settings = get_settings()
            minutes = int(settings.get("poll_minutes") or 0)
            if minutes <= 0:
                _stop.wait(30)
                continue
            try:
                result = run_once(settings)
                log.info("Scheduled run: %s", result.get("message"))
            except Exception as exc:  # noqa: BLE001
                log.error("Scheduled run crashed: %s", exc)
            _stop.wait(minutes * 60)

    _poller = threading.Thread(target=loop, name="inbox-poller", daemon=True)
    _poller.start()
