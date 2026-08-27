# `infra/` — everything that talks to the outside

Configuration specification: `docs/DESIGN.md` §4; schema: §6.

---

## `config` — stores.yaml + defaults merge + secrets from env

Secrets never appear in `stores.yaml`. Tokens come from a single JSON blob keyed by store slug,
so adding a store is a config edit and a token edit, nothing more.

`StoreConfig` is a pydantic model, so the rules in `_check` run at construction rather than on
a separate validation call. They still raise `ConfigError`, not pydantic's `ValidationError`:
pydantic propagates non-`ValueError` exceptions from validators untouched, which keeps the
CLI's error path and every message identical.

`DATA_ROOT` stays a module-level global that the path properties dereference at **access**
time. Tests monkeypatch it, so resolving it eagerly at construction would silently redirect
writes into the real `data/` tree.

`REPO_ROOT` is `Path(__file__).parents[3]`, counted from `src/pier39_poc/infra/config.py`.
Moving this file changes that count and every config and data path with it.

`_merge_tuning` shallow-merges every key except `tuning`, whose groups merge one level deeper.
A store overriding one threshold must not drop the other groups, so `tuning.retrieval.rrf_k`
in a store entry leaves `tuning.blocks` from defaults intact.

`Secrets` deliberately has no `env_file`: `load_env()` is what puts `.env` into `os.environ`,
and reading the file here as well would resolve secrets the old `os.environ.get` path would
have missed. It is instantiated per call so a later `load_env()` is still seen.

---

## `artifacts` — artefact IO and the run manifest

Every stage reads the previous stage's files from disk and writes its own. No stage fetches on
behalf of another. That is what makes profiling re-runnable fifty times without re-crawling.

The manifest records the **resolved** config for each stage run. A coverage number without the
threshold and page count that produced it is worthless a week later.

---

## `shopify_api` — Admin GraphQL client

Two calls per store:

1. `metafieldDefinitions(ownerType: PRODUCT)` — **diagnostic only.** The definition list both
   misses undefined namespaces (skout has values under `global`, `stamped`, `okendo`,
   `SEOMetaManager`, `msft_bingads` with no definitions) and overstates coverage for
   defined-but-empty keys (remi defines the whole `agentiq` namespace and leaves it blank).
   Never treat it as authoritative.
2. Paginated `products` with metafields and variants nested inline, which avoids an N+1 and
   keeps the whole catalogue to one or two calls. skout's 182 products fit in a single page.

`onlineStoreUrl` is both the URL source and the publication filter: it is null when the product
is not published to the online store channel. No URL is ever built by string concatenation.

**Price and inventory are deliberately NOT selected** in the catalogue query and are never
stored. `sellability` reads them and returns one derived boolean per product — a verdict rather
than a commerce fact; no amount or quantity is retained.

That verdict is needed because `status:active` plus a non-null `onlineStoreUrl` does not mean
buyable. 17% of skout's published catalogue is abandoned records: `price` 0.00 with
`inventoryQuantity` at −770, −101, −14, several shadowing a live twin under a legacy handle.
They are 40% of a sampled candidate pool if left in.

**Do NOT use negative inventory as the signal.** remi has 23 of 30 products at negative
quantity and all 30 are buyable, because that store runs continue-selling. Only "no variant
priced above zero AND no variant available" holds across both stores.

`flatten_product` coalesces `handle`, `title`, `vendor`, `product_type` and `status` to `""`
when the API returns null, which is what makes `Product`'s non-optional string fields safe and
removes the scattered `or ""` guards downstream.

---

## `embeddings` — OpenAI embeddings behind one function

Two operational details that matter:

- Truncating via the `dimensions` parameter can break unit length, so we normalise ourselves.
  Whether OpenAI returns unit-normalised vectors is not stated in the API reference, so the
  first batch is measured and logged rather than assumed — the measurement decides whether the
  pgvector operator class could be inner product.
- Batch limits are 2048 inputs and 300,000 tokens per request. Tokens are counted with tiktoken
  rather than approximated from character length. Both pilot stores fit in a handful of calls.

---

## `llm` — one chat completion across disagreeing model families

Reasoning models reject `max_tokens` and require `max_completion_tokens`; older models reject
`max_completion_tokens` and `reasoning_effort`. That disagreement is litellm's problem now: it
knows which parameters each model family supports and drops the rest, so there is one request
rather than a blind three-shape fallback.

What the fallback could never do was say **which shape won**. `complete` returns a `Completion`
carrying both the text and the parameters actually sent, so a caller can record it —
`chat.Turn.to_log` does, which is what makes two chat-replay runs comparable.

`_resolved_params` picks the request shape up front instead of discovering it by failing. The
three shapes are the ones the old fallback tried, in the same order and with the same contents
— including the deliberate absence of `temperature` from the reasoning shape, which reasoning
models reject.

`client_or_default` is the injection seam: tests and callers pass a stub through it. litellm's
`completion` is a module-level function, so a `None` client means "call litellm directly"
rather than "build an OpenAI client".

An empty completion is still not an answer, and is still not an error either: it comes back as
empty text, which `labels` degrades to `UNCERTAIN` and `chat` scores as uncited.

This lives apart from `labels` and `chat` so the two cannot drift on request handling. No
ingest stage imports it.

---

## `db` — Postgres access, plain SQL, psycopg3, no ORM

`connect()` is parameterised explicitly rather than via `psycopg.connect(row_factory=...)`: the
plain form is typed as returning `Connection[TupleRow]`, so every `row["key"]` downstream reads
as a tuple slice. Same object at runtime, correct row type here.

`init_db` reads the repo's own `sql/schema.sql` at runtime. psycopg types `query` as
`LiteralString` to deter injection, which is why that one call carries a pyright suppression.

`report._quotable_age_table` catches bare `Exception` and returns `None`, so an unreachable
database silently drops the entire age table from the report rather than reporting the failure.
Every other degradation path in this repo announces itself; this one does not.
