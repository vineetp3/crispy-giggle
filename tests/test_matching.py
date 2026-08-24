"""Matching and contamination tests, using values observed on skout and remi."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pier39_poc.blocks import extract_blocks, visible_text
from pier39_poc.matching import (
    PageIndex,
    candidates,
    detect_contamination,
    detect_foreign_product_ids,
    is_reference_type,
    match_candidate,
    tokens,
)

FIXTURES = Path(__file__).parent / "fixtures" / "skout"


# --------------------------------------------------------------------------- #
# candidate extraction
# --------------------------------------------------------------------------- #

def test_list_type_yields_one_candidate_per_element():
    value = json.dumps(
        ["Organic Date Syrup", "Organic Peanut Butter", "Himalayan Pink Salt"]
    )
    out = candidates("list.single_line_text_field", value)
    assert out == ["Organic Date Syrup", "Organic Peanut Butter", "Himalayan Pink Salt"]


def test_nutrients_value_yields_bracketed_elements():
    value = json.dumps(
        ["Protein [1g]", "Carbs [13g]", "Calories [110]", "Fiber [1g]", "Sugar [8g]"]
    )
    out = candidates("list.single_line_text_field", value)
    assert "Calories [110]" in out


def test_rich_text_yields_leaf_text():
    value = json.dumps(
        {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [{"type": "text", "value": "The Rolls-Royce of water flossers"}],
                }
            ],
        }
    )
    out = candidates("rich_text_field", value)
    assert "The Rolls-Royce of water flossers" in out


def test_json_metafield_yields_keys_and_values():
    value = json.dumps(
        {
            "Best For": "School lunches, after-school snacks",
            "Storage & Freshness": "Shelf-stable; store in a cool, dry place",
        }
    )
    out = candidates("json", value)
    assert "Best For" in out
    assert "Storage & Freshness" in out
    assert any("School lunches" in c for c in out)


def test_reference_types_yield_no_candidates():
    assert candidates("file_reference", "gid://shopify/MediaImage/123") == []
    assert candidates("list.metaobject_reference", '["gid://shopify/Metaobject/1"]') == []
    assert is_reference_type("list.product_reference")
    assert not is_reference_type("single_line_text_field")


def test_plain_text_passes_through():
    assert candidates("single_line_text_field", "Soft Baked Cookies") == ["Soft Baked Cookies"]
    assert candidates("single_line_text_field", "  ") == []


# --------------------------------------------------------------------------- #
# matching tolerance -- the reason exact containment is wrong
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def page() -> PageIndex:
    raw = (FIXTURES / "peanut-butter.html").read_text(errors="ignore")
    return PageIndex.build(visible_text(raw))


def test_bracketed_nutrient_matches_despite_theme_reformatting(page):
    """The page renders `Protein [1g]` as `Protein 1g`. Exact matching would fail."""
    assert "Calories [110]" not in visible_text(
        (FIXTURES / "peanut-butter.html").read_text(errors="ignore")
    )
    result = match_candidate("Calories [110]", page)
    assert result.matched, result


def test_ingredient_element_matches(page):
    assert match_candidate("Organic Peanut Butter Chips", page).matched


def test_absent_value_does_not_match(page):
    # filter.contains lists allergens NOT in the product; it renders nowhere.
    assert not match_candidate("Hazelnuts Pecans Walnuts", page).matched


def test_short_candidates_are_refused(page):
    for junk in ("true", "new", "55.00", "0"):
        r = match_candidate(junk, page)
        assert not r.matched and r.reason in ("too_short", "too_few_tokens"), junk


def test_threshold_is_respected(page):
    # Mostly-present phrase with one foreign token still clears 0.8 at 5 tokens.
    assert match_candidate("Organic Peanut Butter Chips Salt", page, threshold=0.8).matched
    assert not match_candidate(
        "quantum flux capacitor manifold assembly", page
    ).matched


def test_two_token_candidate_requires_proximity(page):
    """`calories` and `110` both appear on the page; scattered tokens must not match."""
    assert match_candidate("Calories [110]", page).reason == "ok_proximity"
    # Two tokens that exist on the page but never adjacently.
    assert not match_candidate("Hazelnuts Poppyseed", page).matched


# --------------------------------------------------------------------------- #
# contamination -- the highest-value rejection rule
# --------------------------------------------------------------------------- #

SKOUT_PB_ID = "6942124474451"
APPLE_PIE_ID = "3934936825939"


def test_stamped_reviews_blob_is_contaminated_by_product_url():
    value = (
        "<div class='stamped-review-product'>"
        "<a href='//www.skoutorganic.com/products/skout-organic-apple-pie-kids-bar'>"
        "Skout Organic Apple Pie Kids Bar</a></div>"
    )
    known = frozenset(
        {
            "skout-organic-apple-pie-kids-bar",
            "skout-organic-peanut-butter-soft-baked-cookies",
        }
    )
    r = detect_contamination(
        value, SKOUT_PB_ID, "skout-organic-peanut-butter-soft-baked-cookies", known
    )
    assert r.contaminated, r


def test_loox_feed_is_contaminated_by_foreign_numeric_id():
    value = json.dumps(
        {
            "context": {"productId": "8089718030549"},
            "reviews": [
                {
                    "review": "It did not clean my retainer at all.",
                    "product": {"id": "8961879998677"},
                }
            ],
        }
    )
    known_ids = frozenset({"8089718030549", "8961879998677", "8374161080533"})
    r = detect_foreign_product_ids(value, "8089718030549", known_ids)
    assert r.contaminated, r


def test_own_gid_is_not_contamination():
    value = f"gid://shopify/Product/{SKOUT_PB_ID}"
    r = detect_contamination(value, SKOUT_PB_ID, "skout-organic-peanut-butter-soft-baked-cookies")
    assert not r.contaminated


def test_media_image_gid_is_not_product_contamination():
    value = json.dumps(["gid://shopify/MediaImage/53282278506860"])
    r = detect_contamination(value, SKOUT_PB_ID, "skout-organic-peanut-butter-soft-baked-cookies")
    assert not r.contaminated


def test_unknown_handle_in_value_is_not_flagged():
    """Only handles known to the store count; arbitrary URLs are not our business."""
    value = "see https://example.com/products/some-other-thing"
    r = detect_contamination(
        value, SKOUT_PB_ID, "skout-organic-peanut-butter-soft-baked-cookies", frozenset()
    )
    assert not r.contaminated


def test_own_handle_url_is_not_contamination():
    handle = "skout-organic-peanut-butter-soft-baked-cookies"
    value = f"//www.skoutorganic.com/products/{handle}"
    r = detect_contamination(value, SKOUT_PB_ID, handle, frozenset({handle}))
    assert not r.contaminated
