"""Postgres access. Plain SQL, psycopg3, no ORM."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Json

from .config import REPO_ROOT, database_url

SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"


@contextmanager
def connect() -> Iterator[psycopg.Connection[DictRow]]:
    # Parameterised explicitly rather than via `psycopg.connect(row_factory=...)`:
    # the plain form is typed as returning Connection[TupleRow], so every `row["key"]`
    # downstream reads as a tuple slice. Same object at runtime, correct row type here.
    with psycopg.Connection[DictRow].connect(
        database_url(), row_factory=dict_row
    ) as conn:
        register_vector(conn)
        yield conn


def init_db() -> None:
    sql = SCHEMA_PATH.read_text()
    with connect() as conn:
        # psycopg types `query` as LiteralString to deter injection; this is the
        # repo's own schema.sql read at runtime, so it is a plain str by nature.
        conn.execute(sql)  # pyright: ignore[reportCallIssue, reportArgumentType]
        conn.commit()


def upsert_store(conn: psycopg.Connection[DictRow], slug: str, domain: str, api_version: str) -> int:
    row = conn.execute(
        """
        INSERT INTO stores (slug, domain, admin_api_version)
        VALUES (%s, %s, %s)
        ON CONFLICT (slug) DO UPDATE
          SET domain = EXCLUDED.domain,
              admin_api_version = EXCLUDED.admin_api_version,
              last_ingested_at = now()
        RETURNING id
        """,
        (slug, domain, api_version),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def set_coverage(conn: psycopg.Connection[DictRow], store_id: int, coverage_pct: float | None) -> None:
    conn.execute(
        "UPDATE stores SET coverage_pct = %s WHERE id = %s", (coverage_pct, store_id)
    )


def upsert_product(conn: psycopg.Connection[DictRow], store_id: int, product: dict[str, Any]) -> int:
    row = conn.execute(
        """
        INSERT INTO products (
            store_id, shopify_product_id, handle, title, vendor, product_type,
            status, tags, online_store_url, template_suffix, collection_handles, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (store_id, shopify_product_id) DO UPDATE SET
            handle = EXCLUDED.handle,
            title = EXCLUDED.title,
            vendor = EXCLUDED.vendor,
            product_type = EXCLUDED.product_type,
            status = EXCLUDED.status,
            tags = EXCLUDED.tags,
            online_store_url = EXCLUDED.online_store_url,
            template_suffix = EXCLUDED.template_suffix,
            collection_handles = EXCLUDED.collection_handles,
            updated_at = EXCLUDED.updated_at
        RETURNING id
        """,
        (
            store_id,
            product["product_id"],
            product["handle"],
            product.get("title"),
            product.get("vendor"),
            product.get("product_type"),
            product.get("status"),
            product.get("tags") or [],
            product.get("online_store_url"),
            product.get("template_suffix"),
            [c.get("handle") for c in product.get("collections") or [] if c.get("handle")],
            product.get("updated_at"),
        ),
    ).fetchone()
    assert row is not None
    return int(row["id"])


_UPSERT_VARIANT = """
    INSERT INTO variants (product_id, shopify_variant_id, title, sku, selected_options)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (product_id, shopify_variant_id) DO UPDATE SET
        title = EXCLUDED.title,
        sku = EXCLUDED.sku,
        selected_options = EXCLUDED.selected_options
"""


def upsert_variants(conn: psycopg.Connection[DictRow], product_id: int, variants: list[dict[str, Any]]) -> None:
    if not variants:
        return
    params = []
    for v in variants:
        options = {o["name"]: o["value"] for o in (v.get("selectedOptions") or [])}
        params.append(
            (product_id, v["id"], v.get("title"), v.get("sku"), Json(options))
        )
    conn.cursor().executemany(_UPSERT_VARIANT, params)


_UPSERT_ASSERTION = """
        INSERT INTO field_assertions (
            product_id, field, label, value, source, source_kind, rendered,
            trust_class, source_updated_at, value_hash
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (product_id, field, source) DO UPDATE SET
            label = EXCLUDED.label,
            value = EXCLUDED.value,
            rendered = EXCLUDED.rendered,
            trust_class = EXCLUDED.trust_class,
            source_updated_at = EXCLUDED.source_updated_at,
            value_hash = EXCLUDED.value_hash,
            observed_at = now()
"""


def _assertion_params(product_id: int, a: dict[str, Any]) -> tuple[Any, ...]:
    return (
        product_id,
        a["field"],
        a.get("label"),
        a["value"],
        a["source"],
        a["source_kind"],
        a.get("rendered", False),
        a["trust_class"],
        a.get("source_updated_at"),
        a["value_hash"],
    )


def upsert_assertion(conn: psycopg.Connection[DictRow], product_id: int, a: dict[str, Any]) -> None:
    conn.execute(_UPSERT_ASSERTION, _assertion_params(product_id, a))


def delete_products(
    conn: psycopg.Connection[DictRow], store_id: int, shopify_product_ids: list[str]
) -> int:
    if not shopify_product_ids:
        return 0
    row = conn.execute(
        """
        WITH gone AS (
            DELETE FROM products
            WHERE store_id = %s AND shopify_product_id = ANY(%s)
            RETURNING 1
        )
        SELECT count(*) AS n FROM gone
        """,
        (store_id, shopify_product_ids),
    ).fetchone()
    return int(row["n"]) if row else 0


def replace_assertions(
    conn: psycopg.Connection[DictRow], product_id: int, rows: list[dict[str, Any]]
) -> None:
    if rows:
        conn.cursor().executemany(
            _UPSERT_ASSERTION, [_assertion_params(product_id, r) for r in rows]
        )
    if not rows:
        conn.execute("DELETE FROM field_assertions WHERE product_id = %s", (product_id,))
        return
    conn.execute(
        """
        DELETE FROM field_assertions fa
        WHERE fa.product_id = %(pid)s
          AND NOT EXISTS (
              SELECT 1
              FROM unnest(%(fields)s::text[], %(sources)s::text[]) AS k(field, source)
              WHERE k.field = fa.field AND k.source = fa.source
          )
        """,
        {
            "pid": product_id,
            "fields": [r["field"] for r in rows],
            "sources": [r["source"] for r in rows],
        },
    )


def replace_edges(conn: psycopg.Connection[DictRow], store_id: int, from_id: str, rows: list[dict[str, Any]]) -> None:
    conn.execute(
        "DELETE FROM edges WHERE store_id = %s AND from_type = 'product' AND from_id = %s",
        (store_id, from_id),
    )
    if not rows:
        return
    conn.cursor().executemany(
        """
        INSERT INTO edges (store_id, from_type, from_id, relation, to_type, to_id, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        [
            (store_id, r["from_type"], r["from_id"], r["relation"], r["to_type"], r["to_id"], r["source"])
            for r in rows
        ],
    )


def replace_rejected_keys(conn: psycopg.Connection[DictRow], store_id: int, rows: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM rejected_keys WHERE store_id = %s", (store_id,))
    if not rows:
        return
    conn.cursor().executemany(
        """
        INSERT INTO rejected_keys (store_id, namespace, key, reason_code, detail)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (store_id, namespace, key) DO UPDATE
          SET reason_code = EXCLUDED.reason_code, detail = EXCLUDED.detail
        """,
        [
            (store_id, r["namespace"], r["key"], r["reason_code"], (r.get("detail") or "")[:500])
            for r in rows
        ],
    )


def replace_template_constants(
    conn: psycopg.Connection[DictRow], store_id: int, rows: list[dict[str, Any]]
) -> None:
    conn.execute("DELETE FROM template_constants WHERE store_id = %s", (store_id,))
    if not rows:
        return
    conn.cursor().executemany(
        """
        INSERT INTO template_constants
            (store_id, template_key, handle, value, label, value_hash)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (store_id, template_key, handle, value_hash) DO NOTHING
        """,
        [
            (
                store_id,
                r["template_key"],
                r.get("handle") or "",
                r["value"],
                r.get("label"),
                r["value_hash"],
            )
            for r in rows
        ],
    )


def existing_document_hash(conn: psycopg.Connection[DictRow], product_id: int, chunk_key: str) -> str | None:
    row = conn.execute(
        "SELECT text_hash FROM documents WHERE product_id = %s AND chunk_key = %s",
        (product_id, chunk_key),
    ).fetchone()
    return row["text_hash"] if row else None


def upsert_document(
    conn: psycopg.Connection[DictRow],
    product_id: int,
    chunk_key: str,
    trust_class: str,
    text: str,
    text_hash: str,
    embedding: list[float] | None,
) -> None:
    conn.execute(
        """
        INSERT INTO documents (
            product_id, chunk_key, trust_class, text, text_hash, embedding
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (product_id, chunk_key) DO UPDATE SET
            trust_class = EXCLUDED.trust_class,
            text = EXCLUDED.text,
            text_hash = EXCLUDED.text_hash,
            embedding = EXCLUDED.embedding
        """,
        (product_id, chunk_key, trust_class, text, text_hash, embedding),
    )


def products_for_store(conn: psycopg.Connection[DictRow], slug: str) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT p.* FROM products p
        JOIN stores s ON s.id = p.store_id
        WHERE s.slug = %s
        ORDER BY p.handle
        """,
        (slug,),
    ).fetchall()
