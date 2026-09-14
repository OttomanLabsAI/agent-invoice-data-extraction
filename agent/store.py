"""SQLite storage for processed invoices and run history."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB_PATH, ensure_dirs

STATUSES = ("review", "approved", "posted", "queried", "not_invoice", "error")

STATUS_LABELS = {
    "review": "Needs review",
    "approved": "Approved",
    "posted": "Posted to Sage",
    "queried": "Queried",
    "not_invoice": "Not an invoice",
    "error": "Failed",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'gmail',
    gmail_message_id TEXT,
    gmail_thread_id TEXT,
    from_addr TEXT,
    subject TEXT,
    received_at TEXT,
    attachment_name TEXT,
    attachment_path TEXT,
    mime_type TEXT,
    status TEXT NOT NULL DEFAULT 'review',
    extracted_json TEXT,
    sage_json TEXT,
    issues_json TEXT,
    notes TEXT DEFAULT '',
    error TEXT DEFAULT '',
    intacct_recordno TEXT DEFAULT '',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    UNIQUE(gmail_message_id, attachment_name)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT,
    ok INTEGER DEFAULT 1
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("extracted_json", "sage_json", "issues_json"):
        raw = d.get(key)
        try:
            d[key[:-5]] = json.loads(raw) if raw else None
        except ValueError:
            d[key[:-5]] = None
    d["status_label"] = STATUS_LABELS.get(d.get("status"), d.get("status"))
    return d


def already_processed(gmail_message_id: str, attachment_name: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM invoices WHERE gmail_message_id = ? AND attachment_name = ?",
            (gmail_message_id, attachment_name),
        ).fetchone()
    return row is not None


def insert_invoice(record: dict) -> int:
    fields = {
        "created_at": now_iso(),
        "source": record.get("source", "gmail"),
        "gmail_message_id": record.get("gmail_message_id"),
        "gmail_thread_id": record.get("gmail_thread_id"),
        "from_addr": record.get("from_addr"),
        "subject": record.get("subject"),
        "received_at": record.get("received_at"),
        "attachment_name": record.get("attachment_name"),
        "attachment_path": record.get("attachment_path"),
        "mime_type": record.get("mime_type"),
        "status": record.get("status", "review"),
        "extracted_json": json.dumps(record.get("extracted")) if record.get("extracted") is not None else None,
        "sage_json": json.dumps(record.get("sage")) if record.get("sage") is not None else None,
        "issues_json": json.dumps(record.get("issues") or []),
        "notes": record.get("notes", ""),
        "error": record.get("error", ""),
        "input_tokens": record.get("input_tokens", 0),
        "output_tokens": record.get("output_tokens", 0),
    }
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO invoices ({cols}) VALUES ({marks})", tuple(fields.values()))
        return int(cur.lastrowid)


def update_invoice(invoice_id: int, **changes) -> None:
    if not changes:
        return
    payload = {}
    for key, value in changes.items():
        if key in ("extracted", "sage", "issues"):
            payload[key + "_json"] = json.dumps(value)
        else:
            payload[key] = value
    assignments = ", ".join(f"{k} = ?" for k in payload)
    with connect() as conn:
        conn.execute(f"UPDATE invoices SET {assignments} WHERE id = ?", (*payload.values(), invoice_id))


def get_invoice(invoice_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    return _row_to_dict(row)


def list_invoices(status: str | None = None) -> list[dict]:
    with connect() as conn:
        if status and status in STATUSES:
            rows = conn.execute(
                "SELECT * FROM invoices WHERE status = ? ORDER BY COALESCE(received_at, created_at) DESC, id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM invoices ORDER BY COALESCE(received_at, created_at) DESC, id DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def status_counts() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM invoices GROUP BY status").fetchall()
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r["status"]] = r["n"]
    counts["total"] = sum(counts.values())
    return counts


def delete_invoice(invoice_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))


def start_run() -> int:
    with connect() as conn:
        cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now_iso(),))
        return int(cur.lastrowid)


def finish_run(run_id: int, summary: str, ok: bool = True) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, summary = ?, ok = ? WHERE id = ?",
            (now_iso(), summary, 1 if ok else 0, run_id),
        )


def last_run() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
