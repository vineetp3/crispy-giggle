"""Derive per-store truth about where product content lives, into profile.json.

Reads api.jsonl + pages/; runs after fetch-html, before merge. The store's key allowlist,
theme constants and coverage. Gotchas: docs/reference/ingest.md
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from pier39_poc.core.attributes import build as build_attributes
from pier39_poc.core.blocks import (
    ChromeProfile,
    build_chrome_profile,
    extract_blocks,
    inline_label,
    is_numeric_value,
    label_for,
    product_region,
    repeated_block_profile,
    visible_text,
)
from pier39_poc.core.matching import (
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
from pier39_poc.core.models import ChromeSummary, Product, StoreProfile
from pier39_poc.core.quotability import (
    is_commerce_constant,
    is_commerce_fact,
    is_content_free,
    is_widget_markup,
)
from pier39_poc.core.tuning import DEFAULTS
from pier39_poc.infra.artifacts import (
    crawled_handles,
    load_products,
    read_json,
    read_page_html,
    record_stage,
    sha256,
    write_json,
)
from pier39_poc.infra.config import StoreConfig
from pier39_poc.ingest.crawl import template_key
from pier39_poc.ingest.labels import SPEC, load_reference
from pier39_poc.ingest.labels import normalise as normalise_label

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


Ident = tuple[str, str]


@dataclass
class KeyEvidence:
    mf_type: str = ""
    support: int = 0
    observed: int = 0
    matches: int = 0
    foreign_title_hits: int = 0
    foreign_title_detail: str = ""
    updated_at: str = ""
    rejection: tuple[str, str] | None = None
    colon_labels: Counter[str] = field(default_factory=Counter)
    guessed_labels: Counter[str] = field(default_factory=Counter)
    matched_handles: set[str] = field(default_factory=set)
    distinct_values: set[str] = field(default_factory=set)


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
    text = visible_text(product.description_html)
    if not text.strip():
        return False
    sentences = [s.strip() for s in text.replace("\n", ". ").split(". ") if len(s.split()) >= 4]
    if not sentences:
        sentences = [text]
    hits = sum(
        1 for s in sentences if match_candidate(s, index, threshold=threshold).matched
    )
    return hits / len(sentences) >= 0.5


def _reference_key_counts(products: list[Product]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for product in products:
        for mf in product.metafields:
            if not is_reference_type(mf.type):
                continue
            if (mf.value or "").strip() in ("", "[]"):
                continue
            counts[f"{mf.namespace}.{mf.key}"] += 1
    return dict(counts)


def _declaration_audit(products: list[Product]) -> dict[str, Any]:
    published = [p for p in products if p.online_store_url]
    with_decl: list[str] = []
    without: list[str] = []
    for product in published:
        has = any(
            (mf.namespace, mf.key) in FREE_FROM_KEYS
            and (mf.value or "").strip() not in ("", "[]")
            for mf in product.metafields
        )
        (with_decl if has else without).append(product.handle)
    return {
        "fields": sorted(f"{ns}.{key}" for ns, key in FREE_FROM_KEYS),
        "published": len(published),
        "with_declaration": len(with_decl),
        "without_declaration": len(without),
        "handles_without_sample": sorted(without)[:20],
    }


def _analysable_handles(
    store: StoreConfig, by_handle: dict[str, Product]
) -> list[str]:
    return [
        h for h in crawled_handles(store)
        if h in by_handle
        and not (by_handle[h].online_store_url and not by_handle[h].sellable)
    ]


def _regions_by_handle(
    page_blocks: dict[str, list[str]],
    by_handle: dict[str, Product],
    titles_by_handle: dict[str, str],
    chrome: ChromeProfile,
    group_chrome: dict[str, ChromeProfile],
    cross_page: ChromeProfile,
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, list[str]]]:
    regions: dict[str, list[str]] = {}
    pre_group: dict[str, list[str]] = {}
    group_shared: dict[str, list[str]] = {}
    for handle, blocks in page_blocks.items():
        foreign = frozenset(t for h, t in titles_by_handle.items() if h != handle and t)
        local = group_chrome.get(template_key(by_handle[handle]))
        stripped = product_region(blocks, chrome, foreign_titles=foreign)
        pre_group[handle] = stripped
        if local is not None:
            kept = product_region(stripped, local, foreign_titles=frozenset())
            group_shared[handle] = [b for b in stripped if b in local.chrome]
            stripped = kept
        else:
            stripped = product_region(stripped, cross_page, foreign_titles=frozenset())
        regions[handle] = stripped
    return regions, pre_group, group_shared


def load_profile(store: StoreConfig) -> StoreProfile:
    return StoreProfile.model_validate(read_json(store.profile_path))


def build_profile(store: StoreConfig) -> StoreProfile:
    products = load_products(store)
    by_handle = {p.handle: p for p in products if p.handle}
    known_handles = frozenset(by_handle)
    known_ids = frozenset(p.product_id for p in products)
    titles_by_handle = {h: p.title.strip().lower() for h, p in by_handle.items()}

    handles = _analysable_handles(store, by_handle)

    page_blocks = _page_blocks(store, handles, store.tuning.blocks.min_block_chars)
    page_index = _page_indexes(store, handles)

    chrome, group_chrome, cross_page = _two_level_chrome(store, page_blocks, by_handle)
    regions, pre_group, group_shared = _regions_by_handle(
        page_blocks, by_handle, titles_by_handle, chrome, group_chrome, cross_page
    )

    verdicts = _derive_allowlist(
        store, products, page_index, regions, known_handles, known_ids
    )

    constants, coverage = _residual_analysis(
        store, by_handle, regions, verdicts, pre_group, group_shared
    )

    admitted = [v for v in verdicts.values() if v.admitted]
    rejected = [v for v in verdicts.values() if not v.admitted]
    allowlist = [v.to_dict() for v in sorted(admitted, key=lambda v: v.full_key)]
    reference = load_reference(store.slug)
    affirmed = {
        c["label"]
        for blocks in (constants.get("per_product") or {}).values()
        for c in blocks
        if c.get("label") and reference.get(normalise_label(c["label"])) == SPEC
    }
    attributes = build_attributes(
        allowlist,
        constants.get("by_template") or {},
        _reference_key_counts(products),
        affirmed,
    )

    profile = StoreProfile(
        slug=store.slug,
        pages_analysed=len(page_blocks),
        products_total=len(products),
        products_published=sum(1 for p in products if p.online_store_url),
        declarations=_declaration_audit(products),
        description_rendered_handles=sorted(
            h
            for h, index in page_index.items()
            if _description_rendered(by_handle[h], index, store.tuning.matching.containment_threshold)
        ),
        chrome=ChromeSummary(
            threshold=store.tuning.blocks.chrome_threshold,
            blocks=len(chrome.chrome),
            set_hash=sha256(" ".join(sorted(chrome.chrome))),
            frequency_histogram=chrome.histogram(),
        ),
        region_words={h: sum(len(b.split()) for b in r) for h, r in regions.items()},
        attributes=attributes,
        allowlist=allowlist,
        rejected=[v.to_dict() for v in sorted(rejected, key=lambda v: v.full_key)],
        template_constants=constants,
        coverage=coverage,
    )
    payload = profile.model_dump()

    write_json(store.profile_path, payload)
    record_stage(
        store,
        "profile",
        {
            "pages_analysed": len(page_blocks),
            "admitted_keys": len(admitted),
            "rejected_keys": len(rejected),
            "rendered_keys": sum(1 for v in admitted if v.reason == "rendered"),
            "products_without_free_from": profile.declarations.without_declaration,
            "coverage_pct": profile.coverage.coverage_pct,
        },
    )
    return profile


def _two_level_chrome(
    store: StoreConfig,
    page_blocks: dict[str, list[str]],
    by_handle: dict[str, Product],
) -> tuple[ChromeProfile, dict[str, ChromeProfile], ChromeProfile]:
    store_wide = build_chrome_profile(page_blocks, threshold=store.tuning.blocks.chrome_threshold)

    groups: dict[str, dict[str, list[str]]] = defaultdict(dict)
    for handle, blocks in page_blocks.items():
        groups[template_key(by_handle[handle])][handle] = blocks

    per_group: dict[str, ChromeProfile] = {}
    for key, pages in groups.items():
        if len(pages) < 2:
            continue
        per_group[key] = build_chrome_profile(pages, threshold=store.tuning.blocks.chrome_threshold)
    return store_wide, per_group, repeated_block_profile(store_wide)


def _namespace_newest(products: list[Product]) -> dict[str, str]:
    newest: dict[str, str] = {}
    for product in products:
        for mf in product.metafields:
            stamp = mf.updatedAt or ""
            ns = mf.namespace
            if stamp > newest.get(ns, ""):
                newest[ns] = stamp
    return newest


def _early_rejection(
    store: StoreConfig,
    ident: Ident,
    mf_type: str,
    raw: str,
    product_id: str,
    handle: str,
    known_handles: frozenset[str],
    known_ids: frozenset[str],
) -> tuple[str, str] | None:
    contaminated = detect_contamination(raw, product_id, handle, known_handles, store.domain)
    if not contaminated.contaminated:
        contaminated = detect_foreign_product_ids(raw, product_id, known_ids)
    if contaminated.contaminated:
        return ("foreign_product_id", contaminated.detail)

    if ident in ALWAYS_EXCLUDE:
        return ("always_excluded", "internal field")
    if ident[0] in EXCLUDED_NAMESPACES:
        return ("excluded_namespace", ident[0])
    if is_reference_type(mf_type):
        return ("reference_type", mf_type)
    if is_widget_markup(raw):
        return ("widget_markup", "value is rendered widget HTML")
    return None


def _value_rejection(ident: Ident, mf_type: str, cands: list[str]) -> tuple[str, str] | None:
    if is_commerce_fact(ident[0], ident[1], mf_type, cands):
        return (
            "commerce_fact",
            "price/inventory are read live, never stored (DESIGN.md 5.4)",
        )
    if is_content_free(mf_type, cands):
        return (
            "no_content_value",
            "flag, colour or timestamp; carries no product information",
        )
    return None


def _collect_evidence(
    store: StoreConfig,
    products: list[Product],
    page_index: dict[str, PageIndex],
    regions: dict[str, list[str]],
    known_handles: frozenset[str],
    known_ids: frozenset[str],
    distinctive: dict[str, tuple[str, ...]],
) -> dict[Ident, KeyEvidence]:
    evidence: dict[Ident, KeyEvidence] = {}

    for product in products:
        handle = product.handle
        product_id = product.product_id
        for mf in product.metafields:
            ident = (mf.namespace, mf.key)
            mf_type = mf.type
            ev = evidence.setdefault(ident, KeyEvidence(mf_type=mf_type))

            stamp = mf.updatedAt or ""
            if stamp > ev.updated_at:
                ev.updated_at = stamp

            raw = mf.value or ""
            rejection = _early_rejection(
                store, ident, mf_type, raw, product_id, handle, known_handles, known_ids
            )
            if rejection:
                ev.rejection = rejection
                continue

            cands = candidates(mf_type, raw)
            if not cands:
                continue

            rejection = _value_rejection(ident, mf_type, cands)
            if rejection:
                ev.rejection = rejection
                continue

            ev.support += 1
            ev.distinct_values.add(sha_short(" | ".join(cands)))

            for cand in cands:
                foreign = detect_foreign_product_title(cand, handle, distinctive)
                if foreign.contaminated:
                    ev.foreign_title_hits += 1
                    if not ev.foreign_title_detail:
                        ev.foreign_title_detail = foreign.detail
                    break

            index = page_index.get(handle)
            if index is None:
                continue
            ev.observed += 1

            for cand in cands:
                if match_candidate(cand, index, threshold=store.tuning.matching.containment_threshold).matched:
                    ev.matches += 1
                    ev.matched_handles.add(handle)
                    found = _label_from_region(regions.get(handle) or [], cand)
                    if found:
                        text, from_colon = found
                        target = ev.colon_labels if from_colon else ev.guessed_labels
                        target[text] += 1
                    break

    return evidence


def _verdict_for(
    store: StoreConfig, ident: Ident, ev: KeyEvidence, namespace_newest: str
) -> KeyVerdict:
    ns, key = ident

    if ev.rejection:
        reason, detail = ev.rejection
        return KeyVerdict(ns, key, ev.mf_type, False, reason, detail=detail)

    if ev.support == 0:
        return KeyVerdict(ns, key, ev.mf_type, False, "no_usable_value")

    if (
        ev.updated_at
        and namespace_newest
        and ev.updated_at < namespace_newest[:4] + "-01-01"
    ):
        return KeyVerdict(
            ns, key, ev.mf_type, False, "stale_namespace", support=ev.support,
            detail=f"updatedAt {ev.updated_at} vs namespace newest {namespace_newest}",
        )

    if ev.foreign_title_hits / ev.support >= DEFAULTS.profiling.foreign_title_reject_rate:
        return KeyVerdict(
            ns, key, ev.mf_type, False, "foreign_product_title", support=ev.support,
            detail=f"{ev.foreign_title_hits}/{ev.support} values {ev.foreign_title_detail}",
        )

    rate = ev.matches / ev.observed if ev.observed else None

    notes = []
    if ev.support >= 3 and len(ev.distinct_values) == 1:
        notes.append("identical value on every product -- check if store-wide")
    if ev.foreign_title_hits:
        notes.append(
            f"REVIEW: {ev.foreign_title_hits}/{ev.support} values {ev.foreign_title_detail}"
        )

    if ev.support < store.tuning.profiling.allowlist_min_support:
        reason = "low_support_admitted"
    elif ev.observed == 0:
        reason = "no_render_evidence"
    elif ev.observed < store.tuning.profiling.allowlist_min_support:
        reason = "low_render_evidence"
    elif rate is not None and rate >= store.tuning.profiling.allowlist_min_hit_rate:
        reason = "rendered"
    elif ev.matches == 0:
        reason = "unrendered_retrieval_only"
    else:
        reason = "partially_rendered"

    return KeyVerdict(
        ns, key, ev.mf_type, True, reason,
        hit_rate=rate,
        support=ev.support,
        observed=ev.observed,
        matches=ev.matches,
        labels=_resolve_labels(ev.colon_labels, ev.guessed_labels),
        matched_handles=sorted(ev.matched_handles),
        detail="; ".join(notes),
    )


def _derive_allowlist(
    store: StoreConfig,
    products: list[Product],
    page_index: dict[str, PageIndex],
    regions: dict[str, list[str]],
    known_handles: frozenset[str],
    known_ids: frozenset[str],
) -> dict[Ident, KeyVerdict]:
    distinctive = distinctive_title_tokens(
        {p.handle: p.title for p in products if p.handle}
    )
    namespace_newest = _namespace_newest(products)
    evidence = _collect_evidence(
        store, products, page_index, regions, known_handles, known_ids, distinctive
    )
    return {
        ident: _verdict_for(store, ident, ev, namespace_newest.get(ident[0], ""))
        for ident, ev in evidence.items()
    }


def _resolve_labels(
    colon: Counter[str], guessed: Counter[str]
) -> list[tuple[str, int]]:
    if colon:
        return colon.most_common(3)
    if not guessed:
        return []
    total = sum(guessed.values())
    _, count = guessed.most_common(1)[0]
    if count >= DEFAULTS.profiling.label_min_observations and count / total >= DEFAULTS.profiling.label_min_dominance:
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


def _spec_pairs(region: list[str], eligible: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, block in enumerate(region):
        if block not in eligible or block in seen:
            continue
        if len(block.split()) > DEFAULTS.profiling.spec_value_max_tokens:
            continue

        inline = inline_label(block)
        if inline:
            if is_commerce_constant(inline[0], inline[1]):
                seen.add(block)
                continue
            seen.add(block)
            out.append(
                {
                    "block": block,
                    "value": inline[1],
                    "label": inline[0],
                    "support": 1,
                    "single_page": True,
                }
            )
            continue

        found = label_for(region, i)
        if not found or not found[1]:
            continue
        seen.add(block)
        if is_numeric_value(block):
            continue
        if is_commerce_constant(found[0], block):
            continue
        out.append(
            {
                "block": block,
                "value": block,
                "label": found[0],
                "support": 1,
                "single_page": True,
            }
        )
    return out


def _explained_index(
    by_handle: dict[str, Product],
    regions: dict[str, list[str]],
    verdicts: dict[Ident, KeyVerdict],
) -> dict[str, PageIndex]:
    explained: dict[str, PageIndex] = {}
    for handle, product in by_handle.items():
        if handle not in regions:
            continue
        parts = [visible_text(product.description_html), product.title]
        for mf in product.metafields:
            ident = (mf.namespace, mf.key)
            verdict = verdicts.get(ident)
            if not verdict or not verdict.admitted:
                continue
            parts.extend(candidates(mf.type, mf.value or ""))
        explained[handle] = PageIndex.build(" \n ".join(p for p in parts if p))
    return explained


def _residual_blocks(
    store: StoreConfig,
    regions: dict[str, list[str]],
    explained: dict[str, PageIndex],
) -> tuple[dict[str, list[str]], int, int]:
    residual: dict[str, list[str]] = {}
    total_words = 0
    residual_words = 0
    for handle, region in regions.items():
        index = explained.get(handle)
        leftover: list[str] = []
        for block in region:
            block_tokens = tokens(block)
            total_words += len(block_tokens)
            if not block_tokens:
                continue
            matched = index is not None and match_candidate(
                block, index, threshold=store.tuning.matching.containment_threshold
            ).matched
            if not matched:
                leftover.append(block)
                residual_words += len(block_tokens)
        residual[handle] = leftover
    return residual, total_words, residual_words


def _template_constants(
    by_handle: dict[str, Product],
    regions: dict[str, list[str]],
    residual_blocks: dict[str, list[str]],
    pre_group: dict[str, list[str]],
    group_shared: dict[str, list[str]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
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
            claimed = {p["block"] for p in pairs}
            per_product_theme[handle] = sorted(set(residual) - claimed)
            continue

        sets = [set(residual_blocks.get(h, [])) for h in template_handles]
        shared = set.intersection(*sets) if sets else set()

        stripped_by_group: set[str] = set()
        for h in template_handles:
            stripped_by_group |= set(group_shared.get(h, []))

        recovered: dict[str, dict[str, Any]] = {}
        for h in template_handles:
            source = pre_group.get(h) or regions.get(h, [])
            for pair in _spec_pairs(source, shared | stripped_by_group):
                recovered.setdefault(pair["block"], pair)

        constants[template] = [
            {
                "value": recovered[b]["value"] if b in recovered else b,
                "label": recovered[b]["label"] if b in recovered else None,
                "support": len(template_handles),
                "single_page": False,
            }
            for b in sorted(shared | set(recovered))
            if b in recovered or len(b.split()) >= 3
        ]
        for h in template_handles:
            per_product_theme[h] = sorted(set(residual_blocks.get(h, [])) - shared)

    return constants, per_product_theme


def _per_product_pairs(
    by_handle: dict[str, Product],
    regions: dict[str, list[str]],
    constants: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    per_product: dict[str, list[dict[str, Any]]] = {}
    for handle, region in regions.items():
        template = template_key(by_handle[handle])
        already = {
            (c.get("label"), c.get("value")) for c in constants.get(template, [])
        }
        pairs = [
            pair
            for pair in _spec_pairs(region, set(region))
            if pair.get("label") and (pair["label"], pair["value"]) not in already
        ]
        if pairs:
            per_product[handle] = pairs
    return per_product


def _coverage_totals(
    constants: dict[str, list[dict[str, Any]]],
    per_product_theme: dict[str, list[str]],
    total_words: int,
    residual_words: int,
) -> dict[str, Any]:
    return {
        "region_words_total": total_words,
        "residual_words": residual_words,
        "template_constant_words": sum(
            len(c["value"].split()) for blocks in constants.values() for c in blocks
        ),
        "per_product_unreachable_words": sum(
            len(b.split()) for blocks in per_product_theme.values() for b in blocks
        ),
        "coverage_pct": (
            round(100.0 * (total_words - residual_words) / total_words, 1)
            if total_words else None
        ),
    }


def _residual_analysis(
    store: StoreConfig,
    by_handle: dict[str, Product],
    regions: dict[str, list[str]],
    verdicts: dict[Ident, KeyVerdict],
    pre_group: dict[str, list[str]] | None = None,
    group_shared: dict[str, list[str]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    explained = _explained_index(by_handle, regions, verdicts)
    residual_blocks, total_words, residual_words = _residual_blocks(store, regions, explained)
    constants, per_product_theme = _template_constants(
        by_handle, regions, residual_blocks, pre_group or {}, group_shared or {}
    )
    per_product = _per_product_pairs(by_handle, regions, constants)

    theme_counts = {
        h: {"blocks": len(blocks), "words": sum(len(b.split()) for b in blocks)}
        for h, blocks in per_product_theme.items()
    }
    ranked = sorted(theme_counts, key=lambda h: -theme_counts[h]["words"])

    return (
        {
            "by_template": constants,
            "per_product": per_product,
            "per_product_theme_counts": theme_counts,
            "per_product_theme_sample": {
                h: per_product_theme[h][:20] for h in ranked[:5] if per_product_theme[h]
            },
        },
        _coverage_totals(constants, per_product_theme, total_words, residual_words),
    )
