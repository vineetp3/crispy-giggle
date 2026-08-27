# `retrieval/` — answering a query

Algorithm-level specification: `docs/DESIGN.md` §5.6 and §5.7.

Two paths, because they are different questions. `search` ranks the catalogue; `answering`
takes a product you already have and returns what is quotable about it.

---

## `search` — hybrid vector + lexical, fused, filtered in SQL, then reranked

**Negation is a WHITELIST JOIN, not an exclusion scan, and must never be relaxed back.**
`free_from` (Shopify's `filter.contains`) is a positive declaration of what a product is free
of, so "tree nut free" means *require* a declaration naming each excluded term. The previous
exclusion scan removed only products whose prose mentioned the term, which let 30 of skout's 182
published products through with no declaration at all, and could not tell "contains peanuts"
from "does not process peanuts". **A product with no declaration is not an answer to a negation
query.**

Matching is substring (`ILIKE '%term%'`) on purpose: the shopper says `almond`, the data says
`Almonds`. A word-boundary match fails on the plural. The cost is that a short term like `nut`
also matches `Coconut`, so callers should pass specific allergen names.

The `%s IS NULL` casts in the retrieval legs are load-bearing. Postgres has nothing to infer the
parameter type from without them and the query fails with `IndeterminateDatatype`.

`trust_class` travels on the chunk so a caller can retrieve over everything and ground on
quotable text only. Ordering by it is not enough — a mixed string carries no marker.

Duplicate listings are collapsed into one family **between** the whitelist join and the commerce
filter: after negation, so a collapse can never resurrect an undeclared product; before `top_k`,
so the slice sees distinct products.

### Stage order, and why it is not the obvious one

`retrieve → SQL filter → live read → commerce filter → rerank`.

Commerce constraints are applied after the live read, not as SQL, because price and stock are
never stored and there is no column to filter on. This holds because the corpus is a few hundred
products per store; past roughly tens of thousands, a cached price band with an explicit TTL
becomes unavoidable.

### Degradation is visible, not silent

`_rerank` and `_attach_live` still swallow their exceptions, because degrading beats erroring on
a shopper query, but both record what failed on a `Diagnostics` the caller can print.

This matters most under a price filter: `_passes_commerce` rejects any hit with no live read, so
a dead credential turns `--max-price` into an empty result set that reads as "nothing matches"
rather than "the price lookup died".

The reranker hid behind exactly that pattern for every run up to 2026-08-25. `rerank` defaults
to True, so a hosted reranker whose credential was invalid was called on every search, failed
every time, and the fused order it degraded to was indistinguishable from a reranker that
changed nothing. That is the argument for a local model: `_rerank` now runs an ONNX
cross-encoder in-process, so there is no credential to be silently wrong and
`eval --compare-rerank` measures the stage rather than assuming it.

`prepare_rerank` downloads and caches the checkpoint up front. Without it the first shopper
query pays an unannounced download inside `_rerank`, where the bare `except` would turn a
download failure into a silent degrade. Callers that know a rerank is coming should call it
first. One `Ranker` per model is cached, because construction loads the ONNX checkpoint.

`RERANK_CACHE_DIR` is deliberately **not** under `DATA_ROOT`: it is a downloaded model
checkpoint, not store data, and tests monkeypatch `DATA_ROOT` into a tmpdir, which would
re-download it every run.

### Known ordering instability

`assertions_for` orders by `(trust_class = 'quotable') DESC, field` only, so assertions tying on
`field` come back in physical row order — which shifts whenever `merge` re-inserts them. The
`facts` attribute preview prints the first two, so for an attribute whose rows all carry the same
label it shows an arbitrary 2 of N.

Heavy imports (`flashrank`, and therefore onnxruntime and numpy) are function-local on purpose.
Do not hoist them.

---

## `answering` — the facts for one product, when the product is already known

**This is deliberately not `search`.** Most real questions arrive with the product already
decided — a shopper on a product page asks "is it BPA free", and the surface passes the handle.
The product identity is a parameter, not something to infer from the query text.

Half of remi's attribute questions literally contain the word "it", which is the tell: there is
no "it" in a catalogue-wide search, and scoring those questions by whether the right product
surfaces in five slots measures vocabulary distinctiveness rather than retrieval. "tank" narrows
remi to 7 of 48 products; "calories" narrows skout to 48 of 171.

**Ranking barely applies here.** remi averages 1.9 documents per product and never exceeds 2, so
ordering them is not retrieval. The substance of a scoped answer is `field_assertions`, not
document similarity — which is why nothing in this module embeds anything. A scoped answer costs
one round of SQL and no model call.

**Scope expands to the FAMILY, not the single product.** skout lists the same physical bar up to
four times with unevenly populated metafields, so scoping to one product id hides facts that live
on a sibling listing.

**A product's own name is not an answer about it.** `title`, `vendor`, `product_type` and
`handle` are quotable and must stay quotable, but they restate identity rather than assert a
property, so `stated` excludes them (`core.models.IDENTITY_FIELDS`). Without that the literal
check passes `how many tablets are in a pack` against the title `Deep Clean + Freshening
Tablets`, which says nothing about how many.

**Negation is the one place this must not diverge from `search`.** A scoped query cannot answer
with an empty list: "no rows" and "this product declares nothing" are different facts, and
reporting the second as the first is the same silent-empty failure the commerce filter had.
`free_from_outcomes` returns three states per term — free of it, not free of it, or **no
declaration at all** — and it is built on `declared_free_from`, the same function and the same
matching semantics discovery uses, so the two cannot drift.
