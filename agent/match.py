"""Fuzzy name matching shared by the vendor / project maps and the invoice types."""

from __future__ import annotations

import re

LEGAL_SUFFIXES = r"\b(ltd|limited|plc|llp|llc|inc|co|company|uk|group|holdings|services|the)\b"


def norm(name: str | None) -> str:
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(LEGAL_SUFFIXES, " ", text)
    return re.sub(r"\s+", " ", text).strip()


def lookup(mapping: dict | None, *candidates: str | None) -> tuple[str, str]:
    """Return (value, matched_key). Exact normalised match first, then containment either way."""
    if not mapping:
        return "", ""
    normalised = {norm(k): (k, v) for k, v in mapping.items() if k and v}
    cands = [norm(c) for c in candidates if c]
    for cand in cands:
        if cand in normalised:
            key, value = normalised[cand]
            return value, key
    for cand in cands:
        for nkey, (key, value) in normalised.items():
            if len(nkey) >= 3 and (nkey in cand or cand in nkey):
                return value, key
    return "", ""


def name_listed(name: str | None, names: list[str]) -> bool:
    """True when the supplier name matches one of the listed names, by the same rules as the vendor map."""
    return lookup({n: "x" for n in names if n}, name)[0] == "x"
