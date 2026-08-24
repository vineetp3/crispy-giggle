"""Metafield value normalisation, page matching, and contamination detection.

Three jobs:

1. Turn a metafield value of any Shopify type into candidate strings that could appear
   on a page. `list.*` yields one candidate per element; `rich_text_field` and `json`
   yield leaf text nodes.

2. Decide whether a candidate appears on the page. This is token-subset overlap, NOT
   exact substring containment. Exact matching rejects `custom.nutrients`, because the
   value is `Protein [1g]` and the theme renders `Protein 1g`. Silently dropping good
   structured fields is the worst available failure mode -- it looks like success.

3. Reject values carrying a foreign product identifier. This one rule kills
   `loox.review_feed`, `stamped.reviews` and `product_seo.seo_tags`, all of which
   attribute one product's content to another.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_WORD_RE = re.compile(r"[a-z0-9]+")

# Types whose values are references, not displayable text.
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


def tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def is_reference_type(mf_type: str) -> bool:
    base = mf_type.removeprefix("list.")
    return base in REFERENCE_TYPES or base.startswith("metaobject_reference")


# --------------------------------------------------------------------------- #
# 1. value -> candidate strings
# --------------------------------------------------------------------------- #

def _leaf_strings(node: object) -> list[str]:
    """Pull every string leaf out of nested JSON (rich text ASTs, json metafields)."""
    out: list[str] = []
    if isinstance(node, str):
        s = node.strip()
        if s:
            out.append(s)
    elif isinstance(node, dict):
        for k, v in node.items():
            # Keys of a json metafield are meaningful too: "Best For", "Storage & Freshness"
            if isinstance(k, str) and " " in k.strip():
                out.append(k.strip())
            out.extend(_leaf_strings(v))
    elif isinstance(node, (list, tuple)):
        for v in node:
            out.extend(_leaf_strings(v))
    return out


def candidates(mf_type: str, raw_value: str) -> list[str]:
    """Normalise a metafield value into strings that might appear on a page."""
    if raw_value is None:
        return []
    value = raw_value.strip()
    if not value:
        return []
    if is_reference_type(mf_type):
        return []

    base = mf_type.removeprefix("list.")
    is_list = mf_type.startswith("list.")

    if base in STRUCTURED_TYPES or value[:1] in "[{":
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return [value]
        out = _leaf_strings(parsed)
        return out or [value]

    if is_list:
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except (ValueError, TypeError):
            pass

    return [value]


# --------------------------------------------------------------------------- #
# 2. matching
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MatchResult:
    matched: bool
    score: float
    reason: str


@dataclass
class PageIndex:
    """A page's tokens as both a set (membership) and a sequence (proximity)."""

    sequence: list[str]
    token_set: set[str]

    @classmethod
    def build(cls, text: str) -> "PageIndex":
        seq = tokens(text)
        return cls(seq, set(seq))

    def within_window(self, needles: list[str], window: int) -> bool:
        """True when every needle occurs inside some window of consecutive tokens."""
        if not needles:
            return False
        want = set(needles)
        if not want.issubset(self.token_set):
            return False
        seq = self.sequence
        for i, tok in enumerate(seq):
            if tok != needles[0]:
                continue
            if want.issubset(seq[i : i + window]):
                return True
        return False


# A one-token value can never be distinctive enough to attribute to a page.
MIN_CANDIDATE_TOKENS = 2
# `55.00` tokenises to two tokens but only four characters. Real content is longer.
MIN_CANDIDATE_CHARS = 8
# Two-token candidates must be adjacent-ish, not merely both present somewhere.
SHORT_CANDIDATE_WINDOW = 6


def match_candidate(
    candidate: str,
    page: PageIndex,
    threshold: float = 0.8,
    min_tokens: int = MIN_CANDIDATE_TOKENS,
    min_chars: int = MIN_CANDIDATE_CHARS,
    window: int = SHORT_CANDIDATE_WINDOW,
) -> MatchResult:
    """Token-subset overlap, tolerant of theme reformatting.

    Exact substring matching is wrong here: `custom.nutrients` holds `Protein [1g]` while
    the theme renders `Protein 1g`. Overlap on normalised tokens survives that.

    Two guards stop junk matching:

    * a token floor and a character floor, which together reject `true`, `new` and
      `55.00` without rejecting genuinely short structured values like `Calories [110]`;
    * a proximity gate for candidates of fewer than three tokens, so `Calories [110]`
      only matches when `calories` and `110` actually appear near each other rather than
      in unrelated corners of the page.
    """
    cand = tokens(candidate)
    if len(cand) < min_tokens:
        return MatchResult(False, 0.0, "too_few_tokens")
    if sum(len(t) for t in cand) < min_chars:
        return MatchResult(False, 0.0, "too_short")

    hits = sum(1 for t in cand if t in page.token_set)
    score = hits / len(cand)

    if len(cand) < 3:
        if page.within_window(cand, window):
            return MatchResult(True, score, "ok_proximity")
        return MatchResult(False, score, "not_adjacent")

    if score >= threshold:
        return MatchResult(True, score, "ok")
    return MatchResult(False, score, "below_threshold")


# --------------------------------------------------------------------------- #
# 3. contamination
# --------------------------------------------------------------------------- #

_GID_RE = re.compile(r"gid://shopify/(\w+)/(\d+)")
_LONG_ID_RE = re.compile(r"\b(\d{10,})\b")
_PRODUCT_URL_RE = re.compile(r"/products/([a-z0-9][a-z0-9\-_]{2,})", re.I)


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
    """True when the value references a product that is not the one owning it.

    Three signals, in order of confidence:

    1. a `gid://shopify/Product/N` where N is not ours;
    2. a `/products/<handle>` on the store's OWN domain where the handle is not ours --
       this catches siblings that have since been deleted or archived, which a
       catalogue-membership check would miss;
    3. a `/products/<handle>` anywhere, where the handle belongs to a product we know.
    """
    if not value:
        return ContaminationResult(False)

    own_id = str(own_product_id).rsplit("/", 1)[-1]
    own_handle = (own_handle or "").lower()

    for kind, num in _GID_RE.findall(value):
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


def detect_foreign_product_ids(
    value: str, own_product_id: str, known_product_ids: frozenset[str]
) -> ContaminationResult:
    """Flag bare numeric ids belonging to other products in the same store.

    Kept separate from `detect_contamination` because it needs the full id set, which
    is only available once the whole catalogue is fetched.
    """
    if not value:
        return ContaminationResult(False)
    own_id = str(own_product_id).rsplit("/", 1)[-1]
    for num in set(_LONG_ID_RE.findall(value)):
        if num != own_id and num in known_product_ids:
            return ContaminationResult(True, f"product id {num} != {own_id}")
    return ContaminationResult(False)
