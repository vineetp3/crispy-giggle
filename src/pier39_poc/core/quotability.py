"""May a value be repeated to a shopper as fact? Type and shape rules, no IO.

Applied by ingest.profiling when admitting keys and by ingest.merge when assigning trust
class. Gotchas and their measurements: docs/reference/core.md
"""

from __future__ import annotations

import re

from pier39_poc.core.matching import tokens
from pier39_poc.core.tuning import DEFAULTS

PROSE_TYPES = frozenset({"multi_line_text_field", "rich_text_field", "html"})
UNTYPED_TYPES = frozenset({"string", ""})
NEVER_QUOTABLE_TYPES = frozenset({"json", "json_string"})
NEVER_QUOTABLE_NAMESPACES = frozenset({"agentiq"})

_EMBEDDED_PRICE_RE = re.compile(r"[$£€]\s*\d")
_NUMERIC_ONLY_RE = re.compile(r"^[\d\s./:+%-]+$")

WIDGET_MARKERS = ("<div", "<span", "<script", "<link", "data-oke-", "stamped-", "loox")

COMMERCE_TYPES = frozenset({"money"})
_COMMERCE_KEY_RE = re.compile(
    r"pric|cost|msrp|compare_at|saving|saved|discount|promo|_off\b", re.IGNORECASE
)
_COMMERCE_VALUE_RE = re.compile(
    r"^\s*(?:[$£€]\s*\d[\d,.]*|\d[\d,.]*\s*%)\s*$"
)
_PROMO_LABEL_RE = re.compile(r"\b(sale|save|savings|deal|coupon|offer|bundle price)\b", re.IGNORECASE)
_PROMO_VALUE_RE = re.compile(
    r"(\d+\s*%\s*off|\bup to\s+\d+\s*%|[$£€]\s*\d|\bfree shipping\b)", re.IGNORECASE
)

CONTENT_FREE_TYPES = frozenset({"color", "boolean"})
_CONTENT_FREE_RE = re.compile(
    r"^\s*(?:true|false|yes|no|none|null|n/?a|-+|"
    r"#[0-9a-f]{3,8}|"
    r"\d{10}|"
    r"0E-\d+)\s*$",
    re.IGNORECASE,
)


def is_quotable_metafield(
    namespace: str,
    mf_type: str,
    values: list[str],
    max_tokens: int = DEFAULTS.quotability.quotable_max_tokens,
) -> bool:
    if namespace in NEVER_QUOTABLE_NAMESPACES:
        return False
    base = mf_type.removeprefix("list.")
    if base in PROSE_TYPES or base in UNTYPED_TYPES or base in NEVER_QUOTABLE_TYPES:
        return False
    for value in values:
        if "<" in value or value.rstrip().endswith(("?", ":")):
            return False
        if _EMBEDDED_PRICE_RE.search(value):
            return False
        if len(tokens(value)) > max_tokens:
            return False
    return bool(values)


def is_quotable_theme_value(
    value: str, max_tokens: int = DEFAULTS.quotability.theme_quotable_max_tokens
) -> bool:
    if "<" in value or value.rstrip().endswith(("?", ":")):
        return False
    if _EMBEDDED_PRICE_RE.search(value):
        return False
    if _NUMERIC_ONLY_RE.match(value):
        return False
    return 0 < len(tokens(value)) <= max_tokens


def is_widget_markup(raw_value: str) -> bool:
    return any(marker in raw_value for marker in WIDGET_MARKERS)


def is_commerce_fact(namespace: str, key: str, mf_type: str, values: list[str]) -> bool:
    if mf_type.removeprefix("list.") in COMMERCE_TYPES:
        return True
    if _COMMERCE_KEY_RE.search(key) or _COMMERCE_KEY_RE.search(namespace):
        return True
    return bool(values) and all(_COMMERCE_VALUE_RE.match(v) for v in values)


def is_commerce_constant(label: str | None, value: str) -> bool:
    text = (label or "").strip()
    if _COMMERCE_KEY_RE.search(text) or _PROMO_LABEL_RE.search(text):
        return True
    return bool(_PROMO_VALUE_RE.search(value or ""))


def is_content_free(mf_type: str, values: list[str]) -> bool:
    if mf_type.removeprefix("list.") in CONTENT_FREE_TYPES:
        return True
    return bool(values) and all(_CONTENT_FREE_RE.match(v) for v in values)
