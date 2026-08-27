"""Split a page into text blocks and difference them across pages to strip chrome.

Feeds ingest.profiling, which derives each store's product region from the survivors.
Gotchas and their measurements: docs/reference/core.md
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field

from pier39_poc.core.matching import collapse_whitespace
from pier39_poc.core.tuning import DEFAULTS

DROP_ELEMENTS = ("script", "style", "noscript", "svg", "template")

_DROP_RE = re.compile(
    r"(?is)<(" + "|".join(DROP_ELEMENTS) + r")\b[^>]*>.*?</\1\s*>"
)
_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_TAG_RE = re.compile(r"(?s)<[^>]+>")

NOISE_PATTERNS = (
    r"^(Yes|No), this review from .+ was (not )?helpful\.?$",
    r"^\d*\s*(person|people) voted (yes|no)$",
    r"^Was this helpful\??$",
    r"^Read (More|more)( about this review)?$",
    r"^Rated .+ out of 5( stars)?$",
    r"^Total \d+ star reviews?: ?\d*$",
    r"^Slide \d+ selected$",
    r"^Loading\.{0,3}$",
    r"^Sort$",
    r"^\d+ (Reviews?|reviews?)$",
    r"^(Previous|Next) Slide$",
    r"^Customer-uploaded (image|video)\.?.*$",
    r"^Filter by \d+ star reviews$",
    r"^tab (expanded|collapsed)$",
)
_NOISE_RE = tuple(re.compile(p) for p in NOISE_PATTERNS)


def visible_text(raw_html: str) -> str:
    h = _DROP_RE.sub(" ", raw_html)
    h = _COMMENT_RE.sub(" ", h)
    h = _TAG_RE.sub(" ", h)
    return collapse_whitespace(html.unescape(h))


def extract_blocks(raw_html: str, min_chars: int = 3) -> list[str]:
    h = _DROP_RE.sub(" ", raw_html)
    h = _COMMENT_RE.sub(" ", h)
    out: list[str] = []
    for part in _TAG_RE.split(h):
        text = collapse_whitespace(html.unescape(part))
        if len(text) >= min_chars:
            out.append(text)
    return out


def is_noise(block: str) -> bool:
    return any(r.match(block) for r in _NOISE_RE)


@dataclass
class ChromeProfile:

    chrome: frozenset[str]
    page_count: int
    threshold: float
    frequency: dict[str, int] = field(default_factory=dict)

    def histogram(self) -> dict[int, int]:
        hist: Counter[int] = Counter()
        for count in self.frequency.values():
            hist[count] += 1
        return dict(sorted(hist.items()))


def build_chrome_profile(
    pages: dict[str, list[str]], threshold: float = 0.8
) -> ChromeProfile:
    if not pages:
        return ChromeProfile(frozenset(), 0, threshold, {})

    freq: Counter[str] = Counter()
    for blocks in pages.values():
        freq.update(set(blocks))

    n = len(pages)
    cutoff = threshold * n
    chrome = frozenset(b for b, c in freq.items() if c >= cutoff)
    return ChromeProfile(chrome, n, threshold, dict(freq))


def repeated_block_profile(
    profile: ChromeProfile,
    min_pages: int = DEFAULTS.blocks.cross_page_min_pages,
    min_words: int = DEFAULTS.blocks.cross_page_min_words,
) -> ChromeProfile:
    blocks = frozenset(
        b
        for b, c in profile.frequency.items()
        if c >= min_pages and len(b.split()) >= min_words
    )
    return ChromeProfile(blocks, profile.page_count, profile.threshold, profile.frequency)


def product_region(
    blocks: list[str],
    profile: ChromeProfile,
    foreign_titles: frozenset[str] = frozenset(),
    drop_noise: bool = True,
) -> list[str]:
    out: list[str] = []
    for b in blocks:
        if b in profile.chrome:
            continue
        if drop_noise and is_noise(b):
            continue
        if b.strip().lower() in foreign_titles:
            continue
        out.append(b)
    return out


NOT_A_LABEL_PATTERNS = (
    r"^(sold out|out of stock|in stock|bonus|new|sale|free)$",
    r"^(read|learn|shop|see|view|buy|add|get|save|try)\b",
    r"^(select|choose|pick|enter|search|filter|sort)\b",
    r"^[$£€]",
    r"^\d",
    r"^/",
    r"%",
    r"\bper (bar|box|pack|unit|serving)\b",
)
_NOT_A_LABEL_RE = tuple(re.compile(p, re.IGNORECASE) for p in NOT_A_LABEL_PATTERNS)


def looks_like_label(text: str) -> bool:
    return not any(r.search(text) for r in _NOT_A_LABEL_RE)


_INLINE_LABEL_RE = re.compile(r"^([^:]{1,60}):\s+(\S.*)$")
_NUMERIC_VALUE_RE = re.compile(r"^[\d\s/.,:%+-]+$")


def is_numeric_value(text: str) -> bool:
    return bool(_NUMERIC_VALUE_RE.match((text or "").strip()))


def inline_label(text: str) -> tuple[str, str] | None:
    match = _INLINE_LABEL_RE.match((text or "").strip())
    if not match:
        return None
    label, value = match.group(1).strip(), match.group(2).strip()
    if not label or not value:
        return None
    if len(label.split()) > DEFAULTS.blocks.max_label_words:
        return None
    if not looks_like_label(label):
        return None
    if _NUMERIC_VALUE_RE.match(value):
        return None
    return label, value


def label_for(
    blocks: list[str], index: int, max_chars: int = 60
) -> tuple[str, bool] | None:
    if index <= 0:
        return None
    candidate = blocks[index - 1].strip()
    if not candidate or len(candidate) > max_chars:
        return None
    if candidate.endswith(":"):
        stripped = candidate[:-1].strip()
        if not stripped or len(stripped.split()) > DEFAULTS.blocks.max_label_words:
            return None
        return (stripped, True) if looks_like_label(stripped) else None
    if (
        len(candidate.split()) <= DEFAULTS.blocks.max_label_words
        and not candidate.endswith((".", "!", "?", ","))
        and looks_like_label(candidate)
    ):
        return (candidate, False)
    return None
