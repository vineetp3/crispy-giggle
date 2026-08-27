> **ARCHIVED — historical.** Pre-implementation discussion notes, never an approved plan.
> Kept for the reasoning behind the original scoping. The build specification is
> `docs/DESIGN.md`; the current architecture is `docs/ARCHITECTURE.md`.

# Product Discovery — Catalogue Ingestion Design

**Status:** discussion notes, pre-implementation. Not an approved plan.
**Last updated:** 2026-08-24
**Scope:** Shopify publishers only. Storefront surfaces (product and home pages), not checkout.

---

## 1. Context and constraints

We are planning a chatbot that answers product queries, detects shopper hesitation, and pushes a
discount when required. This document covers only the **discovery** half: getting publisher
catalogue content into a form a retrieval layer can use.

Constraints set by the business, not negotiable here:

- Target surfaces are **storefront product and home pages**. Checkout is out of scope. The
  existing checkout nudge flow (`negotiation-agent` extension, `nudge_interaction_service_v2`) is
  a separate surface.
- Ingestion is triggered **on store connection**. Publishers are contracted customers.
- **Opt-in product.** Pilot is ~5 stores, ~50 SKUs average, so ~250 products.
- **Shopify only.** Custom (non-Shopify) publishers are out of scope.
- The Shopify API is authoritative for **price, available count, and variants**. These are read
  live at answer time and never stored.
- We are tied to the **GCP** ecosystem.

### What exists in the codebase today

| Thing | State |
|---|---|
| Product catalogue | None. `StoreProductRecomendationData` (`apps/checkout_nudges_placement/models.py:196`) is one JSON row per store; nothing in this repo writes to it |
| Admin + Storefront tokens | Present per store, `StoreAccessToken` (`apps/store/models.py:229`), types `shopify_admin` and `shopify_storefront` |
| Granted scopes | `read_orders`, `write_discounts`, `read_discounts`, `read_products`, `read_all_orders` by default (`apps/store/constants.py:47`); `write_pixels` + `read_customer_events` when the pixel is on. **No `read_themes`.** |
| Product webhooks | None. `shopify.app.prod.toml` declares only the three mandatory compliance topics, `api_version = "2024-10"` |
| Storefront mount point | `extensions/theme-extension` — app embed block targeting `body`, plus PDP section blocks. Plain ES modules, no build step |
| Theme/HTML ingestion | None. No bs4, lxml, playwright, selenium, or scraping code |
| Search infrastructure | None. No pgvector, numpy, or embedding libraries |
| LLM stack | `anthropic==0.109.0`, `langchain==1.3.4`, `langchain-anthropic==1.4.4` |
| Async | Redis + RQ (`scripts/rq_worker.py --with-scheduler`), no Celery |

---

## 2. Empirical findings from three pilot storefronts

Tested 2026-08-21 against live stores. This is the evidence base for everything below.

### 2.1 Baseline: the public Ajax endpoint works everywhere

`/products/{handle}.js` returned 200 on all three stores with no auth and no scope. It yields
`description`, `options`, `variants` (250 max), `images`, `media`, `tags`, `type`, `vendor`,
`price`, `available`, `selling_plan_groups`. 12–17 KB per product.

It is roughly **API-equivalent product content without authentication**. It contains no
theme-authored content.

### 2.2 Three stores, three different regimes

| Store | `.js` description | Rendered page | Verdict |
|---|---|---|---|
| countrylifefoods.com | 372 words | 2,154 words | API sufficient |
| skoutorganic.com | 97 words | 2,361 words | API sufficient (content is in metafields) |
| shopremi.com | 74 words | 1,868 words | API insufficient — theme holds facts |

**countrylifefoods** — everything is inside `descriptionHtml`: ingredients, allergen statement,
`Organic: YES`, `Non-GMO: YES`, `Country of Origin: Turkey`. The rendered page's extra ~1,800
words are navigation and footer. Scraping this store adds contamination, not content.

**skoutorganic** — the nutrition panel and certifications are *not* in the description, and the
initial hypothesis was that they lived in theme sections. **That was wrong.** They are metafields:

- `custom.nutrients` = `["Protein [1g]","Carbs [13g]","Calories [110]","Fiber [1g]","Sugar [8g]"]`
- `filter.ingredients` = nine-element structured list
- `filter.contains` = `["Almonds","Cashews","Hazelnuts","Pecans","Walnuts"]`
- `display.certification` = `list.file_reference`, six MediaImage GIDs — **image only**, so
  "Certified Plastic Neutral", "Certified Vegan", "GoTexan" exist as text nowhere in the API

The theme sections were merely Liquid rendering these metafields.

**shopremi** — the genuine theme-hardcoded case, and the hardest fetch:

- Plain `curl` returns **HTTP 403, Cloudflare "Just a moment..."** even with realistic headers.
  The `.js` endpoint returned 200 on the same host. Headless Chromium via Playwright passed on
  the first attempt (200, 817 KB, 16 sections).
- `descriptionHtml` is 74 words of emoji bullets and **is not rendered on the page at all**.
- The specification block — `Material: BPA-free, food-safe plastic`, `Battery life: 30 days per
  charge with daily use`, `Power: USB rechargeable`, `Tank capacity`, and the included-items list
  — appears in **neither `descriptionHtml` nor any metafield**. It is literal text in a theme
  section (`shopify-section-template--22440936603861__product_included_K`).

### 2.3 HTML is not a superset of the API

Verified by probing the saved skout page. These metafields render **nowhere** on the storefront:

- `custom.product_attributes` — 14 keyed attributes (Best For, Diet Preferences, Storage &
  Freshness, Allergen Information, …)
- `custom.product_faqs` — 7 question-and-answer pairs
- `custom.description_hero`, `description_technical`, `description_use_case` — all three
- `filter.contains` — the entire allergen exclusion list

That is the most retrieval-useful material on the product, and scraping alone would lose all of
it. Someone ran an enrichment pass into metafields that the theme does not consume.

Conversely the API is not a superset either (remi's spec block). **Both sources have an exclusive
region. Neither alone is sufficient.**

### 2.4 Data-quality hazards found

**Cross-product contamination.** Values attributed to the wrong product:

- skout: `stamped.reviews` is a ~30 KB HTML blob of reviews for product `3934936825939`
  (Apple Pie Kids Bar) while the product is `6942124474451`. `global.description_tag` is also
  apple-pie copy.
- remi: `loox.review_feed` has `context.productId: 8089718030549` but its `reviews` array is
  mostly reviews of `ultrasonic-cleaner-pro` (8961879998677) and `uv-toothbrush-sanitizer`
  (8374161080533), including "It did not clean my retainer at all."
- remi: `product_seo.seo_tags` on the water flosser contains copy for the night-guard cleaner.

**Conflicting review counts on one product (remi):** `okendo.summaryData` says 627 at 4.9; the
older `okendo.ProductReviewsWidgetSnippet` (v2.16.13) says 51; `loox.num_reviews` and
`reviews.rating_count` both say 1193. Three review apps, three answers.

**Stale prices in metafields (remi):** `custom.current_price` "55.00", `banner_pricing` 55.0,
`smarterr_app_price` "55.00", `smartrr_otp_price` "55.00", `smarterr_single_price` "$55.00",
`yellow_badge_save_amount` "$55.00" (labelled a saving, equals full price),
`price_promotion_text` "50% Off". Six copies of one number, one semantically wrong. This
vindicates the rule that price is read live.

**Internal fields:** `custom.admin_title` = "Primary - Cordless Water Flosser" must never reach a
shopper.

**Undefined duplicates:** remi has `product_sub_title` / `product_sub_text` (rich text,
undefined) shadowing the defined `product_subtitle` / `product_subtext`.

**Defined-but-empty keys.** remi defines and leaves empty: `custom.product_blue_content`,
`otp_box_content`, `accord_title`, `shipments_info`, `product_category`, `product_tags`,
`faq1_que`…`faq5_ans`, and the entire `agentiq` namespace (`apl_title`, `apl_category`,
`apl_description` — someone has pre-provisioned agent-facing fields). skout defines
`shopify.dietary-preferences` and `shopify.allergen-information` with no values.

**Query cost is negligible.** 5–14 units per product against a 20,000 bucket restoring at
1,000/s.

---

## 3. What Shopify does and does not give us

Corroborated against Shopify docs.

### Available with the `read_products` we already hold

- Product core: title, `descriptionHtml`, handle, vendor, `productType`, tags, status, SEO,
  options and option values, media, collection membership.
- Variants: price, SKU, `inventoryQuantity`, `availableForSale`, selected options.
- **All merchant-owned metafields.** A definition is the *schema* (type, validation, access,
  visibility); the metafield is the *value*. Values can exist with no definition, as untyped
  strings that are neither validated nor admin-editable
  ([definitions](https://shopify.dev/docs/apps/build/custom-data/metafields/definitions)).
  Merchant-owned namespaces (anything other than `$app`) are readable and writable by the
  merchant and every installed app
  ([ownership](https://shopify.dev/docs/apps/build/custom-data/ownership)).
- Key discovery via `metafieldDefinitions(ownerType: PRODUCT)`, and values regardless of
  definition via `productByIdentifier(identifier: {handle: …}) { metafields(first: 250) }`
  ([productByIdentifier](https://shopify.dev/docs/api/admin-graphql/latest/queries/productByIdentifier)).
- Per-metafield `createdAt` / `updatedAt`, our freshness and incremental-update signal
  ([Metafield](https://shopify.dev/docs/api/admin-graphql/latest/objects/Metafield)).
- Backfill via `bulkOperationRunQuery` → JSONL; the bulk query execution is **exempt from rate
  limits**. One concurrent operation per shop at our `2024-10` version, five from `2026-01`
  ([bulk operations](https://shopify.dev/docs/api/usage/bulk-operations/queries)).

### Available only after adding `read_themes`

`OnlineStoreTheme.files` exposes templates, sections, assets and config files including
`config/settings_data.json`, up to 2,500 files per fetch with wildcard filtering
([OnlineStoreTheme](https://shopify.dev/docs/api/admin-graphql/latest/objects/OnlineStoreTheme)).
`read_themes` is **not a protected scope** and needs no Shopify approval
([access scopes](https://shopify.dev/docs/api/usage/access-scopes)), but the scope change forces
reauthorization on every existing publisher. `ShopifyStore.granted_access_scopes` already tracks
this per store.

### Not available

- **Listing installed apps.** `appInstallations` requires the `read_apps` scope, which Shopify
  grants only to custom apps via Support and **cannot grant to public apps**. Our app is
  distributed, so this route to "which review app is live" is closed
  ([appInstallations](https://shopify.dev/docs/api/admin-graphql/latest/queries/appInstallations)).
- **Any event signal for theme content changes.** `THEMES_UPDATE` exists as a webhook topic but
  **does not fire when theme files are updated**, and both theme topics require `read_themes`
  ([WebhookSubscriptionTopic](https://shopify.dev/docs/api/admin-graphql/latest/enums/WebhookSubscriptionTopic)).
  Editing a section's content is a theme-file update. So for remi-class stores the only mechanism
  is **scheduled re-fetch of one page per template**.
- **Native reviews.** Shopify's own Product Reviews app was withdrawn and uninstalled from stores
  on 7 May 2024. Reviews live in third-party apps; they are reachable only when the vendor writes
  into merchant-owned metafields (Okendo and Loox both do).
- **Text of image-only content.** Out of scope by decision.

### Open question, not yet verified

Does a metafield write fire `products/update`? This determines whether metafield changes give us
an event signal or need polling. **Verify before relying on it.**

---

## 4. The HTML side: three separable jobs

Most scraping libraries bundle three jobs with different failure modes. Choose tools per job.

- **Fetch** — get the bytes. Fails on bot protection, JS rendering, rate limits. Infrastructure.
- **Extract** — 2 MB of HTML to clean markdown. Fails by discarding real content or keeping junk.
  Heuristics.
- **Structure** — markdown to typed fields. Fails by hallucinating or silently omitting.
  Modelling.

On our three stores: fetch is hard on remi only, extract is hard on all three, structure is hard
on remi only.

### 4.1 Fetch — recommend Crawl4AI

[Crawl4AI](https://github.com/unclecode/crawl4ai) (Apache 2.0) wraps Playwright and supplies the
operational layer we would otherwise write. `AsyncWebCrawler` + `CrawlerRunConfig` are the entry
points.

- `arun_many()` with pluggable dispatchers: `MemoryAdaptiveDispatcher` (default, pauses above a
  memory threshold, `max_session_permit` defaults to 10) or `SemaphoreDispatcher` for a fixed
  ceiling.
- `RateLimiter` with `base_delay` range, `max_delay` backoff cap, `max_retries`, and
  `rate_limit_codes` defaulting to 429/503
  ([multi-URL crawling](https://docs.crawl4ai.com/advanced/multi-url-crawling/)).
- **Limitation:** that rate limiter is a random inter-request delay, **not per-domain rate
  control**. So run one crawl per store, or put a Cloud Tasks queue per domain in front.
- Two escalating anti-detection modes: stealth (playwright-stealth, removes
  `navigator.webdriver`, cheap) and an undetected browser adapter with deeper patches. The docs
  name Cloudflare as a case for the latter, give no Turnstile-specific guidance, and say some
  sites may still block
  ([undetected browser](https://docs.crawl4ai.com/advanced/undetected-browser/)).
  Plain Playwright already passed remi, so start there and keep this as escalation.
- Also: response caching, hooks, iframe extraction, full-page scroll for lazy content, and a
  Dockerised FastAPI server if we want it as a separate service.

**Not Firecrawl.** [Firecrawl](https://github.com/firecrawl/firecrawl) handles JS, rotating
proxies and anti-bot automatically, but its value is the hosted service and we have 250 pages,
not 250,000. The core is AGPL-3.0, which needs a legal read before a self-hosted component our
backend calls. Keep as an escape hatch if a store defeats our own fetching.

### 4.2 Extract — and the trap to avoid

[Trafilatura](https://trafilatura.readthedocs.io/en/latest/) (Apache 2.0 since v1.8) is the best
general-purpose main-content extractor: readability- and jusText-style algorithms, explicit
precision/recall balance, targets recurring header and footer removal, outputs
text/markdown/JSON/XML with metadata.

**But it is tuned for articles, and a product page is not an article.** Article extractors find
one contiguous prose region and discard the rest. Remi's spec block is a two-tab `label: value`
widget; skout's nutrition panel is a small grid. An article extractor treats those as furniture
and drops them — correctly, by its own objective, and fatally for us.

**Do not flatten to plain text.** `<strong>Material:</strong> BPA-free, food-safe plastic` carries
the field name in the markup. Keep markdown, which preserves bold, lists, headings and table
pipes. Crawl4AI's headline output is exactly this, plus a "fit markdown" variant and a BM25
content filter.

**Boilerplate removal should not be a single-page heuristic.** We have many pages per template.
Fetch five product pages, tokenise into blocks, delete every block appearing identically in all
five. That removes nav, mega-menu, footer, cookie banner and shipping bar with near-perfect
precision (they are identical strings) and keeps the spec table (it varies). Roughly forty lines
of code, and it beats any heuristic.

**Decision:** Crawl4AI raw markdown as primary, cross-page differencing as the boilerplate
remover, Trafilatura retained as a second opinion for `descriptionHtml` and for stores where
differencing fails (one page per template).

### 4.3 Structure — selector first, LLM fallback

- **Selector-based, no LLM:** Crawl4AI's `JsonCssExtractionStrategy`. Fast, free, deterministic.
  Fragile to theme changes; one schema per store template. Fine at five stores, untenable at a
  thousand.
- **LLM-based:** robust to markup changes, generalises to new stores, costs money, can
  hallucinate.

Use **both, in that order**: try the selector schema, fall back to the LLM on empty result or
validation failure. Cheap where it works, general where it does not. Bonus: a store whose
selector hit rate suddenly drops has changed its theme — which is the polling signal we cannot
get from webhooks.

---

## 5. Pydantic AI — yes, for the structuring step

[Pydantic AI](https://pydantic.dev/docs/ai/overview/) is a typed agent framework. The parts that
matter for ingestion:

- **Typed outputs** declared as Pydantic models.
- **Validation with automatic retry** — "if validation fails, the agent is prompted to try again".
  The single most valuable feature here: `battery_life_days: int` receiving `"30 days"` is caught
  and retried without us writing the loop.
- **Provider swap by string** — Anthropic, Vertex AI and others. Develop against the Anthropic
  API, ship on Claude via Vertex without touching extraction code.
- **Dependency injection** via `RunContext` for per-store context.
- **Pydantic Evals** — "tests agent behavior the way pytest tests code".
- **OpenTelemetry-native instrumentation.**
- **Durable execution** integrations (Temporal, DBOS, Prefect).

**Why not Crawl4AI's own LLM extraction strategy?** It couples extraction to the crawler. You
cannot replay extraction over stored HTML without re-crawling, cannot unit-test against fixtures,
and get no eval harness. Separating fetch from extract lets us iterate on prompts against 250
saved pages in seconds.

**The evals argument is the real one.** A slightly wrong `material` field looks exactly like a
right one. Without a labelled set (start with ~20 hand-checked products) and a harness, we tune
blind, and the failure mode is the dangerous one — it looks like it is working.

**Cost.** We already carry `langchain` + `langchain-anthropic`, and the checkout agent uses
`create_agent` with `ToolStrategy(AgentReply)`. Adding Pydantic AI means two LLM frameworks.
**Decision:** put ingestion in a separate service with its own dependencies and use Pydantic AI
there; keep LangChain for the conversational agent. They are different problems — batch extraction
worker versus interactive tool-calling loop — and the service boundary is justified regardless.
Fallback if we want no new dependency: the Anthropic SDK directly with a tool-shaped schema and a
hand-rolled retry loop (~80 lines), building the eval harness ourselves.

---

## 6. Deriving the metafield allowlist deterministically

Per store, given metafield values plus rendered markdown for sampled products:

1. **Normalise values to candidate strings, type-aware.** Plain text yields the string;
   `rich_text_field` and `json` yield leaf text nodes; `list.*` yields each element separately;
   file and metaobject references are resolved or skipped.
2. **Normalise the page:** strip script/style/comments, unescape entities, collapse whitespace,
   casefold.
3. **Score containment** of each candidate against its own product's page text. Require a minimum
   length (~12 characters or 3 tokens) so `true`, `new` and `55.00` cannot match by accident.
   **Use token-subset overlap with a threshold, not exact substring matching.** Exact matching
   would have rejected `custom.nutrients`, because the theme renders `Protein [1g]` as
   `Protein 1g`. Silently dropping good structured fields is the worst available failure mode.
4. **Aggregate per key:** hit rate = products where the key had a value and matched, over products
   where it had a value. Admit at ~0.8 with minimum support of 3 products.
5. **Chrome guard:** if a value also matches on many unrelated products' pages, it is nav or
   footer text. Demote.
6. **Contamination rejection** (highest value, fully deterministic): if a value contains a product
   GID, numeric ID, handle or `productUrl` that does not match the owning product, reject the key
   outright. This alone kills `loox.review_feed`, `stamped.reviews` and `product_seo.seo_tags`.
7. **Freshness rejection:** drop keys whose `updatedAt` lags far behind the newest write in their
   namespace.
8. **Residual pass** (inverse direction): take page segments explained by neither metafields nor
   `descriptionHtml`. Segments identical across every product of a template are **theme
   constants** (remi's `Material: BPA-free…`). Segments that vary per product are content
   unreachable via the API; their volume is the store's **coverage number**.

**Render-presence must promote, not gate.** An allowlist admitting only rendered keys would have
rejected `product_attributes`, `product_faqs` and all three `description_*` variants — the best
content on the product. Contamination and freshness do the real filtering.

**Second payoff: labels.** `custom.product_blue_content` means nothing. The page supplies the
words rendered beside the value (`Material:`, `Battery life:`, `Power:`, `Tank capacity:`).
Recovering those gives human-readable field names the API never provides.

**Output per store:** allowed keys, rejected keys with reason codes, template constants, coverage
percentage. Only the ambiguous middle needs human or model judgment, and reason codes make every
decision auditable.

### Which stores need HTML polling

Not a general requirement. General theme edits (redesign, colour, reorder) change no product
information and are site-wide. Polling is required **only** for stores whose residual pass found
theme-resident product content — because for those, a merchant revising a spec is a material
product-information change with no product-level event. On our sample that is one store in three,
and it is one page per *template*, not per product.

---

## 7. Merging: field-level provenance, not blob concatenation

Model the canonical product as a set of **field assertions**:

| attribute | example |
|---|---|
| field | `material` |
| value | `BPA-free, food-safe plastic` |
| source | `theme:template--22440936603861__product_included_K` |
| source kind | `metafield` / `theme` / `description` / `api` |
| rendered | `true` |
| observed_at | timestamp |
| source_updated_at | metafield `updatedAt` where available |
| value_hash | for change detection |

### Precedence rules

- **Commerce facts — never stored.** Price, inventory, variant availability: live API only.
- **Structured attributes — metafield wins.** Typed list beats extracted prose.
- **Present in both — metafield wins on value;** HTML contributes confirmation (raising
  confidence) and the human-readable label.
- **HTML only, identical across a template — template constant.** Store once against the
  template, attach to all its products, mark as requiring scheduled re-fetch.
- **HTML only, varying per product — per-product theme content.** The expensive case. Its volume
  is the coverage number.
- **Metafield only, never rendered — keep, but classify.** skout's `product_attributes`,
  `product_faqs` and `description_*` are the most retrieval-useful content on the product, but no
  merchant has vetted them on the storefront and they are visibly LLM-generated with hedges
  ("check the ingredient statement on the package").

### The distinction that matters most

Split every field into one of two classes:

- **Retrieval material** — feeds the embedding, used for matching, **never quoted to the
  shopper**.
- **Quotable claims** — the bot may state these as fact. Restricted to fields either rendered on
  the live page, or from a typed metafield with a recent `updatedAt`.

Unrendered enrichment goes in the first bucket only. This prevents the bot asserting an unvetted
allergen claim, which is the class of error that ends a merchant relationship.

### Conflicts and contamination

- **Conflicts — drop, do not reconcile.** 51 vs 627 vs 1193 reviews has no principled resolution
  from the data. Emit no review count rather than a plausible wrong one.
- **Contamination — hard reject** before merging, per rule 6 above.
- **Review liveness, deterministically:** `appInstallations` is closed to us, so use (a)
  `updatedAt` recency per review namespace across sampled products — on remi, `loox.review_feed`
  updated 2026-08-19 versus the Okendo aggregate at 2026-06-01; (b) DOM presence from the
  one-time render pass — on skout the Okendo widget was server-rendered and Stamped was absent;
  and (c) a hard filter that every ingested review's `productId` matches the owning product.

---

## 8. Storage: what "knowledge graph" should and should not mean

Two different things get called a knowledge graph. Conflating them is expensive.

**Meaning one — LLM-extracted triples.** GraphRAG, LightRAG,
[Graphiti](https://github.com/getzep/graphiti). Graphiti (Apache 2.0) builds a *temporal* graph
where facts carry validity windows rather than being deleted, tracks provenance to source
episodes, supports Neo4j / FalkorDB / Amazon Neptune (Kuzu deprecated), and does hybrid retrieval
over embeddings + BM25 + traversal.

**Meaning two — an explicit graph of known entities and typed edges.**

**We want meaning two, and should avoid meaning one for product facts,** because our relationships
are already explicit and typed in Shopify:

- `shopify--discovery--product_recommendation.related_products` — `list.product_reference`
- `shopify--discovery--product_recommendation.complementary_products` — `list.product_reference`
- `custom.frequently_paired_with` (remi) — `list.product_reference`
- `variety.flavors` (skout) — `list.product_reference`
- `bundle.extra_product`, `bundle.prebuilt` (skout)
- `shopify.dietary-preferences`, `shopify.allergen-information` (skout),
  `shopify.color-pattern`, `shopify.age-group`, `shopify.product-form` (remi) —
  `list.metaobject_reference`
- collection membership; variant-to-product

Running an LLM to *infer* edges we already possess would inject hallucinated relationships into a
dataset where we have ground truth. Strict downgrade.

**The `metaobject_reference` ones matter disproportionately.** Shopify's standard taxonomy
metaobjects are **shared vocabulary across stores**. Two publishers pointing at the same standard
"gluten-free" node gives cross-store attribute alignment for free — exactly the taxonomy
reconciliation problem that cross-publisher discovery would otherwise pose. Preserve these as
first-class edges even though they were empty on the two products sampled.

**Graphiti's likely future home is shopper memory, not product facts.** Its temporal model suits
"this shopper looked at cordless flossers in March, asked about braces, hesitated on price" —
which is the hesitation-detection requirement, a different graph.

### Engine choice, with a hard GCP constraint

[Apache AGE](https://age.apache.org/) is a Postgres extension giving graph storage with
Cypher-like queries alongside relational tables. **It is not available on Cloud SQL**, whose
extension set is a fixed allowlist
([Cloud SQL Postgres extensions](https://docs.cloud.google.com/sql/docs/postgres/extensions)).
AGE therefore means self-managed Postgres on GCE or GKE, plus backups, failover and upgrades.

That same allowlist **does** include **pgvector 0.8.0** (PG 13+), **pg_trgm**, and native
full-text search — i.e. dense, lexical and fuzzy retrieval in the database we already run.

[AlloyDB](https://docs.cloud.google.com/alloydb/docs/ai/work-with-embeddings) adds ScaNN, HNSW,
IVF and IVFFLAT index types plus `google_ml_integration`'s in-SQL `google_ml.embedding()`. The
caveat is that it binds embedding generation to Gemini or OpenAI endpoints and bills through the
provider. Keep embedding generation in our own code so the model stays swappable.

**Decision: start on Cloud SQL for PostgreSQL with three things in one database.**

1. **Canonical tables** — `products`, `variants`, `field_assertions`, `templates`,
   `template_constants`.
2. **An `edges` table** — `(store_id, from_type, from_id, relation, to_type, to_id, source)`.
   Every product and metaobject reference becomes a row. Depth-2/3 traversals are recursive CTEs,
   which Postgres handles at this scale.
3. **pgvector + tsvector** over a purpose-built retrieval document per product.

Defer a graph engine until a query cannot be expressed — genuinely variable-length paths, or
cross-store taxonomy walks at ~1k publishers. Neo4j on day one buys Cypher and costs a second
datastore to keep consistent, a bad trade at 250 products.

### What to embed

**Not the merged blob.** Build a deliberate retrieval document per product: title, subtitle,
product type, vendor, attribute list, allergen exclusions, use cases, FAQ text. Leave everything
we want to *filter* on — price band, dietary flags, stock, collection IDs — as columns. Filtering
in SQL is exact; filtering by embedding similarity is not.

One document per product at 50 SKUs. Where FAQ content is long, emit one chunk per FAQ entry with
a foreign key back to the product, so retrieval can return a specific answer rather than a whole
product.

### The write path

- **Idempotent upsert** on `(store_id, product_id, field, source)`. Re-running must be a no-op.
- **Content-hash gate.** Skip the write *and* the embedding call when `value_hash` is unchanged.
  This is what makes incremental re-ingestion cheap, and why metafield `updatedAt` matters.
- **Edges as a set-diff.** Insert new, delete departed. Append-only edge tables accumulate stale
  relationships silently.
- **One transaction per product** covering canonical fields, edges, document and vector, so
  retrieval never sees new text with old attributes.
- **A generation counter or `valid_from` column** so retrieval reads a consistent snapshot while a
  backfill is in flight.

---

## 9. GCP service mapping

| Stage | Service | Notes |
|---|---|---|
| Webhook ingress | **Pub/Sub** | Shopify delivers natively: URI `pubsub://<PROJECT-ID>:<TOPIC-ID>`, grant `delivery@shopify-pubsub-webhooks.iam.gserviceaccount.com` the Pub/Sub Publisher role. Shopify recommends it over HTTPS for production. Removes HMAC endpoint work for `products/*` and `inventory_levels/*`; compliance topics stay on HTTPS |
| Per-store batch | **Cloud Run jobs** | Up to 10,000 tasks, sharding via `CLOUD_RUN_TASK_INDEX` / `CLOUD_RUN_TASK_COUNT`, timeout up to 168h but **defaults to 10 minutes**, max 10 retries, explicit concurrent-task limit |
| Per-host politeness | **Cloud Tasks** | One queue per store domain. Token bucket, independent per-queue `max dispatches per second` and `max concurrent dispatches`. The right place for remi's throttling; RQ has no per-host primitive |
| Browser workload | **Separate Cloud Run job + image** | Chromium is memory-hungry; keep the GraphQL path cheap. Raise the timeout above the 10-minute default. Memory ceiling to be sized empirically |
| Raw artefacts | **GCS** (already in use) | Bulk-operation JSONL streamed straight to a bucket; raw HTML snapshots keyed by store + template + content hash. Diffing reads from storage, never re-fetches |
| Allowlist compute | **Cloud Run job**, plain Python | Trivial arithmetic at 250 products. BigQuery only if we later want cross-store analytics on key hit rates |
| Enrichment | **Claude on Vertex AI** | Opus 5, Sonnet 5, Sonnet 4.6, Haiku 4.5 listed as partner models, with separate docs for batch predictions, structured outputs and prompt caching. Caching fits the constant-prefix extraction prompt; batch fits connect-time backfill |
| Vector + relational | **Cloud SQL for PostgreSQL** | pgvector 0.8.0, pg_trgm, FTS on the supported-extension allowlist |
| Theme polling | **Cloud Scheduler → Pub/Sub → Cloud Run job** | Affected stores only, one page per template |
| Existing async | **Keep Redis + RQ** for in-request app work | Poor fit for large fan-out and per-host rate limiting; do not extend it to ingestion |

Refs:
[Pub/Sub webhook delivery](https://shopify.dev/docs/apps/build/webhooks/subscribe/get-started?deliveryMethod=pubSub),
[Cloud Run jobs](https://docs.cloud.google.com/run/docs/create-jobs),
[Cloud Tasks queues](https://docs.cloud.google.com/tasks/docs/configuring-queues),
[Claude on Vertex AI](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/partner-models/use-claude).

---

## 10. End-to-end shape

```
CONNECT (per store, once)
  Admin GraphQL  ──> metafieldDefinitions enumeration
                 ──> bulkOperationRunQuery ──> JSONL ──> GCS
  Crawl4AI       ──> N product pages ──> raw HTML + markdown ──> GCS (hashed)
                          │
                          ├─> cross-page differencing ──> boilerplate profile
                          ├─> containment scoring ──> metafield allowlist + labels
                          └─> residual analysis ──> template constants
                                                 ──> coverage %  ──> store profile

INGEST (per product)
  metafields ─────┐
  page markdown ──┼─> contamination reject ──> selector extract, LLM fallback
  descriptionHtml ┘                                    │
                                                       ▼
                              field assertions (value, source, rendered, hash)
                                                       │
                                    ┌──────────────────┼──────────────────┐
                                    ▼                  ▼                  ▼
                              canonical tables    edges table      retrieval doc
                                                  (references)      + pgvector

ANSWER TIME
  retrieval (vector + tsvector + SQL filters + edge traversal)
       └─> candidate variant IDs ──> live Admin API for price / stock / availability
```

The API path is cheap and always runs. The crawl path runs once at connect, and on a schedule only
for stores whose coverage number says the theme holds product content — one store in three on our
sample.

---

## 11. Recommended first step

Build the **connect-time analysis** before any retrieval work. Crawl4AI over all ~250 pilot pages
into GCS, then differencing and containment scoring, then produce five store profiles with
coverage numbers.

That artefact answers the questions still open — how much theme-resident content exists per store,
which metafield keys are live, what labels they carry, whether requesting `read_themes` is worth
the reauthorization cost — and every one of those answers changes what we build next. A few days
of work, and the cheapest way to stop guessing.

---

## 12. Open items

- [ ] Verify whether a metafield write fires `products/update`.
- [ ] Decide whether to request `read_themes` (cost: reauthorization across all publishers).
- [ ] Size Cloud Run memory for Chromium empirically.
- [ ] Legal read on Firecrawl's AGPL-3.0 core if it is ever adopted.
- [ ] Build the ~20-product labelled set for extraction evals.
- [ ] Confirm the merchant agreement covers automated fetching of publisher storefronts.
- [ ] Decide the review-count policy per store once liveness scoring runs on all five pilots.
