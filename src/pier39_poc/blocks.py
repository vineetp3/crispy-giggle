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

# Templated widget text. Unique per review/item, so it never repeats across pages and
# differencing cannot remove it. Observed on Okendo and Loox widgets.
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
    """Strip scripts/styles/comments/tags and normalise whitespace."""
    h = _DROP_RE.sub(" ", raw_html)
    h = _COMMENT_RE.sub(" ", h)
    h = _TAG_RE.sub(" ", h)
    return _WS_RE.sub(" ", html.unescape(h)).strip()


def extract_blocks(raw_html: str, min_chars: int = 3) -> list[str]:
    """Split a page into ordered text runs.

    Runs are delimited by tag boundaries, which keeps `<strong>Material:</strong> BPA-free`
    as two adjacent blocks -- that adjacency is how labels get recovered later.
    """
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
    """Which blocks are site chrome, derived from a sample of pages."""

    chrome: frozenset[str]
    page_count: int
    threshold: float
    frequency: dict[str, int] = field(default_factory=dict)

    def histogram(self) -> dict[int, int]:
        """distinct-block count keyed by 'appeared on N pages'."""
        hist: Counter[int] = Counter()
        for count in self.frequency.values():
            hist[count] += 1
        return dict(sorted(hist.items()))


def build_chrome_profile(
    pages: dict[str, list[str]], threshold: float = 0.8
) -> ChromeProfile:
    """Count block frequency across pages and mark the frequent ones as chrome.

    `pages` maps a page key (product handle) to its ordered blocks. Each block is counted
    once per page, so repetition within a page does not inflate its frequency.
    """
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
    """Strip chrome, templated widget noise, and sibling product titles.

    `foreign_titles` are the titles of *other* products in the same store, lowercased.
    The variant/flavour selector lists them and it varies per page, so differencing
    cannot remove it -- but the catalogue is already known from the API.
    """
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


def label_for(blocks: list[str], index: int, max_chars: int = 60) -> str | None:
    """Recover the label rendered immediately before a value.

    `<strong>Material:</strong> BPA-free, food-safe plastic` yields "Material".
    This is the only source of human-readable names for opaque keys such as
    `custom.product_blue_content`; the Admin API never provides them.
    """
    if index <= 0:
        return None
    candidate = blocks[index - 1].strip()
    if not candidate or len(candidate) > max_chars:
        return None
    if candidate.endswith(":"):
        return candidate[:-1].strip() or None
    # Short title-ish run with no sentence punctuation also reads as a label.
    if len(candidate.split()) <= 4 and not candidate.endswith((".", "!", "?", ",")):
        return candidate
    return None
