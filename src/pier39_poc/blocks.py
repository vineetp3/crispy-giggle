"""Block extraction and cross-page differencing.

The core idea, validated on five live skout product pages: split each page into text
runs ("blocks"), count how many pages each distinct block appears on, and drop the ones
that appear on most pages. Those are navigation, footer, mega-menu and banners.

The threshold must NOT be 1.0. Different pages omit different sections, so requiring a
block to appear on *every* page leaks whole sections into every page's product region.
Measured on skout: `Where do you ship?` appears on 4 of 5 pages, `What does a Skout bar
taste like?` on 3 of 5. At threshold 1.0 the store-wide FAQ survived (1569 words); at
0.8 it did not (664 words).

A ratio threshold cannot catch copy repeated across a handful of pages. Measured on
remi's 30 crawled pages: 1,236 distinct blocks, 72 classed chrome at 0.8 (a 24-page
cutoff), and 2,919 words sitting in blocks that appear on 2 or more pages but below it.
That is where the cross-page marketing copy lives -- the doctor testimonial repeats on
three products at 1,500-2,000 words each. `repeated_block_profile` applies an absolute
page count instead, and `profile._two_level_chrome` uses it only for products alone on
their template, where the per-group pass has no sibling to difference against.

`inline_label` exists because a theme may render a spec as one text run,
`Material: Dental-grade polymer, BPA-free, and phthalate-free.`, rather than as a bold
label node followed by a value node. `label_for` only sees the second shape, so remi's
night guards looked like they had no material at all. The same four-word label cap and
`looks_like_label` guards apply, plus a numeric-value rule, so skout's `February: 2/12`
shipping calendar and `FIND IN A SKOUT BAR: 4.5` rating widget stay excluded.

The floor is 3 pages, not 2. At 2 it strips spec text legitimately shared between two
variants of one product.

**The word floor is load-bearing and was added after a measured regression.** A page count
alone is not enough, because real attributes repeat across sibling products exactly like
boilerplate does. Applying the 3-page rule with no length guard cost remi its
`compatibility` attribute and cost skout both `dimensions` and `usage` -- skout dropped
from `theme 2` to `theme 0`, which is the deliverable this whole stage exists to produce.
Raising the page floor did not fix it: at 5 pages remi recovered but skout did not.

Length separates the two cleanly. The copy this rule targets is long prose -- a
doctor testimonial at 1,500-2,000 words -- while attributes are short `label: value`
pairs. At 20 words both stores keep every attribute they had before the rule, and remi's
coverage still improves from 4.1% to 4.7%. Anything shorter than 20 words is left alone no
matter how often it repeats.
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


CROSS_PAGE_MIN_PAGES = 3
CROSS_PAGE_MIN_WORDS = 20


def repeated_block_profile(
    profile: ChromeProfile,
    min_pages: int = CROSS_PAGE_MIN_PAGES,
    min_words: int = CROSS_PAGE_MIN_WORDS,
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


_INLINE_LABEL_RE = re.compile(r"^([^:]{1,60}):\s+(\S.*)$")
_NUMERIC_VALUE_RE = re.compile(r"^[\d\s/.,:%+-]+$")


def inline_label(text: str) -> tuple[str, str] | None:
    match = _INLINE_LABEL_RE.match((text or "").strip())
    if not match:
        return None
    label, value = match.group(1).strip(), match.group(2).strip()
    if not label or not value:
        return None
    if len(label.split()) > MAX_LABEL_WORDS:
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
