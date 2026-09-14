"""The processing loop: Gmail -> Claude -> Sage mapping -> local store."""

from __future__ import annotations

import logging
import re
import threading
import time
import traceback
from pathlib import Path

from . import config, extractor, gmail_client, sage_mapper, store

log = logging.getLogger("invoice-agent")

_run_lock = threading.Lock()


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
            model=settings.get("claude_model") or "claude-sonnet-5",
            attachment=data,
            mime_type=mime_type,
            company_name=settings.get("company_name") or "Glent Group",
            default_currency=settings.get("default_currency") or "GBP",
            email_meta=meta,
        )
    except Exception as exc:  # noqa: BLE001 - recorded on the row
        log.error("Extraction failed for %s: %s", filename, exc)
        return store.insert_invoice({**base_record, "status": "error", "error": f"{type(exc).__name__}: {exc}"})

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


def run_once(settings: dict) -> dict:
    """Pull new mail and process every supported attachment. Returns a summary dict."""
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "message": "A run is already in progress."}
    run_id = store.start_run()
    summary = {"ok": True, "messages": 0, "processed": 0, "skipped": 0, "errors": 0, "message": ""}
    try:
        if not config.has_claude(settings):
            raise RuntimeError("Add your Claude API key in Settings first.")
        svc = gmail_client.service()
        query = settings.get("gmail_query") or "has:attachment is:unread"
        label = settings.get("gmail_processed_label") or ""
        if label:
            excluded = re.sub(r"[\s/]+", "-", label)
            query = f"{query} -label:{excluded}"
        max_messages = int(settings.get("gmail_max_messages") or 10)
        message_ids = gmail_client.list_messages(svc, query, max_messages)
        summary["messages"] = len(message_ids)

        for msg_id in message_ids:
            try:
                meta = gmail_client.fetch_message(svc, msg_id)
            except Exception as exc:  # noqa: BLE001
                log.error("Could not read message %s: %s", msg_id, exc)
                summary["errors"] += 1
                continue

            if not gmail_client.sender_allowed(meta.get("from", ""), settings.get("gmail_allowed_senders", "")):
                summary["skipped"] += 1
                continue

            handled_any = False
            for att in meta["attachments"]:
                if store.already_processed(msg_id, att["filename"]):
                    summary["skipped"] += 1
                    handled_any = True
                    continue
                invoice_id = process_attachment(
                    settings, att["data"], att["filename"], att["mime_type"], email_meta=meta, source="gmail"
                )
                row = store.get_invoice(invoice_id)
                if row and row["status"] == "error":
                    summary["errors"] += 1
                else:
                    summary["processed"] += 1
                handled_any = True

            if not meta["attachments"]:
                summary["skipped"] += 1
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
                handled_any = True

            if handled_any:
                try:
                    gmail_client.mark_processed(svc, msg_id, label)
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
        log.error("Run failed: %s\n%s", exc, traceback.format_exc())
        summary.update(ok=False, message=f"Run failed: {type(exc).__name__}: {exc}")
        store.finish_run(run_id, summary["message"], ok=False)
    finally:
        _run_lock.release()
    return summary


# --------------------------------------------------------------------------- Background polling

_poller: threading.Thread | None = None
_stop = threading.Event()


def start_poller(get_settings) -> None:
    """Poll the inbox every `poll_minutes` (0 = manual only). Safe to call repeatedly."""
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
