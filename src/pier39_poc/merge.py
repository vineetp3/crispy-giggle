"""Merge API and page evidence into field assertions, then load Postgres.

Assertions, not a merged blob. Every value carries where it came from, whether it is
rendered on the live storefront, and which trust class it belongs to:

  retrieval  -- feeds the embedding and matching. NEVER quoted to a shopper.
  quotable   -- the bot may state it as fact.

Unrendered enrichment is retrieval-only. skout's `custom.product_faqs` are visibly
LLM-generated and hedge with "check the ingredient statement on the package"; no
merchant has vetted them on the storefront. This split is what stops the bot asserting
an unvetted allergen claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import db
from .artifacts import load_products, read_json, record_stage, sha256
from .blocks import visible_text
from .config import StoreConfig
from .matching import candidates, is_reference_type

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


def _trust_class(reason: str, rendered: bool) -> str:
    """Quotable only when the storefront shows it, or a typed metafield matched."""
    if rendered and reason in ("rendered", "partially_rendered"):
        return "quotable"
    return "retrieval"


def build_assertions(
    product: dict[str, Any],
    allowlist: dict[tuple[str, str], dict[str, Any]],
    template_constants: dict[str, list[str]],
) -> list[Assertion]:
    out: list[Assertion] = []

    description = visible_text(product.get("description_html") or "")
    if description:
        out.append(
            Assertion(
                "description", None, description, "descriptionHtml", "description",
                True, "quotable", product.get("updated_at"),
            )
        )

    for key in ("title", "vendor", "product_type"):
        value = (product.get(key) or "").strip()
        if value:
            out.append(
                Assertion(key, None, value, f"api:{key}", "api", True, "quotable",
                          product.get("updated_at"))
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
        rendered = bool(verdict.get("hit_rate")) and verdict.get("reason") in (
            "rendered",
            "partially_rendered",
        )
        trust = _trust_class(verdict.get("reason", ""), rendered)
        label = verdict.get("label")
        field_name = f"{ident[0]}.{ident[1]}"
        joined = "; ".join(values) if len(values) > 1 else values[0]
        out.append(
            Assertion(
                field_name, label, joined, f"metafield:{field_name}", "metafield",
                rendered, trust, mf.get("updatedAt"),
            )
        )

    template_key = product.get("template_suffix") or product.get("product_type") or "_default"
    for i, value in enumerate(template_constants.get(template_key, [])):
        out.append(
            Assertion(
                f"theme.constant_{i}", None, value, f"theme:{template_key}", "theme",
                True, "quotable", None,
            )
        )

    return out


def build_edges(product: dict[str, Any]) -> list[dict[str, Any]]:
    """Every product_reference and metaobject_reference becomes an edge."""
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


def run(store: StoreConfig) -> dict[str, Any]:
    products = load_products(store)
    profile = read_json(store.profile_path)

    allowlist = {
        (entry["namespace"], entry["key"]): entry for entry in profile.get("allowlist", [])
    }
    constants = (profile.get("template_constants") or {}).get("by_template") or {}
    rejected = profile.get("rejected") or []

    counts = {"products": 0, "assertions": 0, "edges": 0, "quotable": 0, "retrieval": 0}

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
                {"template_key": tpl, "value": value, "label": None, "value_hash": sha256(value)}
                for tpl, values in constants.items()
                for value in values
            ],
        )

        for product in products:
            # One transaction per product: assertions, variants and edges land together
            # so retrieval never sees new text beside old attributes.
            product_id = db.upsert_product(conn, store_id, product)
            db.upsert_variants(conn, product_id, product.get("variants") or [])

            assertions = build_assertions(product, allowlist, constants)
            for assertion in assertions:
                db.upsert_assertion(conn, product_id, assertion.to_row())
                counts[assertion.trust_class] += 1

            edges = build_edges(product)
            db.replace_edges(conn, store_id, str(product["product_id"]), edges)

            counts["products"] += 1
            counts["assertions"] += len(assertions)
            counts["edges"] += len(edges)
            conn.commit()

    record_stage(store, "merge", counts)
    return counts
