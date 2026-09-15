"""What an email carries besides a PDF.

Invoices do not always arrive as an attached PDF. They are typed into the body of
the email; the whole thing is forwarded on as a `.eml` file with an empty covering
note; a timesheet or a spreadsheet comes with them. This module turns those parts
into plain text the agents can read, and digs any PDFs or images out of a
forwarded email so they are treated as documents in their own right.

Nothing here knows about Gmail: it works on bytes, so it is easy to test.
"""

from __future__ import annotations

import email
import os
import re
from email import policy
from email.message import EmailMessage
from html import unescape

# What can be sent to Claude as a document.
SUPPORTED_MIME = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
EXT_TO_MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
MIN_IMAGE_BYTES = 20 * 1024  # anything smaller is almost certainly a logo or signature

# A forwarded email, read for its text and for the documents inside it.
EMAIL_MIME = {"message/rfc822"}
EMAIL_EXTS = {".eml"}
# Read as text and added to the email's own text: timesheets, schedules, notes.
TEXT_MIME = {"text/csv", "text/plain", "text/tab-separated-values", "application/csv"}
TEXT_EXTS = {".csv", ".txt", ".tsv"}

MAX_TEXT_CHARS = 6000  # per text part, so one long CSV cannot crowd out the invoice
MAX_EML_DEPTH = 3  # a forward of a forward of a forward is far enough


def ext(filename: str | None) -> str:
    return os.path.splitext(filename or "")[1].lower()


def document_mime(filename: str | None, mime: str | None) -> str:
    """The media type to send to Claude, or "" when this is not a readable document."""
    mime = (mime or "").lower().split(";")[0].strip()
    if mime in SUPPORTED_MIME:
        return mime
    return EXT_TO_MIME.get(ext(filename), "") if EXT_TO_MIME.get(ext(filename), "") in SUPPORTED_MIME else ""


def is_email(filename: str | None, mime: str | None) -> bool:
    return (mime or "").lower().split(";")[0].strip() in EMAIL_MIME or ext(filename) in EMAIL_EXTS


def is_text(filename: str | None, mime: str | None) -> bool:
    return (mime or "").lower().split(";")[0].strip() in TEXT_MIME or ext(filename) in TEXT_EXTS


def too_small(mime: str, data: bytes) -> bool:
    """Logos and signature images ride along on every email; they carry no document."""
    return mime.startswith("image/") and len(data) < MIN_IMAGE_BYTES


def strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</table>", "\n", text, flags=re.I)
    text = re.sub(r"</t[dh]>", "\t", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def decode_text(data: bytes | str | None) -> str:
    if isinstance(data, str):
        return data
    if not data:
        return ""
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def text_attachment(filename: str, data: bytes) -> str:
    """A spreadsheet or note attached to the email, labelled so the model knows what it is."""
    body = decode_text(data).replace("\r\n", "\n").strip()
    if not body:
        return ""
    return f"--- Attached file: {filename}\n{body[:MAX_TEXT_CHARS]}"


def _document(filename: str, mime: str, data: bytes, source: str = "") -> dict:
    name = f"{source} > {filename}" if source else filename
    return {"filename": name, "mime_type": mime, "data": data, "size": len(data)}


def _part_bytes(part: EmailMessage) -> bytes:
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - a broken part must not stop the rest
        payload = None
    return payload or b""


def _body_text(msg: EmailMessage) -> str:
    try:
        plain = msg.get_body(preferencelist=("plain",))
        if plain is not None:
            return decode_text(plain.get_content()).strip()
        html = msg.get_body(preferencelist=("html",))
        if html is not None:
            return strip_html(decode_text(html.get_content()))
    except Exception:  # noqa: BLE001
        pass
    return ""


def read_message(msg: EmailMessage, source: str = "", depth: int = 0) -> dict:
    """A forwarded email -> {text, documents, skipped}. Its own attachments are read too."""
    out: dict = {"text": "", "documents": [], "skipped": []}
    lines = [f"--- Forwarded email: {source}" if source else "--- Forwarded email"]
    for header in ("From", "To", "Subject", "Date"):
        value = str(msg.get(header) or "").strip()
        if value:
            lines.append(f"{header}: {value}")
    body = _body_text(msg)
    if body:
        lines.append("")
        lines.append(body[:MAX_TEXT_CHARS])

    try:
        attachments = list(msg.iter_attachments())
    except Exception:  # noqa: BLE001
        attachments = []
    for part in attachments:
        filename = part.get_filename() or "attachment"
        mime = (part.get_content_type() or "").lower()
        if is_email(filename, mime):
            if depth >= MAX_EML_DEPTH:
                out["skipped"].append(filename)
                continue
            nested = None
            try:
                inner = part.get_content()
                if isinstance(inner, EmailMessage):
                    nested = read_message(inner, filename, depth + 1)
            except Exception:  # noqa: BLE001
                nested = None
            if nested is None:
                nested = read_email(_part_bytes(part), filename, depth + 1)
            if nested["text"]:
                lines.append("")
                lines.append(nested["text"])
            out["documents"] += nested["documents"]
            out["skipped"] += nested["skipped"]
            continue
        data = _part_bytes(part)
        doc_mime = document_mime(filename, mime)
        if doc_mime and data and not too_small(doc_mime, data):
            out["documents"].append(_document(filename, doc_mime, data, source))
            continue
        if doc_mime and data:
            continue  # a logo or signature image
        if is_text(filename, mime):
            text = text_attachment(filename, data)
            if text:
                lines.append("")
                lines.append(text)
            continue
        if data:
            out["skipped"].append(filename)

    out["text"] = "\n".join(lines).strip()
    return out


def read_email(data: bytes, source: str = "", depth: int = 0) -> dict:
    """Parse a .eml file. Never raises: an unreadable file is reported as skipped."""
    try:
        msg = email.message_from_bytes(data, policy=policy.default)
    except Exception:  # noqa: BLE001
        return {"text": "", "documents": [], "skipped": [source or "forwarded email"]}
    return read_message(msg, source, depth)
