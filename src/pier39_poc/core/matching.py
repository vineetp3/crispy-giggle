"""Metafield value normalisation, page matching, and contamination detection.

Answers "does this value actually render on the page" for ingest.profiling.
Gotchas and their measurements: docs/reference/core.md
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pier39_poc.core.tuning import DEFAULTS

_WORD_RE = re.compile(r"[a-z0-9]+")

REFERENCE_TYPES = (
    "file_reference",
    "metaobject_reference",
    "product_reference",
    "variant_reference",
    "collection_reference",
    "page_reference",
    "company_reference",
    "customer_reference",
    "metaobject",
)

STRUCTURED_TYPES = ("json", "json_string", "rich_text_field", "rating", "dimension",
                    "volume", "weight", "money")

RICH_TEXT_TYPES = ("rich_text_field",)

FREE_FROM_KEYS = frozenset({("filter", "contains")})
FREE_FROM_FIELD = "free_from"

_AST_STRUCTURAL_KEYS = frozenset({"type", "level", "url", "target", "title", "listType"})

_AST_BLOCK_TYPES = frozenset({"paragraph", "heading", "list-item"})


_WS_RE = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def is_reference_type(mf_type: str) -> bool:
    base = mf_type.removeprefix("list.")
    return base in REFERENCE_TYPES or base.startswith("metaobject_reference")


def _rich_text_runs(node: object) -> list[str]:
    if isinstance(node, dict):
        kind = node.get("type")
        children = node.get("children")
        if kind in _AST_BLOCK_TYPES and isinstance(children, list):
            parts = [p for child in children for p in _rich_text_runs(child)]
            joined = " ".join(parts).strip()
            return [joined] if joined else []
        if kind == "text":
            value = node.get("value")
            if isinstance(value, str) and value.strip():
                return [value.strip()]
            return []
        if isinstance(children, list):
            return [p for child in children for p in _rich_text_runs(child)]
        return []
    if isinstance(node, list):
        return [p for child in node for p in _rich_text_runs(child)]
    return []


def _json_leaves(node: object) -> list[str]:
    out: list[str] = []
    if isinstance(node, str):
        s = node.strip()
        if s:
            out.append(s)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in _AST_STRUCTURAL_KEYS:
                continue
            if isinstance(key, str) and " " in key.strip():
                out.append(key.strip())
            out.extend(_json_leaves(value))
    elif isinstance(node, (list, tuple)):
        for value in node:
            out.extend(_json_leaves(value))
    return out


def candidates(mf_type: str, raw_value: str | None) -> list[str]:
    if raw_value is None:
        return []
    value = raw_value.strip()
    if not value:
        return []
    if is_reference_type(mf_type):
        return []

    base = mf_type.removeprefix("list.")
    is_list = mf_type.startswith("list.")

    if base in RICH_TEXT_TYPES:
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return [value]
        runs = _rich_text_runs(parsed)
        return runs or [value]

    if base in ("rating", "dimension", "volume", "weight", "money"):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return [value]
        if isinstance(parsed, dict):
            scalar = parsed.get("value")
            unit = parsed.get("unit") or parsed.get("currency_code")
            if scalar is not None:
                return [f"{scalar} {unit}".strip() if unit else str(scalar)]
        return [value]

    if base in STRUCTURED_TYPES or value[:1] in "[{":
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return [value]
        out = _json_leaves(parsed)
        return out or [value]

    if is_list:
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except (ValueError, TypeError):
            pass

    return [value]


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    score: float
    reason: str


@dataclass
class PageIndex:

    sequence: list[str]
    token_set: set[str]

    @classmethod
    def build(cls, text: str) -> PageIndex:
        seq = tokens(text)
        return cls(seq, set(seq))

    def best_window_overlap(self, needles: list[str], window: int) -> float:
        want = set(needles)
        if not want or not self.sequence:
            return 0.0
        seq = self.sequence
        span = min(max(window, 1), len(seq))
        counts: dict[str, int] = {}
        distinct = 0
        best = 0
        for i, tok in enumerate(seq):
            if tok in want:
                seen = counts.get(tok, 0)
                counts[tok] = seen + 1
                if seen == 0:
                    distinct += 1
            if i >= span:
                leaving = seq[i - span]
                if leaving in want:
                    counts[leaving] -= 1
                    if counts[leaving] == 0:
                        distinct -= 1
            if distinct > best:
                best = distinct
                if best == len(want):
                    break
        return best / len(want)


def match_candidate(
    candidate: str,
    page: PageIndex,
    threshold: float = DEFAULTS.matching.containment_threshold,
    min_tokens: int = DEFAULTS.matching.min_candidate_tokens,
    min_chars: int = DEFAULTS.matching.min_candidate_chars,
    slack: float = DEFAULTS.matching.window_slack,
) -> MatchResult:
    cand = tokens(candidate)
    if len(cand) < min_tokens:
        return MatchResult(False, 0.0, "too_few_tokens")
    if sum(len(t) for t in cand) < min_chars:
        return MatchResult(False, 0.0, "too_short")

    distinct = set(cand)
    present = sum(1 for t in distinct if t in page.token_set)
    score = present / len(distinct)
    if score < threshold:
        return MatchResult(False, score, "below_threshold")

    window = max(DEFAULTS.matching.min_window, int(len(cand) * slack))
    local = page.best_window_overlap(cand, window)
    if local >= threshold:
        return MatchResult(True, local, "ok")
    return MatchResult(False, local, "not_local")


GID_RE = re.compile(r"gid://shopify/(\w+)/(\d+)")
_LONG_ID_RE = re.compile(r"\b(\d{10,})\b")
_PRODUCT_URL_RE = re.compile(r"/products/([a-z0-9][a-z0-9\-_]{2,})", re.IGNORECASE)


@dataclass(frozen=True)
class ContaminationResult:
    contaminated: bool
    detail: str = ""


def detect_contamination(
    value: str,
    own_product_id: str,
    own_handle: str,
    known_handles: frozenset[str] = frozenset(),
    store_domain: str | None = None,
) -> ContaminationResult:
    if not value:
        return ContaminationResult(False)

    own_id = str(own_product_id).rsplit("/", 1)[-1]
    own_handle = (own_handle or "").lower()

    for kind, num in GID_RE.findall(value):
        if kind == "Product" and num != own_id:
            return ContaminationResult(True, f"gid Product/{num} != {own_id}")

    domain = (store_domain or "").lower().removeprefix("www.")
    for match in _PRODUCT_URL_RE.finditer(value):
        handle = match.group(1).lower()
        if handle == own_handle:
            continue
        prefix = value[max(0, match.start() - 120) : match.start()].lower()
        if domain and domain in prefix:
            return ContaminationResult(
                True, f"own-domain /products/{handle} != {own_handle}"
            )
        if handle in known_handles:
            return ContaminationResult(True, f"/products/{handle} != {own_handle}")

    return ContaminationResult(False)


def distinctive_title_tokens(
    titles: dict[str, str], max_document_frequency: float = 0.2
) -> dict[str, tuple[str, ...]]:
    if not titles:
        return {}
    frequency: dict[str, int] = {}
    tokenised = {h: set(tokens(t)) for h, t in titles.items()}
    for toks in tokenised.values():
        for tok in toks:
            frequency[tok] = frequency.get(tok, 0) + 1
    ceiling = max(1, int(max_document_frequency * len(titles)))
    return {
        handle: tuple(sorted(t for t in toks if frequency[t] <= ceiling and len(t) > 2))
        for handle, toks in tokenised.items()
    }


SKU_FORMAT_WORDS = frozenset({
    "pack", "bundle", "box", "batch", "variety", "sample", "build", "set", "kit",
    "case", "small", "large", "mini", "single", "multi", "count", "combo", "starter",
})


def _related_handles(a: str, b: str) -> bool:
    return a in b or b in a


def _is_sku_format_name(toks: tuple[str, ...]) -> bool:
    return all(t in SKU_FORMAT_WORDS for t in toks)


def detect_foreign_product_title(
    value: str,
    own_handle: str,
    distinctive: dict[str, tuple[str, ...]],
    min_tokens: int = 2,
    slack: float = 3.0,
) -> ContaminationResult:
    if not value or not distinctive:
        return ContaminationResult(False)
    index = PageIndex.build(value)
    if len(index.sequence) < DEFAULTS.matching.min_prose_tokens_for_title_check:
        return ContaminationResult(False)

    own = distinctive.get(own_handle) or ()
    if own and index.best_window_overlap(
        list(own), max(DEFAULTS.matching.min_window, int(len(own) * slack))
    ) == 1.0:
        return ContaminationResult(False)
    if _is_sku_format_name(own) or any(w in own_handle for w in SKU_FORMAT_WORDS):
        return ContaminationResult(False)

    for handle, toks in distinctive.items():
        if handle == own_handle or len(toks) < min_tokens:
            continue
        if _related_handles(handle, own_handle) or _is_sku_format_name(toks):
            continue
        window = max(DEFAULTS.matching.min_window, int(len(toks) * slack))
        if index.best_window_overlap(list(toks), window) == 1.0:
            return ContaminationResult(
                True, f"describes {handle} ({', '.join(toks)}), not {own_handle}"
            )
    return ContaminationResult(False)


def detect_foreign_product_ids(
    value: str, own_product_id: str, known_product_ids: frozenset[str]
) -> ContaminationResult:
    if not value:
        return ContaminationResult(False)
    own_id = str(own_product_id).rsplit("/", 1)[-1]
    for num in set(_LONG_ID_RE.findall(value)):
        if num != own_id and num in known_product_ids:
            return ContaminationResult(True, f"product id {num} != {own_id}")
    return ContaminationResult(False)
