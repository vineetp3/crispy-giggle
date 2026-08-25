"""End-to-end profile test against the real skout pages, with a synthetic api.jsonl.

The metafield values below are the actual values observed on
skout-organic-peanut-butter-soft-baked-cookies on 2026-08-21, including the two hazards:
the `stamped.reviews` blob that belongs to a different product, and `custom.admin_title`.

This test covers DESIGN.md success criteria 3, 4 and 5 without needing a token.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pier39_poc.config import StoreConfig

FIXTURES = Path(__file__).parent / "fixtures" / "skout"
HANDLES = [
    "peanut-butter",
    "oatmeal-chocolate-chip",
    "lemon-poppyseed",
    "oatmeal-raisin",
    "double-chocolate",
]
IDS = {
    "peanut-butter": "6942124474451",
    "oatmeal-chocolate-chip": "6942124507219",
    "lemon-poppyseed": "6942124539987",
    "oatmeal-raisin": "6942124572755",
    "double-chocolate": "6942124605523",
}
APPLE_PIE_ID = "3934936825939"

DESCRIPTION = (
    "<p>This Peanut Butter cookie came to do one thing. As if baking organic peanut "
    "butter into a soft, chewy cookie wasn't enough, you'll find creamy peanut butter "
    "chips all through it, plus a pinch of Himalayan pink salt for contrast.</p>"
)


def _metafields(handle: str) -> list[dict]:
    """Real observed values. Only peanut-butter carries the contaminated ones."""
    common = [
        {
            "namespace": "filter",
            "key": "ingredients",
            "type": "list.single_line_text_field",
            "updatedAt": "2026-01-10T00:00:00Z",
            "value": json.dumps(
                [
                    "Organic Date Syrup",
                    "Organic Peanut Butter",
                    "Organic Coconut Sugar",
                    "Organic Coconut Oil",
                    "Organic Oat Flour",
                    "Organic Cassava Flour",
                    "Organic Peanut Butter Chips",
                    "Baking Soda",
                    "Himalayan Pink Salt",
                ]
            ),
        },
        {
            "namespace": "custom",
            "key": "nutrients",
            "type": "list.single_line_text_field",
            "updatedAt": "2026-01-10T00:00:00Z",
            "value": json.dumps(
                ["Protein [1g]", "Carbs [13g]", "Calories [110]", "Fiber [1g]", "Sugar [8g]"]
            ),
        },
        {
            "namespace": "filter",
            "key": "contains",
            "type": "list.single_line_text_field",
            "updatedAt": "2026-01-10T00:00:00Z",
            "value": json.dumps(["Almonds", "Cashews", "Hazelnuts", "Pecans", "Walnuts"]),
        },
        {
            "namespace": "descriptors",
            "key": "subtitle",
            "type": "single_line_text_field",
            "updatedAt": "2026-01-10T00:00:00Z",
            "value": "Soft Baked Cookies",
        },
        {
            "namespace": "custom",
            "key": "product_faqs",
            "type": "json",
            "updatedAt": "2026-02-01T00:00:00Z",
            "value": json.dumps(
                [
                    {
                        "question": "Are these cookies vegan and gluten-free?",
                        "answer": "They are certified vegan and certified gluten free.",
                    }
                ]
            ),
        },
        {
            "namespace": "display",
            "key": "certification",
            "type": "list.file_reference",
            "updatedAt": "2026-01-10T00:00:00Z",
            "value": json.dumps(["gid://shopify/MediaImage/53282278506860"]),
        },
        {
            "namespace": "custom",
            "key": "admin_title",
            "type": "single_line_text_field",
            "updatedAt": "2026-01-10T00:00:00Z",
            "value": f"Internal - {handle}",
        },
    ]
    if handle != "peanut-butter":
        return common
    return common + [
        {
            "namespace": "stamped",
            "key": "reviews",
            "type": "string",
            "updatedAt": "2022-09-10T00:00:00Z",
            "value": (
                "<div class='stamped-review-product'>"
                "<a href='//www.skoutorganic.com/products/skout-organic-apple-pie-kids-bar'>"
                "Skout Organic Apple Pie Kids Bar</a></div>"
                f"<span data-product-id='{APPLE_PIE_ID}'>63 reviews</span>"
            ),
        },
        {
            "namespace": "global",
            "key": "description_tag",
            "type": "string",
            "updatedAt": "2022-09-10T00:00:00Z",
            "value": (
                "No Thanksgiving necessary. This bar will curb your cravings for "
                "Grandma's apple pie 365 days a year."
            ),
        },
    ]


@pytest.fixture()
def store(tmp_path, monkeypatch) -> StoreConfig:
    """A store whose data dir is a tmp dir seeded with the real fixture pages."""
    import pier39_poc.config as config

    # StoreConfig path properties read config.DATA_ROOT at access time.
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)

    cfg = StoreConfig(
        slug="skout",
        domain="www.skoutorganic.com",
        profile_pages=5,
        chrome_threshold=0.8,
        allowlist_min_support=3,
    )
    cfg.pages_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for handle in HANDLES:
        (cfg.pages_dir / f"{handle}.html").write_text(
            (FIXTURES / f"{handle}.html").read_text(errors="ignore"), encoding="utf-8"
        )
        rows.append(
            {
                "id": f"gid://shopify/Product/{IDS[handle]}",
                "product_id": IDS[handle],
                "handle": handle,
                "title": f"Skout Organic {handle.replace('-', ' ').title()} Soft Baked Cookies",
                "vendor": "Skout Organic",
                "product_type": "Soft Baked Cookies",
                "tags": ["cr-ignore"],
                "status": "ACTIVE",
                "online_store_url": f"https://www.skoutorganic.com/products/{handle}",
                "description_html": DESCRIPTION if handle == "peanut-butter" else "<p>A cookie.</p>",
                "template_suffix": None,
                "collections": [{"handle": "soft-baked-cookies", "title": "Soft-Baked Cookies"}],
                "metafields": _metafields(handle),
                "variants": [
                    {
                        "id": f"gid://shopify/ProductVariant/40525591969{i}",
                        "title": f"{i} Boxes",
                        "sku": None,
                        "selectedOptions": [{"name": "Pack Size", "value": f"{i} Boxes"}],
                    }
                    for i in (3, 6)
                ],
            }
        )

    from pier39_poc.artifacts import write_jsonl

    write_jsonl(cfg.api_path, rows)
    return cfg


@pytest.fixture()
def payload(store):
    from pier39_poc.profile import build_profile

    return build_profile(store)


def _verdict(payload, namespace, key):
    for bucket in ("allowlist", "rejected"):
        for entry in payload[bucket]:
            if entry["namespace"] == namespace and entry["key"] == key:
                return entry
    return None


# ------------------------------------------------------------------ criterion 3

def test_nutrients_is_admitted_despite_bracket_reformatting(payload):
    """The theme renders `Protein [1g]` as `Protein 1g`. DESIGN.md criterion 3."""
    verdict = _verdict(payload, "custom", "nutrients")
    assert verdict is not None
    assert verdict["admitted"], verdict
    assert verdict["hit_rate"] and verdict["hit_rate"] > 0, verdict


def test_ingredients_is_admitted(payload):
    verdict = _verdict(payload, "filter", "ingredients")
    assert verdict is not None and verdict["admitted"], verdict


# ------------------------------------------------------------------ criterion 4

def test_stamped_reviews_rejected_as_foreign_product(payload):
    """DESIGN.md criterion 4."""
    verdict = _verdict(payload, "stamped", "reviews")
    assert verdict is not None
    assert not verdict["admitted"]
    assert verdict["reason"] == "foreign_product_id", verdict


def test_admin_title_never_admitted(payload):
    verdict = _verdict(payload, "custom", "admin_title")
    assert verdict is not None and not verdict["admitted"]
    assert verdict["reason"] == "always_excluded"


def test_file_reference_is_not_content(payload):
    verdict = _verdict(payload, "display", "certification")
    assert verdict is not None and not verdict["admitted"]
    assert verdict["reason"] == "reference_type"


# ------------------------------------------------------------------ policy

def test_unrendered_enrichment_is_kept_as_retrieval_material(payload):
    """filter.contains renders nowhere, but must not be discarded."""
    verdict = _verdict(payload, "filter", "contains")
    assert verdict is not None and verdict["admitted"], verdict
    assert verdict["reason"] in ("unrendered_retrieval_only", "low_support_admitted")


def test_faqs_are_kept(payload):
    verdict = _verdict(payload, "custom", "product_faqs")
    assert verdict is not None and verdict["admitted"], verdict


# ------------------------------------------------------------------ coverage

def test_coverage_is_reported(payload):
    coverage = payload["coverage"]
    assert coverage["region_words_total"] > 0
    assert coverage["coverage_pct"] is not None
    assert 0 <= coverage["coverage_pct"] <= 100


def test_chrome_removal_actually_removed_something(payload):
    assert payload["chrome"]["blocks"] > 100
    assert payload["pages_analysed"] == 5


def test_region_words_are_smaller_than_the_raw_page(payload):
    for handle, words in payload["region_words"].items():
        assert 50 < words < 1500, (handle, words)


def test_attribute_reachability_separates_sources():
    from pier39_poc.attributes import build, summary

    allowlist = [
        {"namespace": "filter", "key": "contains", "support": 154, "label": None},
        {"namespace": "custom", "key": "nutrients", "support": 48, "label": None},
        {"namespace": "custom", "key": "short_title", "support": 171, "label": None},
    ]
    constants = {
        "water-flosser": [
            {"value": "30 days per charge", "label": "Battery life", "single_page": True},
            {"value": "BPA-free plastic", "label": "Material", "single_page": True},
            {"value": "unlabelled residual", "label": None, "single_page": True},
        ]
    }
    references = {"custom.nutrition_facts_image": 121, "display.certification": 155}

    got = build(allowlist, constants, references)
    assert got["allergens"]["sources"] == ["api"]
    assert got["nutrition"]["sources"] == ["api", "image"]
    assert got["power"]["sources"] == ["theme"]
    assert got["materials"]["sources"] == ["theme"]
    assert got["certifications"]["sources"] == ["image"]
    assert got["compatibility"]["sources"] == []
    assert "custom.short_title" in got["_unmapped"]["api"]
    assert summary(got)["absent"] >= 1


def test_attribute_table_ignores_unlabelled_constants():
    from pier39_poc.attributes import build

    got = build([], {"t": [{"value": "Battery life is great", "label": None}]}, {})
    assert got["power"]["sources"] == []


def test_negation_question_scoring_is_constraint_based():
    """A constraint query passes when every result satisfies it, not when a named
    handle appears. Relevance is scored separately so the two cannot trade off."""
    import pier39_poc.evaluate as ev

    calls = {}

    class FakeHit:
        def __init__(self, handle):
            self.handle = handle
            self.rerank_score = None
            self.siblings = []

    class FakeStore:
        slug = "skout"

    def fake_search(q, store, **kw):
        return [FakeHit(h) for h in calls["returns"]]

    original_search, original_undeclared = ev.search, ev.undeclared_returns
    try:
        ev.search = fake_search
        ev.undeclared_returns = lambda slug, handles, terms: [
            h for h in handles if h in calls["undeclared"]
        ]

        question = {
            "q": "cookies without peanuts",
            "kind": "negation",
            "exclude_terms": ["peanut"],
            "expect_handles": ["lemon-poppyseed"],
        }

        calls.update(returns=["oatmeal-raisin", "double-chocolate"], undeclared=set())
        got = ev._evaluate_one(question, store=FakeStore(), top_k=5, rerank=False)
        assert got["ok"] is True
        assert got["relevant"] is False

        calls.update(returns=["lemon-poppyseed", "peanut-butter"], undeclared={"peanut-butter"})
        got = ev._evaluate_one(question, store=FakeStore(), top_k=5, rerank=False)
        assert got["ok"] is False
        assert got["violations"] == ["peanut-butter"]
        assert got["relevant"] is True
    finally:
        ev.search, ev.undeclared_returns = original_search, original_undeclared


def test_expect_empty_requires_no_results():
    import pier39_poc.evaluate as ev

    original_search, original_undeclared = ev.search, ev.undeclared_returns
    try:
        ev.search = lambda q, store, **kw: []
        ev.undeclared_returns = lambda slug, handles, terms: []
        class FakeStore:
            slug = "remi"

        question = {"q": "x", "exclude_terms": ["peroxide"], "expect_empty": True}
        assert ev._evaluate_one(
            question, store=FakeStore(), top_k=5, rerank=False
        )["ok"] is True
    finally:
        ev.search, ev.undeclared_returns = original_search, original_undeclared
