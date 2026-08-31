"""Page selection and fetching via Crawl4AI, with automatic escalation on a block.

First stage after fetch-api; writes data/<slug>/pages/. Gotchas: docs/reference/ingest.md
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pier39_poc.core.models import Product
from pier39_poc.core.tuning import DEFAULTS
from pier39_poc.infra.artifacts import append_jsonl, ensure_dirs, now_iso, record_stage, write_page
from pier39_poc.infra.config import FETCH_PROFILES, StoreConfig

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


def template_key(product: Product) -> str:
    for value in (product.template_suffix, product.product_type):
        cleaned = (value or "").strip().lower()
        if cleaned:
            return cleaned
    return "_default"


def product_type_key(product: Product) -> str:
    return product.product_type.strip().lower() or "_default"


def selectable(products: Iterable[Product]) -> list[Product]:
    return [
        p for p in products
        if p.online_store_url and p.sellable
    ]


def select_pages(store: StoreConfig, products: list[Product]) -> list[Product]:
    if store.crawl_scope == "none":
        return []

    pool = selectable(products)
    if not pool:
        return []

    if store.crawl_scope == "all":
        return pool[: store.max_pages]

    if store.crawl_scope == "template_representatives":
        chosen: dict[str, Product] = {}
        for p in pool:
            chosen.setdefault(template_key(p), p)
        return list(chosen.values())[: store.max_pages]

    budget = min(store.profile_pages, store.max_pages)
    return _sample(store, pool, budget)


def floor_shortfall(
    store: StoreConfig, products: list[Product]
) -> tuple[int, int] | None:
    if store.crawl_scope != "sample" or store.sampling not in (
        "by_template",
        "by_product_type",
    ):
        return None

    pool = selectable(products)
    if not pool:
        return None

    key_of: Callable[[Product], str] = (
        product_type_key if store.sampling == "by_product_type" else template_key
    )

    reachable = min(len(pool), len({key_of(p) for p in pool}) * DEFAULTS.crawl.group_floor)
    budget = min(store.profile_pages, store.max_pages)
    if budget >= reachable:
        return None
    return budget, reachable


def _sample(
    store: StoreConfig, pool: list[Product], budget: int
) -> list[Product]:
    mode = store.sampling

    if mode == "explicit":
        wanted = set(store.explicit_handles)
        picked = [p for p in pool if p.handle in wanted]
        missing = wanted - {p.handle for p in picked}
        if missing:
            raise ValueError(f"{store.slug}: explicit handles not in catalogue: {sorted(missing)}")
        return picked[:budget]

    if mode == "first_n":
        return pool[:budget]

    if mode == "random":
        rng = random.Random(store.sampling_seed)
        return rng.sample(pool, min(budget, len(pool)))

    key_of: Callable[[Product], str] = (
        product_type_key if mode == "by_product_type" else template_key
    )

    groups: dict[str, list[Product]] = {}
    for p in pool:
        groups.setdefault(key_of(p), []).append(p)

    rng = random.Random(store.sampling_seed)
    for items in groups.values():
        rng.shuffle(items)

    order = sorted(groups)
    picked: list[Product] = []

    for want in range(1, DEFAULTS.crawl.group_floor + 1):
        for k in order:
            if len(picked) >= budget:
                return picked
            taken = sum(1 for p in picked if key_of(p) == k)
            if taken < want and groups[k]:
                picked.append(groups[k].pop())

    while len(picked) < budget and any(groups[k] for k in order):
        for k in order:
            if groups[k] and len(picked) < budget:
                picked.append(groups[k].pop())
    return picked


def _looks_blocked(status: int | None, body: str) -> bool:
    if status == 403:
        return True
    head = (body or "")[:4000].lower()
    return any(m in head for m in CLOUDFLARE_MARKERS)


def _failure_reason(result: Any, status: int | None, raw: str) -> str | None:
    if not raw:
        return getattr(result, "error_message", None) or "empty body"
    if status is not None and status >= 400:
        return f"HTTP {status}"
    if not bool(getattr(result, "success", False)):
        return getattr(result, "error_message", None) or "crawl reported failure"
    if _looks_blocked(status, raw):
        return getattr(result, "error_message", None) or "blocked"
    return None


def _escalation_ladder(start: str) -> list[str]:
    idx = FETCH_PROFILES.index(start)
    return list(FETCH_PROFILES[idx:])


def _browser_config(profile: str, store: StoreConfig):
    from crawl4ai import BrowserConfig

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
    store: StoreConfig, targets: list[Product], profile: str
) -> dict[str, tuple[Any, str]]:
    from crawl4ai import AsyncWebCrawler

    by_url = {t.online_store_url: t.handle for t in targets if t.online_store_url}
    out: dict[str, tuple[Any, str]] = {}

    async with AsyncWebCrawler(config=_browser_config(profile, store)) as crawler:
        results = await crawler.arun_many(
            urls=list(by_url),
            config=_run_config(store),
            dispatcher=_dispatcher(store),
        )
        for result in results:  # pyright: ignore[reportGeneralTypeIssues]
            handle = by_url.get(str(getattr(result, "url", "") or ""))
            if handle:
                out[handle] = (result, profile)
    return out


def _record_batch(
    store: StoreConfig,
    pending: list[Product],
    batch: dict[str, tuple[Any, str]],
    profile: str,
    outcomes: dict[str, FetchOutcome],
) -> list[Product]:
    still_pending: list[Product] = []

    for target in pending:
        handle = target.handle
        url = target.online_store_url or ""
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
        failure = _failure_reason(result, status, raw)

        if failure:
            still_pending.append(target)
            outcomes[handle] = FetchOutcome(
                handle, url, False, status, len(raw), used, None, failure
            )
            continue

        markdown = _markdown_of(result)
        content_hash = write_page(store, handle, raw, markdown)
        outcomes[handle] = FetchOutcome(
            handle, url, True, status, len(raw), used, content_hash
        )

    return still_pending


async def fetch_pages(
    store: StoreConfig, targets: list[Product]
) -> list[FetchOutcome]:
    ensure_dirs(store)
    if store.fetch_manifest_path.exists():
        store.fetch_manifest_path.unlink()

    outcomes: dict[str, FetchOutcome] = {}
    pending = list(targets)
    ladder = _escalation_ladder(store.fetch_profile)

    for index, profile in enumerate(ladder):
        if not pending:
            break
        if index:
            await asyncio.sleep(store.escalation_cooldown_seconds)

        batch = await _crawl_batch(store, pending, profile)
        pending = _record_batch(store, pending, batch, profile, outcomes)

    if pending:
        await asyncio.sleep(store.final_retry_delay_seconds)
        profile = ladder[-1]
        stragglers = list(pending)
        pending = []
        for target in stragglers:
            batch = await _crawl_batch(store, [target], profile)
            pending.extend(_record_batch(store, [target], batch, profile, outcomes))

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
    md = getattr(result, "markdown", None)
    if md is None:
        return ""
    for attr in ("raw_markdown", "fit_markdown"):
        value = getattr(md, attr, None)
        if value:
            return str(value)
    return str(md)
