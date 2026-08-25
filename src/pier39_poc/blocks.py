"""Block extraction and cross-page differencing.

The core idea, validated on five live skout product pages: split each page into text
runs ("blocks"), count how many pages each distinct block appears on, and drop the ones
that appear on most pages. Those are navigation, footer, mega-menu and banners.

The threshold must NOT be 1.0. Different pages omit different sections, so requiring a
block to appear on *every* page leaks whole sections into every page's product region.
Measured on skout: `Where do you ship?` appears on 4 of 5 pages, `What does a Skout bar
taste like?` on 3 of 5. At threshold 1.0 the store-wide FAQ survived (1569 words); at
0.8 it did not (664 words).
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field

DROP_ELEMENTS = ("script", "style", "noscript", "svg", "template")

_DROP_RE = re.compile(
    r"(?is)<(" + "|".join(DROP_ELEMENTS) + r")\b[^>]*>.*?</\1\s*>"
)
_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"\s+")

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
    return _WS_RE.sub(" ", html.unescape(h)).strip()


def extract_blocks(raw_html: str, min_chars: int = 3) -> list[str]:
    h = _DROP_RE.sub(" ", raw_html)
    h = _COMMENT_RE.sub(" ", h)
    out: list[str] = []
    for part in _TAG_RE.split(h):
        text = _WS_RE.sub(" ", html.unescape(part)).strip()
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
    r"^(select|choose|pick|enter|search|filter|sort|quantity|qty)\b",
    r"^[$£€]",
    r"^\d",
    r"^/",
    r"%",
    r"\bper (bar|box|pack|unit|serving)\b",
)
_NOT_A_LABEL_RE = tuple(re.compile(p, re.IGNORECASE) for p in NOT_A_LABEL_PATTERNS)

MAX_LABEL_WORDS = 4


def looks_like_label(text: str) -> bool:
    return not any(r.search(text) for r in _NOT_A_LABEL_RE)


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
        if not stripped or len(stripped.split()) > MAX_LABEL_WORDS:
            return None
        return (stripped, True) if looks_like_label(stripped) else None
    if (
        len(candidate.split()) <= MAX_LABEL_WORDS
        and not candidate.endswith((".", "!", "?", ","))
        and looks_like_label(candidate)
    ):
        return (candidate, False)
    return None
