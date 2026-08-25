"""Collapse duplicate listings of one product into a family at retrieval time.

skout lists the same physical product up to three times: a base handle, a `-bundle`
handle, and a `skout-organic-` prefixed legacy listing. Ten products in the peanut-butter
protein bar family carry byte-identical `free_from` values, so five result slots can go to
five spellings of one bar while other flavours never surface.

Collapsing happens here rather than at index time on purpose. Every product stays indexed
and individually addressable, bundles stay retrievable, and the behaviour is one toggle.
Merging at index time would make the bundle listings unreachable and cost a full re-index
to undo.

The grouping key is the normalised title, not the `edges` table. `flavor_of` (208 rows on
skout), `bundle_prebuilt` and `bundle_extra` are merchant-declared bundle-to-member links
-- a 15-pack pointing at the three flavours inside it -- which is a different relation from
two listings of one bar. Measured against the live catalogue, the title key collapses skout
172 -> 121 and remi 48 -> 44.

Canonical selection prefers a non-bundle listing, then the most quotable assertions, then
the shortest handle: `peanut-butter-protein-bar` (14 quotable) wins over
`peanut-butter-protein-bar-bundle` (12) and `peanut-butter-organic-protein-bar` (9).

A family ranks where its best member ranked, so the canonical adopts that member's fused
score. Without it the canonical carries its own lower score and the result list stops
being monotonic in rrf, which reads as a ranking bug.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

_PACK_SUFFIX_RE = re.compile(
    r"\s*[-‐-―]\s*(bundle|\d+\s*pack|pack of \d+)\b.*$", re.IGNORECASE
)
_TRAILING_BUNDLE_RE = re.compile(r"\s+bundle\s*$", re.IGNORECASE)
_BUNDLE_RE = re.compile(r"\b(bundle|\d+\s*pack|pack of \d+|variety pack)\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


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
    return _WS_RE.sub(" ", text).strip()


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
