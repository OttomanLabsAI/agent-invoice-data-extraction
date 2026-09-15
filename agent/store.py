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
    source_text TEXT DEFAULT '',
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
    stage TEXT NOT NULL DEFAULT 'inbox',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT,
    ok INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    gmail_message_id TEXT UNIQUE,
    from_addr TEXT,
    subject TEXT,
    received_at TEXT,
    attachments TEXT DEFAULT '',
    verdict TEXT NOT NULL,
    document_kind TEXT DEFAULT '',
    confidence REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    label_applied TEXT DEFAULT '',
    model TEXT DEFAULT '',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0
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


def _add_column(conn, table: str, column: str, ddl: str) -> None:
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Columns added after the first release; databases made by an older version keep working.
        _add_column(conn, "runs", "stage", "stage TEXT NOT NULL DEFAULT 'inbox'")
        _add_column(conn, "invoices", "source_text", "source_text TEXT DEFAULT ''")


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
        "source_text": record.get("source_text", ""),
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


def start_run(stage: str = "inbox") -> int:
    with connect() as conn:
        cur = conn.execute("INSERT INTO runs (stage, started_at) VALUES (?, ?)", (stage, now_iso()))
        return int(cur.lastrowid)


def finish_run(run_id: int, summary: str, ok: bool = True) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, summary = ?, ok = ? WHERE id = ?",
            (now_iso(), summary, 1 if ok else 0, run_id),
        )


def last_run(stage: str | None = None) -> dict | None:
    with connect() as conn:
        if stage:
            row = conn.execute("SELECT * FROM runs WHERE stage = ? ORDER BY id DESC LIMIT 1", (stage,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- classification log


def record_classification(entry: dict) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO classifications (created_at, gmail_message_id, from_addr, subject, received_at, attachments, verdict,
                 document_kind, confidence, reason, label_applied, model, input_tokens, output_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(gmail_message_id) DO UPDATE SET created_at = excluded.created_at, verdict = excluded.verdict,
                 document_kind = excluded.document_kind, confidence = excluded.confidence, reason = excluded.reason,
                 label_applied = excluded.label_applied, model = excluded.model, input_tokens = excluded.input_tokens,
                 output_tokens = excluded.output_tokens""",
            (
                now_iso(), entry.get("gmail_message_id"), entry.get("from_addr") or "", entry.get("subject") or "",
                entry.get("received_at") or "", entry.get("attachments") or "", entry["verdict"], entry.get("document_kind") or "",
                float(entry.get("confidence") or 0), entry.get("reason") or "", entry.get("label_applied") or "",
                entry.get("model") or "", int(entry.get("input_tokens") or 0), int(entry.get("output_tokens") or 0),
            ),
        )


def list_classifications(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM classifications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def classification_counts() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT verdict, COUNT(*) AS n FROM classifications GROUP BY verdict").fetchall()
    counts = {"invoice": 0, "not_invoice": 0}
    for r in rows:
        counts[r["verdict"]] = r["n"]
    counts["total"] = counts["invoice"] + counts["not_invoice"]
    return counts
