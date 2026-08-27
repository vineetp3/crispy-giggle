"""Merge API and page evidence into field assertions, then load Postgres.

Reads api.jsonl + profile.json + spec_labels/; runs after profiling, before indexing.
Gotchas and their measurements: docs/reference/ingest.md
"""

from __future__ import annotations

import re
from typing import Any

from pier39_poc.core.blocks import visible_text
from pier39_poc.core.matching import (
    FREE_FROM_FIELD,
    FREE_FROM_KEYS,
    GID_RE,
    candidates,
    is_reference_type,
)
from pier39_poc.core.models import (
    IDENTITY_ASSERTION_FIELDS,
    Assertion,
    KeyVerdictRecord,
    Product,
)
from pier39_poc.core.quotability import is_quotable_metafield, is_quotable_theme_value
from pier39_poc.infra import db
from pier39_poc.infra.artifacts import load_products, record_stage, sha256
from pier39_poc.infra.config import StoreConfig
from pier39_poc.ingest.crawl import template_key
from pier39_poc.ingest.labels import SPEC, UNCERTAIN, WIDGET, LabelPolicy, NonePolicy
from pier39_poc.ingest.profiling import load_profile

REFERENCE_RELATIONS = {
    "related_products": "related_product",
    "complementary_products": "complementary_product",
    "frequently_paired_with": "frequently_paired_with",
    "flavors": "flavor_of",
    "extra_product": "bundle_extra",
    "prebuilt": "bundle_prebuilt",
    "multiple_bundle_products": "bundle_member",
}


REVIEW_QUANTITIES = {
    "rating": ("reviews.rating", "stamped.reviews_average", "loox.avg_rating"),
    "rating_count": (
        "reviews.rating_count",
        "stamped.reviews_count",
        "loox.num_reviews",
    ),
}


def assertion_row(a: Assertion) -> dict[str, Any]:
    return {
        "field": a.field,
        "label": a.label,
        "value": a.value,
        "source": a.source,
        "source_kind": a.source_kind,
        "rendered": a.rendered,
        "trust_class": a.trust_class,
        "source_updated_at": a.source_updated_at,
        "value_hash": sha256(f"{a.field}|{a.source}|{a.value}"),
    }


def _conflicting_review_fields(product: Product) -> set[str]:
    values: dict[str, set[str]] = {}
    for mf in product.metafields:
        full = f"{mf.namespace}.{mf.key}"
        for quantity, keys in REVIEW_QUANTITIES.items():
            if full in keys:
                for cand in candidates(mf.type, mf.value or ""):
                    values.setdefault(quantity, set()).add(cand.strip())
    dropped: set[str] = set()
    for quantity, seen in values.items():
        if len(seen) > 1:
            dropped.update(REVIEW_QUANTITIES[quantity])
    return dropped


def build_assertions(
    product: Product,
    allowlist: dict[tuple[str, str], KeyVerdictRecord],
    template_constants: dict[str, list[dict[str, Any]]],
    description_rendered: frozenset[str] = frozenset(),
    crawled: frozenset[str] = frozenset(),
    per_product: dict[str, list[dict[str, Any]]] | None = None,
    policy: LabelPolicy | None = None,
    store: StoreConfig | None = None,
) -> list[Assertion]:
    out: list[Assertion] = []
    handle = product.handle
    conflicting = _conflicting_review_fields(product)

    description = visible_text(product.description_html)
    if description:
        shown = handle in description_rendered
        out.append(
            Assertion(
                field="description",
                label=None,
                value=description,
                source="descriptionHtml",
                source_kind="description",
                rendered=shown,
                trust_class="quotable" if shown else "retrieval",
                source_updated_at=product.updated_at,
            )
        )

    for key in IDENTITY_ASSERTION_FIELDS:
        value = (getattr(product, key) or "").strip()
        if value:
            out.append(
                Assertion(
                    field=key,
                    label=None,
                    value=value,
                    source=f"api:{key}",
                    source_kind="api",
                    rendered=handle in crawled,
                    trust_class="quotable",
                    source_updated_at=product.updated_at,
                )
            )

    for mf in product.metafields:
        ident = (mf.namespace, mf.key)
        verdict = allowlist.get(ident)
        if verdict is None:
            continue
        mf_type = mf.type
        if is_reference_type(mf_type):
            continue
        values = candidates(mf_type, mf.value or "")
        if not values:
            continue
        if f"{ident[0]}.{ident[1]}" in conflicting:
            continue

        rendered = handle in set(verdict.matched_handles)
        quotable = is_quotable_metafield(ident[0], mf_type, values)
        field_name = (
            FREE_FROM_FIELD if ident in FREE_FROM_KEYS else f"{ident[0]}.{ident[1]}"
        )
        out.append(
            Assertion(
                field=field_name,
                label=verdict.label,
                value="; ".join(values) if len(values) > 1 else values[0],
                source=f"metafield:{ident[0]}.{ident[1]}",
                source_kind="metafield",
                rendered=rendered,
                trust_class="quotable" if quotable else "retrieval",
                source_updated_at=mf.updatedAt,
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
                field=label.lower().replace(" ", "_") if label else f"theme.constant_{i}",
                label=label,
                value=constant["value"],
                source=f"theme:{key}",
                source_kind="theme",
                rendered=True,
                trust_class="quotable" if quotable else "retrieval",
                source_updated_at=None,
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
                field=field,
                label=label,
                value=pair["value"],
                source=f"theme:{handle}",
                source_kind="theme",
                rendered=True,
                trust_class="quotable" if quotable else "retrieval",
                source_updated_at=None,
            )
        )

    return out


def build_edges(product: Product) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pid = product.product_id

    for mf in product.metafields:
        if not is_reference_type(mf.type):
            continue
        key = mf.key
        relation = REFERENCE_RELATIONS.get(key, key)
        source = f"metafield:{mf.namespace}.{key}"
        for kind, num in GID_RE.findall(mf.value or ""):
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

    for collection in product.collections:
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

    for variant in product.variants:
        rows.append(
            {
                "from_type": "product",
                "from_id": pid,
                "relation": "has_variant",
                "to_type": "variant",
                "to_id": str(variant.id).rsplit("/", 1)[-1],
                "source": "api:variants",
            }
        )

    return rows


def run(store: StoreConfig, policy: LabelPolicy | None = None) -> dict[str, Any]:
    products = load_products(store)
    profile = load_profile(store)

    allowlist = {
        (entry.namespace, entry.key): entry for entry in profile.allowlist
    }
    constants = profile.template_constants.by_template
    per_product = profile.template_constants.per_product
    gate = policy or NonePolicy()
    rejected = profile.rejected
    description_rendered = frozenset(profile.description_rendered_handles)
    crawled = frozenset(profile.region_words)

    template_key_for = {
        p.handle: template_key(p) for p in products if p.handle
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
        db.set_coverage(conn, store_id, profile.coverage.coverage_pct)

        db.replace_rejected_keys(
            conn,
            store_id,
            [
                {
                    "namespace": r.namespace,
                    "key": r.key,
                    "reason_code": r.reason,
                    "detail": r.detail,
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
            p.product_id
            for p in products
            if p.online_store_url and not p.sellable
        ]
        counts["abandoned_skipped"] = len(abandoned)
        counts["abandoned_deleted"] = db.delete_products(conn, store_id, abandoned)
        skip = set(abandoned)

        for product in products:
            if product.product_id in skip:
                continue
            product_id = db.upsert_product(conn, store_id, product)
            db.upsert_variants(conn, product_id, [v.model_dump() for v in product.variants])

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
                conn, product_id, [assertion_row(a) for a in assertions]
            )
            for assertion in assertions:
                counts[assertion.trust_class] += 1

            edges = build_edges(product)
            db.replace_edges(conn, store_id, product.product_id, edges)

            counts["products"] += 1
            counts["assertions"] += len(assertions)
            counts["edges"] += len(edges)
            conn.commit()

    record_stage(store, "merge", counts)
    return counts
