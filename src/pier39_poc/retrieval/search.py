"""Hybrid retrieval: vector + lexical, fused, filtered in SQL, live-read, then reranked.

The discovery path. Returns data; presentation.render prints it.
Gotchas and their measurements: docs/reference/retrieval.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pier39_poc.core.families import collapse
from pier39_poc.core.matching import FREE_FROM_FIELD
from pier39_poc.core.tuning import DEFAULTS
from pier39_poc.infra import db
from pier39_poc.infra.config import (
    REPO_ROOT,
    StoreConfig,
    load_stores,
    storefront_token_for,
    token_for,
)
from pier39_poc.infra.embeddings import Embedder

RERANK_CACHE_DIR = REPO_ROOT / "data" / "models"


@dataclass
class Diagnostics:
    live_read_failed: bool = False
    live_read_error: str | None = None
    rerank_requested: bool = False
    rerank_failed: bool = False
    rerank_error: str | None = None

    @property
    def degraded(self) -> bool:
        return self.live_read_failed or self.rerank_failed


@dataclass
class Hit:
    product_id: int
    handle: str
    title: str
    vendor: str | None
    store_slug: str
    online_store_url: str | None
    chunk_key: str
    trust_class: str
    text: str
    vector_rank: int | None = None
    lexical_rank: int | None = None
    rrf: float = 0.0
    rerank_score: float | None = None
    matched_fields: list[dict[str, Any]] = field(default_factory=list)
    siblings: list[str] = field(default_factory=list)
    live: dict[str, Any] | None = None

    @property
    def groundable(self) -> bool:
        return self.trust_class == "quotable"


def _rrf(rank: int | None) -> float:
    return 0.0 if rank is None else 1.0 / (DEFAULTS.retrieval.rrf_k + rank)


_SELECT = """
    SELECT d.product_id, d.chunk_key, d.trust_class, d.text, p.handle, p.title,
           p.vendor, p.online_store_url, s.slug
    FROM documents d
    JOIN products p ON p.id = d.product_id
    JOIN stores s ON s.id = p.store_id
"""


def _vector_leg(conn, query_vector: list[float], slug: str | None, limit: int) -> list[dict]:
    return conn.execute(
        _SELECT
        + """
        WHERE d.embedding IS NOT NULL
          AND (%(slug)s::text IS NULL OR s.slug = %(slug)s::text)
        ORDER BY d.embedding <=> %(vec)s
        LIMIT %(limit)s
        """,
        {"slug": slug, "vec": np.asarray(query_vector, dtype=np.float32), "limit": limit},
    ).fetchall()


def _lexical_leg(conn, query: str, slug: str | None, limit: int) -> list[dict]:
    return conn.execute(
        _SELECT
        + """
        WHERE d.tsv @@ websearch_to_tsquery('english', %(q)s)
          AND (%(slug)s::text IS NULL OR s.slug = %(slug)s::text)
        ORDER BY ts_rank(d.tsv, websearch_to_tsquery('english', %(q)s)) DESC
        LIMIT %(limit)s
        """,
        {"q": query, "slug": slug, "limit": limit},
    ).fetchall()


def declared_free_from(conn, product_ids: list[int], exclude: list[str]) -> set[int]:
    terms = sorted({t.strip().lower() for t in exclude if t.strip()})
    if not product_ids or not terms:
        return set(product_ids)
    rows = conn.execute(
        """
        SELECT fa.product_id, count(DISTINCT t.term) AS declared
        FROM field_assertions fa
        CROSS JOIN unnest(%(terms)s::text[]) AS t(term)
        WHERE fa.product_id = ANY(%(ids)s)
          AND fa.field = %(field)s
          AND fa.value ILIKE '%%' || t.term || '%%'
        GROUP BY fa.product_id
        """,
        {"terms": terms, "ids": product_ids, "field": FREE_FROM_FIELD},
    ).fetchall()
    return {int(r["product_id"]) for r in rows if int(r["declared"]) == len(terms)}


def _hit_from_row(row: dict[str, Any], **ranks: int) -> Hit:
    return Hit(
        product_id=row["product_id"],
        handle=row["handle"],
        title=row["title"],
        vendor=row["vendor"],
        store_slug=row["slug"],
        online_store_url=row["online_store_url"],
        chunk_key=row["chunk_key"],
        trust_class=row["trust_class"],
        text=row["text"],
        **ranks,  # pyright: ignore[reportArgumentType]
    )


def search(
    query: str,
    store: StoreConfig,
    slug: str | None = None,
    top_k: int = 5,
    exclude_terms: list[str] | None = None,
    rerank: bool = True,
    live_prices: bool = True,
    max_price: float | None = None,
    in_stock_only: bool = False,
    group_families: bool = True,
    diagnostics: Diagnostics | None = None,
) -> list[Hit]:
    diag = diagnostics if diagnostics is not None else Diagnostics()
    embedder = Embedder(store.embedding_model, store.embedding_dimensions)
    query_vector = embedder.embed_one(query)

    hits: dict[tuple[int, str], Hit] = {}

    with db.connect() as conn:
        for rank, row in enumerate(_vector_leg(conn, query_vector, slug, DEFAULTS.retrieval.first_stage_limit), 1):
            hits[(row["product_id"], row["chunk_key"])] = _hit_from_row(row, vector_rank=rank)

        for rank, row in enumerate(_lexical_leg(conn, query, slug, DEFAULTS.retrieval.first_stage_limit), 1):
            key = (row["product_id"], row["chunk_key"])
            if key in hits:
                hits[key].lexical_rank = rank
            else:
                hits[key] = _hit_from_row(row, lexical_rank=rank)

        for hit in hits.values():
            hit.rrf = _rrf(hit.vector_rank) + _rrf(hit.lexical_rank)

        ranked = sorted(hits.values(), key=lambda h: h.rrf, reverse=True)

        if exclude_terms:
            allowed = declared_free_from(
                conn, [h.product_id for h in ranked], exclude_terms
            )
            ranked = [h for h in ranked if h.product_id in allowed]

        if group_families and ranked:
            ranked = collapse(ranked, _quotable_counts(conn, ranked))

        filtering = max_price is not None or in_stock_only
        if filtering and ranked:
            ranked = ranked[:DEFAULTS.retrieval.live_read_limit]
            attach_live(conn, ranked, diag)
            ranked = [h for h in ranked if _passes_commerce(h, max_price, in_stock_only)]

        if rerank and ranked:
            diag.rerank_requested = True
            ranked = _rerank(query, ranked, store.rerank_model, diag)

        ranked = ranked[:top_k]

        if live_prices and ranked and not filtering:
            attach_live(conn, ranked, diag)

        for hit in ranked:
            hit.matched_fields = assertions_for(conn, [hit.product_id], limit=12)

    return ranked


@dataclass
class FreeFromOutcome:
    term: str
    has_declaration: bool
    declared_free: bool

    @property
    def answerable(self) -> bool:
        return self.has_declaration


def assertions_for(
    conn, product_ids: list[int], limit: int | None = None
) -> list[dict[str, Any]]:
    if not product_ids:
        return []
    rows = conn.execute(
        """
        SELECT id, field, label, value, source, source_kind, trust_class,
               rendered, source_updated_at
        FROM field_assertions
        WHERE product_id = ANY(%s)
        ORDER BY (trust_class = 'quotable') DESC, field
        """,
        (sorted(set(product_ids)),),
    ).fetchall()

    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row["field"], row["value"])
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
        if limit is not None and len(out) >= limit:
            break
    return out


def free_from_outcomes(
    conn, product_ids: list[int], terms: list[str]
) -> list[FreeFromOutcome]:
    wanted = [t.strip().lower() for t in terms if t.strip()]
    if not product_ids or not wanted:
        return []

    has_any = bool(
        conn.execute(
            """
            SELECT 1 FROM field_assertions
            WHERE product_id = ANY(%s) AND field = %s
            LIMIT 1
            """,
            (sorted(set(product_ids)), FREE_FROM_FIELD),
        ).fetchone()
    )

    out: list[FreeFromOutcome] = []
    for term in wanted:
        free = bool(declared_free_from(conn, product_ids, [term])) if has_any else False
        out.append(FreeFromOutcome(term=term, has_declaration=has_any, declared_free=free))
    return out


def _brief(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    if len(text) > 160:
        text = text[:157] + "..."
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _quotable_counts(conn, hits: list[Hit]) -> dict[int, int]:
    ids = sorted({h.product_id for h in hits})
    if not ids:
        return {}
    rows = conn.execute(
        """
        SELECT product_id, count(*) AS n
        FROM field_assertions
        WHERE product_id = ANY(%s) AND trust_class = 'quotable'
        GROUP BY product_id
        """,
        (ids,),
    ).fetchall()
    return {int(r["product_id"]): int(r["n"]) for r in rows}


def _passes_commerce(hit: Hit, max_price: float | None, in_stock_only: bool) -> bool:
    live = hit.live
    if not live:
        return False
    if in_stock_only and not live.get("available"):
        return False
    if max_price is not None:
        low = live.get("min_price")
        if low is None or float(low) > max_price:
            return False
    return True


DEFAULT_FLASHRANK_MODEL = "ms-marco-MiniLM-L-12-v2"
_FLASHRANK_CACHE: dict[str, Any] = {}


def _flashrank_ranker(name: str):
    if name not in _FLASHRANK_CACHE:
        from flashrank import Ranker

        _FLASHRANK_CACHE[name] = Ranker(
            model_name=name or DEFAULT_FLASHRANK_MODEL,
            cache_dir=str(RERANK_CACHE_DIR),
            log_level="ERROR",
        )
    return _FLASHRANK_CACHE[name]


def prepare_rerank(model: str) -> None:
    _flashrank_ranker(model)


def _rerank_flashrank(query: str, hits: list[Hit], name: str) -> list[Hit]:
    from flashrank import RerankRequest

    ranker = _flashrank_ranker(name)
    passages = [
        {"id": i, "text": h.text[:DEFAULTS.retrieval.rerank_doc_chars]} for i, h in enumerate(hits)
    ]
    results = ranker.rerank(RerankRequest(query=query, passages=passages))
    ordered: list[Hit] = []
    for result in results:
        hit = hits[int(result["id"])]
        hit.rerank_score = float(result["score"])
        ordered.append(hit)
    return ordered


def _rerank(
    query: str, hits: list[Hit], model: str, diag: Diagnostics | None = None
) -> list[Hit]:
    try:
        return _rerank_flashrank(query, hits, model)
    except Exception as exc:
        if diag is not None:
            diag.rerank_failed = True
            diag.rerank_error = _brief(exc)
        return hits


def attach_live(conn, hits: list[Hit], diag: Diagnostics | None = None) -> None:
    from pier39_poc.infra.shopify_api import AdminClient, StorefrontClient

    configs = {s.slug: s for s in load_stores()}

    by_slug: dict[str, list[Hit]] = {}
    for hit in hits:
        by_slug.setdefault(hit.store_slug, []).append(hit)

    for slug, slug_hits in by_slug.items():
        target = configs.get(slug)
        if target is None:
            continue
        rows = conn.execute(
            """
            SELECT v.product_id, v.shopify_variant_id
            FROM variants v
            WHERE v.product_id = ANY(%s)
            """,
            ([h.product_id for h in slug_hits],),
        ).fetchall()
        if not rows:
            continue

        storefront = storefront_token_for(slug)
        gids = [r["shopify_variant_id"] for r in rows]
        live: dict[str, dict[str, Any]] = {}
        try:
            if storefront:
                with StorefrontClient(target, storefront) as client:
                    for start in range(0, len(gids), 50):
                        live.update(client.fetch_live_variants(gids[start : start + 50]))
            else:
                with AdminClient(target, token_for(slug)) as client:
                    for start in range(0, len(gids), 50):
                        live.update(client.fetch_live_variants(gids[start : start + 50]))
        except Exception as exc:
            if diag is not None:
                diag.live_read_failed = True
                diag.live_read_error = _brief(exc)
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
            prices = [
                float(v["price"])
                for v in variants
                if v.get("price") is not None and float(v["price"]) > 0
            ]
            hit.live = {
                "variants": len(variants),
                "available": len([v for v in variants if v.get("availableForSale")]),
                "min_price": min(prices) if prices else None,
                "max_price": max(prices) if prices else None,
            }
