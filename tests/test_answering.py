"""Product-scoped answering, and the three-way free-from outcome.

The safety property here is the mirror of the negation whitelist in discovery: "this
product declares nothing" must never be reported as "this product is free of it". An
empty answer and an unknown answer are different facts.
"""

from __future__ import annotations

import pytest

from pier39_poc.answering import ProductAnswer
from pier39_poc.search import FreeFromOutcome


def _answer(**kw) -> ProductAnswer:
    base = dict(
        handle="peanut-butter-protein-bar",
        product_id=1,
        title="Peanut Butter Protein Bar",
        store_slug="skout",
        online_store_url=None,
    )
    base.update(kw)
    return ProductAnswer(**base)


def _fact(field, value, trust_class="quotable", label=None):
    return {"field": field, "value": value, "trust_class": trust_class, "label": label}


def test_attribute_lookup_matches_on_field_name():
    a = _answer(quotable=[_fact("custom.nutrients", "Protein [10g]; Calories [210]")])
    assert a.can_answer("nutrition")
    assert not a.can_answer("materials")


def test_attribute_lookup_matches_on_recovered_theme_label():
    a = _answer(quotable=[_fact("theme.constant_3", "BPA-free plastic", label="Material")])
    assert a.can_answer("materials")
    assert a.answers("materials")[0]["value"] == "BPA-free plastic"


def test_retrieval_class_material_is_never_an_answer():
    a = _answer(retrieval=[_fact("custom.blurb", "BPA-free", "retrieval", "Material")])
    assert not a.can_answer("materials")


def test_unknown_attribute_name_returns_nothing():
    a = _answer(quotable=[_fact("custom.nutrients", "Calories [210]")])
    assert a.answers("not_a_real_attribute") == []


@pytest.mark.parametrize(
    "has_declaration,declared_free,answerable",
    [(True, True, True), (True, False, True), (False, False, False)],
)
def test_free_from_outcome_states(has_declaration, declared_free, answerable):
    o = FreeFromOutcome(term="peanut", has_declaration=has_declaration, declared_free=declared_free)
    assert o.answerable is answerable


def test_no_declaration_is_not_declared_free():
    o = FreeFromOutcome(term="peanut", has_declaration=False, declared_free=False)
    assert not o.declared_free
    assert not o.answerable


def test_declares_but_does_not_list_the_term_is_answerable_and_negative():
    o = FreeFromOutcome(term="peanut", has_declaration=True, declared_free=False)
    assert o.answerable
    assert not o.declared_free


def test_a_products_own_name_is_not_an_answer_about_it():
    a = _answer(
        title="Deep Clean + Freshening Tablets",
        quotable=[
            _fact("title", "Deep Clean + Freshening Tablets"),
            _fact("product_type", "Whitening Product"),
            _fact("custom.product_tags", "Deep Clean"),
        ],
    )
    assert len(a.quotable) == 3
    assert len(a.stated) == 1
    assert not any("tablet" in f["value"].lower() for f in a.stated)


def test_identity_fields_are_excluded_from_attribute_answers():
    a = _answer(quotable=[_fact("title", "BPA-free Night Guard Material")])
    assert not a.can_answer("materials")
