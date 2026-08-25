"""Build retrieval documents, embed them, and load them into Postgres.

Gotchas:

The content-hash gate is what makes re-running cheap: an unchanged document skips both
the write and the embedding call. It is keyed on (product, chunk), so splitting a product
into a quotable and a retrieval chunk means a change to one does not re-embed the other.

A product can legitimately have no quotable chunk. Empty documents are skipped rather
than written, so absence of a quotable chunk is the signal that nothing about that product
may be stated as fact.
"""

from __future__ import annotations

from typing import Any

from . import db
from .artifacts import record_stage, sha256
from .config import StoreConfig
from .documents import build_document, faq_chunks
from .embeddings import Embedder

TRUST_CLASSES = ("quotable", "retrieval")


def run(store: StoreConfig, force: bool = False) -> dict[str, Any]:
    embedder = Embedder(store.embedding_model, store.embedding_dimensions)
    counts = {
        "products": 0,
        "documents": 0,
        "embedded": 0,
        "skipped_unchanged": 0,
        "quotable_chunks": 0,
        "retrieval_chunks": 0,
        "products_without_quotable": 0,
    }

    with db.connect() as conn:
        row = conn.execute("SELECT id FROM stores WHERE slug = %s", (store.slug,)).fetchone()
        if row is None:
            raise RuntimeError(f"store '{store.slug}' not in the database; run merge first")
        store_id = int(row["id"])

        products = conn.execute(
            "SELECT * FROM products WHERE store_id = %s ORDER BY handle", (store_id,)
        ).fetchall()

        pending: list[tuple[int, str, str, str, str]] = []

        for product in products:
            assertions = [
                dict(a)
                for a in conn.execute(
                    """
                    SELECT field, label, value, trust_class, rendered
                    FROM field_assertions
                    WHERE product_id = %s
                    ORDER BY field
                    """,
                    (product["id"],),
                ).fetchall()
            ]

            chunks: list[tuple[str, str, str]] = []
            for trust_class in TRUST_CLASSES:
                text = build_document(dict(product), assertions, trust_class)
                if text.strip():
                    chunks.append((trust_class, text, trust_class))
            chunks.extend(faq_chunks(assertions))

            if not any(c[2] == "quotable" for c in chunks):
                counts["products_without_quotable"] += 1

            for chunk_key, text, trust_class in chunks:
                if not text.strip():
                    continue
                text_hash = sha256(f"{trust_class}|{text}")
                counts[f"{trust_class}_chunks"] += 1
                if db.existing_document_hash(conn, product["id"], chunk_key) == text_hash and not force:
                    counts["skipped_unchanged"] += 1
                    continue
                pending.append((product["id"], chunk_key, trust_class, text, text_hash))

            counts["products"] += 1

        if pending:
            vectors = embedder.embed([p[3] for p in pending])
            for (product_id, chunk_key, trust_class, text, text_hash), vector in zip(
                pending, vectors
            ):
                db.upsert_document(
                    conn, product_id, chunk_key, trust_class, text, text_hash, vector
                )
                counts["documents"] += 1
                counts["embedded"] += 1
            conn.commit()

    counts["embedding_stats"] = embedder.stats.summary()
    counts["looked_normalised"] = embedder.stats.looked_normalised
    record_stage(store, "index", counts)
    return counts
