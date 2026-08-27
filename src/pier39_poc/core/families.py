"""Collapse duplicate listings of one product into a family, at retrieval time.

Used by retrieval.search between the negation join and the top_k slice.
Gotchas and their measurements: docs/reference/core.md
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from pier39_poc.core.matching import collapse_whitespace

_PACK_SUFFIX_RE = re.compile(
    r"\s*[-‐-―]\s*(bundle|\d+\s*pack|pack of \d+)\b.*$", re.IGNORECASE
)
_TRAILING_BUNDLE_RE = re.compile(r"\s+bundle\s*$", re.IGNORECASE)
_BUNDLE_RE = re.compile(r"\b(bundle|\d+\s*pack|pack of \d+|variety pack)\b", re.IGNORECASE)


def family_key(title: str | None, vendor: str | None = None) -> str:
    text = (title or "").strip().lower()
    if not text:
        return ""
    prefix = (vendor or "").strip().lower()
    if prefix and text.startswith(prefix + " "):
        text = text[len(prefix) + 1 :]
    while True:
        reduced = _TRAILING_BUNDLE_RE.sub("", _PACK_SUFFIX_RE.sub("", text))
        if reduced == text:
            break
        text = reduced
    return collapse_whitespace(text)


def is_bundle(handle: str | None, title: str | None) -> bool:
    return bool(_BUNDLE_RE.search(handle or "")) or bool(_BUNDLE_RE.search(title or ""))


def collapse(
    hits: Sequence[Any], quotable_counts: dict[int, int] | None = None
) -> list[Any]:
    counts = quotable_counts or {}
    order: list[str] = []
    grouped: dict[str, list[Any]] = {}

    for hit in hits:
        key = f"{hit.store_slug}\x00" + (
            family_key(hit.title, getattr(hit, "vendor", None)) or hit.handle
        )
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(hit)

    collapsed: list[Any] = []
    for key in order:
        members = grouped[key]
        canonical = min(
            members,
            key=lambda h: (
                is_bundle(h.handle, h.title),
                -counts.get(h.product_id, 0),
                len(h.handle or ""),
                h.handle or "",
            ),
        )
        canonical.siblings = _sibling_handles(members, canonical)
        _adopt_best_rank(canonical, members)
        collapsed.append(canonical)
    return collapsed


def _adopt_best_rank(canonical: Any, members: Sequence[Any]) -> None:
    if not hasattr(canonical, "rrf"):
        return
    best = max(members, key=lambda h: getattr(h, "rrf", 0.0))
    if best.product_id == canonical.product_id:
        return
    canonical.rrf = best.rrf
    canonical.vector_rank = best.vector_rank
    canonical.lexical_rank = best.lexical_rank


def _sibling_handles(members: Iterable[Any], canonical: Any) -> list[str]:
    seen: dict[str, None] = {}
    for member in members:
        if member.product_id == canonical.product_id:
            continue
        seen.setdefault(member.handle, None)
    return sorted(seen)
