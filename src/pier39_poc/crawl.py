"""Page fetching via Crawl4AI, with page selection and automatic escalation.

One crawl run per store, because Crawl4AI's RateLimiter applies a random inter-request
delay rather than a per-domain control -- crawling two stores in one run would not let
you give remi gentler settings than skout.

Escalation exists because remi returns HTTP 403 with a Cloudflare "Just a moment"
interstitial to plain requests while its `.js` endpoint returns 200. Headless Chromium
passes. The ladder is plain -> stealth -> undetected, and the profile that succeeded is
recorded per page.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Iterable

from .artifacts import append_jsonl, ensure_dirs, now_iso, record_stage, write_page
from .config import FETCH_PROFILES, StoreConfig

CLOUDFLARE_MARKERS = ("just a moment", "cf-browser-verification", "challenges.cloudflare.com")


@dataclass
class FetchOutcome:
    handle: str
    url: str
    ok: bool
    status: int | None
    bytes: int
    profile_used: str
    content_hash: str | None
    error: str | None = None


# --------------------------------------------------------------------------- #
# page selection
# --------------------------------------------------------------------------- #

def selectable(products: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only products published to the online store have a fetchable URL."""
    return [p for p in products if p.get("online_store_url")]


def select_pages(store: StoreConfig, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose which product pages to fetch, honouring crawl_scope and sampling."""
    if store.crawl_scope == "none":
        return []

    pool = selectable(products)
    if not pool:
        return []

    if store.crawl_scope == "all":
        return pool[: store.max_pages]

    if store.crawl_scope == "template_representatives":
        chosen: dict[str | None, dict[str, Any]] = {}
        for p in pool:
            key = p.get("template_suffix") or p.get("product_type")
            chosen.setdefault(key, p)
        return list(chosen.values())[: store.max_pages]

    # crawl_scope == "sample"
    budget = min(store.profile_pages, store.max_pages)
    return _sample(store, pool, budget)


def _sample(
    store: StoreConfig, pool: list[dict[str, Any]], budget: int
) -> list[dict[str, Any]]:
    mode = store.sampling

    if mode == "explicit":
        wanted = set(store.explicit_handles)
        picked = [p for p in pool if p["handle"] in wanted]
        missing = wanted - {p["handle"] for p in picked}
        if missing:
            raise ValueError(f"{store.slug}: explicit handles not in catalogue: {sorted(missing)}")
        return picked[:budget]

    if mode == "first_n":
        return pool[:budget]

    if mode == "random":
        rng = random.Random(store.sampling_seed)
        return rng.sample(pool, min(budget, len(pool)))

    # by_template / by_product_type: spread the budget across groups so the sample
    # covers layout variety rather than one template repeated.
    key = "template_suffix" if mode == "by_template" else "product_type"
    groups: dict[Any, list[dict[str, Any]]] = {}
    for p in pool:
        groups.setdefault(p.get(key), []).append(p)

    rng = random.Random(store.sampling_seed)
    for items in groups.values():
        rng.shuffle(items)

    picked: list[dict[str, Any]] = []
    order = sorted(groups, key=lambda k: (k is None, str(k)))
    while len(picked) < budget and any(groups[k] for k in order):
        for k in order:
            if groups[k] and len(picked) < budget:
                picked.append(groups[k].pop())
    return picked


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

def _looks_blocked(status: int | None, body: str) -> bool:
    if status == 403:
        return True
    head = (body or "")[:4000].lower()
    return any(m in head for m in CLOUDFLARE_MARKERS)


def _escalation_ladder(start: str) -> list[str]:
    idx = FETCH_PROFILES.index(start)
    return list(FETCH_PROFILES[idx:])


def _browser_config(profile: str, store: StoreConfig):
    from crawl4ai import BrowserConfig  # imported lazily; pulls playwright

    kwargs: dict[str, Any] = {"headless": True, "verbose": False}
    if profile == "stealth":
        kwargs["enable_stealth"] = True
    elif profile == "undetected":
        kwargs["enable_stealth"] = True
        kwargs["browser_mode"] = "undetected"
    return BrowserConfig(**kwargs)


def _run_config(store: StoreConfig):
    from crawl4ai import CacheMode, CrawlerRunConfig

    return CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=store.page_timeout_ms,
        wait_until="domcontentloaded",
        scan_full_page=True,
        remove_overlay_elements=True,
    )


def _dispatcher(store: StoreConfig):
    from crawl4ai import RateLimiter, SemaphoreDispatcher

    return SemaphoreDispatcher(
        max_session_permit=store.concurrency,
        rate_limiter=RateLimiter(
            base_delay=store.delay_seconds,
            max_delay=60.0,
            max_retries=3,
            rate_limit_codes=[429, 503],
        ),
    )


async def _crawl_batch(
    store: StoreConfig, targets: list[dict[str, Any]], profile: str
) -> dict[str, tuple[Any, str]]:
    """Crawl a batch at one fetch profile. Returns handle -> (result, profile)."""
    from crawl4ai import AsyncWebCrawler

    by_url = {t["online_store_url"]: t["handle"] for t in targets}
    out: dict[str, tuple[Any, str]] = {}

    async with AsyncWebCrawler(config=_browser_config(profile, store)) as crawler:
        results = await crawler.arun_many(
            urls=list(by_url),
            config=_run_config(store),
            dispatcher=_dispatcher(store),
        )
        for result in results:
            handle = by_url.get(getattr(result, "url", None))
            if handle:
                out[handle] = (result, profile)
    return out


async def fetch_pages(
    store: StoreConfig, targets: list[dict[str, Any]]
) -> list[FetchOutcome]:
    """Fetch every target, escalating the fetch profile for pages that look blocked."""
    ensure_dirs(store)
    if store.fetch_manifest_path.exists():
        store.fetch_manifest_path.unlink()

    outcomes: dict[str, FetchOutcome] = {}
    pending = list(targets)

    for profile in _escalation_ladder(store.fetch_profile):
        if not pending:
            break

        batch = await _crawl_batch(store, pending, profile)
        still_pending: list[dict[str, Any]] = []

        for target in pending:
            handle = target["handle"]
            url = target["online_store_url"]
            entry = batch.get(handle)

            if entry is None:
                still_pending.append(target)
                outcomes[handle] = FetchOutcome(
                    handle, url, False, None, 0, profile, None, "no result returned"
                )
                continue

            result, used = entry
            raw = getattr(result, "html", "") or ""
            status = getattr(result, "status_code", None)
            success = bool(getattr(result, "success", False)) and bool(raw)

            if not success or _looks_blocked(status, raw):
                still_pending.append(target)
                outcomes[handle] = FetchOutcome(
                    handle, url, False, status, len(raw), used, None,
                    getattr(result, "error_message", None) or "blocked or empty",
                )
                continue

            markdown = _markdown_of(result)
            content_hash = write_page(store, handle, raw, markdown)
            outcomes[handle] = FetchOutcome(
                handle, url, True, status, len(raw), used, content_hash
            )

        pending = still_pending

    for outcome in outcomes.values():
        append_jsonl(
            store.fetch_manifest_path,
            {
                "at": now_iso(),
                "handle": outcome.handle,
                "url": outcome.url,
                "ok": outcome.ok,
                "status": outcome.status,
                "bytes": outcome.bytes,
                "fetch_profile": outcome.profile_used,
                "content_hash": outcome.content_hash,
                "error": outcome.error,
            },
        )

    results = list(outcomes.values())
    record_stage(
        store,
        "fetch-html",
        {
            "requested": len(targets),
            "succeeded": sum(1 for o in results if o.ok),
            "failed": sum(1 for o in results if not o.ok),
            "profiles_used": sorted({o.profile_used for o in results}),
        },
    )
    return results


def _markdown_of(result: Any) -> str:
    """Crawl4AI has moved this attribute between versions; try the known shapes."""
    md = getattr(result, "markdown", None)
    if md is None:
        return ""
    for attr in ("raw_markdown", "fit_markdown"):
        value = getattr(md, attr, None)
        if value:
            return str(value)
    return str(md)
