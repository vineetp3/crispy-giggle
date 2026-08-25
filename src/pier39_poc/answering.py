"""Product-scoped answering: the facts for one product, when the product is already known.

This is the other half of retrieval, and it is deliberately not `search`.

Most real questions arrive with the product already decided -- a shopper on a product page
asks "is it BPA free", and the surface passes the handle. The product identity is a
parameter, not something to infer from the query text. Half of remi's attribute questions
literally contain the word "it", which is the tell: there is no "it" in a catalogue-wide
search, and scoring those questions by whether the right product surfaces in five slots
measures vocabulary distinctiveness rather than retrieval. "tank" narrows remi to 7 of 48
products; "calories" narrows skout to 48 of 171.

Ranking barely applies here. remi averages 1.9 documents per product and never exceeds 2,
so ordering them is not retrieval. The substance of a scoped answer is `field_assertions`,
not document similarity, which is why nothing in this module embeds anything: a scoped
answer costs one round of SQL and no model call.

Scope expands to the FAMILY, not the single product. skout lists the same physical bar up
to four times with unevenly populated metafields, so scoping to one product id hides facts
that live on a sibling listing.

A product's own name is not an answer about it. `title`, `vendor`, `product_type` and `handle`
are quotable and must stay quotable, but they restate identity rather than assert a property, so
`stated` excludes them. Without that the literal check passes `how many tablets are in a pack`
against the title `Deep Clean + Freshening Tablets`, which says nothing about how many.

Negation is the one place this must not diverge from `search`. A scoped query cannot answer
with an empty list: "no rows" and "this product declares nothing" are different facts, and
reporting the second as the first is the same silent-empty failure the commerce filter had.
`free_from_outcomes` returns three states per term -- free of it, not free of it, or no
declaration at all -- and it is built on `declared_free_from`, the same function and the
same matching semantics discovery uses, so the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import db
from .attributes import ATTRIBUTES, _matches
from .config import StoreConfig
from .families import family_key
from .search import (
    Diagnostics,
    FreeFromOutcome,
    Hit,
    assertions_for,
    attach_live,
    free_from_outcomes,
)


@dataclass
class ProductAnswer:
    handle: str
    product_id: int
    title: str
    store_slug: str
    online_store_url: str | None
    family: list[str] = field(default_factory=list)
    quotable: list[dict[str, Any]] = field(default_factory=list)
    retrieval: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    free_from: list[FreeFromOutcome] = field(default_factory=list)
    live: dict[str, Any] | None = None

    @property
    def stated(self) -> list[dict[str, Any]]:
        return [a for a in self.quotable if a["field"] not in IDENTITY_FIELDS]

    def answers(self, attribute: str) -> list[dict[str, Any]]:
        needles = ATTRIBUTES.get(attribute)
        if not needles:
            return []
        return [
            a
            for a in self.stated
            if _matches(a["field"], needles) or _matches(a.get("label") or "", needles)
        ]

    def can_answer(self, attribute: str) -> bool:
        return bool(self.answers(attribute))


IDENTITY_FIELDS = frozenset({"title", "vendor", "product_type", "handle"})


class ProductNotFound(LookupError):
    pass


def resolve_family(conn, slug: str, handle: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.id, p.handle, p.title, p.vendor, p.online_store_url, s.slug
        FROM products p JOIN stores s ON s.id = p.store_id
        WHERE s.slug = %s
        """,
        (slug,),
    ).fetchall()
    target = next((r for r in rows if r["handle"] == handle), None)
    if target is None:
        raise ProductNotFound(f"{slug}: no product with handle {handle!r}")

    key = family_key(target["title"], target["vendor"])
    if not key:
        return [dict(target)]
    return [dict(r) for r in rows if family_key(r["title"], r["vendor"]) == key]


def answer_for_product(
    store: StoreConfig,
    handle: str,
    exclude_terms: list[str] | None = None,
    live: bool = True,
    diagnostics: Diagnostics | None = None,
) -> ProductAnswer:
    diag = diagnostics if diagnostics is not None else Diagnostics()

    with db.connect() as conn:
        members = resolve_family(conn, store.slug, handle)
        target = next(m for m in members if m["handle"] == handle)
        ids = [int(m["id"]) for m in members]

        rows = assertions_for(conn, ids)
        answer = ProductAnswer(
            handle=target["handle"],
            product_id=int(target["id"]),
            title=target["title"],
            store_slug=store.slug,
            online_store_url=target["online_store_url"],
            family=sorted(m["handle"] for m in members if m["handle"] != handle),
            quotable=[r for r in rows if r["trust_class"] == "quotable"],
            retrieval=[r for r in rows if r["trust_class"] != "quotable"],
        )

        answer.documents = [
            dict(r)
            for r in conn.execute(
                """
                SELECT product_id, chunk_key, trust_class, text
                FROM documents
                WHERE product_id = ANY(%s)
                ORDER BY (trust_class = 'quotable') DESC, chunk_key
                """,
                (ids,),
            ).fetchall()
        ]

        if exclude_terms:
            answer.free_from = free_from_outcomes(conn, ids, exclude_terms)

        if live:
            probe = Hit(
                product_id=answer.product_id,
                handle=answer.handle,
                title=answer.title,
                vendor=target["vendor"],
                store_slug=store.slug,
                online_store_url=answer.online_store_url,
                chunk_key="",
                trust_class="quotable",
                text="",
            )
            attach_live(conn, [probe], diag)
            answer.live = probe.live

    return answer
