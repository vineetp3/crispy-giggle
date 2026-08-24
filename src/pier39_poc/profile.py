"""The profiling stage: derive per-store truth about where product content lives.

Five steps, in the order specified in DESIGN.md 5.3:

1. block extraction
2. chrome removal by cross-page frequency
3. residual cleanup (widget noise, sibling product titles)
4. metafield allowlist derivation, with contamination rejection first
5. residual analysis -> template constants and a coverage number

The output, profile.json, is the deliverable of this POC. The search CLI merely
validates it.
"""

from __future__ import annotations

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
from .blocks import (
    ChromeProfile,
    build_chrome_profile,
    extract_blocks,
    label_for,
    product_region,
)
from .config import StoreConfig
from .matching import (
    PageIndex,
    candidates,
    detect_contamination,
    detect_foreign_product_ids,
    is_reference_type,
    match_candidate,
    tokens,
)

# Keys that must never reach a shopper regardless of match behaviour.
ALWAYS_EXCLUDE = {
    ("custom", "admin_title"),
}

# Namespaces that are plumbing for other apps/channels, never product content.
EXCLUDED_NAMESPACES = {
    "mm-google-shopping",
    "mc-facebook",
    "msft_bingads",
    "SEOMetaManager",
    "seo",
    "shopify--discovery--product_search_boost",
}

# Value substrings that mark a metafield as a rendered widget blob rather than content.
WIDGET_MARKERS = ("<div", "<span", "<script", "<link", "data-oke-", "stamped-", "loox")


@dataclass
class KeyVerdict:
    namespace: str
    key: str
    type: str
    admitted: bool
    reason: str
    hit_rate: float | None = None
    support: int = 0
    matches: int = 0
    labels: list[str] = field(default_factory=list)
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
            "matches": self.matches,
            "label": self.labels[0] if self.labels else None,
            "labels_seen": self.labels,
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
        from .blocks import visible_text

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


def _description_tokens(product: dict[str, Any]) -> set[str]:
    from .blocks import visible_text

    return set(tokens(visible_text(product.get("description_html") or "")))


def build_profile(store: StoreConfig) -> dict[str, Any]:
    products = load_products(store)
    by_handle = {p["handle"]: p for p in products if p.get("handle")}
    known_handles = frozenset(by_handle)
    known_ids = frozenset(str(p["product_id"]) for p in products)
    titles_by_handle = {h: (p.get("title") or "").strip().lower() for h, p in by_handle.items()}

    handles = [h for h in crawled_handles(store) if h in by_handle]

    # ---------------------------------------------------------------- step 1
    page_blocks = _page_blocks(store, handles, store.min_block_chars)
    page_index = _page_indexes(store, handles)

    # ---------------------------------------------------------------- step 2
    chrome = build_chrome_profile(page_blocks, threshold=store.chrome_threshold)

    # ---------------------------------------------------------------- step 3
    regions: dict[str, list[str]] = {}
    for handle, blocks in page_blocks.items():
        foreign = frozenset(t for h, t in titles_by_handle.items() if h != handle and t)
        regions[handle] = product_region(blocks, chrome, foreign_titles=foreign)

    # ---------------------------------------------------------------- step 4
    verdicts = _derive_allowlist(
        store, products, by_handle, page_index, regions, known_handles, known_ids
    )

    # ---------------------------------------------------------------- step 5
    constants, coverage = _residual_analysis(
        store, by_handle, regions, verdicts, products
    )

    admitted = [v for v in verdicts.values() if v.admitted]
    rejected = [v for v in verdicts.values() if not v.admitted]

    payload = {
        "slug": store.slug,
        "pages_analysed": len(page_blocks),
        "products_total": len(products),
        "products_published": sum(1 for p in products if p.get("online_store_url")),
        "chrome": {
            "threshold": store.chrome_threshold,
            "blocks": len(chrome.chrome),
            "set_hash": sha256(" ".join(sorted(chrome.chrome))),
            "frequency_histogram": chrome.histogram(),
        },
        "region_words": {h: sum(len(b.split()) for b in r) for h, r in regions.items()},
        "allowlist": [v.to_dict() for v in sorted(admitted, key=lambda v: v.full_key)],
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
            "coverage_pct": coverage.get("coverage_pct"),
        },
    )
    return payload


def _derive_allowlist(
    store: StoreConfig,
    products: list[dict[str, Any]],
    by_handle: dict[str, dict[str, Any]],
    page_index: dict[str, PageIndex],
    regions: dict[str, list[str]],
    known_handles: frozenset[str],
    known_ids: frozenset[str],
) -> dict[tuple[str, str], KeyVerdict]:
    """One verdict per metafield key, with a reason code either way."""
    support: Counter[tuple[str, str]] = Counter()
    matches: Counter[tuple[str, str]] = Counter()
    types: dict[tuple[str, str], str] = {}
    labels: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    newest: dict[str, str] = {}
    updated: dict[tuple[str, str], str] = {}
    rejections: dict[tuple[str, str], tuple[str, str]] = {}
    distinct_values: dict[tuple[str, str], set[str]] = defaultdict(set)

    # Newest write per namespace, for the freshness check.
    for product in products:
        for mf in product.get("metafields") or []:
            stamp = mf.get("updatedAt") or ""
            ns = mf.get("namespace") or ""
            if stamp > newest.get(ns, ""):
                newest[ns] = stamp

    for product in products:
        handle = product.get("handle")
        pid = str(product.get("product_id"))
        for mf in product.get("metafields") or []:
            ns, key = mf.get("namespace") or "", mf.get("key") or ""
            ident = (ns, key)
            types.setdefault(ident, mf.get("type") or "")
            stamp = mf.get("updatedAt") or ""
            if stamp > updated.get(ident, ""):
                updated[ident] = stamp

            raw = mf.get("value") or ""

            # Contamination first, unconditionally. Highest-value rule.
            contaminated = detect_contamination(
                raw, pid, handle or "", known_handles, store.domain
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

            support[ident] += 1
            index = page_index.get(handle or "")
            if index is None:
                continue

            hit = False
            for cand in cands:
                if match_candidate(cand, index, threshold=store.containment_threshold).matched:
                    hit = True
                    label = _label_from_region(regions.get(handle or "") or [], cand)
                    if label:
                        labels[ident][label] += 1
                    break
            if hit:
                matches[ident] += 1

            # Diagnostic, not a rejection rule. Page chrome is removed by differencing;
            # metafields are product-scoped by construction, so "this value also
            # appears on sibling pages" is usually just a shared attribute (every
            # cookie flavour lists Organic Oat Flour). Recorded so a human can spot a
            # key that is genuinely identical store-wide.
            distinct_values[ident].add(sha_short(cands[0]))

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

        rate = matches[ident] / n
        seen_labels = [lbl for lbl, _ in labels[ident].most_common(3)]
        variety = len(distinct_values[ident])
        note = (
            "identical value on every product -- check whether this is store-wide"
            if n >= 3 and variety == 1
            else ""
        )

        if n < store.allowlist_min_support:
            # Not enough evidence to judge. Admit unrendered-but-clean keys as
            # retrieval material rather than discarding them: render-presence
            # promotes, it does not gate.
            verdicts[ident] = KeyVerdict(
                ns, key, mf_type, True, "low_support_admitted", rate, n,
                matches[ident], seen_labels, note,
            )
            continue

        if rate >= store.allowlist_min_hit_rate:
            verdicts[ident] = KeyVerdict(
                ns, key, mf_type, True, "rendered", rate, n, matches[ident],
                seen_labels, note,
            )
        elif matches[ident] == 0:
            verdicts[ident] = KeyVerdict(
                ns, key, mf_type, True, "unrendered_retrieval_only", rate, n, 0,
                seen_labels, note,
            )
        else:
            verdicts[ident] = KeyVerdict(
                ns, key, mf_type, True, "partially_rendered", rate, n,
                matches[ident], seen_labels, note,
            )

    return verdicts


def _label_from_region(region: list[str], candidate: str) -> str | None:
    """Find the candidate in the cleaned region and read the label before it."""
    cand_tokens = set(tokens(candidate))
    if not cand_tokens:
        return None
    for i, block in enumerate(region):
        block_tokens = set(tokens(block))
        if cand_tokens and cand_tokens.issubset(block_tokens):
            return label_for(region, i)
    return None


def _residual_analysis(
    store: StoreConfig,
    by_handle: dict[str, dict[str, Any]],
    regions: dict[str, list[str]],
    verdicts: dict[tuple[str, str], KeyVerdict],
    products: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split unexplained page text into template constants and unreachable content."""
    explained_tokens: dict[str, set[str]] = {}
    for handle, product in by_handle.items():
        if handle not in regions:
            continue
        toks = _description_tokens(product)
        for mf in product.get("metafields") or []:
            ident = (mf.get("namespace") or "", mf.get("key") or "")
            verdict = verdicts.get(ident)
            if not verdict or not verdict.admitted:
                continue
            for cand in candidates(mf.get("type") or "", mf.get("value") or ""):
                toks |= set(tokens(cand))
        toks |= set(tokens(product.get("title") or ""))
        explained_tokens[handle] = toks

    residual_blocks: dict[str, list[str]] = {}
    total_words = 0
    residual_words = 0

    for handle, region in regions.items():
        explained = explained_tokens.get(handle, set())
        leftover: list[str] = []
        for block in region:
            block_tokens = tokens(block)
            total_words += len(block_tokens)
            if not block_tokens:
                continue
            covered = sum(1 for t in block_tokens if t in explained) / len(block_tokens)
            if covered < 0.6:
                leftover.append(block)
                residual_words += len(block_tokens)
        residual_blocks[handle] = leftover

    # Template constants: residual blocks identical across every page of a template.
    groups: dict[str, list[str]] = defaultdict(list)
    for handle in regions:
        product = by_handle[handle]
        key = product.get("template_suffix") or product.get("product_type") or "_default"
        groups[key].append(handle)

    constants: dict[str, list[str]] = {}
    per_product_theme: dict[str, list[str]] = {}
    for template, template_handles in groups.items():
        if len(template_handles) < 2:
            for h in template_handles:
                per_product_theme[h] = residual_blocks.get(h, [])
            continue
        sets = [set(residual_blocks.get(h, [])) for h in template_handles]
        shared = set.intersection(*sets) if sets else set()
        constants[template] = sorted(b for b in shared if len(b.split()) >= 3)
        for h in template_handles:
            per_product_theme[h] = sorted(set(residual_blocks.get(h, [])) - shared)

    constant_words = sum(
        len(b.split()) for blocks in constants.values() for b in blocks
    )
    unreachable_words = sum(
        len(b.split()) for blocks in per_product_theme.values() for b in blocks
    )
    coverage_pct = (
        round(100.0 * (total_words - residual_words) / total_words, 1) if total_words else None
    )

    return (
        {
            "by_template": constants,
            "per_product_theme_sample": {
                h: blocks[:20] for h, blocks in list(per_product_theme.items())[:5]
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
