"""Settings storage.

Everything the app needs to run (API keys, the two agents, Sage coding) lives in
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

from .invoice_types import DEFAULT_TYPES, normalise_type

BASE_DIR = Path(__file__).resolve().parent.parent
# Override with INVOICE_AGENT_DATA to keep keys/database somewhere else (e.g. a OneDrive folder).
DATA_DIR = Path(os.environ.get("INVOICE_AGENT_DATA") or (BASE_DIR / "data")).expanduser()
ATTACHMENTS_DIR = DATA_DIR / "attachments"
SETTINGS_PATH = DATA_DIR / "settings.json"
GOOGLE_TOKEN_PATH = DATA_DIR / "google_token.json"
SECRET_KEY_PATH = DATA_DIR / "flask_secret"
DB_PATH = DATA_DIR / "invoices.db"

DEFAULT_MODEL = "claude-opus-5"
CLAUDE_MODELS = [
    ("claude-opus-5", "Claude Opus 5 (default)"),
    ("claude-sonnet-5", "Claude Sonnet 5 (faster, cheaper)"),
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

# What each generic code stands for, shown on the Settings page.
GENERIC_CODES = [
    ("5000", "materials purchased"), ("6000", "direct labour"), ("7700", "plant and equipment hire"), ("6002", "subcontractors"),
    ("7603", "professional and site services"), ("7400", "travel and subsistence"), ("5002", "miscellaneous purchases (fallback)"),
    ("2214", "CIS deductions withheld"), ("2215", "retentions held"),
]

DEFAULTS = {
    # Claude - both agents use this key; each picks its own model on its tab
    "anthropic_api_key": "",
    # Agent - Classification: reads new mail and labels invoices
    "classify_model": DEFAULT_MODEL,
    "classify_reference_text": "",
    "classify_invoice_label": "Invoice Incoming",
    "classify_other_label": "Not an invoice",
    "gmail_query": "has:attachment is:unread",
    "gmail_allowed_senders": "",
    "gmail_max_messages": 10,
    # Agent - Invoice Extraction: reads mail labelled as invoices and extracts the data
    "extract_model": DEFAULT_MODEL,
    "extract_reference_text": "",
    "gmail_processed_label": "Invoices/Processed",
    "poll_minutes": 0,
    # Gmail OAuth client
    "google_client_id": "",
    "google_client_secret": "",
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
    # Sage Intacct coding defaults. Generic starting points for a UK contractor
    # paying subcontractors and suppliers: Sage-style nominal codes and Intacct's
    # standard UK VAT tax detail names. Every ID must be replaced with, or checked
    # against, the lists in the company's own Intacct before the first real post.
    "sage_location_id": "",
    "sage_department_id": "",
    "sage_tax_solution_id": "United Kingdom - VAT",
    "sage_default_gl": "5002",
    "sage_cis_gl": "2214",
    "sage_retention_gl": "2215",
    # Lookup maps (edited as "key = value" lines in the settings page)
    "gl_map": {
        "materials": "5000",
        "labour": "6000",
        "plant": "7700",
        "subcontract": "6002",
        "services": "7603",
        "expenses": "7400",
        "other": "5002",
    },
    # The fictional supplier and project on the bundled sample invoice, so it maps end to end out of the box.
    "vendor_map": {"Northbank Mechanical Services Ltd": "V0088"},
    "project_map": {"HEL18": "P-HEL18"},
    "vat_detail_map": {
        "20": "UK Purchase Goods Standard Rate",
        "5": "UK Purchase Goods Reduced Rate",
        "0": "UK Purchase Goods Zero Rate",
        "reverse_charge": "UK Purchase Services Reverse Charge Standard Rate",
    },
    "terms_map": {"0": "Due on receipt", "7": "Net 7", "14": "Net 14", "30": "Net 30", "45": "Net 45", "60": "Net 60", "90": "Net 90"},
    # Invoice types: per-kind overrides and checks on top of the coding above (see invoice_types.py)
    "invoice_types": DEFAULT_TYPES,
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


def with_defaults(stored: dict | None) -> dict:
    settings = deepcopy(DEFAULTS)
    stored = dict(stored or {})
    # Settings saved by the single-model version carry claude_model; it becomes the extraction model.
    if stored.get("claude_model") and not stored.get("extract_model"):
        stored["extract_model"] = stored["claude_model"]
    for key, value in stored.items():
        if key == "invoice_types":
            if isinstance(value, list):
                settings["invoice_types"] = [normalise_type(t, i) for i, t in enumerate(value)]
        elif key in MAP_FIELDS and isinstance(value, dict):
            merged = deepcopy(DEFAULTS.get(key, {}))
            merged.update(value)
            settings[key] = merged
        elif key in DEFAULTS:
            settings[key] = value
    return settings


def load_settings() -> dict:
    ensure_dirs()
    stored = {}
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
    return with_defaults(stored)


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


def model_label(model_id: str) -> str:
    for value, label in CLAUDE_MODELS:
        if value == model_id:
            return label
    return model_id or ""


def has_claude(settings: dict) -> bool:
    return bool(settings.get("anthropic_api_key"))


def has_google_client(settings: dict) -> bool:
    return bool(settings.get("google_client_id") and settings.get("google_client_secret"))


def has_sage(settings: dict) -> bool:
    return all(
        settings.get(k)
        for k in ("sage_sender_id", "sage_sender_password", "sage_company_id", "sage_user_id", "sage_user_password")
    )
