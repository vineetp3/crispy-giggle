"""Merge API and page evidence into field assertions, then load Postgres.

Gotchas:

`filter.contains` is a FREE-FROM list, not a contains list. It is renamed to
`free_from` here so no downstream reader can invert the allergen filter. Proved on
skout: the peanut-butter bar omits `Peanut`; the lemon-poppyseed cookie includes it.

Quotability is decided by type and shape, NOT by render presence. Rendering only tells
you the theme consumed the key -- the page is rendered from the metafield, so a match
says nothing about whether a merchant vetted the value. skout's `custom.short_description`
renders on every sampled product and is generated marketing prose; `custom.nutrients` is
a typed list of checkable facts whose theme presence is incidental. So: prose types and
untyped `string` are never quotable, `json` is never quotable (skout's `product_faqs` and
`product_attributes` are json holding hedged generated sentences), and a candidate over
QUOTABLE_MAX_TOKENS is prose regardless of its declared type.

Freshness is recorded (`source_updated_at`) but deliberately does NOT gate quotability.
Median metafield age on skout is over 1,000 days for `custom.nutrients`, `filter.contains`
and `filter.curated`; an age cliff would empty the quotable set rather than make it safer.
Decay needs a re-confirmation loop that v0 does not have.

`rendered` is per product, never per key. A key at an 0.85 hit rate does not render on
the other 15%, and a product whose page was never fetched has no render evidence at all.

Conflicts are dropped, never reconciled. skout's peanut-butter cookie reports 72, 63 and
4.8 across three review namespaces and remi reports 51, 627 and 1193 for one product, so
neither gets a review count at all.

A theme constant is quotable only when it carries a recovered label. A labelled pair is
markup-evidenced (`Material: BPA-free, food-safe plastic`); an unlabelled one is whatever
survived intersecting a template group's residual, and on skout's heterogeneous `_default`
group that includes blog titles, "1 year ago" and per-variant pricing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import db
from .artifacts import load_products, read_json, record_stage, sha256
from .blocks import visible_text
from .config import StoreConfig
from .crawl import template_key
from .labels import SPEC, UNCERTAIN, WIDGET, LabelPolicy, NonePolicy
from .matching import (
    FREE_FROM_FIELD,
    FREE_FROM_KEYS,
    candidates,
    is_reference_type,
    tokens,
)

REFERENCE_RELATIONS = {
    "related_products": "related_product",
    "complementary_products": "complementary_product",
    "frequently_paired_with": "frequently_paired_with",
    "flavors": "flavor_of",
    "extra_product": "bundle_extra",
    "prebuilt": "bundle_prebuilt",
    "multiple_bundle_products": "bundle_member",
}

_GID_RE = re.compile(r"gid://shopify/(\w+)/(\d+)")

PROSE_TYPES = frozenset({"multi_line_text_field", "rich_text_field", "html"})
UNTYPED_TYPES = frozenset({"string", ""})
NEVER_QUOTABLE_TYPES = frozenset({"json", "json_string"})
QUOTABLE_MAX_TOKENS = 8

_EMBEDDED_PRICE_RE = re.compile(r"[$£€]\s*\d")

THEME_QUOTABLE_MAX_TOKENS = 15
_NUMERIC_ONLY_RE = re.compile(r"^[\d\s./:+%-]+$")

REVIEW_QUANTITIES = {
    "rating": ("reviews.rating", "stamped.reviews_average", "loox.avg_rating"),
    "rating_count": (
        "reviews.rating_count",
        "stamped.reviews_count",
        "loox.num_reviews",
    ),
}

NEVER_QUOTABLE_NAMESPACES = frozenset({"agentiq"})

IDENTITY_FIELDS = ("title", "vendor", "product_type")


@dataclass
class Assertion:
    field: str
    label: str | None
    value: str
    source: str
    source_kind: str
    rendered: bool
    trust_class: str
    source_updated_at: str | None

    def to_row(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label": self.label,
            "value": self.value,
            "source": self.source,
            "source_kind": self.source_kind,
            "rendered": self.rendered,
            "trust_class": self.trust_class,
            "source_updated_at": self.source_updated_at,
            "value_hash": sha256(f"{self.field}|{self.source}|{self.value}"),
        }


def is_quotable_metafield(namespace: str, mf_type: str, values: list[str]) -> bool:
    if namespace in NEVER_QUOTABLE_NAMESPACES:
        return False
    base = mf_type.removeprefix("list.")
    if base in PROSE_TYPES or base in UNTYPED_TYPES or base in NEVER_QUOTABLE_TYPES:
        return False
    for value in values:
        if "<" in value or value.rstrip().endswith(("?", ":")):
            return False
        if _EMBEDDED_PRICE_RE.search(value):
            return False
        if len(tokens(value)) > QUOTABLE_MAX_TOKENS:
            return False
    return bool(values)


def is_quotable_theme_value(value: str) -> bool:
    if "<" in value or value.rstrip().endswith(("?", ":")):
        return False
    if _EMBEDDED_PRICE_RE.search(value):
        return False
    if _NUMERIC_ONLY_RE.match(value):
        return False
    return 0 < len(tokens(value)) <= THEME_QUOTABLE_MAX_TOKENS


def _conflicting_review_fields(product: dict[str, Any]) -> set[str]:
    values: dict[str, set[str]] = {}
    for mf in product.get("metafields") or []:
        full = f"{mf.get('namespace')}.{mf.get('key')}"
        for quantity, keys in REVIEW_QUANTITIES.items():
            if full in keys:
                for cand in candidates(mf.get("type") or "", mf.get("value") or ""):
                    values.setdefault(quantity, set()).add(cand.strip())
    dropped: set[str] = set()
    for quantity, seen in values.items():
        if len(seen) > 1:
            dropped.update(REVIEW_QUANTITIES[quantity])
    return dropped


def build_assertions(
    product: dict[str, Any],
    allowlist: dict[tuple[str, str], dict[str, Any]],
    template_constants: dict[str, list[dict[str, Any]]],
    description_rendered: frozenset[str] = frozenset(),
    crawled: frozenset[str] = frozenset(),
    per_product: dict[str, list[dict[str, Any]]] | None = None,
    policy: LabelPolicy | None = None,
    store: StoreConfig | None = None,
) -> list[Assertion]:
    out: list[Assertion] = []
    handle = product.get("handle") or ""
    conflicting = _conflicting_review_fields(product)

    description = visible_text(product.get("description_html") or "")
    if description:
        shown = handle in description_rendered
        out.append(
            Assertion(
                "description", None, description, "descriptionHtml", "description",
                shown, "quotable" if shown else "retrieval", product.get("updated_at"),
            )
        )

    for key in IDENTITY_FIELDS:
        value = (product.get(key) or "").strip()
        if value:
            out.append(
                Assertion(key, None, value, f"api:{key}", "api", handle in crawled,
                          "quotable", product.get("updated_at"))
            )

    for mf in product.get("metafields") or []:
        ident = (mf.get("namespace") or "", mf.get("key") or "")
        verdict = allowlist.get(ident)
        if verdict is None:
            continue
        mf_type = mf.get("type") or ""
        if is_reference_type(mf_type):
            continue
        values = candidates(mf_type, mf.get("value") or "")
        if not values:
            continue
        if f"{ident[0]}.{ident[1]}" in conflicting:
            continue

        rendered = handle in set(verdict.get("matched_handles") or ())
        quotable = is_quotable_metafield(ident[0], mf_type, values)
        field_name = (
            FREE_FROM_FIELD if ident in FREE_FROM_KEYS else f"{ident[0]}.{ident[1]}"
        )
        out.append(
            Assertion(
                field_name,
                verdict.get("label"),
                "; ".join(values) if len(values) > 1 else values[0],
                f"metafield:{ident[0]}.{ident[1]}",
                "metafield",
                rendered,
                "quotable" if quotable else "retrieval",
                mf.get("updatedAt"),
            )
        )

    key = template_key(product)
    gate = policy or NonePolicy()
    for i, constant in enumerate(template_constants.get(key, [])):
        label = constant.get("label")
        if label and gate.gates_template_constants and store is not None:
            if gate.verdict(store, label, constant["value"]) == WIDGET:
                continue
        quotable = bool(label) and is_quotable_theme_value(constant["value"])
        out.append(
            Assertion(
                label.lower().replace(" ", "_") if label else f"theme.constant_{i}",
                label,
                constant["value"],
                f"theme:{key}",
                "theme",
                True,
                "quotable" if quotable else "retrieval",
                None,
            )
        )

    used: dict[str, int] = {}
    for pair in (per_product or {}).get(handle, []):
        label = pair.get("label")
        if not label:
            continue
        verdict = gate.verdict(store, label, pair["value"]) if store else "widget"
        if verdict not in (SPEC, UNCERTAIN):
            continue
        quotable = verdict == SPEC and is_quotable_theme_value(pair["value"])
        field = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "theme_spec"
        used[field] = used.get(field, 0) + 1
        if used[field] > 1:
            field = f"{field}_{used[field]}"
        out.append(
            Assertion(
                field,
                label,
                pair["value"],
                f"theme:{handle}",
                "theme",
                True,
                "quotable" if quotable else "retrieval",
                None,
            )
        )

    return out


def build_edges(product: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pid = str(product["product_id"])

    for mf in product.get("metafields") or []:
        mf_type = mf.get("type") or ""
        if not is_reference_type(mf_type):
            continue
        key = mf.get("key") or ""
        relation = REFERENCE_RELATIONS.get(key, key)
        source = f"metafield:{mf.get('namespace')}.{key}"
        for kind, num in _GID_RE.findall(mf.get("value") or ""):
            if kind == "MediaImage":
                continue
            rows.append(
                {
                    "from_type": "product",
                    "from_id": pid,
                    "relation": relation,
                    "to_type": kind.lower(),
                    "to_id": num,
                    "source": source,
                }
            )

    for collection in product.get("collections") or []:
        if collection.get("handle"):
            rows.append(
                {
                    "from_type": "product",
                    "from_id": pid,
                    "relation": "in_collection",
                    "to_type": "collection",
                    "to_id": collection["handle"],
                    "source": "api:collections",
                }
            )

    for variant in product.get("variants") or []:
        rows.append(
            {
                "from_type": "product",
                "from_id": pid,
                "relation": "has_variant",
                "to_type": "variant",
                "to_id": str(variant["id"]).rsplit("/", 1)[-1],
                "source": "api:variants",
            }
        )

    return rows


def run(store: StoreConfig, policy: LabelPolicy | None = None) -> dict[str, Any]:
    products = load_products(store)
    profile = read_json(store.profile_path)

    allowlist = {
        (entry["namespace"], entry["key"]): entry for entry in profile.get("allowlist", [])
    }
    constants = (profile.get("template_constants") or {}).get("by_template") or {}
    per_product = (profile.get("template_constants") or {}).get("per_product") or {}
    gate = policy or NonePolicy()
    rejected = profile.get("rejected") or []
    description_rendered = frozenset(profile.get("description_rendered_handles") or ())
    crawled = frozenset(profile.get("region_words") or {})

    template_key_for = {
        p["handle"]: template_key(p) for p in products if p.get("handle")
    }

    counts = {
        "products": 0,
        "assertions": 0,
        "edges": 0,
        "quotable": 0,
        "retrieval": 0,
        "abandoned_skipped": 0,
    }

    with db.connect() as conn:
        store_id = db.upsert_store(conn, store.slug, store.domain, store.admin_api_version)
        db.set_coverage(conn, store_id, (profile.get("coverage") or {}).get("coverage_pct"))

        db.replace_rejected_keys(
            conn,
            store_id,
            [
                {
                    "namespace": r["namespace"],
                    "key": r["key"],
                    "reason_code": r["reason"],
                    "detail": r.get("detail"),
                }
                for r in rejected
            ],
        )
        db.replace_template_constants(
            conn,
            store_id,
            [
                {
                    "template_key": tpl,
                    "value": c["value"],
                    "label": c.get("label"),
                    "value_hash": sha256(c["value"]),
                }
                for tpl, values in constants.items()
                for c in values
            ]
            + [
                {
                    "template_key": template_key_for.get(h, "_default"),
                    "handle": h,
                    "value": c["value"],
                    "label": c.get("label"),
                    "value_hash": sha256(c["value"]),
                }
                for h, values in per_product.items()
                for c in values
            ],
        )

        abandoned = [
            str(p["product_id"])
            for p in products
            if p.get("online_store_url") and not p.get("sellable", True)
        ]
        counts["abandoned_skipped"] = len(abandoned)
        counts["abandoned_deleted"] = db.delete_products(conn, store_id, abandoned)
        skip = set(abandoned)

        for product in products:
            if str(product["product_id"]) in skip:
                continue
            product_id = db.upsert_product(conn, store_id, product)
            db.upsert_variants(conn, product_id, product.get("variants") or [])

            assertions = build_assertions(
                product,
                allowlist,
                constants,
                description_rendered,
                crawled,
                per_product,
                gate,
                store,
            )
            db.replace_assertions(
                conn, product_id, [a.to_row() for a in assertions]
            )
            for assertion in assertions:
                counts[assertion.trust_class] += 1

            edges = build_edges(product)
            db.replace_edges(conn, store_id, str(product["product_id"]), edges)

            counts["products"] += 1
            counts["assertions"] += len(assertions)
            counts["edges"] += len(edges)
            conn.commit()

    record_stage(store, "merge", counts)
    return counts
