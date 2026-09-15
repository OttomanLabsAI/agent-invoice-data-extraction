"""Gmail access via the Gmail API (OAuth 2.0).

The app registers as an OAuth client using the Client ID / Client Secret pasted
into Settings. The first "Connect Gmail" click sends the user through Google's
consent screen; the resulting token (with a refresh token) is stored in
data/google_token.json and refreshed automatically afterwards.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from . import mail_parts
from .config import GOOGLE_TOKEN_PATH, ensure_dirs

# Read mail, download attachments, add labels and clear the unread flag.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Local HTTP redirect + Google occasionally returning extra scopes would
# otherwise make oauthlib refuse the token exchange.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# What counts as a readable document lives in mail_parts; re-exported here for the rest of the app.
SUPPORTED_MIME = mail_parts.SUPPORTED_MIME
EXT_TO_MIME = mail_parts.EXT_TO_MIME
MIN_IMAGE_BYTES = mail_parts.MIN_IMAGE_BYTES


class GmailNotConnected(Exception):
    pass


# --------------------------------------------------------------------------- OAuth


def client_config(settings: dict) -> dict:
    return {
        "installed": {
            "client_id": settings.get("google_client_id", ""),
            "client_secret": settings.get("google_client_secret", ""),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def make_flow(settings: dict, redirect_uri: str, state: str | None = None, code_verifier: str | None = None) -> Flow:
    """Build the OAuth flow. The library uses PKCE, so the callback must be given the same
    code_verifier the sign-in step generated - otherwise Google rejects the token exchange."""
    kwargs = {"state": state}
    if code_verifier:
        kwargs["code_verifier"] = code_verifier
    flow = Flow.from_client_config(client_config(settings), scopes=SCOPES, **kwargs)
    flow.redirect_uri = redirect_uri
    return flow


def save_credentials(creds: Credentials) -> None:
    ensure_dirs()
    GOOGLE_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(GOOGLE_TOKEN_PATH, 0o600)
    except OSError:
        pass


def clear_credentials() -> None:
    if GOOGLE_TOKEN_PATH.exists():
        GOOGLE_TOKEN_PATH.unlink()


def load_credentials() -> Credentials | None:
    if not GOOGLE_TOKEN_PATH.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_PATH), SCOPES)
    except (OSError, ValueError):
        return None
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
    return creds


def service():
    creds = load_credentials()
    if creds is None or not creds.valid:
        raise GmailNotConnected("Gmail is not connected. Open Settings and click Connect Gmail.")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def connection_status() -> dict:
    """Cheap check used by the Settings page."""
    if not GOOGLE_TOKEN_PATH.exists():
        return {"connected": False, "email": None, "error": None}
    try:
        svc = service()
        profile = svc.users().getProfile(userId="me").execute()
        return {"connected": True, "email": profile.get("emailAddress"), "error": None}
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        return {"connected": False, "email": None, "error": str(exc)}


# --------------------------------------------------------------------------- Reading mail


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _header(headers: list[dict], name: str) -> str:
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _part_data(svc, msg_id: str, part: dict) -> bytes:
    body = part.get("body", {}) or {}
    if body.get("attachmentId"):
        att = svc.users().messages().attachments().get(userId="me", messageId=msg_id, id=body["attachmentId"]).execute()
        return _b64url_decode(att.get("data", ""))
    if body.get("data"):
        return _b64url_decode(body["data"])
    return b""


def _forwarded_name(part: dict) -> str:
    """A name for a forwarded email that arrived without a filename."""
    headers = part.get("headers", [])
    subject = _header(headers, "Subject") or "forwarded email"
    sender = _header(headers, "From")
    return f"{subject} (from {sender})" if sender else subject


def _collect(svc, msg_id: str, part: dict, out: dict, container: str = "", depth: int = 0) -> None:
    """Walk one part of a Gmail message, sorting it into body text, attached text and documents.

    `container` is set while inside a forwarded email, so its text is kept apart from
    the covering email's own body - forwards often arrive with an empty covering note.
    """
    mime = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""
    children = part.get("parts") or []

    if mail_parts.is_email(filename, mime):
        name = filename or _forwarded_name(part)
        if depth >= mail_parts.MAX_EML_DEPTH:
            out["skipped"].append(name)
            return
        raw = _part_data(svc, msg_id, part)
        if raw:
            parsed = mail_parts.read_email(raw, name, depth + 1)
            if parsed["text"]:
                out["attached_text"].append(parsed["text"])
                out["text_sources"].append(name)
            out["documents"] += parsed["documents"]
            out["skipped"] += parsed["skipped"]
            return
        # No raw copy: Gmail has already broken the forwarded email into parts, so read those.
        out["attached_text"].append(f"--- Forwarded email: {name}")
        out["text_sources"].append(name)
        for child in children:
            _collect(svc, msg_id, child, out, container=name, depth=depth + 1)
        return

    if filename:
        doc_mime = mail_parts.document_mime(filename, mime)
        if doc_mime:
            data = _part_data(svc, msg_id, part)
            if data and not mail_parts.too_small(doc_mime, data):
                name = f"{container} > {filename}" if container else filename
                out["documents"].append({"filename": name, "mime_type": doc_mime, "data": data, "size": len(data)})
            return
        if mail_parts.is_text(filename, mime):
            text = mail_parts.text_attachment(filename, _part_data(svc, msg_id, part))
            if text:
                out["attached_text"].append(text)
                out["text_sources"].append(filename)
            return
        out["skipped"].append(filename)
        return

    if mime in ("text/plain", "text/html") and (part.get("body") or {}).get("data"):
        raw = mail_parts.decode_text(_part_data(svc, msg_id, part))
        text = raw.strip() if mime == "text/plain" else mail_parts.strip_html(raw)
        key = container or ""
        if text and key not in out["seen_text"]:
            out["seen_text"].add(key)
            if container:
                out["attached_text"].append(text[: mail_parts.MAX_TEXT_CHARS])
            else:
                out["body_text"] = text
    for child in children:
        _collect(svc, msg_id, child, out, container, depth)


def list_messages(svc, query: str, max_messages: int = 10) -> list[str]:
    resp = svc.users().messages().list(userId="me", q=query, maxResults=max_messages).execute()
    return [m["id"] for m in resp.get("messages", [])]


def fetch_message(svc, msg_id: str) -> dict:
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])

    out: dict = {"body_text": "", "attached_text": [], "documents": [], "skipped": [], "text_sources": [], "seen_text": set()}
    _collect(svc, msg_id, payload, out)

    internal_ms = int(msg.get("internalDate", "0") or 0)
    received_at = (
        datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()
        if internal_ms
        else ""
    )

    return {
        "id": msg_id,
        "thread_id": msg.get("threadId"),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "subject": _header(headers, "Subject"),
        "date": _header(headers, "Date"),
        "received_at": received_at,
        "snippet": msg.get("snippet", ""),
        "body_text": out["body_text"][:4000],
        # Forwarded emails and text attachments (timesheets, schedules): read, not sent as documents.
        "attached_text": "\n\n".join(out["attached_text"])[:12000],
        "attachments": out["documents"],
        # Where the text came from, for the decisions list; the documents above are what Claude is shown as files.
        "text_sources": out["text_sources"],
        "skipped_attachments": out["skipped"],
        "label_ids": msg.get("labelIds", []),
    }


def sender_allowed(from_addr: str, allowed: str) -> bool:
    entries = [e.strip().lower() for e in (allowed or "").replace("\n", ",").split(",") if e.strip()]
    if not entries:
        return True
    from_lower = (from_addr or "").lower()
    return any(e in from_lower for e in entries)


# --------------------------------------------------------------------------- Labelling


def _label_id(svc, name: str) -> str:
    """Find a label by name, creating it (and any missing parents of a nested name) if needed."""
    existing = {l.get("name", "").lower(): l["id"] for l in svc.users().labels().list(userId="me").execute().get("labels", [])}
    if name.lower() in existing:
        return existing[name.lower()]
    parts = [p.strip() for p in name.split("/") if p.strip()]
    label_id = ""
    for depth in range(1, len(parts) + 1):
        partial = "/".join(parts[:depth])
        if partial.lower() in existing:
            label_id = existing[partial.lower()]
            continue
        created = (
            svc.users()
            .labels()
            .create(
                userId="me",
                body={"name": partial, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
            )
            .execute()
        )
        label_id = created["id"]
        existing[partial.lower()] = label_id
    return label_id


def search_label(name: str) -> str:
    """Gmail search wants label names with spaces and slashes as hyphens."""
    return re.sub(r"[\s/]+", "-", (name or "").strip())


def add_labels(svc, msg_id: str, label_names: list[str]) -> None:
    ids = [_label_id(svc, name) for name in label_names if name]
    if ids:
        svc.users().messages().modify(userId="me", id=msg_id, body={"addLabelIds": ids}).execute()


class Mailbox:
    """The few Gmail operations the agents need, bound to one service. Tests replace connect() with a fake."""

    def __init__(self, svc):
        self.svc = svc

    def list(self, query: str, max_messages: int = 10) -> list[str]:
        return list_messages(self.svc, query, max_messages)

    def fetch(self, msg_id: str) -> dict:
        return fetch_message(self.svc, msg_id)

    def add_labels(self, msg_id: str, label_names: list[str]) -> None:
        add_labels(self.svc, msg_id, label_names)

    def mark_processed(self, msg_id: str, label_name: str) -> None:
        mark_processed(self.svc, msg_id, label_name)


def connect() -> Mailbox:
    return Mailbox(service())


def mark_processed(svc, msg_id: str, label_name: str) -> None:
    body = {"removeLabelIds": ["UNREAD"]}
    if label_name:
        body["addLabelIds"] = [_label_id(svc, label_name)]
    svc.users().messages().modify(userId="me", id=msg_id, body=body).execute()


def token_summary() -> dict:
    if not GOOGLE_TOKEN_PATH.exists():
        return {}
    try:
        return json.loads(GOOGLE_TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
