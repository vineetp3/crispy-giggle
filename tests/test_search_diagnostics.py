"""Degraded retrieval must be reported, not swallowed."""

from __future__ import annotations

import sys
import types

from pier39_poc.search import Diagnostics, _brief, _passes_commerce, _rerank


class FakeHit:
    def __init__(self, text="a bar", live=None):
        self.text = text
        self.live = live
        self.rerank_score = None


def test_rerank_records_the_failure_and_keeps_the_fused_order(monkeypatch):
    stub = types.ModuleType("cohere")

    class Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("Incorrect API key provided")

    stub.ClientV2 = Boom
    monkeypatch.setitem(sys.modules, "cohere", stub)

    hits = [FakeHit("one"), FakeHit("two")]
    diag = Diagnostics()
    out = _rerank("query", hits, "rerank-v4.0-fast", diag)

    assert out == hits
    assert diag.rerank_failed is True
    assert "Incorrect API key" in diag.rerank_error
    assert diag.degraded is True


def test_rerank_without_diagnostics_still_degrades(monkeypatch):
    stub = types.ModuleType("cohere")

    class Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("nope")

    stub.ClientV2 = Boom
    monkeypatch.setitem(sys.modules, "cohere", stub)

    hits = [FakeHit()]
    assert _rerank("q", hits, "m", None) == hits


def test_a_hit_with_no_live_read_fails_a_price_filter():
    assert _passes_commerce(FakeHit(live=None), max_price=30.0, in_stock_only=False) is False
    assert _passes_commerce(FakeHit(live=None), max_price=None, in_stock_only=True) is False


def test_a_priced_hit_passes_and_an_expensive_one_does_not():
    cheap = FakeHit(live={"min_price": 12.0, "available": 2})
    dear = FakeHit(live={"min_price": 45.0, "available": 2})
    assert _passes_commerce(cheap, 30.0, False) is True
    assert _passes_commerce(dear, 30.0, False) is False


def test_brief_truncates_a_noisy_vendor_error():
    text = _brief(RuntimeError("x " * 500))
    assert len(text) < 200
    assert text.startswith("RuntimeError: ")


def test_clean_diagnostics_is_not_degraded():
    assert Diagnostics().degraded is False
