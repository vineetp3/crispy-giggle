# Decisions register

Settled decisions and the empirical facts this build rests on. Extracted from
`docs/DESIGN.md` so the specification stays readable and the register stays findable.

Do not relitigate these during implementation. `docs/PENDING.md` carries what is still open.

---

## 3. Decisions register

These are settled. Do not relitigate them during implementation.

A row marked **contested** is the exception: it was decided on reasoning that a live run has
since put in doubt, and §10 carries the evidence and the rule for resolving it. Contested rows
stay in force until that rule produces a number.

| Decision | Value | Note |
|---|---|---|
| Repo location | `/Users/vineetsawhney/Desktop/code/pier39-discovery-poc` | Now a git repository with an `origin` remote. The original decision was local-only; superseded 2026-08-25 |
| Relationship to main repo | None | `personapay-backend-publisher` is not modified |
| Python | 3.11 | Matches the existing runtime |
| Dependency management | `uv` + `pyproject.toml` | |
| Database | Postgres 16 + pgvector, `pgvector/pgvector:pg16` via `docker compose` | Only container in the project |
| DB access | psycopg3, plain SQL, single idempotent `schema.sql` | No ORM, no Alembic |
| CLI | Typer | One subcommand per pipeline stage |
| Crawler | Crawl4AI (Apache 2.0) | |
| Boilerplate removal | Cross-page block frequency differencing | Not Trafilatura. Article extractors discard `label: value` spec blocks |
| Embeddings | OpenAI `text-embedding-3-large`, `dimensions=1024` | Sticky: the pgvector column is `vector(1024)`; changing it means a migration plus full re-embed |
| Reranking | Local ONNX cross-encoder; **not earning its place in v0** | Originally a hosted reranker promoted on theory and marked contested. Settled by measurement on 2026-08-26 under §10's rule: the A/B executed for the first time and the delta fell below the metric's resolution on both stores. `_rerank` runs `flashrank` in-process, so no query path depends on a vendor account and the arm stays re-measurable rather than removed. Measurement in `docs/PENDING.md` §2 |
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

---

## 11. Facts this build depends on

Established empirically on 2026-08-21 and 2026-08-24, and re-confirmed against live runs on
2026-08-25. Full detail in `docs/archive/product-discovery-ingestion.md`.

- `/products/{handle}.js` returns 200 unauthenticated on all three tested stores.
- `/collections/all/products.json?limit=250` returned **182 products** for skout in one page.
  The Admin API now returns 201 products, 184 published. The pilot is therefore closer to
  500–900 pages than the 250 originally assumed.
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
- `filter.contains` is a **free-from** list. skout's peanut-butter bar omits `Peanut`; the
  lemon-poppyseed cookie includes it. Reading it as a contains list inverts every allergen
  answer.
- 31 of skout's 184 published products are abandoned records: priced 0.00 with
  `inventoryQuantity` at -770, -101 or -14, several shadowing a live twin under a legacy handle.
  Negative inventory alone does not identify them — remi runs continue-selling and has 23 of 30
  products at negative quantity, all buyable.
- OpenAI `text-embedding-3-large` at `dimensions=1024` returns unit-normalised vectors. Measured
  L2 norms across four batches: 0.9997 to 1.0001. Cosine is therefore correct and inner product
  would be equivalent.
- `Product.templateSuffix` exists. Present on 141 of skout's 201 products and 40 of remi's 48.
- Metafield queries cost 5–14 units against a 20,000 bucket restoring at 1,000/s.
- Block differencing on five skout pages: 430 blocks and 2,322 words reduce to 67 blocks and 664
  words at a 0.8 threshold, with the product region intact.
- **remi holds zero free-from declarations** across all 30 published products, so every allergen
  negation query there must correctly return nothing. skout can answer for 152 of 184; the other
  30 are excluded from negation queries rather than admitted on absent evidence. This is why the
  eval harness needs `expect_empty` — without it, "correctly refused" and "found nothing" are the
  same result.
- skout's live catalogue splits three ways: 58.2% buyable, 17.0% abandoned, 24.7% transient
  stockout. remi is 100% buyable.
- **Text-only contamination is real and partly ambiguous.** skout's `global.description_tag`
  describes a different product on 19 of 145 values and `custom.short_description` on 9 of 91.
  Both sit below the 25% rejection bar and are admitted with a review flag, because at low rates
  flavour-family overlap and genuine cross-sell copy are indistinguishable from contamination.
- **Two publisher-side data errors, worth raising with the merchant rather than coding around.**
  remi pairs `Tank capacity:` with `Cordless and portable, no sink required` — the extraction is
  faithful and the source is wrong. And remi titles a product
  `Night Guard Cleaning + Teeth Whitening Foam (SALES DISCOUNT) $15`, which is the single quotable
  value on either store containing a currency amount. No price *metafield* reaches a quotable
  assertion; a merchant can still put a price in a name.
- **A rich-text AST leak once inflated every coverage number.** Parsing `rich_text_field` put the
  structural node names `root`, `paragraph` and `text` into the explained-token set, so coverage
  read 20.2% and 10.3% where the honest figures were roughly half that. Coverage percentages
  predating 2026-08-25 are not comparable to later ones.
