"""Fetch-stage failure handling: non-200 rejection, escalation pacing, straggler pass."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pier39_poc.core.models import Product
from pier39_poc.ingest import crawl


def _result(status: int | None, html: str, success: bool = True, error: str | None = None):
    return SimpleNamespace(
        status_code=status, html=html, success=success, error_message=error, markdown=""
    )


def _product(handle: str) -> Product:
    return Product(handle=handle, online_store_url=f"https://example.com/p/{handle}")


def test_error_status_fails_even_when_the_body_is_large():
    big = "x" * 400_000
    assert crawl._failure_reason(_result(404, big), 404, big) == "HTTP 404"
    assert crawl._failure_reason(_result(503, big), 503, big) == "HTTP 503"


def test_redirect_to_real_content_is_kept():
    """A 307 is deterministic: rejecting it loses the page rather than retrying it."""
    body = "<html>" + "product" * 50_000 + "</html>"
    assert crawl._failure_reason(_result(307, body), 307, body) is None


def test_redirect_to_a_challenge_still_fails():
    body = "<html>cf</html>"
    result = _result(307, body, success=False, error="Blocked: Cloudflare JS challenge")
    assert crawl._failure_reason(result, 307, body) == "Blocked: Cloudflare JS challenge"


def test_200_with_real_body_passes():
    assert crawl._failure_reason(_result(200, "<html>ok</html>"), 200, "<html>ok</html>") is None


def test_empty_body_and_cloudflare_marker_still_fail():
    assert crawl._failure_reason(_result(200, ""), 200, "") == "empty body"
    body = "<html>just a moment</html>"
    assert crawl._failure_reason(_result(200, body), 200, body) is not None


@pytest.fixture()
def crawl_store(store):
    return store.model_copy(
        update={"escalation_cooldown_seconds": 0.0, "final_retry_delay_seconds": 0.0}
    )


def _patch_batches(monkeypatch, responses):
    """responses: handle -> list of (status, html), consumed one per attempt."""
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def fake_batch(store, targets, profile):
        calls.append((profile, tuple(t.handle for t in targets)))
        out = {}
        for t in targets:
            status, html = responses[t.handle].pop(0)
            out[t.handle] = (_result(status, html), profile)
        return out

    monkeypatch.setattr(crawl, "_crawl_batch", fake_batch)
    return calls


def test_ladder_escalates_then_retries_stragglers_serially(crawl_store, monkeypatch):
    targets = [_product("good"), _product("hard")]
    calls = _patch_batches(
        monkeypatch,
        {
            "good": [(200, "<html>good</html>")],
            "hard": [(429, "b"), (429, "b"), (503, "b"), (200, "<html>hard</html>")],
        },
    )

    outcomes = {o.handle: o for o in asyncio.run(crawl.fetch_pages(crawl_store, targets))}

    assert outcomes["good"].ok and outcomes["good"].profile_used == "plain"
    assert outcomes["hard"].ok, "straggler pass should rescue the blocked handle"
    assert [profile for profile, _ in calls] == ["plain", "stealth", "undetected", "undetected"]
    assert calls[1][1] == ("hard",), "only the pending handle escalates"
    assert calls[-1][1] == ("hard",), "straggler pass runs one handle at a time"


def test_exhausted_straggler_stays_failed(crawl_store, monkeypatch):
    targets = [_product("blocked")]
    _patch_batches(monkeypatch, {"blocked": [(429, "b")] * 4})

    outcomes = asyncio.run(crawl.fetch_pages(crawl_store, targets))

    assert len(outcomes) == 1
    assert not outcomes[0].ok
    assert outcomes[0].error == "HTTP 429"
    assert not list(crawl_store.pages_dir.glob("blocked.html"))


def test_cooldowns_are_awaited_between_rungs(store, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(crawl.asyncio, "sleep", fake_sleep)
    _patch_batches(monkeypatch, {"hard": [(429, "b")] * 4})

    asyncio.run(crawl.fetch_pages(store, [_product("hard")]))

    assert slept == [
        store.escalation_cooldown_seconds,
        store.escalation_cooldown_seconds,
        store.final_retry_delay_seconds,
    ]
