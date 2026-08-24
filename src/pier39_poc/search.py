"""Hybrid retrieval: vector + lexical, fused, filtered in SQL, then reranked.

Four stages, in this order for a reason:

1. embed the query
2. two first-stage retrievers over the same table, fused with reciprocal rank fusion.
   The lexical leg catches exact terms -- a brand, a SKU, "poppyseed" -- that
   embeddings miss.
3. constraints as SQL WHERE clauses. Negation is handled HERE. "cookies without
   peanuts" must not be left to cosine similarity, which routinely retrieves the thing
   you excluded.
4. rerank with a cross-encoder, then one live Admin call for price and stock on the
   survivors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import db
from .config import StoreConfig, token_for
from .embeddings import Embedder

RRF_K = 60
FIRST_STAGE_LIMIT = 50


@dataclass
class Hit:
    product_id: int
    handle: str
    title: str
    store_slug: str
    online_store_url: str | None
    chunk_key: str
    text: str
    vector_rank: int | None = None
    lexical_rank: int | None = None
    rrf: float = 0.0
    rerank_score: float | None = None
    matched_fields: list[dict[str, Any]] = field(default_factory=list)
    live: dict[str, Any] | None = None


def _rrf(rank: int | None) -> float:
    return 0.0 if rank is None else 1.0 / (RRF_K + rank)


def vector_literal(vector: list[float]) -> str:
    """pgvector's text input form.

    Passed as a string with an explicit `::vector` cast rather than relying on a list
    adapter, so this works without registering the pgvector type on the connection.
    """
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


# `%s IS NULL` gives Postgres nothing to infer the parameter type from, so the casts
# below are load-bearing: without them the query fails with IndeterminateDatatype.
def _vector_leg(conn, query_vector: list[float], slug: str | None, limit: int) -> list[dict]:
    return conn.execute(
        """
        SELECT d.product_id, d.chunk_key, d.text, p.handle, p.title,
               p.online_store_url, s.slug
        FROM documents d
        JOIN products p ON p.id = d.product_id
        JOIN stores s ON s.id = p.store_id
        WHERE d.embedding IS NOT NULL
          AND (%(slug)s::text IS NULL OR s.slug = %(slug)s::text)
        ORDER BY d.embedding <=> %(vec)s::vector
        LIMIT %(limit)s
        """,
        {"slug": slug, "vec": vector_literal(query_vector), "limit": limit},
    ).fetchall()


def _lexical_leg(conn, query: str, slug: str | None, limit: int) -> list[dict]:
    return conn.execute(
        """
        SELECT d.product_id, d.chunk_key, d.text, p.handle, p.title,
               p.online_store_url, s.slug
        FROM documents d
        JOIN products p ON p.id = d.product_id
        JOIN stores s ON s.id = p.store_id
        WHERE d.tsv @@ websearch_to_tsquery('english', %(q)s)
          AND (%(slug)s::text IS NULL OR s.slug = %(slug)s::text)
        ORDER BY ts_rank(d.tsv, websearch_to_tsquery('english', %(q)s)) DESC
        LIMIT %(limit)s
        """,
        {"q": query, "slug": slug, "limit": limit},
    ).fetchall()


def _exclude_by_allergen(conn, product_ids: list[int], exclude: list[str]) -> set[int]:
    """Products whose own content asserts a term the shopper excluded.

    This is the negation path. `filter.contains` on skout lists allergens the product
    does NOT contain, so it must not be read as a positive assertion -- only fields
    naming the term as an ingredient count.
    """
    if not product_ids or not exclude:
        return set()
    rows = conn.execute(
        """
        SELECT DISTINCT product_id
        FROM field_assertions
        WHERE product_id = ANY(%s)
          AND field NOT LIKE '%%contains%%'
          AND field NOT LIKE '%%free%%'
          AND value ILIKE ANY(%s)
        """,
        (product_ids, [f"%{term}%" for term in exclude]),
    ).fetchall()
    return {int(r["product_id"]) for r in rows}


def search(
    query: str,
    store: StoreConfig,
    slug: str | None = None,
    top_k: int = 5,
    exclude_terms: list[str] | None = None,
    rerank: bool = True,
    live_prices: bool = True,
) -> list[Hit]:
    embedder = Embedder(store.embedding_model, store.embedding_dimensions)
    query_vector = embedder.embed_one(query)

    hits: dict[tuple[int, str], Hit] = {}

    with db.connect() as conn:
        for rank, row in enumerate(_vector_leg(conn, query_vector, slug, FIRST_STAGE_LIMIT), 1):
            key = (row["product_id"], row["chunk_key"])
            hits[key] = Hit(
                product_id=row["product_id"],
                handle=row["handle"],
                title=row["title"],
                store_slug=row["slug"],
                online_store_url=row["online_store_url"],
                chunk_key=row["chunk_key"],
                text=row["text"],
                vector_rank=rank,
            )

        for rank, row in enumerate(_lexical_leg(conn, query, slug, FIRST_STAGE_LIMIT), 1):
            key = (row["product_id"], row["chunk_key"])
            if key in hits:
                hits[key].lexical_rank = rank
            else:
                hits[key] = Hit(
                    product_id=row["product_id"],
                    handle=row["handle"],
                    title=row["title"],
                    store_slug=row["slug"],
                    online_store_url=row["online_store_url"],
                    chunk_key=row["chunk_key"],
                    text=row["text"],
                    lexical_rank=rank,
                )

        for hit in hits.values():
            hit.rrf = _rrf(hit.vector_rank) + _rrf(hit.lexical_rank)

        ranked = sorted(hits.values(), key=lambda h: h.rrf, reverse=True)

        if exclude_terms:
            banned = _exclude_by_allergen(
                conn, [h.product_id for h in ranked], exclude_terms
            )
            ranked = [h for h in ranked if h.product_id not in banned]

        if rerank and ranked:
            ranked = _rerank(query, ranked, store.rerank_model)

        ranked = ranked[:top_k]

        for hit in ranked:
            rows = conn.execute(
                """
                SELECT field, label, value, source, source_kind, trust_class, rendered
                FROM field_assertions
                WHERE product_id = %s
                ORDER BY (trust_class = 'quotable') DESC, field
                LIMIT 12
                """,
                (hit.product_id,),
            ).fetchall()
            hit.matched_fields = [dict(r) for r in rows]

        if live_prices and ranked:
            _attach_live(conn, ranked, store)

    return ranked


def _rerank(query: str, hits: list[Hit], model: str) -> list[Hit]:
    """Cross-encoder rerank. Degrades to the fused order if the call fails."""
    try:
        import cohere

        client = cohere.ClientV2()
        response = client.rerank(
            model=model,
            query=query,
            documents=[h.text[:4000] for h in hits],
            top_n=len(hits),
        )
        ordered: list[Hit] = []
        for result in response.results:
            hit = hits[result.index]
            hit.rerank_score = float(result.relevance_score)
            ordered.append(hit)
        return ordered
    except Exception:
        return hits


def _attach_live(conn, hits: list[Hit], store: StoreConfig) -> None:
    """The only place price, inventory and availability are read. Never stored."""
    from .shopify_api import AdminClient

    by_slug: dict[str, list[Hit]] = {}
    for hit in hits:
        by_slug.setdefault(hit.store_slug, []).append(hit)

    for slug, slug_hits in by_slug.items():
        product_ids = [h.product_id for h in slug_hits]
        rows = conn.execute(
            """
            SELECT v.product_id, v.shopify_variant_id
            FROM variants v
            WHERE v.product_id = ANY(%s)
            """,
            (product_ids,),
        ).fetchall()
        if not rows:
            continue

        gids = [r["shopify_variant_id"] for r in rows]
        try:
            token = token_for(slug)
        except Exception:
            continue

        target = store if store.slug == slug else store
        try:
            with AdminClient(target, token) as client:
                live = client.fetch_live_variants(gids[:50])
        except Exception:
            continue

        by_product: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            variant = live.get(row["shopify_variant_id"])
            if variant:
                by_product.setdefault(int(row["product_id"]), []).append(variant)

        for hit in slug_hits:
            variants = by_product.get(hit.product_id) or []
            if not variants:
                continue
            available = [v for v in variants if v.get("availableForSale")]
            prices = [float(v["price"]) for v in variants if v.get("price") is not None]
            hit.live = {
                "variants": len(variants),
                "available": len(available),
                "min_price": min(prices) if prices else None,
                "max_price": max(prices) if prices else None,
            }
