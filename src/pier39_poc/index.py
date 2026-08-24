"""Build retrieval documents, embed them, and load them into Postgres.

The content-hash gate is what makes re-running cheap: an unchanged document skips both
the write and the embedding call.
"""

from __future__ import annotations

from typing import Any

from . import db
from .artifacts import record_stage, sha256
from .config import StoreConfig
from .documents import build_document, faq_chunks
from .embeddings import Embedder


def run(store: StoreConfig, force: bool = False) -> dict[str, Any]:
    embedder = Embedder(store.embedding_model, store.embedding_dimensions)
    counts = {"products": 0, "documents": 0, "embedded": 0, "skipped_unchanged": 0}

    with db.connect() as conn:
        row = conn.execute("SELECT id FROM stores WHERE slug = %s", (store.slug,)).fetchone()
        if row is None:
            raise RuntimeError(f"store '{store.slug}' not in the database; run merge first")
        store_id = int(row["id"])

        products = conn.execute(
            "SELECT * FROM products WHERE store_id = %s ORDER BY handle", (store_id,)
        ).fetchall()

        pending: list[tuple[int, str, str, str]] = []  # product_id, chunk, text, hash

        for product in products:
            assertions = conn.execute(
                """
                SELECT field, label, value, trust_class, rendered
                FROM field_assertions
                WHERE product_id = %s
                ORDER BY field
                """,
                (product["id"],),
            ).fetchall()

            chunks: list[tuple[str, str]] = [
                ("main", build_document(dict(product), [dict(a) for a in assertions]))
            ]
            chunks.extend(faq_chunks([dict(a) for a in assertions]))

            for chunk_key, text in chunks:
                if not text.strip():
                    continue
                text_hash = sha256(text)
                existing = db.existing_document_hash(conn, product["id"], chunk_key)
                if existing == text_hash and not force:
                    counts["skipped_unchanged"] += 1
                    continue
                pending.append((product["id"], chunk_key, text, text_hash))

            counts["products"] += 1

        if pending:
            vectors = embedder.embed([p[2] for p in pending])
            for (product_id, chunk_key, text, text_hash), vector in zip(pending, vectors):
                db.upsert_document(conn, product_id, chunk_key, text, text_hash, vector)
                counts["documents"] += 1
                counts["embedded"] += 1
            conn.commit()

    counts["embedding_stats"] = embedder.stats.summary()
    counts["looked_normalised"] = embedder.stats.looked_normalised
    record_stage(store, "index", counts)
    return counts
