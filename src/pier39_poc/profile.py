"""Derive per-store truth about where product content lives.

Gotchas:

`support` and `observed` are different denominators and must not be conflated.
`support` counts every product carrying a usable value; `observed` counts only those
whose page was fetched. A hit rate over `support` has an arithmetic ceiling of
crawled/total — on a 20-page skout sample `custom.nutrients` reported 7/48 = 0.15
against a 0.8 bar, so no key could ever be classed `rendered` and everything fell
through to `partially_rendered`, which downstream read as quotable. Render verdicts
use `observed`; retrieval value uses `support`.

`chrome_threshold` must NOT be 1.0. Different pages omit different sections, so a
unanimity rule leaks whole sections into every page's product region. Measured on
skout: at 1.0 the store-wide FAQ survived (1,569 words); at 0.8 it did not (664).

There is no chrome guard on metafield keys. It double-counted across products (a key
on 5 products with 4 sibling pages scored 20 against a threshold of 4) and wrongly
rejected `custom.nutrients`, `filter.ingredients` and `custom.product_faqs`. Page
chrome is handled by differencing and metafields are product-scoped by construction.
What replaced it is the recorded `identical value on every product` diagnostic, which
must hash all candidates rather than the first: `cands[0]` is `root` for every
rich-text field, so hashing the first made the diagnostic fire on everything.

Render presence promotes; it does not gate. skout's `custom.product_attributes`,
`custom.product_faqs` and all three `custom.description_*` render nowhere and are the
most retrieval-useful content on the product. Contamination and freshness filter;
rendering only informs the trust class, and `merge` decides that.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from .artifacts import (
    crawled_handles,
    load_products,
    read_page_html,
    record_stage,
    sha256,
    write_json,
)
from .attributes import build as build_attributes
from .blocks import (
    ChromeProfile,
    build_chrome_profile,
    extract_blocks,
    label_for,
    product_region,
    visible_text,
)
from .config import StoreConfig
from .crawl import template_key
from .matching import (
    FREE_FROM_KEYS,
    PageIndex,
    candidates,
    detect_contamination,
    detect_foreign_product_ids,
    detect_foreign_product_title,
    distinctive_title_tokens,
    is_reference_type,
    match_candidate,
    tokens,
)

ALWAYS_EXCLUDE = {
    ("custom", "admin_title"),
}

EXCLUDED_NAMESPACES = {
    "mm-google-shopping",
    "mc-facebook",
    "msft_bingads",
    "SEOMetaManager",
    "seo",
    "shopify--discovery--product_search_boost",
    "yotpo",
    "appsyl_seo_product",
    "palize",
    "rc_bundles",
    "videowise",
    "shogun",
    "subscriptions",
    "ecomify",
    "_eko_",
}

WIDGET_MARKERS = ("<div", "<span", "<script", "<link", "data-oke-", "stamped-", "loox")

_COMMERCE_KEY_RE = re.compile(
    r"pric|cost|msrp|compare_at|saving|saved|discount|promo|_off\b", re.IGNORECASE
)
COMMERCE_TYPES = frozenset({"money"})
_COMMERCE_VALUE_RE = re.compile(
    r"^\s*(?:[$£€]\s*\d[\d,.]*|\d[\d,.]*\s*%)\s*$"
)


_CONTENT_FREE_RE = re.compile(
    r"^\s*(?:true|false|yes|no|none|null|n/?a|-+|"
    r"#[0-9a-f]{3,8}|"
    r"\d{10}|"
    r"0E-\d+)\s*$",
    re.IGNORECASE,
)
CONTENT_FREE_TYPES = frozenset({"color", "boolean"})


def is_content_free(mf_type: str, values: list[str]) -> bool:
    if mf_type.removeprefix("list.") in CONTENT_FREE_TYPES:
        return True
    return bool(values) and all(_CONTENT_FREE_RE.match(v) for v in values)


def is_commerce_fact(namespace: str, key: str, mf_type: str, values: list[str]) -> bool:
    if mf_type.removeprefix("list.") in COMMERCE_TYPES:
        return True
    if _COMMERCE_KEY_RE.search(key) or _COMMERCE_KEY_RE.search(namespace):
        return True
    return bool(values) and all(_COMMERCE_VALUE_RE.match(v) for v in values)


@dataclass
class KeyVerdict:
    namespace: str
    key: str
    type: str
    admitted: bool
    reason: str
    hit_rate: float | None = None
    support: int = 0
    observed: int = 0
    matches: int = 0
    labels: list[tuple[str, int]] = field(default_factory=list)
    matched_handles: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def full_key(self) -> str:
        return f"{self.namespace}.{self.key}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "key": self.key,
            "type": self.type,
            "admitted": self.admitted,
            "reason": self.reason,
            "hit_rate": self.hit_rate,
            "support": self.support,
            "observed": self.observed,
            "matches": self.matches,
            "label": self.labels[0][0] if self.labels else None,
            "labels_seen": [list(pair) for pair in self.labels],
            "label_observations": sum(count for _, count in self.labels),
            "matched_handles": sorted(self.matched_handles),
            "detail": self.detail,
        }


def sha_short(text: str) -> str:
    return sha256(text)[:12]


def _page_indexes(store: StoreConfig, handles: list[str]) -> dict[str, PageIndex]:
    out: dict[str, PageIndex] = {}
    for handle in handles:
        raw = read_page_html(store, handle)
        if raw is None:
            continue
        out[handle] = PageIndex.build(visible_text(raw))
    return out


def _page_blocks(store: StoreConfig, handles: list[str], min_chars: int) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for handle in handles:
        raw = read_page_html(store, handle)
        if raw is None:
            continue
        out[handle] = extract_blocks(raw, min_chars=min_chars)
    return out


def _description_rendered(
    product: dict[str, Any], index: PageIndex, threshold: float
) -> bool:
    text = visible_text(product.get("description_html") or "")
    if not text.strip():
        return False
    sentences = [s.strip() for s in text.replace("\n", ". ").split(". ") if len(s.split()) >= 4]
    if not sentences:
        sentences = [text]
    hits = sum(
        1 for s in sentences if match_candidate(s, index, threshold=threshold).matched
    )
    return hits / len(sentences) >= 0.5


def _reference_key_counts(products: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for product in products:
        for mf in product.get("metafields") or []:
            if not is_reference_type(mf.get("type") or ""):
                continue
            if (mf.get("value") or "").strip() in ("", "[]"):
                continue
            counts[f"{mf.get('namespace')}.{mf.get('key')}"] += 1
    return dict(counts)


def _declaration_audit(products: list[dict[str, Any]]) -> dict[str, Any]:
    published = [p for p in products if p.get("online_store_url")]
    with_decl: list[str] = []
    without: list[str] = []
    for product in published:
        has = any(
            (mf.get("namespace"), mf.get("key")) in FREE_FROM_KEYS
            and (mf.get("value") or "").strip() not in ("", "[]")
            for mf in product.get("metafields") or []
        )
        (with_decl if has else without).append(product.get("handle") or "")
    return {
        "fields": sorted(f"{ns}.{key}" for ns, key in FREE_FROM_KEYS),
        "published": len(published),
        "with_declaration": len(with_decl),
        "without_declaration": len(without),
        "handles_without_sample": sorted(without)[:20],
    }


def build_profile(store: StoreConfig) -> dict[str, Any]:
    products = load_products(store)
    by_handle = {p["handle"]: p for p in products if p.get("handle")}
    known_handles = frozenset(by_handle)
    known_ids = frozenset(str(p["product_id"]) for p in products)
    titles_by_handle = {h: (p.get("title") or "").strip().lower() for h, p in by_handle.items()}

    handles = [
        h for h in crawled_handles(store)
        if h in by_handle
        and not (by_handle[h].get("online_store_url") and not by_handle[h].get("sellable", True))
    ]

    page_blocks = _page_blocks(store, handles, store.min_block_chars)
    page_index = _page_indexes(store, handles)

    chrome, group_chrome = _two_level_chrome(store, page_blocks, by_handle)

    regions: dict[str, list[str]] = {}
    for handle, blocks in page_blocks.items():
        foreign = frozenset(t for h, t in titles_by_handle.items() if h != handle and t)
        local = group_chrome.get(template_key(by_handle[handle]))
        stripped = product_region(blocks, chrome, foreign_titles=foreign)
        if local is not None:
            stripped = product_region(stripped, local, foreign_titles=frozenset())
        regions[handle] = stripped

    verdicts = _derive_allowlist(
        store, products, page_index, regions, known_handles, known_ids
    )

    constants, coverage = _residual_analysis(store, by_handle, regions, verdicts)

    admitted = [v for v in verdicts.values() if v.admitted]
    rejected = [v for v in verdicts.values() if not v.admitted]
    allowlist = [v.to_dict() for v in sorted(admitted, key=lambda v: v.full_key)]
    attributes = build_attributes(
        allowlist, constants.get("by_template") or {}, _reference_key_counts(products)
    )

    payload = {
        "slug": store.slug,
        "pages_analysed": len(page_blocks),
        "products_total": len(products),
        "products_published": sum(1 for p in products if p.get("online_store_url")),
        "declarations": _declaration_audit(products),
        "description_rendered_handles": sorted(
            h
            for h, index in page_index.items()
            if _description_rendered(by_handle[h], index, store.containment_threshold)
        ),
        "chrome": {
            "threshold": store.chrome_threshold,
            "blocks": len(chrome.chrome),
            "set_hash": sha256(" ".join(sorted(chrome.chrome))),
            "frequency_histogram": chrome.histogram(),
        },
        "region_words": {h: sum(len(b.split()) for b in r) for h, r in regions.items()},
        "attributes": attributes,
        "allowlist": allowlist,
        "rejected": [v.to_dict() for v in sorted(rejected, key=lambda v: v.full_key)],
        "template_constants": constants,
        "coverage": coverage,
    }

    write_json(store.profile_path, payload)
    record_stage(
        store,
        "profile",
        {
            "pages_analysed": len(page_blocks),
            "admitted_keys": len(admitted),
            "rejected_keys": len(rejected),
            "rendered_keys": sum(1 for v in admitted if v.reason == "rendered"),
            "products_without_free_from": payload["declarations"]["without_declaration"],
            "coverage_pct": coverage.get("coverage_pct"),
        },
    )
    return payload


def _two_level_chrome(
    store: StoreConfig,
    page_blocks: dict[str, list[str]],
    by_handle: dict[str, dict[str, Any]],
) -> tuple[ChromeProfile, dict[str, ChromeProfile]]:
    store_wide = build_chrome_profile(page_blocks, threshold=store.chrome_threshold)

    groups: dict[str, dict[str, list[str]]] = defaultdict(dict)
    for handle, blocks in page_blocks.items():
        groups[template_key(by_handle[handle])][handle] = blocks

    per_group: dict[str, ChromeProfile] = {}
    for key, pages in groups.items():
        if len(pages) < 2:
            continue
        per_group[key] = build_chrome_profile(pages, threshold=store.chrome_threshold)
    return store_wide, per_group


def _derive_allowlist(
    store: StoreConfig,
    products: list[dict[str, Any]],
    page_index: dict[str, PageIndex],
    regions: dict[str, list[str]],
    known_handles: frozenset[str],
    known_ids: frozenset[str],
) -> dict[tuple[str, str], KeyVerdict]:
    foreign_title: Counter[tuple[str, str]] = Counter()
    foreign_detail: dict[tuple[str, str], str] = {}
    distinctive = distinctive_title_tokens(
        {p["handle"]: p.get("title") or "" for p in products if p.get("handle")}
    )
    support: Counter[tuple[str, str]] = Counter()
    observed: Counter[tuple[str, str]] = Counter()
    matches: Counter[tuple[str, str]] = Counter()
    types: dict[tuple[str, str], str] = {}
    colon_labels: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    guessed_labels: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    matched_handles: dict[tuple[str, str], set[str]] = defaultdict(set)
    newest: dict[str, str] = {}
    updated: dict[tuple[str, str], str] = {}
    rejections: dict[tuple[str, str], tuple[str, str]] = {}
    distinct_values: dict[tuple[str, str], set[str]] = defaultdict(set)

    for product in products:
        for mf in product.get("metafields") or []:
            stamp = mf.get("updatedAt") or ""
            ns = mf.get("namespace") or ""
            if stamp > newest.get(ns, ""):
                newest[ns] = stamp

    for product in products:
        handle = product.get("handle") or ""
        pid = str(product.get("product_id"))
        for mf in product.get("metafields") or []:
            ns, key = mf.get("namespace") or "", mf.get("key") or ""
            ident = (ns, key)
            types.setdefault(ident, mf.get("type") or "")
            stamp = mf.get("updatedAt") or ""
            if stamp > updated.get(ident, ""):
                updated[ident] = stamp

            raw = mf.get("value") or ""

            contaminated = detect_contamination(
                raw, pid, handle, known_handles, store.domain
            )
            if not contaminated.contaminated:
                contaminated = detect_foreign_product_ids(raw, pid, known_ids)
            if contaminated.contaminated:
                rejections[ident] = ("foreign_product_id", contaminated.detail)
                continue

            if ident in ALWAYS_EXCLUDE:
                rejections[ident] = ("always_excluded", "internal field")
                continue
            if ns in EXCLUDED_NAMESPACES:
                rejections[ident] = ("excluded_namespace", ns)
                continue
            if is_reference_type(mf.get("type") or ""):
                rejections[ident] = ("reference_type", mf.get("type") or "")
                continue
            if any(m in raw for m in WIDGET_MARKERS):
                rejections[ident] = ("widget_markup", "value is rendered widget HTML")
                continue

            cands = candidates(mf.get("type") or "", raw)
            if not cands:
                continue

            if is_commerce_fact(ns, key, mf.get("type") or "", cands):
                rejections[ident] = (
                    "commerce_fact",
                    "price/inventory are read live, never stored (DESIGN.md 5.4)",
                )
                continue

            if is_content_free(mf.get("type") or "", cands):
                rejections[ident] = (
                    "no_content_value",
                    "flag, colour or timestamp; carries no product information",
                )
                continue

            support[ident] += 1
            distinct_values[ident].add(sha_short(" | ".join(cands)))

            for cand in cands:
                foreign = detect_foreign_product_title(cand, handle, distinctive)
                if foreign.contaminated:
                    foreign_title[ident] += 1
                    foreign_detail.setdefault(ident, foreign.detail)
                    break

            index = page_index.get(handle)
            if index is None:
                continue
            observed[ident] += 1

            for cand in cands:
                if match_candidate(cand, index, threshold=store.containment_threshold).matched:
                    matches[ident] += 1
                    matched_handles[ident].add(handle)
                    found = _label_from_region(regions.get(handle) or [], cand)
                    if found:
                        text, from_colon = found
                        target = colon_labels if from_colon else guessed_labels
                        target[ident][text] += 1
                    break

    verdicts: dict[tuple[str, str], KeyVerdict] = {}

    for ident, mf_type in types.items():
        ns, key = ident
        if ident in rejections:
            reason, detail = rejections[ident]
            verdicts[ident] = KeyVerdict(ns, key, mf_type, False, reason, detail=detail)
            continue

        n = support[ident]
        if n == 0:
            verdicts[ident] = KeyVerdict(ns, key, mf_type, False, "no_usable_value")
            continue

        stamp = updated.get(ident, "")
        namespace_newest = newest.get(ns, "")
        if stamp and namespace_newest and stamp < namespace_newest[:4] + "-01-01":
            verdicts[ident] = KeyVerdict(
                ns, key, mf_type, False, "stale_namespace", support=n,
                detail=f"updatedAt {stamp} vs namespace newest {namespace_newest}",
            )
            continue

        flagged = foreign_title[ident]
        foreign_rate = flagged / n
        if foreign_rate >= FOREIGN_TITLE_REJECT_RATE:
            verdicts[ident] = KeyVerdict(
                ns, key, mf_type, False, "foreign_product_title", support=n,
                detail=f"{flagged}/{n} values {foreign_detail.get(ident, '')}",
            )
            continue

        seen = observed[ident]
        hit = matches[ident]
        rate = hit / seen if seen else None
        seen_labels = _resolve_labels(colon_labels[ident], guessed_labels[ident])
        notes = []
        if n >= 3 and len(distinct_values[ident]) == 1:
            notes.append("identical value on every product -- check if store-wide")
        if flagged:
            notes.append(
                f"REVIEW: {flagged}/{n} values {foreign_detail.get(ident, '')}"
            )
        note = "; ".join(notes)
        common = {
            "hit_rate": rate,
            "support": n,
            "observed": seen,
            "matches": hit,
            "labels": seen_labels,
            "matched_handles": sorted(matched_handles[ident]),
            "detail": note,
        }

        if n < store.allowlist_min_support:
            reason = "low_support_admitted"
        elif seen == 0:
            reason = "no_render_evidence"
        elif seen < store.allowlist_min_support:
            reason = "low_render_evidence"
        elif rate is not None and rate >= store.allowlist_min_hit_rate:
            reason = "rendered"
        elif hit == 0:
            reason = "unrendered_retrieval_only"
        else:
            reason = "partially_rendered"

        verdicts[ident] = KeyVerdict(ns, key, mf_type, True, reason, **common)

    return verdicts


FOREIGN_TITLE_REJECT_RATE = 0.25

LABEL_MIN_OBSERVATIONS = 2
LABEL_MIN_DOMINANCE = 0.8


def _resolve_labels(
    colon: Counter[str], guessed: Counter[str]
) -> list[tuple[str, int]]:
    if colon:
        return colon.most_common(3)
    if not guessed:
        return []
    total = sum(guessed.values())
    _, count = guessed.most_common(1)[0]
    if count >= LABEL_MIN_OBSERVATIONS and count / total >= LABEL_MIN_DOMINANCE:
        return guessed.most_common(3)
    return []


def _label_from_region(
    region: list[str], candidate: str
) -> tuple[str, bool] | None:
    cand_tokens = set(tokens(candidate))
    if not cand_tokens:
        return None
    for i, block in enumerate(region):
        if cand_tokens.issubset(set(tokens(block))):
            return label_for(region, i)
    return None


SPEC_VALUE_MAX_TOKENS = 25


def _spec_pairs(region: list[str], eligible: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, block in enumerate(region):
        if block not in eligible or block in seen:
            continue
        if len(block.split()) > SPEC_VALUE_MAX_TOKENS:
            continue
        found = label_for(region, i)
        if not found or not found[1]:
            continue
        seen.add(block)
        out.append(
            {"value": block, "label": found[0], "support": 1, "single_page": True}
        )
    return out


def _residual_analysis(
    store: StoreConfig,
    by_handle: dict[str, dict[str, Any]],
    regions: dict[str, list[str]],
    verdicts: dict[tuple[str, str], KeyVerdict],
) -> tuple[dict[str, Any], dict[str, Any]]:
    explained_index: dict[str, PageIndex] = {}
    for handle, product in by_handle.items():
        if handle not in regions:
            continue
        parts = [visible_text(product.get("description_html") or ""), product.get("title") or ""]
        for mf in product.get("metafields") or []:
            ident = (mf.get("namespace") or "", mf.get("key") or "")
            verdict = verdicts.get(ident)
            if not verdict or not verdict.admitted:
                continue
            parts.extend(candidates(mf.get("type") or "", mf.get("value") or ""))
        explained_index[handle] = PageIndex.build(" \n ".join(p for p in parts if p))

    residual_blocks: dict[str, list[str]] = {}
    total_words = 0
    residual_words = 0

    for handle, region in regions.items():
        index = explained_index.get(handle)
        leftover: list[str] = []
        for block in region:
            block_tokens = tokens(block)
            total_words += len(block_tokens)
            if not block_tokens:
                continue
            explained = index is not None and match_candidate(
                block, index, threshold=store.containment_threshold
            ).matched
            if not explained:
                leftover.append(block)
                residual_words += len(block_tokens)
        residual_blocks[handle] = leftover

    groups: dict[str, list[str]] = defaultdict(list)
    for handle in regions:
        groups[template_key(by_handle[handle])].append(handle)

    constants: dict[str, list[dict[str, Any]]] = {}
    per_product_theme: dict[str, list[str]] = {}
    for template, template_handles in groups.items():
        if len(template_handles) < 2:
            handle = template_handles[0]
            residual = residual_blocks.get(handle, [])
            pairs = _spec_pairs(regions[handle], set(residual))
            if pairs:
                constants[template] = pairs
            claimed = {p["value"] for p in pairs}
            per_product_theme[handle] = sorted(set(residual) - claimed)
            continue
        sets = [set(residual_blocks.get(h, [])) for h in template_handles]
        shared = set.intersection(*sets) if sets else set()
        labelled = {
            p["value"]: p["label"]
            for h in template_handles
            for p in _spec_pairs(regions[h], shared)
        }
        constants[template] = [
            {
                "value": b,
                "label": labelled.get(b),
                "support": len(template_handles),
                "single_page": False,
            }
            for b in sorted(shared)
            if len(b.split()) >= 3
        ]
        for h in template_handles:
            per_product_theme[h] = sorted(set(residual_blocks.get(h, [])) - shared)

    constant_words = sum(
        len(c["value"].split()) for blocks in constants.values() for c in blocks
    )
    unreachable_words = sum(
        len(b.split()) for blocks in per_product_theme.values() for b in blocks
    )
    coverage_pct = (
        round(100.0 * (total_words - residual_words) / total_words, 1) if total_words else None
    )
    theme_counts = {
        h: {"blocks": len(blocks), "words": sum(len(b.split()) for b in blocks)}
        for h, blocks in per_product_theme.items()
    }
    ranked = sorted(theme_counts, key=lambda h: -theme_counts[h]["words"])

    return (
        {
            "by_template": constants,
            "per_product_theme_counts": theme_counts,
            "per_product_theme_sample": {
                h: per_product_theme[h][:20] for h in ranked[:5] if per_product_theme[h]
            },
        },
        {
            "region_words_total": total_words,
            "residual_words": residual_words,
            "template_constant_words": constant_words,
            "per_product_unreachable_words": unreachable_words,
            "coverage_pct": coverage_pct,
        },
    )
