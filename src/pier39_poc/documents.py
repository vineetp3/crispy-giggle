"""Build the retrieval document for a product.

Deliberately written, not a dump of the merged record. Two rules:

* Anything we want to FILTER on stays a column, never prose. Filtering in SQL is exact;
  filtering by embedding similarity is not.
* Both trust classes go into the document, because unrendered enrichment is the most
  retrieval-useful content on some products. The trust class governs what may be
  *quoted*, not what may be *matched*.
"""

from __future__ import annotations

from typing import Any

MAX_FIELD_CHARS = 1200
SKIP_FIELDS = {"title", "vendor", "product_type"}


def build_document(product: dict[str, Any], assertions: list[dict[str, Any]]) -> str:
    """Compose one retrieval document. Labels are used when we recovered them."""
    lines: list[str] = []

    title = product.get("title") or product.get("handle") or ""
    if title:
        lines.append(title)

    descriptors = [
        product.get("product_type"),
        product.get("vendor"),
    ]
    descriptor_line = " | ".join(d for d in descriptors if d)
    if descriptor_line:
        lines.append(descriptor_line)

    tags = product.get("tags") or []
    if tags:
        lines.append("Tags: " + ", ".join(tags[:25]))

    description = next(
        (a["value"] for a in assertions if a["field"] == "description"), None
    )
    if description:
        lines.append(description[:MAX_FIELD_CHARS])

    for assertion in assertions:
        field = assertion["field"]
        if field in SKIP_FIELDS or field == "description":
            continue
        value = (assertion.get("value") or "").strip()
        if not value:
            continue
        label = assertion.get("label") or _readable(field)
        lines.append(f"{label}: {value[:MAX_FIELD_CHARS]}")

    return "\n".join(lines).strip()


def _readable(field: str) -> str:
    """`custom.product_blue_content` -> `Product Blue Content` as a last resort.

    A recovered label from the page is always better; this is the fallback when the
    theme gave us nothing to read.
    """
    tail = field.split(".", 1)[-1]
    if tail.startswith("constant_"):
        return "Details"
    return tail.replace("_", " ").replace("-", " ").strip().title()


def faq_chunks(assertions: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Split FAQ-shaped fields into their own chunks.

    A long FAQ blob dilutes the product's main embedding and makes retrieval return the
    whole product when the shopper asked one specific question.
    """
    out: list[tuple[str, str]] = []
    for assertion in assertions:
        field = assertion["field"]
        if "faq" not in field.lower():
            continue
        value = assertion.get("value") or ""
        parts = [p.strip() for p in value.split("; ") if p.strip()]
        if len(parts) < 2:
            continue
        for i, part in enumerate(parts):
            out.append((f"faq_{i}", part[:MAX_FIELD_CHARS]))
    return out
