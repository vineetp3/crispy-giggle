"""End-to-end profile against the real skout pages, with the synthetic seed catalogue.

The metafield values in tests/fixtures/skout/seed_catalogue.json are the actual values
observed on skout-organic-peanut-butter-soft-baked-cookies on 2026-08-21, including the two
hazards: the `stamped.reviews` blob belonging to a different product, and
`custom.admin_title`. Covers DESIGN.md success criteria 3, 4 and 5 without a token.
"""

from __future__ import annotations

import pytest

APPLE_PIE_ID = "3934936825939"


@pytest.fixture()
def payload(store):
    from pier39_poc.ingest.profiling import build_profile

    return build_profile(store)


def _verdict(payload, namespace, key):
    for entry in [*payload.allowlist, *payload.rejected]:
        if entry.namespace == namespace and entry.key == key:
            return entry
    return None


# ------------------------------------------------------------------ criterion 3

def test_nutrients_is_admitted_despite_bracket_reformatting(payload):
    """The theme renders `Protein [1g]` as `Protein 1g`. DESIGN.md criterion 3."""
    verdict = _verdict(payload, "custom", "nutrients")
    assert verdict is not None
    assert verdict.admitted, verdict
    assert verdict.hit_rate and verdict.hit_rate > 0, verdict


def test_ingredients_is_admitted(payload):
    verdict = _verdict(payload, "filter", "ingredients")
    assert verdict is not None and verdict.admitted, verdict


# ------------------------------------------------------------------ criterion 4

def test_stamped_reviews_rejected_as_foreign_product(payload):
    """DESIGN.md criterion 4."""
    verdict = _verdict(payload, "stamped", "reviews")
    assert verdict is not None
    assert not verdict.admitted
    assert verdict.reason == "foreign_product_id", verdict


def test_admin_title_never_admitted(payload):
    verdict = _verdict(payload, "custom", "admin_title")
    assert verdict is not None and not verdict.admitted
    assert verdict.reason == "always_excluded"


def test_file_reference_is_not_content(payload):
    verdict = _verdict(payload, "display", "certification")
    assert verdict is not None and not verdict.admitted
    assert verdict.reason == "reference_type"


# ------------------------------------------------------------------ policy

def test_unrendered_enrichment_is_kept_as_retrieval_material(payload):
    """filter.contains renders nowhere, but must not be discarded."""
    verdict = _verdict(payload, "filter", "contains")
    assert verdict is not None and verdict.admitted, verdict
    assert verdict.reason in ("unrendered_retrieval_only", "low_support_admitted")


def test_faqs_are_kept(payload):
    verdict = _verdict(payload, "custom", "product_faqs")
    assert verdict is not None and verdict.admitted, verdict


# ------------------------------------------------------------------ coverage

def test_coverage_is_reported(payload):
    coverage = payload.coverage
    assert coverage.region_words_total > 0
    assert coverage.coverage_pct is not None
    assert 0 <= coverage.coverage_pct <= 100


def test_chrome_removal_actually_removed_something(payload, seeded_handles):
    assert payload.chrome.blocks > 100
    assert payload.pages_analysed == len(seeded_handles)


def test_region_words_are_smaller_than_the_raw_page(payload):
    for handle, words in payload.region_words.items():
        assert 50 < words < 1500, (handle, words)


def test_attribute_reachability_separates_sources():
    from pier39_poc.core.attributes import build, summary

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
    from pier39_poc.core.attributes import build

    got = build([], {"t": [{"value": "Battery life is great", "label": None}]}, {})
    assert got["power"]["sources"] == []


def test_negation_question_scoring_is_constraint_based():
    """A constraint query passes when every result satisfies it, not when a named
    handle appears. Relevance is scored separately so the two cannot trade off."""
    import pier39_poc.evaluation.harness as ev

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
    import pier39_poc.evaluation.harness as ev

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
