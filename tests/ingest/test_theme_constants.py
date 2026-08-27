"""Theme spec recovery: inline labels, and specs shared across a template group.

Both bugs here cost remi its night-guard material, which the page states plainly as
`Material: Dental-grade polymer, BPA-free, and phthalate-free.`
"""

from __future__ import annotations

from pier39_poc.core.blocks import inline_label
from pier39_poc.core.quotability import is_commerce_constant
from pier39_poc.ingest.profiling import _spec_pairs


def test_inline_label_reads_a_single_run_spec():
    assert inline_label("Material: Dental-grade polymer, BPA-free.") == (
        "Material",
        "Dental-grade polymer, BPA-free.",
    )
    assert inline_label("Tank capacity: 300ml") == ("Tank capacity", "300ml")


def test_inline_label_keeps_the_existing_junk_guards():
    assert inline_label("February: 2/12") is None
    assert inline_label("FIND IN A SKOUT BAR: 4.5") is None
    assert inline_label("We also ship internationally to: Australia") is None


def test_spec_pairs_reads_an_inline_label_with_no_preceding_block():
    block = "Material: Dental-grade polymer, BPA-free, and phthalate-free."
    pairs = _spec_pairs([block], {block})
    assert len(pairs) == 1
    assert pairs[0]["label"] == "Material"
    assert pairs[0]["value"] == "Dental-grade polymer, BPA-free, and phthalate-free."
    assert pairs[0]["block"] == block


def test_spec_pairs_still_reads_a_label_in_the_preceding_block():
    region = ["Battery life:", "30 days per charge with daily use"]
    pairs = _spec_pairs(region, {region[1]})
    assert len(pairs) == 1
    assert pairs[0]["label"] == "Battery life"
    assert pairs[0]["value"] == "30 days per charge with daily use"


def test_spec_pairs_drops_a_promotion():
    block = "Birthday Sale: 50% Off"
    assert _spec_pairs([block], {block}) == []


def test_commerce_constant_keeps_a_concentration_and_drops_a_discount():
    assert is_commerce_constant("Birthday Sale", "50% Off")
    assert is_commerce_constant("Birthday Sale", "Up to 67% Off")
    assert is_commerce_constant("Subscribe & Save", "Two custom-fit night guards")
    assert not is_commerce_constant("Formula", "3.8% Hydrogen Peroxide. Gluten-free")
    assert not is_commerce_constant("Material", "Dental-grade polymer, BPA-free")


def test_commerce_constant_drops_a_currency_amount_and_free_shipping():
    assert is_commerce_constant("Anything", "$49 today only")
    assert is_commerce_constant("Anything", "Free shipping on every order")
