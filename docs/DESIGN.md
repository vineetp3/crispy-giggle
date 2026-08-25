# Discovery Ingestion POC — Build Specification

**Status:** approved for build. Implementation spec, not a discussion document.
**Created:** 2026-08-24
**Location:** `/Users/vineetsawhney/Desktop/code/pier39-discovery-poc` — local only, no remote.
**Background and evidence:** `docs/product-discovery-ingestion.md` — pre-implementation
discussion notes, superseded by this document wherever the two differ.

---

## 1. Purpose

Prove that a single Shopify admin token is sufficient to build a queryable product index, and
measure how much of each publisher's product content is reachable.

This POC exists to produce two artefacts:

1. **A per-store profile report** — which attributes the store can answer and from which source,
   which metafield keys carry live product content, what their human-readable labels are, which
   are rejected and why, and what content exists only in the theme.
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
| 4 | `profile` rejects `stamped.reviews`, `loox.review_feed` and `product_seo.seo_tags` as contamination, with reason code `foreign_product_id` or `foreign_product_title` |
| 5 | `profile` recovers human-readable labels for at least three otherwise-opaque fields, from metafields or from the theme |
| 6 | `profile` captures remi's `Material`, `Battery life`, `Power` and `Tank capacity` as template constants |
| 7 | `report` emits per-attribute reachability per store, and a coverage percentage as a diagnostic |
| 8 | `eval` reports recall@5 of at least 0.70 across at least 30 hand-written questions per store, with zero constraint violations |

Criterion 8's bar is an arbitrary starting point. Its value is that it is measured, not that it
is 0.70. The violation count is not arbitrary: a returned product that contradicts the query's
exclusion is a safety failure and the only acceptable number is zero.

Criteria 4 and 5 were narrower when written. Criterion 4 named `foreign_product_id` for all
three keys; `product_seo.seo_tags` carries no product ID, GID or URL, so it is caught by the
same-store title check instead. Criterion 5 named metafield keys; the labels that exist on these
two stores are overwhelmingly theme-resident, and restricting the criterion to metafields
measured the wrong surface.

## 3. Decisions register

These are settled. Do not relitigate them during implementation.

A row marked **contested** is the exception: it was decided on reasoning that a live run has
since put in doubt, and §10 carries the evidence and the rule for resolving it. Contested rows
stay in force until that rule produces a number.

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
| Reranking | Cohere Rerank 4.0, **in v0** | Promoted from optional because OpenAI has no `input_type`, so the cross-encoder carries the asymmetry. **Contested — see §10.** Promoted on theory; the reranker has never executed, and measured recall without it is 0.79 and 0.97 |
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
| `profile_pages` | `40` | Pages used to derive the boilerplate profile and metafield allowlist. This number drives differencing quality |
| `crawl_scope` | `sample` | `none` / `sample` / `template_representatives` / `all`. Both pilot stores override to `all` — see §5.2 |
| `max_pages` | `250` | Hard ceiling, independent of `crawl_scope` |
| `sampling` | `by_template` | `by_template` / `by_product_type` / `random` / `first_n` / `explicit` |
| `sampling_seed` | `1739` | Fixed, so `random` is reproducible |
| `fetch_profile` | `plain` | `plain` / `stealth` / `undetected` |
| `concurrency` | `4` | Per-store; a Cloudflare-fronted store wants less |
| `delay_seconds` | `[1.0, 3.0]` | Random inter-request delay range |
| `chrome_threshold` | `0.8` | Block appearing on ≥ this fraction of sampled pages is chrome |
| `min_block_chars` | `3` | Minimum text-run length to count as a block |
| `containment_threshold` | `0.8` | Token overlap required for a match, within one window |
| `allowlist_min_support` | `3` | Products with a value required before a key can be admitted |
| `allowlist_min_hit_rate` | `0.8` | Match rate required to class a key `rendered` |
| `storefront_api_version` | `2026-01` | Used by the answer-time commerce read |
| `market_country` | `US` | `@inContext` market for live pricing |

Candidate floors are code constants, not config: a candidate needs at least 2 tokens and 8
characters to match at all. They are not tunable because loosening them reintroduces accidental
matches on `true`, `new` and `55.00`.

**Per store:** `slug`, `domain`, `enabled`, plus any overrides. `crawl_scope: none` is meaningful
and correct for a store whose coverage analysis shows the API already holds everything.

**Initial store config:**

- `skout` — domain `www.skoutorganic.com`, `fetch_profile: plain`
- `remi` — domain `shopremi.com`, `fetch_profile: stealth`, `concurrency: 2`

### 4.2 Environment

A single JSON blob, keyed by store slug:

```
PIER39_SHOPIFY_TOKENS={"skout":"shpat_...","remi":"shpat_..."}
PIER39_SHOPIFY_STOREFRONT_TOKENS={"skout":"...","remi":"..."}
OPENAI_API_KEY=...
COHERE_API_KEY=...
DATABASE_URL=postgresql://pier39:pier39@localhost:5433/discovery
```

`PIER39_SHOPIFY_STOREFRONT_TOKENS` is optional. Without it the answer-time commerce read falls
back to the Admin API, which has no market context and shares the ingestion rate-limit bucket.
`COHERE_API_KEY` is optional and `search` degrades to the fused order without it; that
degradation is silent, so `eval --compare-rerank` reports when the reranker did not execute.

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

Fetched price and inventory fields are **not** requested in the catalogue query and are
**never** stored. This applies to prices held in **metafields** too: remi keeps six copies
of one price across `custom.current_price`, `banner_pricing`, `smarterr_app_price`,
`smarterr_single_price`, `smartrr_otp_price` and `current_subscription_price`, plus a
`yellow_badge_save_amount` that equals full price. Those are rejected during `profile` with
reason `commerce_fact`.

One derived read is permitted: a **sellability verdict** per published product, from
`availableForSale` and `price`. The verdict is stored; neither input is. It exists because
`status:active` with a non-null `onlineStoreUrl` does not mean buyable — 31 of skout's 184
published products are abandoned records priced 0.00 with `inventoryQuantity` at -770, -101
or -14, several shadowing a live twin under a legacy handle. They are excluded from
crawling, indexing and retrieval with reason `abandoned_sku`. Negative inventory alone is
**not** the signal: remi has 23 of 30 products at negative quantity and all 30 are buyable,
because that store runs continue-selling.

### 5.2 `fetch-html`

Selects URLs according to `crawl_scope` and `sampling`, capped by `max_pages`. Only products
that are published **and** sellable are selectable; abandoned SKUs are excluded here, not just
from indexing, because their pages distort the boilerplate profile.

`by_template` takes a floor of three pages per template group before spreading the remaining
budget. Plain round-robin gave every group exactly one page whenever the budget approached the
group count, and a single-page group has nothing to difference against.

**The floor was unreachable at the configured budget, and both pilot stores now crawl in full.**
`profile_pages: 40` against skout's 28 template groups spends 28 slots on the `want=1` pass and
the remaining 12 on `want=2`; the loop returns before `want=3` executes. The floor was written to
fix exactly this and the budget was never raised to let it work.

Crawling was never the expensive part, and nothing in the repo had ever measured it. Measured on
the 2026-08-25 full run: 152 pages in 5m43s, 0 failures, all on the `plain` profile. Raw HTTP is
0.6–0.9s per page; Chromium rendering dominates at roughly 10s per page, which four at a time
works out to about 6 minutes for skout's whole sellable catalogue against roughly 1 minute for a
40-page sample.
`profile_pages` is a differencing-quality knob, not a cost control. The reason to go gently on
these stores is politeness to a partner's live storefront and remi's Cloudflare front door —
which argues for a low `concurrency` and a real `delay_seconds`, both retained, not for stopping
early. Sampling stays in the codebase for stores large enough to need it; `fetch-html` warns when
the configured budget cannot reach `GROUP_FLOOR` for every group.

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

A spec may arrive as one text run (`Material: Dental-grade polymer`) or as a label node followed
by a value node (`Battery life:` then `30 days per charge`). `label_for` reads the second shape
and `blocks.inline_label` the first; both apply the same four-word label cap, `looks_like_label`
guards and a numeric-value rule, so skout's `February: 2/12` shipping calendar stays out.

**Step 2 — Chrome removal by frequency, at two levels.** Count the number of sampled pages each
distinct block string appears on, counting each block once per page. Blocks appearing on at
least `chrome_threshold × page_count` pages are chrome and are dropped.

The pass runs twice: once store-wide, then once within each template group of two or more
pages. A block repeated across every page of one template but absent elsewhere is that
template's furniture, and only the per-group pass sees it. One store-wide pass across
heterogeneous layouts has almost nothing to difference — 70% of remi's distinct blocks appear
on exactly one page.

**A third pass covers products alone on their template.** The per-group pass skips any group with
fewer than two pages, so a product that is the only one on its layout gets store-wide chrome
removal and nothing else. That is 20 of remi's 48 products. A ratio threshold cannot see copy
repeated across a handful of pages: measured on remi's 30 crawled pages, 1,236 distinct blocks
yield 72 classed chrome at 0.8 (a 24-page cutoff), leaving 2,919 words in blocks that appear on
2 or more pages but below it — the doctor testimonial repeating across three products at
1,500–2,000 words each. For singleton-group pages only, `blocks.repeated_block_profile` applies an
absolute floor of 3 pages instead of a ratio. The floor is 3 rather than 2 because at 2 it strips
spec text legitimately shared between two variants of one product.

**A page count alone is not sufficient, and shipping it without a length guard was a measured
regression.** Real attributes repeat across sibling products exactly like boilerplate does. The
3-page rule with no guard cost remi its `compatibility` attribute and cost skout both `dimensions`
and `usage` — skout fell from `theme 2` to `theme 0`, destroying the very output §1 names as the
deliverable. Raising the page floor did not fix it: at 5 pages remi recovered and skout did not.
Length separates the two cleanly, because the copy this rule targets is long prose while
attributes are short `label: value` pairs. The rule therefore requires **both** 3 pages and 20
words. At that setting both stores keep every attribute they had, remi's coverage improves from
4.0% to 4.7%, and nothing shorter than 20 words is touched however often it repeats.

**The threshold must not be 1.0.** Different pages omit different sections, so a unanimity rule
leaks whole sections into every page's product region. Measured on five skout pages: at 1.0,
1,569 words survived and included the store-wide FAQ (`Where do you ship?` was present on 4 of 5
pages, `What does a Skout bar taste like?` on 3 of 5). At 0.8, 664 words survived and the
survivors were the genuine product region.

**Group chrome is a template-constant candidate, not furniture.** A block common to every page
of one template is either the template's furniture *or* a spec shared by every product of that
type — and the second is exactly what this stage exists to find. Discarding the group-chrome set
before constant extraction meant a template's whole spec table vanished whenever the group had two
or more crawled pages, while groups with a single crawled page kept theirs. remi's night guards
sit in a 3-page group and lost `Material: Dental-grade polymer, BPA-free, and phthalate-free.`
that way; the water-flosser sits alone on its template and kept everything, which is why one
product accounted for most of remi's answerable attributes. Blocks removed by the per-group pass
are now offered to `_spec_pairs`, and those that parse as a labelled spec become template
constants. Only labelled pairs are recovered — the night-guard group has 155 group-chrome blocks
and 5 of them are specs.

**Theme constants get their own commerce guard.** `is_commerce_fact` keys on namespace, key and
type, none of which a theme constant has. remi renders `Birthday Sale: 50% Off` as a spec pair on
22 products. `is_commerce_constant` keys on discount language rather than on the presence of a
number, because `Formula: 3.8% Hydrogen Peroxide` is a real concentration and must survive.

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
2. Discard candidates under 2 tokens or 8 characters, so `true`, `new` and `55.00` cannot match
   by accident.
3. **Match by token overlap at `containment_threshold`, within a single window.** Exact substring
   matching rejects `custom.nutrients`, because the theme renders `Protein [1g]` as `Protein 1g`.
   Overlap alone is equally wrong in the other direction: a long candidate built from common
   words clears 0.8 against any large page without being present. Both conditions are required —
   the tokens must overlap **and** co-occur inside one span of `max(6, 2 × candidate length)`
   tokens. Without the window, `custom.product_faqs` scores 0.833 on a page carrying none of it.
4. **Contamination rejection, applied before anything else.** Four rules, any of which rejects
   the key outright:
   - a product GID, numeric product ID, handle or `productUrl` that is not the owning product's
     → `foreign_product_id`
   - values that describe a different product in the same store by name, for at least 25% of the
     key's products → `foreign_product_title`. Below 25% the key is admitted and flagged for
     human review, because flavour-family overlap and genuine cross-sell copy are
     indistinguishable at low rates
   - a price, percentage or `money` value → `commerce_fact`
   - a flag, hex colour or unix timestamp → `no_content_value`
5. **Freshness rejection.** Drop keys whose `updatedAt` lags far behind the newest write in their
   namespace, with reason `stale_namespace`.
6. Compute hit rate = products where the key matched, over products **whose page was fetched**.
   Class `rendered` at `allowlist_min_hit_rate`. Scoring against every product carrying a value
   caps the rate at crawled/total and makes `rendered` unreachable.

There is no chrome guard on metafield keys. It double-counted across products and wrongly
rejected `custom.nutrients`, `filter.ingredients` and `custom.product_faqs`. Page chrome is
handled by differencing; metafields are product-scoped by construction. What replaced it is a
recorded diagnostic naming keys whose value is identical on every product.

**Render-presence promotes; it does not gate.** An allowlist admitting only rendered keys would
reject `custom.product_attributes`, `custom.product_faqs` and all three `custom.description_*`
variants, which render nowhere on skout's storefront and are the most retrieval-useful content on
the product. Contamination and freshness do the filtering.

**Step 5 — Residual analysis, template constants, and reachability.** A page block counts as
explained only when it matches an API source under the same window rule as step 4. Token-set
coverage without a window marks a block explained whenever 60% of its words appear anywhere in
any source: remi's `30 days per charge with daily use` scored 5/7 against `descriptionHtml` on
the scattered words `30 daily days use with`, which silently deleted the battery spec.

Blocks identical across every sampled page of a template are **template constants**. A template
with only one sampled page is not skipped: a colon-terminated label followed by a short value is
a specification pair on its own markup evidence, which is how remi's `water-flosser` — the only
product on its template — yields `Material`, `Battery life`, `Power` and `Tank capacity`.

**Attribute reachability is the deliverable.** For a fixed shopper-facing attribute set —
allergens, nutrition, ingredients, materials, power, dimensions, care, compatibility, usage,
certifications — record whether the store answers it from `api`, `theme`, `image`, or not at
all. `image` exists because reference-typed metafields hold text that is in neither the API nor
the page region, so the word-based coverage number cannot see it in either direction.

Coverage percentage is retained as a diagnostic only. It is word-weighted, so it tracks review
volume as much as API completeness, and its denominator moves with `chrome_threshold` and the
page sample, which makes two stores' percentages incomparable.

**Label recovery.** Capture the text rendered immediately before a value. A trailing colon is
markup evidence and is trusted outright; a short run without one is inference and needs the same
label to recur across at least two observations with 0.8 dominance. Both forms reject storefront
UI strings by pattern, and neither accepts a label over four words — skout ends prose with
colons, and `We also ship internationally to:` is not a field name.

Output: `profile.json` containing attribute reachability, admitted keys with labels and hit
rates, rejected keys with reason codes, template constants with their labels, the free-from
declaration audit, per-product theme counts, the chrome block set hash, and coverage.

### 5.4 `merge`

Emits **field assertions**, not a merged blob. One row per `(product, field, source)`:

`field`, `value`, `source`, `source_kind` ∈ {`metafield`, `theme`, `description`, `api`},
`rendered` boolean, `observed_at`, `source_updated_at`, `value_hash`.

**Precedence:**

| Case | Rule |
|---|---|
| Price, inventory, variant availability | Never stored, from any source including metafields. Live Storefront read at query time |
| Structured attribute in a metafield | Metafield wins. A typed list beats extracted prose |
| Present in both metafield and page | Metafield wins on value. The page contributes confirmation and the label |
| Page only, identical across a template | Template constant. Store once against the template, attach to all its products |
| Page only, varying per product | Per-product theme content. Counts against coverage |
| Metafield only, never rendered | Keep. Classified as retrieval material only |

**Every assertion is classified into exactly one of two trust classes:**

- **`retrieval`** — feeds the embedding and matching. **Never quoted to a shopper.**
- **`quotable`** — the bot may state it as fact.

**Quotability is decided by type and shape, not by render presence.** The page is rendered from
the metafield, so a match proves only that the theme consumed the key — it says nothing about
whether a merchant vetted the value. skout's `custom.short_description` renders on every sampled
product and is generated marketing prose; `custom.nutrients` is a typed list of checkable facts
whose theme presence is incidental. Therefore: prose types and untyped `string` are never
quotable, `json` is never quotable, and any value over 8 tokens, containing markup, containing a
currency amount, or ending in `?` or `:` is not quotable whatever its declared type. Theme
constants are quotable only when they carry a recovered label.

Freshness is recorded on every assertion and deliberately does **not** gate quotability. Median
metafield age on skout exceeds 1,000 days for `custom.nutrients`, `filter.contains` and
`filter.curated`; an age cliff would empty the quotable set rather than make it safer, and
`updatedAt` is not evidence that a fact stopped being true. Decay requires a re-confirmation
loop, which v0 does not have.

`rendered` is recorded per product, never per key. A key at an 0.85 hit rate does not render on
the other 15%, and a product whose page was never fetched has no render evidence at all.

Unrendered enrichment is `retrieval` only. skout's `product_faqs` are visibly LLM-generated and
carry hedges such as "check the ingredient statement on the package"; no merchant has vetted them
on the storefront. This classification is what prevents the bot asserting an unvetted allergen
claim.

**`filter.contains` is a free-from list, not a contains list.** It is emitted as field
`free_from`. skout's peanut-butter bar omits `Peanut`; the lemon-poppyseed cookie includes it.
The rename exists so no downstream reader can invert the allergen filter.

**Conflicts are dropped, never reconciled.** remi reports 51, 627 and 1193 reviews for one
product across three apps; skout's peanut-butter cookie reports 72, 63 and 4.8 across three
namespaces. Neither product gets a review count. Dropping requires set-diff writes — an
upsert-only path leaves the previous run's value in place and the rule becomes a no-op.

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

Builds **one purpose-written document per trust class per product** from the canonical record:
title, subtitle, product type, vendor, admitted attributes with their recovered labels, use
cases, FAQ text. The merged blob is not embedded.

Both classes are retrievable, because unrendered enrichment is the most retrieval-useful content
on some products. They are separate chunks because the document text is what an answer layer
receives as grounding context, and a single mixed string carries no marker separating a vetted
nutrition panel from generated prose. A trust class stored only on a sibling assertion row is
invisible to whoever reads `documents.text`. A product with no quotable chunk is a product about
which nothing may be stated as fact.

`free_from` never enters a document. Its polarity is invisible to an embedding: writing
`Almonds; Cashews; Hazelnuts` for a product containing none of them teaches the vector the
opposite of the fact. Polarity-bearing fields are filters, and negation is answered in SQL.

Everything filterable stays a column — dietary flags, collection IDs, product type.
Filtering in SQL is exact; filtering by embedding similarity is not.

**Price and stock are deliberately NOT columns**, so they are not SQL-filterable. Earlier
revisions listed `price band` and `in_stock` here while §5.4 and §6 forbade storing them;
that was unimplementable and is resolved in favour of §5.4. Commerce constraints are
applied after the live read — see §5.6.

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

Seven stages.

1. Embed the query.
2. Two first-stage retrievers over the same table: pgvector cosine top-50, and `ts_rank` over the
   generated `tsvector` top-50. Fuse with reciprocal rank fusion, scoring each document
   `Σ 1/(60 + rank)` across both lists. This catches exact terms — a brand, a SKU — that
   embeddings miss.
3. Apply non-commerce constraints as SQL `WHERE` clauses. **Allergen negation is a whitelist
   join, not an exclusion scan**: `filter.contains` is a *free-from* declaration, so an
   excluded term must be **present** in it, and a product with no declaration is not an
   answer. Negation is handled here, never in vector space.
4. **Collapse duplicate listings into families.** skout lists the same physical product up to
   three times — a base handle, a `-bundle` handle, and a legacy `skout-organic-` prefixed
   listing — and ten products in the peanut-butter protein bar family carry byte-identical
   `free_from` values. Without this, five result slots go to five spellings of one bar. The key
   is the normalised title (`families.family_key`), the canonical prefers a non-bundle listing
   with the most quotable assertions, and the rest ride along as `siblings` so the answer layer
   can still offer the bundle. A family ranks where its best member ranked. This runs *after* the
   negation join, so a collapse can never resurrect an undeclared product, and *before* `top_k`,
   so the slice sees distinct products. `--no-group` disables it. Measured: skout 172 → 120
   families, remi 48 → 44. Grouping is not done at index time; every listing stays individually
   retrievable.
5. **Live commerce read**, on the Storefront API rather than Admin — `@inContext` gives
   market-correct pricing, and the separate rate-limit bucket keeps shopper queries from
   competing with ingestion. Admin is the fallback when no storefront token is configured.
6. Apply commerce constraints (price ceiling, in stock) to the live values. This must
   happen before the rerank, so the cross-encoder is not spent on results about to be
   dropped. A hit whose live read returned nothing fails the filter.
7. Rerank the survivors with Cohere Rerank 4.0 down to 5.

The first stage retrieves 200 per leg rather than 50, because a post-retrieval commerce
filter can empty a shallow pool; the live read is bounded to the top 60 of the fused
ranking so one query is not a dozen API round trips. This ordering holds because the corpus
is a few hundred products per store. Past roughly tens of thousands, a cached price band
with an explicit TTL and staleness contract becomes unavoidable.

Output per hit: product title, sibling listings, matched fields with their provenance, trust class
and source date, and the live price.

**Degradation is reported, not silent.** The reranker and the live read both swallow their
exceptions — degrading beats erroring on a shopper query — but both now record the failure on a
`Diagnostics` the CLI prints. This matters most under a price filter: stage 6 rejects any hit
with no live read, so a dead credential turns `--max-price` into an empty result set that reads
as "nothing matches" rather than "the price lookup died". A placeholder `COHERE_API_KEY` hid
behind the same pattern across every run on 2026-08-25.

Quotable facts are rendered with the date their source was last updated. Nothing expires — see
§10 — but a three-year-old allergen declaration should be visibly three years old.

### 5.7 `facts` — product-scoped answering

The other half of retrieval, and deliberately not `search`. Most real questions arrive with the
product already decided: a shopper on a product page asks "is it BPA free" and the surface passes
the handle. The product identity is a **parameter**, not something to infer from the query text.
Resolving "it" from conversation history, or deciding to break out of scope when a scoped query is
really a discovery one, are jobs for the answer layer — they need the conversation and the page
context, and neither belongs in retrieval.

Split entry points, shared primitives. `answering.answer_for_product` does not duplicate the
safety-critical logic: negation goes through `search.declared_free_from`, the same function and
the same matching semantics discovery uses, so the two cannot drift.

**Ranking barely applies.** remi averages 1.9 documents per product and never exceeds 2, so
ordering them is not retrieval. The substance of a scoped answer is `field_assertions`, which is
why this path embeds nothing: it costs one round of SQL and no model call.

**Scope expands to the family, not the product.** skout lists the same bar up to four times with
unevenly populated metafields, so scoping to a single product id hides facts held on a sibling
listing. Scoping `peanut-butter-protein-bar` yields 22 quotable assertions against the 14 on that
listing alone.

**Negation returns three states, never an empty list.** A scoped query cannot answer with "no
rows": *declared free of X*, *declares, and does not list X*, and *no declaration at all* are
three different facts, and reporting the third as the first is the same silent-empty failure the
commerce filter had. Only the first two are answerable.

---

## 6. Schema

| Table | Columns of note |
|---|---|
| `stores` | slug, domain, admin_api_version, first_ingested_at |
| `products` | store_id, shopify_product_id, handle, title, vendor, product_type, status, online_store_url, template_suffix |
| `variants` | product_id, shopify_variant_id, title, sku, selected_options. **No price or inventory columns, by design** |
| `field_assertions` | product_id, field, label, value, source, source_kind, rendered, trust_class, observed_at, source_updated_at, value_hash |
| `template_constants` | store_id, template_key, value, label, value_hash, observed_at |
| `documents` | product_id, chunk_key, **trust_class**, text, text_hash, embedding `vector(1024)`, tsv generated `tsvector` |
| `edges` | store_id, from_type, from_id, relation, to_type, to_id, source |
| `rejected_keys` | store_id, namespace, key, reason_code, detail |

`trust_class` lives on the document row, not only on `field_assertions`, because `text` is what
an answer layer receives and the class has to travel with it.

Assertions and edges are both written as a **set-diff** — insert new, update changed, delete
departed. An upsert-only path cannot express a withdrawal, and `merge` needs one: dropping a
conflicting review count means the row that used to be there has to go.

`schema.sql` is `CREATE TABLE IF NOT EXISTS` throughout, so it cannot alter an existing
database. Adding a column requires an explicit idempotent `ALTER TABLE ... ADD COLUMN IF NOT
EXISTS`, or a drop and rebuild.

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
| `search` | Query, with provenance and live price. `--exclude`, `--max-price`, `--in-stock` |
| `report` | Print the per-store profile report |
| `eval` | Recall@5 against `questions.yaml`. `--compare-rerank` runs both arms |
| `run` | Chain `fetch-api` → `index` |

All accept `--store` (overriding `enabled`) and `--limit`.

`report` is the primary deliverable: attribute reachability, allergen-negation capability,
admitted keys with labels and hit rates, rejected keys with reason codes, template constants with
their labels, per-product theme volume, and coverage as a diagnostic.

---

## 8. Eval harness

`config/questions/{slug}.yaml` — at least thirty hand-written shopper questions per store with
distinct expected handles. Questions must include negation cases (`cookies without peanuts`) and
attribute cases (`how long does the battery last`), because those are what the design is betting
on. Ten questions is not a measurement: recall at n=10 has a standard error near 0.15, so 0.70
is indistinguishable from 0.55.

**Two modes, scored separately, because they are not the same task.** A question with a `scope`
is asked of a product that is already known and runs through §5.7; everything else is a discovery
question and runs through §5.6. Scoring a scoped question by whether its product ranks in the top
five measures vocabulary distinctiveness, not retrieval — "tank" narrows remi to 7 of 48 products
while "calories" narrows skout to 48 of 171. Half the original attribute questions contained the
word "it", which is the tell: there is no "it" in a catalogue-wide search.

Scoped questions expand to **one case per (question, product)**. All-or-nothing across a
question's whole scope hides the actionable half: `what material is it made of` is answerable on
remi's water-flosser and on neither night guard, and merging those into a single failure discards
the useful part.

Scores, kept apart on purpose:

- **discovery recall@5** — the catalogue surfaced the right product.
- **scoped answerability** — the fact is present *and quotable* on a product already known.
- **relevance@5** — a named expected handle appeared, where one was named.
- **violations** — a returned product contradicts the query's exclusion.

A constraint query is correct when **every** result satisfies the constraint, not when a named
handle appears; ranking among many equally-valid products is relevance. Violations are checked
against the database rather than a hand-written forbid list, because a fixture can be passed by
omitting the awkward product. `expect_empty` marks questions a store cannot answer at all: remi
holds no free-from declarations, so its negation queries must return nothing, and without this
the harness cannot tell "correctly refused" from "found nothing".

`search._rerank` degrades to the fused order on any exception, so a missing or invalid
`COHERE_API_KEY` is indistinguishable from a reranker that changed nothing. `eval` detects when
the reranker never executed and `--compare-rerank` refuses to report a delta in that case.

---

## 9. Out of scope for v0

No LLM extraction. No Pydantic AI. No GCP services — no Cloud Run, Pub/Sub, Cloud Tasks, Cloud
Scheduler. No webhooks. No incremental or `updatedAt`-gated re-ingestion. No edge traversal in
search. No `read_themes`. No `bulkOperationRunQuery`. No web API surface. No application
Dockerfile. No quotability decay — it needs the re-confirmation loop that incremental
re-ingestion would provide.

**Amended 2026-08-25 — LLM label classification is permitted; LLM extraction is not.**

The exclusion above was written against a model *reading a page and producing facts*. That
stays excluded, and nothing in the ingestion path sends page content to a model.

What is now permitted is narrower: classifying a recovered **label** as a product
specification or a storefront widget. `labels.ClassifierPolicy` sends one label and up to
three example values, and receives one word back. It cannot produce a value, only decide
what an already-extracted label means. Verdicts are cached by `(store, label)` in
`data/<slug>/label_verdicts.json` and committed, so a classified profile re-runs offline and
deterministically. The deterministic guards still run afterwards and can reject anything the
model accepted; no policy can promote past `is_quotable_theme_value` or
`is_commerce_constant`.

**The measurement did not justify making it the default.** Scored against the hand-authored
reference sets, the classifier agreed on 22 of 30 remi labels and 4 of 8 skout labels, read
skout's `Pack Size` and `Size` as specifications, and read remi's `Quantity` as a widget —
the single case the label gate was built to fix. Scoped answerability under the classifier
matched the ungated control on both stores rather than the reference set, and it
reintroduced the widget-quotability breach. `--label-policy llm` therefore remains available
and off by default. See `docs/FINDINGS.md` §7.

---

## 10. Open items

Resolved items and their measured outcomes are in `docs/FINDINGS.md`. This section is the terse
register of *whether* an item is open; `docs/PENDING.md` carries the evidence, the options and the
tradeoffs for each, and is the document to read before picking one up.

What remains open:

- [ ] **The reranker has never executed, and whether it stays in v0 is undecided.**
      `COHERE_API_KEY` is a placeholder, Cohere returns 401, and every recall figure recorded so
      far is RRF only. The §3 decision that the cross-encoder carries the query/document
      asymmetry is untested. `search` now reports the failure instead of hiding it, but reporting
      it is not deciding it.

      **Decide by measurement, not preference.** Run `eval --compare-rerank` on both stores.
      The metric's resolution is one question, so 0.03 on these question sets:

      - **Delta below 0.03** — drop reranking from v0 and record the measurement in
        `docs/FINDINGS.md`. Revisit when a store's catalogue is large enough for first-stage
        recall to fail.
      - **Delta clearly positive** — keep a reranker, but reconsider the vendor. See below.

      Duplicate collapse (§5.6) changed what a reranker would be reordering: the top-5 now holds
      distinct products rather than several spellings of one bar, which is exactly the crowding a
      cross-encoder could never have fixed. An A/B is more meaningful after that change than
      before it.

      **OpenAI has no rerank endpoint** (checked 2026-08-25; verify before relying on it).
      The `file_search` tool in the Assistants/Responses API exposes `ranking_options`, but
      it only reranks OpenAI-hosted vector stores, which conflicts with the pgvector decision
      in §3. There is no way to rerank arbitrary `(query, document)` pairs from our own
      Postgres against an OpenAI model.

      Cohere is not enterprise-only — it has a self-serve tier — but it is the
      highest-friction option for a POC and the §3 promotion was made on theory rather than
      measurement. Candidates, ordered by fit at a few hundred products per store. The live
      constraint is dependency weight: there is no `torch` in the venv and it is already
      623 MB from Crawl4AI and Playwright, so anything pulling `torch` roughly triples the
      Cloud Run image that §9 of the evidence doc maps to.

      | option | local weight | key | note |
      |---|---|---|---|
      | ONNX cross-encoder, `ms-marco-MiniLM-L-6-v2` | ~90 MB | no | Apache 2.0, 22M params, tens of ms for 60 docs on CPU. No vendor, no rate limit, no silent-401 failure mode |
      | Voyage `rerank-2.5` / `-lite` | none | yes | Self-serve, free tier |
      | Jina `jina-reranker-v2-base-multilingual` | none | yes | Self-serve. Weights licence differs from the API — check before self-hosting |
      | Mixedbread `mxbai-rerank-base-v1` | ~500 MB via ONNX | optional | Apache 2.0, usable either way |
      | `bge-reranker-v2-m3` | ~1.1 GB | no | Better quality, too heavy for a query path already constrained by Chromium memory |
      | LLM-as-reranker | none | reuses OpenAI | Works; slower and dearer per query than a 22M cross-encoder that does it better |

      The same reasoning that rejected Firecrawl in §3 applies here: the value of a hosted
      reranker is the hosting, and the corpus is a few hundred products. Model names and tier
      terms change faster than this document; verify current ones before committing.

      If the intent is to answer the question without spending a Cohere key at all, the ONNX
      cross-encoder makes `--compare-rerank` runnable with no account. `_rerank` is one
      function and one call site.

- [ ] **Quotability decay — deferred, and contraindicated as an age rule.** Assertions carry
      `source_updated_at` and nothing expires. Two separate reasons it stays open.

      It needs `first_observed_at` / `last_confirmed_at` / `withdrawn_at` and a re-confirmation
      loop, which is meaningless without the incremental re-ingestion §9 puts out of scope.

      **An age cliff is the wrong mechanism and would make things worse, not safer.** Measured on
      skout: `free_from` averages 719 days across 131 products and reaches 1,098;
      `descriptors.subtitle` 956; `filter.curated` 865; `custom.short_title` 753. A one-year
      cutoff empties the quotable set, because that is simply how old this store's data is. Age
      does not separate stale from stable-and-still-correct — ingredients and materials do not
      change — so only re-confirmation can. Do not reopen this as an expiry date.

      What v0 does instead: `search` and `report` render the source date on every quotable fact,
      so a three-year-old allergen declaration is visibly three years old. Whether that is
      acceptable to repeat to a shopper is a business decision and belongs with whoever owns that
      risk, not in this document.

- [x] **Theme spec extraction — the label gate.** Closed 2026-08-25. Three extraction defects
      were fixed and a per-store label policy now decides whether a recovered `Label: Value`
      pair is a product specification or a storefront widget. remi's scoped answerability rose
      from 0.65 to 0.75 and skout holds 0.87. `Delivery Frequency` and `This item` are no
      longer quotable on skout; `Pack Size` and `Size` deliberately still are, by a product
      decision recorded in `docs/PENDING.md` §1a. The LLM classifier was built, measured and left off by default — see §9's amendment
      and `docs/FINDINGS.md` §7. Two consequences worth carrying forward, both in
      `docs/PENDING.md`: the reference sets are one reader's judgement on two stores, and
      whether a variant picker legitimately answers "how many come in a pack" is an unsettled
      product question rather than a defect.

- [ ] Whether a metafield write fires `products/update`. Irrelevant to v0, required before any
      incremental design.

**Still unverified rather than open.** The Storefront read has never executed:
`PIER39_SHOPIFY_STOREFRONT_TOKENS` is unset, so every live read has used the Admin fallback and
the market-pricing claim in §5.6 is unverified. This is blocked on a credential, not a decision.
The read now reports its own failure rather than degrading in silence.

### Closed since the 2026-08-25 runs

- [x] **`search._rerank` fails silently.** `_rerank` and `_attach_live` both record the failure on
      a `Diagnostics` and the CLI prints it. They still degrade rather than raise, which is right
      for a shopper query. The invalid-`rerank_model` case this item also named surfaces through
      the same path.
- [x] **The sampler floor had never been exercised** — and could not be. `profile_pages: 40`
      cannot reach `GROUP_FLOOR = 3` across skout's 28 template groups. Both pilot stores now use
      `crawl_scope: all`, and `fetch-html` warns when a configured budget cannot reach the floor.
      See §5.2.
- [x] **Legacy duplicate handles.** Collapsed into families at retrieval time — §5.6 and
      `families.py`. skout 172 → 120 families, remi 48 → 44. Every listing stays indexed and
      individually retrievable; only the result list is deduplicated.
- [x] **Cross-page marketing copy in singleton template groups.** A 3-page absolute floor now
      applies to products alone on their template, which is 20 of remi's 48. See §5.3.
- [x] **Merchant agreement covers automated fetching of publisher storefronts.** Confirmed
      granted.

---

## 11. Facts this build depends on

Established empirically on 2026-08-21 and 2026-08-24, and re-confirmed against live runs on
2026-08-25. Full detail in `docs/product-discovery-ingestion.md`.

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
