"""Matching and contamination tests, using values observed on skout and remi."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pier39_poc.blocks import visible_text
from pier39_poc.matching import (
    PageIndex,
    candidates,
    detect_contamination,
    detect_foreign_product_ids,
    is_reference_type,
    match_candidate,
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
    assert match_candidate("Calories [110]", page).matched
    assert not match_candidate("Hazelnuts Poppyseed", page).matched


def test_long_candidate_requires_locality(page):
    """A page-wide bag-of-words match is not a render signal.

    Every token below is on the page; none of it is on the page as a run. Before the
    locality gate this scored 0.83 and promoted unrendered LLM enrichment to quotable.
    """
    scattered = (
        "peanut butter cookies organic soft baked chocolate treats suitable "
        "brands contrast discount referral credits"
    )
    assert not match_candidate(scattered, page).matched


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


def test_rich_text_ast_keys_are_not_content():
    """`type` values are structure, not text.

    A generic string-leaf walk harvested "root"/"paragraph"/"text" as candidates, which
    polluted every embedded document and made cands[0] identical across all products.
    """
    raw = (
        '{"type":"root","children":[{"type":"paragraph","children":['
        '{"type":"text","value":"ONLY 5 SIMPLE INGREDIENTS! ","bold":true},'
        '{"type":"text","value":"Think apple-orchard air."}]}]}'
    )
    got = candidates("rich_text_field", raw)
    assert not {"root", "paragraph", "text"} & set(got), got
    assert got == ["ONLY 5 SIMPLE INGREDIENTS! Think apple-orchard air."]


def test_json_metafield_keeps_keyed_attributes():
    raw = '{"Best For":"school lunches","Storage & Freshness":"keep sealed"}'
    got = candidates("json", raw)
    assert "Best For" in got and "school lunches" in got


def test_commerce_facts_are_rejected_but_ratings_are_not():
    """Bare numbers are not commerce facts.

    `reviews.rating` is 4.8 and `reviews.rating_count` is 72; rejecting those as commerce
    would discard review data the conflict rule in `merge` already handles. Every price
    field observed on either store names itself, so the key stem carries those.
    """
    from pier39_poc.profile import is_commerce_fact

    commerce = [
        ("custom", "current_price", "string", ["55.00"]),
        ("custom", "banner_pricing", "number_decimal", ["55.0"]),
        ("custom", "yellow_badge_save_amount", "string", ["$55.00"]),
        ("custom", "price_promotion_text", "string", ["50% Off"]),
        ("custom", "amount_saved_purchasing_subscription", "number_decimal", ["108.0"]),
        ("shop", "anything", "money", ["12.00"]),
    ]
    for ns, key, mf_type, values in commerce:
        assert is_commerce_fact(ns, key, mf_type, values), f"{ns}.{key}"

    keep = [
        ("custom", "nutrients", "list.single_line_text_field", ["Protein [1g]"]),
        ("filter", "ingredients", "list.single_line_text_field", ["Organic Date Syrup"]),
        ("reviews", "rating", "rating", ["4.8"]),
        ("reviews", "rating_count", "number_integer", ["72"]),
        ("loox", "avg_rating", "string", ["3.8"]),
        ("filter", "contains", "list.single_line_text_field", ["Almonds"]),
    ]
    for ns, key, mf_type, values in keep:
        assert not is_commerce_fact(ns, key, mf_type, values), f"{ns}.{key}"


def test_abandoned_skus_are_not_selectable():
    """Published is not buyable. `sellable` missing means sellable (pre-verdict data)."""
    from pier39_poc.crawl import selectable

    products = [
        {"handle": "live", "online_store_url": "https://x/products/live", "sellable": True},
        {"handle": "dead", "online_store_url": "https://x/products/dead", "sellable": False},
        {"handle": "unpublished", "online_store_url": None, "sellable": True},
        {"handle": "legacy", "online_store_url": "https://x/products/legacy"},
    ]
    assert [p["handle"] for p in selectable(products)] == ["live", "legacy"]


def test_foreign_title_catches_text_only_contamination():
    """The case DESIGN.md 10 recorded as undetectable.

    skout's peanut-butter cookie carries apple-pie copy with no product id, gid or URL
    in it, so neither contamination rule can see it.
    """
    from pier39_poc.matching import (
        detect_foreign_product_title,
        distinctive_title_tokens,
    )

    # Enough of the catalogue that "skout", "organic", "kids" and "bar" are common and
    # therefore not distinctive, which is what makes "apple pie" a product identity.
    titles = {
        "skout-organic-peanut-butter-soft-baked-cookies": "Skout Organic Peanut Butter Soft Baked Cookies",
        "apple-pie-organic-kids-snack-bars": "Skout Organic Apple Pie Kids Bar",
        "blueberry-blast-kids-bar": "Skout Organic Blueberry Blast Kids Bar",
        "lemon-zest-protein-bar": "Skout Organic Lemon Zest Protein Bar",
        "mango-mayhem-kids-bar": "Skout Organic Mango Mayhem Kids Bar",
        "double-chocolate-cookies": "Skout Organic Double Chocolate Soft Baked Cookies",
        "oatmeal-raisin-cookies": "Skout Organic Oatmeal Raisin Soft Baked Cookies",
    }
    dist = distinctive_title_tokens(titles)
    assert dist["apple-pie-organic-kids-snack-bars"] == ("apple", "pie")
    value = (
        "No Thanksgiving necessary. This bar will curb your cravings for Grandma's "
        "apple pie 365 days a year. We blended five ingredients to craft the perfect "
        "taste and texture for you and your family."
    )
    got = detect_foreign_product_title(
        value, "skout-organic-peanut-butter-soft-baked-cookies", dist
    )
    assert got.contaminated, got

    own = "Peanut butter cookies made with organic peanut butter and a pinch of salt for contrast every time"
    assert not detect_foreign_product_title(
        own, "skout-organic-peanut-butter-soft-baked-cookies", dist
    ).contaminated


def test_foreign_title_ignores_sku_format_names_and_short_values():
    from pier39_poc.matching import (
        detect_foreign_product_title,
        distinctive_title_tokens,
    )

    titles = {
        "chocolate-banana-kids-bar": "Skout Organic Chocolate Banana Kids Bar",
        "skout-organic-kids-bar-bundle-pack": "Skout Organic Kids Bar Small Batch Bundle Pack",
    }
    dist = distinctive_title_tokens(titles)
    prose = (
        "Our newest limited run is here. This is a small batch flavour we make once a "
        "year and it always sells out before the season ends, so grab one early."
    )
    assert not detect_foreign_product_title(
        prose, "chocolate-banana-kids-bar", dist
    ).contaminated
    assert not detect_foreign_product_title(
        "Small Batch", "chocolate-banana-kids-bar", dist
    ).contaminated
