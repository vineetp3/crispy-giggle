"""The label gate: spec, widget or uncertain, and what each verdict is allowed to do.

The cases below are the failures that motivated the gate. remi renders
`Quantity: 120 tablets (roughly 4 months of daily use)`, which the global denylist used to
reject as a cart widget. skout renders `Delivery Frequency: One-time`, which no rule keyed
on wording alone can separate from a real specification. Both stores are right about their
own store, which is why the decision is per store rather than global.

skout's `Pack Size` is deliberately NOT the example here. It is a variant picker that the
product owner chose to keep quotable, so it is a live policy decision rather than a fixed
property, and a test asserting either verdict would break the next time that call is
revisited. See docs/PENDING.md 1a.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pier39_poc.core.blocks import inline_label, is_numeric_value
from pier39_poc.core.models import Product
from pier39_poc.infra.config import StoreConfig
from pier39_poc.ingest.labels import (
    SPEC,
    UNCERTAIN,
    WIDGET,
    ClassifierPolicy,
    NonePolicy,
    StaticPolicy,
    get_policy,
    load_reference,
    normalise,
)
from pier39_poc.ingest.merge import build_assertions

REFERENCE = Path(__file__).parent.parent.parent / "config" / "spec_labels"


def store(slug: str, **kwargs) -> StoreConfig:
    return StoreConfig(slug=slug, domain=f"{slug}.example.com", **kwargs)


def test_quantity_survives_the_global_denylist_with_a_real_value():
    assert inline_label("Quantity: 120 tablets (roughly 4 months of daily use)") == (
        "Quantity",
        "120 tablets (roughly 4 months of daily use)",
    )


def test_quantity_with_a_bare_count_is_still_rejected():
    assert inline_label("Quantity: 12") is None


def test_is_numeric_value_rejects_counts_and_dates():
    assert is_numeric_value("12")
    assert is_numeric_value("12/19")
    assert not is_numeric_value("120 tablets")


def test_none_policy_rejects_every_per_product_pair():
    policy = NonePolicy()
    assert policy.verdict(store("remi"), "Material", "BPA-free") == WIDGET
    assert policy.gates_template_constants is False


def test_static_policy_reads_the_reference_set():
    policy = StaticPolicy(directory=REFERENCE)
    assert policy.verdict(store("remi"), "Quantity", "120 tablets") == SPEC
    assert policy.verdict(store("skout"), "Delivery Frequency", "One-time") == WIDGET


def test_the_same_label_may_differ_between_stores():
    """`Quantity` is a real count on remi and a cart control on most stores.

    skout renders no `Quantity` pair, so the pairing shown here is `Quantity` against the
    subscription selector that the same global rule would have to cover.
    """
    policy = StaticPolicy(directory=REFERENCE)
    assert policy.verdict(store("remi"), "Quantity", "120 tablets") == SPEC
    assert policy.verdict(store("skout"), "Delivery Frequency", "Every 4 weeks") == WIDGET


def test_an_unlisted_label_is_uncertain_not_widget():
    policy = StaticPolicy(directory=REFERENCE)
    assert policy.verdict(store("remi"), "Nobody Has Ruled On This", "x") == UNCERTAIN


def test_manual_deny_overrides_the_reference_set():
    policy = StaticPolicy(directory=REFERENCE)
    cfg = store("remi", spec_label_deny=["Material"])
    assert policy.verdict(cfg, "Material", "BPA-free") == WIDGET


def test_manual_allow_overrides_the_reference_set():
    policy = StaticPolicy(directory=REFERENCE)
    cfg = store("skout", spec_label_allow=["Delivery Frequency"])
    assert policy.verdict(cfg, "Delivery Frequency", "One-time") == SPEC


def test_manual_deny_beats_manual_allow():
    policy = StaticPolicy(directory=REFERENCE)
    cfg = store("remi", spec_label_allow=["Material"], spec_label_deny=["Material"])
    assert policy.verdict(cfg, "Material", "BPA-free") == WIDGET


def test_reference_set_rejects_an_unknown_verdict(tmp_path):
    (tmp_path / "bogus.yaml").write_text("labels:\n  Material: probably\n")
    with pytest.raises(ValueError):
        load_reference("bogus", tmp_path)


def test_normalise_folds_case_and_whitespace():
    assert normalise("  Pack   Size ") == "pack size"


def test_get_policy_rejects_an_unknown_name():
    with pytest.raises(ValueError):
        get_policy("wishful")


class StubClient:
    """Stands in for OpenAI so the suite stays offline and token-free."""

    def __init__(self, answer: str):
        self.answer = answer
        self.calls = 0
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        message = type("M", (), {"content": self.answer})
        choice = type("C", (), {"message": message})
        return type("R", (), {"choices": [choice]})


def test_classifier_caches_one_call_per_label(tmp_path):
    cfg = store("remi")
    client = StubClient("SPEC")
    policy = ClassifierPolicy(client=client)
    policy._cache = {"remi": {}}
    policy.cache_path = lambda _s: tmp_path / "verdicts.json"

    assert policy.verdict(cfg, "Material", "BPA-free") == SPEC
    assert policy.verdict(cfg, "Material", "something else entirely") == SPEC
    assert client.calls == 1


def test_classifier_falls_back_to_uncertain_on_an_unparseable_answer(tmp_path):
    cfg = store("remi")
    policy = ClassifierPolicy(client=StubClient("mu"))
    policy._cache = {"remi": {}}
    policy.cache_path = lambda _s: tmp_path / "verdicts.json"
    assert policy.verdict(cfg, "Material", "BPA-free") == UNCERTAIN


def test_classifier_never_overrides_a_manual_deny(tmp_path):
    cfg = store("skout", spec_label_deny=["Delivery Frequency"])
    client = StubClient("SPEC")
    policy = ClassifierPolicy(client=client)
    policy._cache = {"skout": {}}
    policy.cache_path = lambda _s: tmp_path / "verdicts.json"
    assert policy.verdict(cfg, "Delivery Frequency", "One-time") == WIDGET
    assert client.calls == 0


PRODUCT = Product(
    product_id="1",
    handle="deep-clean-freshening-tablets",
    title="Deep Clean Tablets",
    description_html="",
)

PER_PRODUCT = {
    "deep-clean-freshening-tablets": [
        {"label": "Quantity", "value": "120 tablets, about 4 months of daily use"},
        {"label": "Delivery Frequency", "value": "One-time"},
        {"label": "Nobody Has Ruled On This", "value": "some value"},
    ]
}


def theme_assertions(policy, slug):
    return [
        a
        for a in build_assertions(
            PRODUCT,
            {},
            {},
            per_product=PER_PRODUCT,
            policy=policy,
            store=store(slug),
        )
        if a.source_kind == "theme"
    ]


def test_none_policy_stores_no_per_product_theme_assertions():
    assert theme_assertions(NonePolicy(), "remi") == []


def test_spec_becomes_quotable_and_uncertain_becomes_retrieval():
    got = {a.label: a.trust_class for a in theme_assertions(StaticPolicy(directory=REFERENCE), "remi")}
    assert got["Quantity"] == "quotable"
    assert got["Nobody Has Ruled On This"] == "retrieval"


def test_a_widget_label_is_never_stored_at_all():
    per_product = {
        PRODUCT.handle: [
            {"label": "Delivery Frequency", "value": "One-time"},
            {"label": "Pro Tip", "value": "Try them warm!"},
        ]
    }
    out = [
        a
        for a in build_assertions(
            PRODUCT,
            {},
            {},
            per_product=per_product,
            policy=StaticPolicy(directory=REFERENCE),
            store=store("skout"),
        )
        if a.source_kind == "theme"
    ]
    labels = {a.label for a in out}
    assert "Delivery Frequency" not in labels
    assert "Pro Tip" in labels


def test_repeated_labels_on_one_product_do_not_collide():
    per_product = {
        "deep-clean-freshening-tablets": [
            {"label": "Material", "value": "Dental-grade polymer"},
            {"label": "Material", "value": "Food-grade silicone"},
        ]
    }
    out = [
        a
        for a in build_assertions(
            PRODUCT,
            {},
            {},
            per_product=per_product,
            policy=StaticPolicy(directory=REFERENCE),
            store=store("remi"),
        )
        if a.source_kind == "theme"
    ]
    assert len({a.field for a in out}) == 2
