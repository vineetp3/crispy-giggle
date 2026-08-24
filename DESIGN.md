# Discovery Ingestion POC — Build Specification

**Status:** approved for build. Implementation spec, not a discussion document.
**Created:** 2026-08-24
**Location:** `/Users/vineetsawhney/Desktop/code/pier39-discovery-poc` — local only, no remote.
**Background and evidence:** `personapay-backend-publisher/docs/product-discovery-ingestion.md`

---

## 1. Purpose

Prove that a single Shopify admin token is sufficient to build a queryable product index, and
measure how much of each publisher's product content is reachable.

This POC exists to produce two artefacts:

1. **A per-store profile report** — which metafield keys carry live product content, what their
   human-readable labels are, which are rejected and why, what content exists only in the theme,
   and what percentage of visible product content the API covers.
2. **A search CLI with provenance** — so a bad answer can be attributed to bad content, bad
   retrieval, or bad filtering.

The report is the deliverable. The search CLI is how the report gets validated.

## 2. Success criteria

The POC succeeds when all of the following hold.

| # | Criterion |
|---|---|
| 1 | `fetch-api` retrieves every active product with its metafields and variants for both stores using only the admin token |
| 2 | `fetch-html` succeeds on remi, escalating the fetch profile automatically when a Cloudflare interstitial is detected |
| 3 | `profile` admits `custom.nutrients` on skout despite the theme reformatting `Protein [1g]` to `Protein 1g` |
| 4 | `profile` rejects `stamped.reviews`, `loox.review_feed` and `product_seo.seo_tags` with reason code `foreign_product_id` |
| 5 | `profile` recovers human-readable labels for at least three otherwise-opaque metafield keys |
| 6 | `profile` captures remi's `Material`, `Battery life`, `Power` and `Tank capacity` as template constants |
| 7 | `report` emits a coverage percentage per store |
| 8 | `eval` reports recall@5 of at least 0.70 across 10 hand-written questions per store |

Criterion 8's bar is an arbitrary starting point. Its value is that it is measured, not that it
is 0.70.

## 3. Decisions register

These are settled. Do not relitigate them during implementation.

| Decision | Value | Note |
|---|---|---|
| Repo location | `/Users/vineetsawhney/Desktop/code/pier39-discovery-poc` | Local only. No git remote. Do not `git init` until asked |
| Relationship to main repo | None | `personapay-backend-publisher` is not modified |
| Python | 3.11 | Matches the existing runtime |
| Dependency management | `uv` + `pyproject.toml` | |
| Database | Postgres 16 + pgvector, `pgvector/pgvector:pg16` via `docker compose` | Only container in the project |
| DB access | psycopg3, plain SQL, single idempotent `schema.sql` | No ORM, no Alembic |
| CLI | Typer | One subcommand per pipeline stage |
| Crawler | Crawl4AI (Apache 2.0) | |
| Boilerplate removal | Cross-page block frequency differencing | Not Trafilatura. Article extractors discard `label: value` spec blocks |
| Embeddings | OpenAI `text-embedding-3-large`, `dimensions=1024` | Sticky: the pgvector column is `vector(1024)`; changing it means a migration plus full re-embed |
| Reranking | Cohere Rerank 4.0, **in v0** | Promoted from optional because OpenAI has no `input_type`, so the cross-encoder carries the asymmetry |
| LLM extraction | Not in v0 | remi's theme block is entered by hand |
| Edges | Table included and populated in v0 | Not traversed by search yet |
| Eval harness | In v0 | |
| Token transport | Single JSON blob environment variable | |
| Pilot stores | `skout`, `remi` | countrylifefoods excluded — it is the easy case and proves nothing new |

### Rejected alternatives, for the record

- **Trafilatura as primary extractor.** Tuned for articles. Would discard remi's two-tab
  specification widget and skout's nutrition grid, which are the only reasons to fetch pages.
- **Firecrawl.** Its value is the hosted service; the corpus is a few hundred pages. AGPL-3.0
  core requires legal review before a self-hosted component is called from our backend.
- **`bulkOperationRunQuery`.** Adds async polling for no benefit at this scale. Plain pagination
  retrieves skout's 182 products in one page.
- **Faking asymmetric encoding** with `"query: "` / `"passage: "` prefixes. Those work on
  E5-family models because they were trained that way. On OpenAI models it is cargo cult.
- **Neo4j or Apache AGE.** AGE is absent from Cloud SQL's fixed extension allowlist. A second
  datastore is unjustified at this scale; an `edges` table with recursive CTEs is sufficient.

---

## 4. Configuration

Two sources. Non-secret settings are committable; secrets are not.

### 4.1 `config/stores.yaml`

A `defaults` block and a `stores` list. Any default is overridable per store. Per-store overrides
are used only where the stores demonstrably differ: `fetch_profile`, the page budget, and
`chrome_threshold`.

**Global defaults:**

| Key | Default | Meaning |
|---|---|---|
| `admin_api_version` | `2026-01` | Recorded in the run manifest |
| `profile_pages` | `20` | Pages used to derive the boilerplate profile and metafield allowlist. This number drives differencing quality |
| `crawl_scope` | `sample` | `none` / `sample` / `template_representatives` / `all` |
| `max_pages` | `250` | Hard ceiling, independent of `crawl_scope` |
| `sampling` | `by_template` | `by_template` / `by_product_type` / `random` / `first_n` / `explicit` |
| `sampling_seed` | `1739` | Fixed, so `random` is reproducible |
| `fetch_profile` | `plain` | `plain` / `stealth` / `undetected` |
| `concurrency` | `4` | Per-store; a Cloudflare-fronted store wants less |
| `delay_seconds` | `[1.0, 3.0]` | Random inter-request delay range |
| `chrome_threshold` | `0.8` | Block appearing on ≥ this fraction of sampled pages is chrome |
| `min_block_chars` | `3` | Minimum text-run length to count as a block |
| `containment_min_tokens` | `3` | Candidate strings shorter than this cannot match |
| `containment_threshold` | `0.8` | Token-subset overlap required for a match |
| `allowlist_min_support` | `3` | Products with a value required before a key can be admitted |
| `allowlist_min_hit_rate` | `0.8` | Match rate required to admit a key |

**Per store:** `slug`, `domain`, `enabled`, plus any overrides. `crawl_scope: none` is meaningful
and correct for a store whose coverage analysis shows the API already holds everything.

**Initial store config:**

- `skout` — domain `www.skoutorganic.com`, `fetch_profile: plain`
- `remi` — domain `shopremi.com`, `fetch_profile: stealth`, `concurrency: 2`

### 4.2 Environment

A single JSON blob, keyed by store slug:

```
PIER39_SHOPIFY_TOKENS={"skout":"shpat_...","remi":"shpat_..."}
OPENAI_API_KEY=...
COHERE_API_KEY=...
DATABASE_URL=postgresql://pier39:pier39@localhost:5433/discovery
```

`.env` is gitignored. `config/stores.yaml` contains no secrets and no domains-to-token mapping.

### 4.3 Reproducibility

Every run writes the **resolved** configuration — after defaults merge and CLI overrides — to
`data/{slug}/run.json`, alongside the `admin_api_version`, timestamps, and tool versions. A
coverage number without its resolved config is worthless a week later.

---

## 5. Pipeline

Each stage reads the previous stage's files from disk and writes its own. No stage calls the
network on behalf of another. This is what allows profiling to be re-run fifty times without
re-crawling.

Artefact layout: `data/{slug}/` containing `api.jsonl`, `pages/`, `profile.json`, `run.json`.

### 5.1 `fetch-api`

Paginated Admin GraphQL against `products(first: 250, query: "status:active")`, selecting per
product: `id`, `handle`, `title`, `vendor`, `productType`, `tags`, `status`, `publishedAt`,
`onlineStoreUrl`, `descriptionHtml`, `templateSuffix`, nested `metafields(first: 250)` with
`namespace`, `key`, `type`, `value`, `updatedAt`, and nested `variants(first: 100)` with `id`,
`title`, `sku`, `selectedOptions`.

Separately, one `metafieldDefinitions(ownerType: PRODUCT, first: 250)` call per store, stored for
comparison against observed values. The definition list both misses undefined namespaces and
overstates coverage for defined-but-empty keys; the comparison is diagnostic, not authoritative.

**`onlineStoreUrl` is the URL source and the publication filter.** It returns the product's
online-store URL and is null when the product is not published to that channel. Products with a
null value are recorded and excluded from crawling. No URL is constructed by string concatenation.

Output: `api.jsonl`, one JSON object per product.

Fetched price and inventory fields are **not** requested and **not** stored. They are read live
at query time.

### 5.2 `fetch-html`

Selects URLs according to `crawl_scope` and `sampling`, capped by `max_pages`.

Crawl4AI `arun_many()` with `SemaphoreDispatcher(max_session_permit=concurrency)` and
`RateLimiter(base_delay=delay_seconds, max_delay=60, rate_limit_codes=[429, 503])`. One crawl run
per store, because Crawl4AI's rate limiter is a random inter-request delay and not a per-domain
control.

**Escalation ladder.** Start at the configured `fetch_profile`. On HTTP 403 with `Just a moment`
in the body, escalate `plain` → `stealth` → `undetected` and retry. Record the profile that
succeeded in the fetch manifest. This is exactly how remi presents.

Output: `pages/{handle}.html`, `pages/{handle}.md`, and a manifest row per page carrying URL,
HTTP status, byte count, content hash, fetch profile used, and timestamp.

Both raw HTML and markdown are written. Markdown is the working format because it preserves the
structure that carries meaning — `<strong>Material:</strong>` becomes a bold label adjacent to its
value. Plain text loses that.

### 5.3 `profile`

Five steps, in order.

**Step 1 — Block extraction.** Strip `script`, `style`, `noscript`, `svg`, `template` elements
and HTML comments. Split on tag boundaries. Unescape entities, collapse whitespace, trim. Keep
runs of at least `min_block_chars`. Measured yield: 380–440 blocks per skout product page.

**Step 2 — Chrome removal by frequency.** Count the number of sampled pages each distinct block
string appears on, counting each block once per page. Blocks appearing on at least
`chrome_threshold × page_count` pages are chrome and are dropped.

**The threshold must not be 1.0.** Different pages omit different sections, so a unanimity rule
leaks whole sections into every page's product region. Measured on five skout pages: at 1.0,
1,569 words survived and included the store-wide FAQ (`Where do you ship?` was present on 4 of 5
pages, `What does a Skout bar taste like?` on 3 of 5). At 0.8, 664 words survived and the
survivors were the genuine product region.

**Step 3 — Residual cleanup.** Two deterministic filters:

- **Templated widget text.** Review-widget accessibility strings such as
  `Yes, this review from {name} was helpful` and `person voted yes` are unique per review and so
  never repeat across pages. Remove by regex pattern list.
- **Foreign product titles.** The variant or flavour selector lists sibling products, and it
  varies per page so differencing cannot remove it. Drop any block exactly matching another
  product's title from `api.jsonl`.

**Step 4 — Metafield allowlist derivation.** For each metafield key:

1. Normalise the value to candidate strings, type-aware. Plain text yields the string.
   `rich_text_field` and `json` yield leaf text nodes. `list.*` yields each element separately.
   `file_reference` and `metaobject_reference` are resolved or skipped.
2. Discard candidates shorter than `containment_min_tokens`, so `true`, `new` and `55.00` cannot
   match by accident.
3. **Match by token-subset overlap at `containment_threshold`, not exact substring containment.**
   Exact matching rejects `custom.nutrients` because the theme renders `Protein [1g]` as
   `Protein 1g`. Silently discarding good structured fields is the worst available failure mode,
   because it looks like success.
4. Compute hit rate = products where the key had a value and matched, over products where it had
   a value. Admit at `allowlist_min_hit_rate` with at least `allowlist_min_support` products.
5. **Chrome guard.** If a value also matches on many unrelated products' pages, demote it.
6. **Contamination rejection, applied unconditionally and before everything else.** If a value
   contains a product GID, numeric product ID, handle or `productUrl` that does not match the
   owning product, reject the key with reason `foreign_product_id`. This single rule kills
   `loox.review_feed`, `stamped.reviews` and `product_seo.seo_tags`.
7. **Freshness rejection.** Drop keys whose `updatedAt` lags far behind the newest write in their
   namespace, with reason `stale_namespace`.

**Render-presence promotes; it does not gate.** An allowlist admitting only rendered keys would
reject `custom.product_attributes`, `custom.product_faqs` and all three `custom.description_*`
variants, which render nowhere on skout's storefront and are the most retrieval-useful content on
the product. Contamination and freshness do the filtering.

**Step 5 — Residual analysis and coverage.** Take page blocks explained by neither a metafield
nor `descriptionHtml`. Blocks identical across every sampled page of a template are **template
constants** — remi's `Material: BPA-free, food-safe plastic` and its included-items list. Blocks
varying per product are content unreachable through the API. Coverage percentage is the share of
surviving product-region words explained by API sources.

**Label recovery.** For each admitted key, capture the text rendered immediately before its value
in the markdown. This yields `Material:`, `Battery life:`, `Power:`, `Tank capacity:` — names the
API never provides for keys like `custom.product_blue_content`.

Output: `profile.json` containing admitted keys with labels and hit rates, rejected keys with
reason codes, template constants keyed by template, the chrome block set hash, and coverage.

### 5.4 `merge`

Emits **field assertions**, not a merged blob. One row per `(product, field, source)`:

`field`, `value`, `source`, `source_kind` ∈ {`metafield`, `theme`, `description`, `api`},
`rendered` boolean, `observed_at`, `source_updated_at`, `value_hash`.

**Precedence:**

| Case | Rule |
|---|---|
| Price, inventory, variant availability | Never stored. Live API at query time |
| Structured attribute in a metafield | Metafield wins. A typed list beats extracted prose |
| Present in both metafield and page | Metafield wins on value. The page contributes confirmation and the label |
| Page only, identical across a template | Template constant. Store once against the template, attach to all its products |
| Page only, varying per product | Per-product theme content. Counts against coverage |
| Metafield only, never rendered | Keep. Classified as retrieval material only |

**Every assertion is classified into exactly one of two trust classes:**

- **`retrieval`** — feeds the embedding and matching. **Never quoted to a shopper.**
- **`quotable`** — the bot may state it as fact. Restricted to assertions either rendered on the
  live page, or from a typed metafield with a recent `updatedAt`.

Unrendered enrichment is `retrieval` only. skout's `product_faqs` are visibly LLM-generated and
carry hedges such as "check the ingredient statement on the package"; no merchant has vetted them
on the storefront. This classification is what prevents the bot asserting an unvetted allergen
claim.

**Conflicts are dropped, never reconciled.** remi reports 51, 627 and 1193 reviews for one
product across three apps. Emit no review count for that product.

**Review app liveness** is determined by, in order: `updatedAt` recency per review namespace
across sampled products; DOM presence in the crawled pages; and a hard filter that every ingested
review's product ID matches the owning product. `appInstallations` is unavailable — it requires
`read_apps`, which Shopify does not grant to public apps.

**Edges.** Every `product_reference` and `metaobject_reference` value becomes an edge row:
`related_products`, `complementary_products`, `frequently_paired_with`, `variety.flavors`,
`bundle.extra_product`, `bundle.prebuilt`, plus collection membership and variant-to-product.
Populated in v0, not traversed by search. Metaobject references are preserved because Shopify's
standard taxonomy metaobjects are shared vocabulary across stores, and are therefore the
cross-store attribute alignment that cross-publisher discovery will need.

### 5.5 `index`

Builds one **purpose-written retrieval document** per product from the canonical record: title,
subtitle, product type, vendor, admitted attributes with their recovered labels, allergen
exclusions, use cases, FAQ text. The merged blob is not embedded.

Everything filterable stays a column — price band, dietary flags, stock, collection IDs.
Filtering in SQL is exact; filtering by embedding similarity is not.

Where FAQ content is long, emit one chunk per FAQ entry with a foreign key to the product, so
retrieval can return a specific answer rather than a whole product.

**Embedding:** OpenAI `text-embedding-3-large`, `dimensions=1024`. Batch limits are 2048 inputs
and 300,000 tokens per request, so both stores fit in a handful of calls. Truncation can break
unit length, so **normalise vectors after truncating**. At the first batch, compute the L2 norm
and log it; choose the pgvector operator class from the result rather than assuming.

**Content-hash gate.** Skip the write and the embedding call when `value_hash` is unchanged.

**One transaction per product** covering assertions, edges, document and vector, so retrieval
never observes new text beside old attributes.

### 5.6 `search`

Four stages.

1. Embed the query.
2. Two first-stage retrievers over the same table: pgvector cosine top-50, and `ts_rank` over the
   generated `tsvector` top-50. Fuse with reciprocal rank fusion, scoring each document
   `Σ 1/(60 + rank)` across both lists. This catches exact terms — a brand, a SKU — that
   embeddings miss.
3. Apply constraints as SQL `WHERE` clauses: price band, `in_stock`, and allergen exclusions from
   `filter.contains` as `NOT` predicates. **Negation is handled here, not in vector space.**
4. Rerank the survivors with Cohere Rerank 4.0 down to 5, then make **one live Admin API call** on
   those variant IDs for price, inventory and availability.

Output per hit: product title, matched fields with their provenance and trust class, and the live
price.

---

## 6. Schema

| Table | Columns of note |
|---|---|
| `stores` | slug, domain, admin_api_version, first_ingested_at |
| `products` | store_id, shopify_product_id, handle, title, vendor, product_type, status, online_store_url, template_suffix |
| `variants` | product_id, shopify_variant_id, title, sku, selected_options. **No price or inventory columns, by design** |
| `field_assertions` | product_id, field, value, source, source_kind, rendered, trust_class, observed_at, source_updated_at, value_hash |
| `template_constants` | store_id, template_suffix, field, value, label, observed_at |
| `documents` | product_id, chunk_key, text, embedding `vector(1024)`, tsv generated `tsvector` |
| `edges` | store_id, from_type, from_id, relation, to_type, to_id, source |
| `rejected_keys` | store_id, namespace, key, reason_code, detail |

Unique constraint on `field_assertions (product_id, field, source)` so re-running is a no-op.
Edges are written as a set-diff — insert new, delete departed — because append-only edge tables
silently accumulate stale relationships.

---

## 7. CLI

| Command | Purpose |
|---|---|
| `init-db` | Apply `schema.sql` idempotently |
| `fetch-api` | Products, metafields, variants, definitions → `api.jsonl` |
| `fetch-html` | Selected pages → `pages/`, with escalation |
| `profile` | Differencing, allowlist, constants, coverage → `profile.json` |
| `merge` | Field assertions and edges → Postgres |
| `index` | Retrieval documents, embeddings → Postgres |
| `search` | Query, with provenance and live price |
| `report` | Print the per-store profile report |
| `eval` | Recall@5 against `questions.yaml` |
| `run` | Chain `fetch-api` → `index` |

All accept `--store` (overriding `enabled`) and `--limit`.

`report` is the primary deliverable: admitted keys with labels and hit rates, rejected keys with
reason codes, template constants, and coverage percentage.

---

## 8. Eval harness

`config/questions/{slug}.yaml` — ten hand-written shopper questions per store with expected
product handles. Questions must include negation cases (`cookies without peanuts`,
`dairy free snacks`) and attribute cases (`how long does the battery last`), because those are
what the design is betting on.

`eval` reports recall@5 overall and per question. Without this the POC is a demo; with it, a
threshold or prompt change can be judged.

---

## 9. Out of scope for v0

No LLM extraction. No Pydantic AI. No GCP services — no Cloud Run, Pub/Sub, Cloud Tasks, Cloud
Scheduler. No webhooks. No incremental or `updatedAt`-gated re-ingestion. No edge traversal in
search. No `read_themes`. No `bulkOperationRunQuery`. No web API surface. No application
Dockerfile. No tests beyond unit tests on block extraction and the differencing function.

---

## 10. Open items

- [ ] **`Product.templateSuffix` is unverified.** Confirm during `fetch-api`. If absent, set
      `sampling: by_product_type` and key `template_constants` on product type instead. No impact
      on feasibility.
- [ ] Whether OpenAI returns unit-normalised vectors is not stated in the API reference. Measure
      at first batch and choose the pgvector operator class accordingly.
- [ ] Whether a metafield write fires `products/update`. Irrelevant to v0, required before any
      incremental design.
- [ ] Confirm the merchant agreement covers automated fetching of publisher storefronts.

---

## 11. Facts this build depends on

Established empirically on 2026-08-21 and 2026-08-24. Full detail in
`personapay-backend-publisher/docs/product-discovery-ingestion.md`.

- `/products/{handle}.js` returns 200 unauthenticated on all three tested stores.
- `/collections/all/products.json?limit=250` returned **182 products** for skout in one page.
  The pilot is therefore closer to 500–900 pages than the 250 originally assumed.
- `/sitemap.xml` is a sitemap *index*. The product entry carries mandatory `?from=&to=` query
  parameters and must be followed verbatim; requesting `sitemap_products_1.xml` bare returns
  HTTP 400.
- remi returns HTTP 403 with a Cloudflare `Just a moment` interstitial to plain `curl` even with
  realistic headers, while its `.js` endpoint returns 200. Headless Chromium passes.
- remi's `descriptionHtml` is 74 words and is **not rendered on its product page at all**. The
  specification block exists only in a theme section.
- skout's `custom.product_attributes`, `custom.product_faqs`, all three `custom.description_*`
  variants, and `filter.contains` render **nowhere** on the storefront. HTML is not a superset of
  the API.
- Metafield queries cost 5–14 units against a 20,000 bucket restoring at 1,000/s.
- Block differencing on five skout pages: 430 blocks and 2,322 words reduce to 67 blocks and 664
  words at a 0.8 threshold, with the product region intact.
