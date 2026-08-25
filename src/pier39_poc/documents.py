"""Build retrieval documents for a product.

Gotchas:

One document per trust class, not one per product. Both classes must be retrievable --
unrendered enrichment is the most retrieval-useful content on some products -- but the
document text is what an answer layer receives as grounding context, and a single mixed
string carries no marker separating a vetted nutrition panel from generated prose. The
trust class has to live on the chunk, because a class stored only on a sibling assertion
row is not visible to whoever reads `documents.text`.

`free_from` never enters a document. Its polarity is invisible to an embedding: writing
`Almonds; Cashews; Hazelnuts` for a product that contains none of them teaches the vector
the opposite of the fact. Polarity-bearing fields are filters, not prose, and negation is
answered in SQL.

Anything filterable stays a column. Filtering in SQL is exact; filtering by embedding
similarity is not.
"""

from __future__ import annotations

from typing import Any

from .matching import FREE_FROM_FIELD

MAX_FIELD_CHARS = 1200
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
        body.append(description[:MAX_FIELD_CHARS])

    for assertion in scoped:
        field = assertion["field"]
        if field in SKIP_FIELDS or field in EXCLUDED_FROM_TEXT:
            continue
        value = (assertion.get("value") or "").strip()
        if not value:
            continue
        label = assertion.get("label") or _readable(field)
        body.append(f"{label}: {value[:MAX_FIELD_CHARS]}")

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
                (f"faq_{i}", part[:MAX_FIELD_CHARS], assertion.get("trust_class", "retrieval"))
            )
    return out
