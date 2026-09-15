"""Reference-text retrieval for the agents.

Short texts are sent whole; a long text is split into paragraphs and only the
ones that share words with the email (subject, sender, body, attachment names)
are sent, up to a budget.
"""

from __future__ import annotations

import re

RAG_MAX_CHARS = 16000

STOP = set(
    "a an and are as at be by for from has have in is it its of on or that the this to was were will with you your we our "
    "not no if then than so do does did can could would should may might into onto over under please find attached see "
    "regards thanks thank hi hello dear re fw fwd".split()
)


def tokens(text: str | None) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9'.\-]*", (text or "").lower())
    return [w for w in words if len(w) >= 2 and w not in STOP]


def paragraphs(text: str | None) -> list[str]:
    return [b.strip() for b in re.split(r"\r?\n\s*\r?\n", text or "") if b.strip()]


def retrieve(text: str | None, query: str | None, max_chars: int = RAG_MAX_CHARS) -> str:
    """The whole text when it fits, otherwise the best-matching paragraphs in their original order."""
    whole = (text or "").strip()
    if not whole:
        return ""
    if len(whole) <= max_chars:
        return whole
    wanted = set(tokens(query))
    scored = []
    for index, block in enumerate(paragraphs(whole)):
        seen = {w for w in tokens(block) if w in wanted}
        scored.append((len(seen), index, block))
    scored.sort(key=lambda s: (-s[0], len(s[2]), s[1]))
    chosen: list[tuple[int, str]] = []
    used = 0
    for score, index, block in scored:
        if score == 0 and chosen:
            break
        if used + len(block) + 2 > max_chars:
            continue
        chosen.append((index, block))
        used += len(block) + 2
    if not chosen:
        return whole[:max_chars]
    chosen.sort()
    return "\n\n".join(block for _, block in chosen)


def describe(text: str | None, max_chars: int = RAG_MAX_CHARS) -> dict:
    whole = (text or "").strip()
    return {"chars": len(whole), "paragraphs": len(paragraphs(whole)), "whole": len(whole) <= max_chars, "max_chars": max_chars}
