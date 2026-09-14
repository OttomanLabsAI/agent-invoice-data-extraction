"""Settings storage.

Everything the app needs to run (API keys, Gmail filter, Sage mapping) lives in
data/settings.json, written with owner-only permissions. Secrets are stored in
plain text on purpose: this is a local tool and the file never leaves the
machine. Keep the data/ folder out of version control.
"""

from __future__ import annotations

import json
import os
import secrets
from copy import deepcopy
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# Override with INVOICE_AGENT_DATA to keep keys/database somewhere else (e.g. a OneDrive folder).
DATA_DIR = Path(os.environ.get("INVOICE_AGENT_DATA") or (BASE_DIR / "data")).expanduser()
ATTACHMENTS_DIR = DATA_DIR / "attachments"
SETTINGS_PATH = DATA_DIR / "settings.json"
GOOGLE_TOKEN_PATH = DATA_DIR / "google_token.json"
SECRET_KEY_PATH = DATA_DIR / "flask_secret"
DB_PATH = DATA_DIR / "invoices.db"

CLAUDE_MODELS = [
    ("claude-sonnet-5", "Claude Sonnet 5 (recommended)"),
    ("claude-opus-5", "Claude Opus 5"),
    ("claude-fable-5-1", "Claude Fable 5.1"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5 (cheapest)"),
]

SECRET_FIELDS = {
    "anthropic_api_key",
    "google_client_id",
    "google_client_secret",
    "sage_sender_id",
    "sage_sender_password",
    "sage_company_id",
    "sage_user_id",
    "sage_user_password",
}

MAP_FIELDS = {"vendor_map", "project_map", "gl_map", "vat_detail_map", "terms_map"}

DEFAULTS = {
    # Claude
    "anthropic_api_key": "",
    "claude_model": "claude-sonnet-5",
    # Gmail
    "google_client_id": "",
    "google_client_secret": "",
    "gmail_query": "has:attachment is:unread",
    "gmail_allowed_senders": "",
    "gmail_processed_label": "Invoices/Processed",
    "gmail_max_messages": 10,
    "poll_minutes": 0,
    # Company
    "company_name": "Glent Group",
    "default_currency": "GBP",
    # Sage Intacct connection (optional - leave blank to work review-and-export only)
    "sage_endpoint": "https://api.intacct.com/ia/xml/xmlgw.phtml",
    "sage_sender_id": "",
    "sage_sender_password": "",
    "sage_company_id": "",
    "sage_user_id": "",
    "sage_user_password": "",
    "sage_action": "Draft",
    # Sage Intacct coding defaults
    "sage_location_id": "",
    "sage_department_id": "",
    "sage_tax_solution_id": "United Kingdom - VAT",
    "sage_default_gl": "",
    "sage_cis_gl": "",
    "sage_retention_gl": "",
    # Lookup maps (edited as "key = value" lines in the settings page)
    "gl_map": {
        "materials": "",
        "labour": "",
        "plant": "",
        "subcontract": "",
        "services": "",
        "other": "",
    },
    "vendor_map": {},
    "project_map": {},
    "vat_detail_map": {"20": "", "5": "", "0": "", "reverse_charge": ""},
    "terms_map": {"30": "Net 30", "14": "Net 14", "60": "Net 60", "0": "Due on receipt"},
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load_settings() -> dict:
    ensure_dirs()
    settings = deepcopy(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        for key, value in stored.items():
            if key in MAP_FIELDS and isinstance(value, dict):
                merged = deepcopy(DEFAULTS.get(key, {}))
                merged.update(value)
                settings[key] = merged
            else:
                settings[key] = value
    return settings


def save_settings(settings: dict) -> None:
    ensure_dirs()
    _write_private(SETTINGS_PATH, json.dumps(settings, indent=2, ensure_ascii=False))


def flask_secret_key() -> str:
    ensure_dirs()
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    _write_private(SECRET_KEY_PATH, key)
    return key


def parse_map(text: str) -> dict:
    """Turn "key = value" lines into a dict. Blank lines and # comments are ignored."""
    result: dict = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def format_map(mapping: dict) -> str:
    return "\n".join(f"{k} = {v}" for k, v in (mapping or {}).items())


def has_claude(settings: dict) -> bool:
    return bool(settings.get("anthropic_api_key"))


def has_google_client(settings: dict) -> bool:
    return bool(settings.get("google_client_id") and settings.get("google_client_secret"))


def has_sage(settings: dict) -> bool:
    return all(
        settings.get(k)
        for k in ("sage_sender_id", "sage_sender_password", "sage_company_id", "sage_user_id", "sage_user_password")
    )
