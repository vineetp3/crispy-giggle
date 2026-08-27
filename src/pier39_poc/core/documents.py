"""Build the retrieval documents for a product, one per trust class.

Called by ingest.indexing, which embeds them. Takes Postgres rows, not core.models.Product.
Gotchas and their measurements: docs/reference/core.md
"""

from __future__ import annotations

from typing import Any

from pier39_poc.core.matching import FREE_FROM_FIELD
from pier39_poc.core.tuning import DEFAULTS

SKIP_FIELDS = {"title", "vendor", "product_type", "description"}
EXCLUDED_FROM_TEXT = {FREE_FROM_FIELD}


def _header(product: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    title = product.get("title") or product.get("handle") or ""
    if title:
        lines.append(title)
    descriptor = " | ".join(
        d for d in (product.get("product_type"), product.get("vendor")) if d
    )
    if descriptor:
        lines.append(descriptor)
    tags = product.get("tags") or []
    if tags:
        lines.append("Tags: " + ", ".join(tags[:25]))
    return lines


def build_document(
    product: dict[str, Any], assertions: list[dict[str, Any]], trust_class: str
) -> str:
    scoped = [a for a in assertions if a.get("trust_class") == trust_class]
    body: list[str] = []

    description = next((a["value"] for a in scoped if a["field"] == "description"), None)
    if description:
        body.append(description[:DEFAULTS.documents.max_field_chars])

    for assertion in scoped:
        field = assertion["field"]
        if field in SKIP_FIELDS or field in EXCLUDED_FROM_TEXT:
            continue
        value = (assertion.get("value") or "").strip()
        if not value:
            continue
        label = assertion.get("label") or _readable(field)
        body.append(f"{label}: {value[:DEFAULTS.documents.max_field_chars]}")

    if not body:
        return ""
    return "\n".join(_header(product) + body).strip()


def _readable(field: str) -> str:
    tail = field.split(".", 1)[-1]
    if tail.startswith("constant_"):
        return "Details"
    return tail.replace("_", " ").replace("-", " ").strip().title()


def faq_chunks(assertions: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for assertion in assertions:
        if "faq" not in assertion["field"].lower():
            continue
        parts = [p.strip() for p in (assertion.get("value") or "").split("; ") if p.strip()]
        if len(parts) < 2:
            continue
        for i, part in enumerate(parts):
            out.append(
                (f"faq_{i}", part[:DEFAULTS.documents.max_field_chars], assertion.get("trust_class", "retrieval"))
            )
    return out
