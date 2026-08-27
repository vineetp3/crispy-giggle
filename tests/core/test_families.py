"""Family collapse: duplicate listings of one product become one result."""

from __future__ import annotations

from dataclasses import dataclass, field

from pier39_poc.core.families import collapse, family_key, is_bundle


@dataclass
class FakeHit:
    product_id: int
    handle: str
    title: str
    vendor: str | None = "Skout Organic"
    store_slug: str = "skout"
    siblings: list[str] = field(default_factory=list)


def test_family_key_strips_vendor_prefix_and_pack_suffix():
    key = "peanut butter protein bar"
    assert family_key("Skout Organic Peanut Butter Protein Bar", "Skout Organic") == key
    assert family_key("Peanut Butter Protein Bar - Bundle", "Skout Organic") == key
    assert (
        family_key(
            "Skout Organic Peanut Butter Protein Bar Bundle - 15 Pack", "Skout Organic"
        )
        == key
    )


def test_family_key_keeps_distinct_flavours_apart():
    a = family_key("Skout Organic Apple Pie Kids Bar", "Skout Organic")
    b = family_key("Skout Organic Carrot Cake Kids Bar", "Skout Organic")
    assert a != b


def test_family_key_does_not_collapse_a_variety_pack_into_a_single():
    assert family_key("Kids Bar Variety Pack", "Skout Organic") != family_key(
        "Kids Bar", "Skout Organic"
    )


def test_is_bundle():
    assert is_bundle("peanut-butter-protein-bar-bundle", "")
    assert is_bundle("", "Peanut Butter Protein Bar - 15 Pack")
    assert not is_bundle("peanut-butter-protein-bar", "Peanut Butter Protein Bar")


def test_collapse_prefers_the_non_bundle_listing():
    hits = [
        FakeHit(1, "peanut-butter-protein-bar-bundle", "Peanut Butter Protein Bar - Bundle"),
        FakeHit(2, "peanut-butter-protein-bar", "Peanut Butter Protein Bar"),
    ]
    out = collapse(hits, {1: 99, 2: 1})
    assert len(out) == 1
    assert out[0].handle == "peanut-butter-protein-bar"
    assert out[0].siblings == ["peanut-butter-protein-bar-bundle"]


def test_collapse_breaks_ties_on_quotable_count():
    hits = [
        FakeHit(1, "peanut-butter-organic-protein-bar", "Skout Organic Peanut Butter Protein Bar"),
        FakeHit(2, "peanut-butter-protein-bar", "Peanut Butter Protein Bar"),
    ]
    out = collapse(hits, {1: 9, 2: 14})
    assert out[0].handle == "peanut-butter-protein-bar"
    assert out[0].siblings == ["peanut-butter-organic-protein-bar"]


def test_collapse_preserves_incoming_rank_order():
    hits = [
        FakeHit(1, "carrot-cake-kids-bar", "Carrot Cake Kids Bar"),
        FakeHit(2, "apple-pie-kids-bar", "Apple Pie Kids Bar"),
        FakeHit(3, "apple-pie-kids-bar-bundle", "Apple Pie Kids Bar - Bundle"),
    ]
    out = collapse(hits, {})
    assert [h.handle for h in out] == ["carrot-cake-kids-bar", "apple-pie-kids-bar"]


def test_collapse_keeps_stores_separate():
    hits = [
        FakeHit(1, "night-guard", "Night Guard", vendor="Remi", store_slug="remi"),
        FakeHit(2, "night-guard", "Night Guard", vendor="Skout Organic", store_slug="skout"),
    ]
    assert len(collapse(hits, {})) == 2
