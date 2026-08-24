"""Differencing tests against five real skout product pages.

These fixtures were fetched live on 2026-08-24. The numbers asserted below are the
measurements the design rests on; if they move, the design's premise moved with them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pier39_poc.blocks import (
    build_chrome_profile,
    extract_blocks,
    is_noise,
    label_for,
    product_region,
    visible_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "skout"
HANDLES = [
    "peanut-butter",
    "oatmeal-chocolate-chip",
    "lemon-poppyseed",
    "oatmeal-raisin",
    "double-chocolate",
]


@pytest.fixture(scope="module")
def pages() -> dict[str, list[str]]:
    return {
        h: extract_blocks((FIXTURES / f"{h}.html").read_text(errors="ignore"))
        for h in HANDLES
    }


def test_all_fixtures_present():
    for h in HANDLES:
        assert (FIXTURES / f"{h}.html").exists(), h


def test_block_yield_in_expected_range(pages):
    # Measured 384-436 blocks per page. A large deviation means extraction changed.
    for handle, blocks in pages.items():
        assert 300 <= len(blocks) <= 550, f"{handle}: {len(blocks)}"


def test_scripts_and_styles_are_stripped(pages):
    joined = " ".join(pages["peanut-butter"])
    assert "function(" not in joined
    assert "okeSetWidgetSizes" not in joined


def test_unanimity_threshold_leaks_store_wide_faq(pages):
    """The failure mode that justifies threshold < 1.0."""
    profile = build_chrome_profile(pages, threshold=1.0)
    kept = product_region(pages["peanut-butter"], profile, drop_noise=False)
    text = " ".join(kept)
    assert "Where do you ship?" in text, "expected the leak at threshold 1.0"


def test_default_threshold_removes_store_wide_faq(pages):
    profile = build_chrome_profile(pages, threshold=0.8)
    kept = product_region(pages["peanut-butter"], profile, drop_noise=False)
    text = " ".join(kept)
    assert "Where do you ship?" not in text
    assert "At what age can my child eat Skout?" not in text


def test_default_threshold_keeps_the_product_description(pages):
    profile = build_chrome_profile(pages, threshold=0.8)
    kept = product_region(pages["peanut-butter"], profile, drop_noise=False)
    text = " ".join(kept)
    assert "This Peanut Butter cookie came to do one thing" in text
    assert "Organic Peanut Butter Chips" in text


def test_threshold_monotonically_shrinks_the_kept_region(pages):
    sizes = []
    for threshold in (1.0, 0.8, 0.6):
        profile = build_chrome_profile(pages, threshold=threshold)
        kept = product_region(pages["peanut-butter"], profile, drop_noise=False)
        sizes.append(sum(len(b.split()) for b in kept))
    assert sizes[0] > sizes[1] > sizes[2], sizes
    # Measured: 1569 -> 664 -> 503 words.
    assert 1200 <= sizes[0] <= 1900
    assert 450 <= sizes[1] <= 900


def test_section_presence_varies_across_pages(pages):
    """Documents *why* unanimity fails: pages omit different sections."""
    present = [h for h, blocks in pages.items() if "Where do you ship?" in blocks]
    assert 0 < len(present) < len(HANDLES), present


def test_noise_filter_removes_widget_accessibility_text():
    assert is_noise("Yes, this review from Ashley B. was helpful.")
    assert is_noise("person voted yes")
    assert is_noise("Was this helpful?")
    assert not is_noise("Our family is gluten/dairy/and seed oil free.")


def test_foreign_titles_are_dropped(pages):
    profile = build_chrome_profile(pages, threshold=0.8)
    foreign = frozenset({"oatmeal chocolate chip soft baked cookies"})
    kept = product_region(pages["peanut-butter"], profile, foreign_titles=foreign)
    assert "Oatmeal Chocolate Chip Soft Baked Cookies" not in kept


def test_visible_text_matches_block_join(pages):
    raw = (FIXTURES / "peanut-butter.html").read_text(errors="ignore")
    text = visible_text(raw)
    assert "This Peanut Butter cookie came to do one thing" in text


def test_label_recovery():
    blocks = ["Material:", "BPA-free, food-safe plastic", "Battery life:", "30 days"]
    assert label_for(blocks, 1) == "Material"
    assert label_for(blocks, 3) == "Battery life"
    assert label_for(blocks, 0) is None


def test_label_recovery_rejects_prose():
    blocks = ["That is a lot of peanut butter for one cookie, which is the point.", "8g"]
    assert label_for(blocks, 1) is None


def test_product_json_fixture_is_usable():
    data = json.loads((FIXTURES / "peanut-butter.product.json").read_text())
    assert data["handle"] == "skout-organic-peanut-butter-soft-baked-cookies"
    assert data["variants"]
