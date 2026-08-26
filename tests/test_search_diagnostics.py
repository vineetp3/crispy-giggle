"""Degraded retrieval must be reported, not swallowed."""

from __future__ import annotations

from pier39_poc import search
from pier39_poc.search import Diagnostics, _brief, _passes_commerce, _rerank


class FakeHit:
    def __init__(self, text="a bar", live=None):
        self.text = text
        self.live = live
        self.rerank_score = None


def _break_reranker(monkeypatch, message):
    """Break the reranker at its entry point, the cached Ranker."""

    def boom(_name):
        raise RuntimeError(message)

    monkeypatch.setattr(search, "_flashrank_ranker", boom)


def test_rerank_records_the_failure_and_keeps_the_fused_order(monkeypatch):
    _break_reranker(monkeypatch, "checkpoint unavailable")

    hits = [FakeHit("one"), FakeHit("two")]
    diag = Diagnostics()
    out = _rerank("query", hits, "ms-marco-MiniLM-L-12-v2", diag)

    assert out == hits
    assert diag.rerank_failed is True
    assert "checkpoint unavailable" in diag.rerank_error
    assert diag.degraded is True


def test_rerank_without_diagnostics_still_degrades(monkeypatch):
    _break_reranker(monkeypatch, "nope")

    hits = [FakeHit()]
    assert _rerank("q", hits, "ms-marco-MiniLM-L-12-v2", None) == hits


def test_rerank_leaves_scores_unset_when_it_degrades(monkeypatch):
    _break_reranker(monkeypatch, "no model")

    hits = [FakeHit("one"), FakeHit("two")]
    out = _rerank("q", hits, "ms-marco-MiniLM-L-12-v2", Diagnostics())

    assert out == hits
    assert all(h.rerank_score is None for h in out)


def test_an_unknown_checkpoint_degrades_rather_than_raising():
    """An invalid `rerank_model` is a config error, but not a shopper-facing one."""
    hits = [FakeHit("one")]
    diag = Diagnostics()
    assert _rerank("q", hits, "no-such-checkpoint", diag) == hits
    assert diag.rerank_failed is True
    assert diag.rerank_error


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
