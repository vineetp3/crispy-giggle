# Pending work

**Last updated:** 2026-08-25.

The working list of unfinished work, with the evidence behind each item and the options
considered. Written to be picked up by someone with no prior context.

`docs/DESIGN.md` §10 is the terse open-items register and stays authoritative for *whether* an
item is open. This document carries the *why*, the *options* and the *tradeoffs*. When an item
closes, close it in both.

---

## Orientation

A feasibility POC for a product-discovery chatbot's catalogue ingestion. For each Shopify store it
pulls the Admin API catalogue, crawls the rendered storefront pages, works out where each
publisher's product content actually lives, loads it into Postgres with provenance, and queries
it. Two pilot stores: `skout` (snack bars, content mostly in metafields) and `remi` (dental
products, content almost entirely in the page theme).

Pipeline, one CLI subcommand per stage:
`fetch-api → fetch-html → profile → merge → index`, then `search` / `facts` / `report` / `eval`.

```bash
docker compose up -d db
uv run poc report --store remi     # the deliverable; reads the existing database
uv run poc eval   --store remi
uv run pytest -q                   # 90 tests, no token needed
```

`.env` holds `PIER39_SHOPIFY_TOKENS` and `OPENAI_API_KEY`. `COHERE_API_KEY` is a placeholder.
`PIER39_SHOPIFY_STOREFRONT_TOKENS` is unset.

**State as of this writing** — all uncommitted on `main`:

| | skout | remi |
|---|---|---|
| Products indexed | 171 | 48 |
| Assertions (quotable / retrieval) | 2,185 (1,473 / 712) | 636 (384 / 252) |
| Theme-sourced assertions | 118 | 73 |
| Attribute reachability | api 4, theme 2, image 3, absent 4 | theme 6, absent 4 |
| Discovery recall@5 | 0.92 (23/25) | 1.00 (22/22) |
| Scoped answerability | 0.87 (20/23) | 0.65 (13/20) |
| Constraint violations | 0 | 0 |

---

## 1. Theme spec extraction — the label gate *(highest priority, direction agreed)*

### The problem

Facts plainly visible on a product page are not becoming queryable facts. Four distinct causes
were found; two are fixed, two are not.

**Fixed already:**

- Group chrome was discarded before template-constant extraction, so any template group with two
  or more crawled pages lost its entire spec table.
- A spec rendered as a single text run (`Material: Dental-grade polymer`) could not be read,
  because `blocks.label_for` only handles a label node followed by a value node.
  `blocks.inline_label` now handles the first shape.

**Still open:**

**1a. The not-a-label denylist is global and store-blind.** `blocks.NOT_A_LABEL_PATTERNS` rejects
any label matching `select|choose|pick|enter|search|filter|sort|quantity|qty`, to keep storefront
widgets out. On `deep-clean-freshening-tablets` the page renders:

```
<p><strong>Quantity:</strong> 120 tablets (roughly 4 months of daily use)
```

The pair extracts cleanly and is thrown away because `quantity` is denylisted. It is a cart widget
on most stores and a real spec on this one.

**1b. Per-product theme specs are never stored at all.** `merge.py` writes theme assertions only
from **template constants** — spec pairs shared across every crawled page of a template group (the
loop at `merge.py:219`). A spec appearing on a single product's page is classed as per-product
theme content, counted toward coverage, and discarded. On remi this loses 9 real pairs:

```
mouth-night-guard-removal-tool   Material           = Food-grade material, BPA-free, phthalate-free
night-guard-super-bundle         Material           = 10% Hydrogen Peroxide, BPA-free, phthalate-free
night-guard-super-bundle         Treatment duration = On-going, use nightly
uv-toothbrush-sanitizer          Kill rate          = 99.9% of bacteria
deep-clean-freshening-tablets    Compatible with    = Night guards, retainers, aligners, dentures
```

### Why it cannot simply be relaxed

Extraction is not the weak part — it finds 69 labelled pairs on remi and 303 on skout. The gate
deciding which become facts is, and it is wrong in both directions. Storing per-product pairs
naively floods skout, whose 222 unstored pairs are dominated by subscription widgets:

| label | count |
|---|---|
| This item | 113 |
| Pack Size | 53 |
| Delivery Frequency | 50 |
| everything else | 6 |

A single global regex cannot separate "product spec" from "storefront widget" across two themes.
`Quantity` is a widget on most stores and a spec here; `Pack Size` sounds like a spec and is a
variant picker. Same conclusion `families.family_key` reached against a third store (item 6) —
the rules are store-shaped.

### Options

**A — Per-store label allow/deny lists in `config/stores.yaml`.** *(agreed direction)*
Each store gets `spec_labels.allow` / `spec_labels.deny`, applied on top of the global guards.
Cheap, deterministic, inspectable, reversible per store.
*Tradeoff:* manual work per store, and it concedes the deterministic rules do not generalise —
which the POC was partly built to test. Accepted explicitly: attributes and their labels differ
per store, so per-store configuration is expected rather than a workaround.

**B — Store per-product spec pairs as `retrieval` by default, promote to `quotable` only for
allow-listed labels.** *(agreed direction)*
Keeps the material findable by search without risking a widget being quoted to a shopper as fact.
Pairs naturally with A: the allow list is what promotes.
*Tradeoff:* grows the retrieval corpus and the embedding cost; a real spec on an unlisted label
stays unquotable until someone adds it.

**C — One LLM pass classifying candidate `label: value` pairs as spec vs widget.** *(under
consideration, not decided)*
A model would trivially separate `Quantity: 120 tablets` from a cart picker, and `This item:` from
`Material:`. The only option that generalises to a store nobody has looked at.
*Tradeoff:* `DESIGN.md` §9 puts LLM extraction out of scope for v0 and §3 says such decisions are
not to be relitigated casually, so this reopens a settled one. It adds a model dependency to the
ingestion path, needs its own eval to show it beats the regex, and introduces non-determinism into
a stage whose current virtue is reproducibility. Mitigation if adopted: classify only the *label*,
cache by `(store, label)`, and keep the deterministic guards as a floor — the model can only ever
demote, never promote past the commerce and quotability checks.

### Recommended order

A and B together first; they are agreed and independent of C. Handle `quantity` as part of A
rather than as a special case: drop it from the global denylist when the value is not a bare
integer. Then re-measure scoped answerability before deciding on C — if A+B closes most of the
gap, C becomes a generalisation question rather than an accuracy one.

### How to verify

```bash
uv run poc profile --store remi && uv run poc merge --store remi && uv run poc index --store remi
uv run poc facts deep-clean-freshening-tablets --store remi --no-live
uv run poc facts mouth-night-guard-removal-tool --store remi --no-live --attribute materials
uv run poc eval --store remi    # scoped answerability should rise from 0.65
uv run poc eval --store skout   # must NOT fall; watch for widget labels turning quotable
```

Regression guards: 0 promotional theme facts quotable (`profile.is_commerce_constant`), and
`This item` / `Pack Size` / `Delivery Frequency` must never appear as quotable on skout.

**Current scoped answerability numbers are a floor, not a measurement.** At least two of remi's
seven failures are extraction defects rather than missing content.

---

## 2. Reranking — deferred, and undecided

`COHERE_API_KEY` is the placeholder `xxx`, Cohere returns 401, and every recall figure ever
recorded is RRF only. `search` now reports the failure instead of hiding it, but reporting is not
deciding.

`DESIGN.md` §10 holds the decision rule, the finding that OpenAI has no rerank endpoint, and a
candidate table weighed against the constraint that there is no `torch` in the venv. Not repeated
here.

Two things changed since that rule was written, both arguing for re-measuring first:

- Duplicate collapse means the top-5 now holds distinct products rather than several spellings of
  one bar. A cross-encoder could never have fixed that crowding, so an A/B before the collapse was
  measuring the wrong thing.
- Scoped questions no longer touch the ranking path at all. If most production traffic is
  product-scoped, a reranker only ever affects discovery queries, which narrows its case.

The ONNX cross-encoder option makes `--compare-rerank` runnable with no vendor account, which is
the cheapest way to get a number.

---

## 3. Chat layer — agreed shape, not started

Purpose is to strengthen the evals, not to ship a product. The end product is a storefront chatbot
needing its own service; this is a playground.

**Agreed shape: A + D over one shared answer function.**

- **A — CLI REPL, single grounded call.** `poc chat --store remi [--product <handle>]`. Each turn
  calls the existing retrieval, builds a prompt with quotable and retrieval material clearly
  separated, makes one model call, prints the answer alongside the assertions it was given, and
  logs the turn to JSONL.
- **D — Batch replay harness.** Runs a list of questions through the *same* answer function and
  scores groundedness automatically.

Building the answer function once means the REPL and the harness cannot disagree about what the
model was shown.

Two details that make it pay off:

- Log turns in the same YAML shape as `config/questions/*.yaml`, so promoting a good turn into the
  eval set is a copy rather than a translation. This also fixes the context problem that motivated
  the scoped/discovery split: a playground holding a product in scope produces questions with the
  scope attached.
- Have the model cite assertion ids. Groundedness then becomes checkable in code — verify each
  cited id exists, belongs to that product, and is `quotable` — turning a subjective judgement into
  a countable number. Nothing currently measures this, and it is the actual product risk the whole
  quotable/retrieval split exists to manage.

**Rejected for now: an agentic REPL with tools.** It would test whether a model routes correctly
between scoped and discovery, useful later, but it adds a second failure mode on top of the one
being measured and makes every bad answer ambiguous between bad routing and bad retrieval.
Revisit once A+D has a baseline.

**Boundary to keep:** `DESIGN.md` §9 rules out LLM extraction in the ingestion pipeline. A
playground does not violate that, but it must live in its own module with its own optional
dependency and never be imported by an ingest stage, so the v0 line stays honest.

Provider: only `OPENAI_API_KEY` is present and chat-capable. Claude would need a key added.

**Routing does not exist and is deliberately absent from retrieval.** In production the product
identity arrives as structured context from the surface (a product page, or the previous turn),
not inferred from query text. Resolving "it" from history, and deciding to break scope when a
scoped query is really a discovery one, belong to the answer layer, which holds the conversation.
Today the question file declares the mode via its `scope` field.

---

## 4. Storefront access token — never executed

`PIER39_SHOPIFY_STOREFRONT_TOKENS` is unset, so every live price and stock read has used the Admin
fallback. Blocked on a credential, not a decision.

Price and inventory are deliberately never stored — no columns — because remi carries six stale
copies of its own price across six metafields, one labelled a saving while equal to full price.
They are read live at query time for the few products a search is about to return.

Three reasons the Storefront client is preferred over the Admin fallback, all currently unverified:

- **Market-correct pricing.** The Storefront query is wrapped in `@inContext(country:)` driven by
  `market_country`; the Admin query has no market context and returns the shop's base price. Under
  Shopify Markets that is the wrong number outside the base market. Storefront also returns an
  explicit `currencyCode`; Admin returns a bare scalar.
- **Separate rate-limit bucket.** Admin shares the ingestion bucket, so shopper queries compete
  with crawls.
- **Availability semantics.** Storefront returns `availableForSale` as a buyer sees it; Admin
  returns internal `inventoryQuantity`. remi runs continue-selling with 23 of 30 products at
  negative quantity and all buyable, so the internal number is actively misleading there.

Not a blocker for anything else. Only matters for `--max-price`, `--in-stock`, and quoting a live
price.

---

## 5. Quotability decay — deferred, and contraindicated as an age rule

Assertions carry `source_updated_at` and nothing expires. Two separate reasons it stays open; the
second is the important one.

It needs `first_observed_at` / `last_confirmed_at` / `withdrawn_at` and a re-confirmation loop,
meaningless without incremental re-ingestion (out of scope per §9).

**An age cliff is the wrong mechanism and would make things worse.** On skout, `free_from` averages
719 days across 130 products and reaches 1,098; `descriptors.subtitle` 956; `filter.curated` 865.
A one-year cutoff empties the quotable set, because that is simply how old this store's data is.
Age does not separate stale from stable-and-still-correct — ingredients and materials do not
change. Only re-confirmation can. **Do not reopen this as an expiry date.**

What v0 does instead: `search` and `report` render the source date on every quotable fact, so a
two-year-old allergen declaration is visibly two years old. Whether that is acceptable to repeat to
a shopper is a business decision and belongs with whoever owns that risk.

---

## 6. Third-store validation — proposed, not started

Several rules are tuned on n=2 and are the most likely to be overfit: `chrome_threshold` 0.8, the
3-page/20-word cross-page rule, `allowlist_min_hit_rate` 0.8, and above all
`families.family_key`, whose vendor-prefix and `- Bundle` / `N Pack` suffix conventions came
entirely from skout.

**Much of this is testable with no credentials.** Shopify serves the catalogue publicly.
`countrylifefoods.com` is already in `config/stores.yaml` (disabled, `crawl_scope: none`) and
returns HTTP 200 on `/collections/all/products.json?limit=250` — 1.18 MB, structurally unlike
either pilot store: a distributor with 70 distinct vendors in 250 products.

Running `family_key` against it already produced a keepable result:

- 250 products collapse to 248 families. Only 2 collapsed, both genuine exact-title duplicates.
  **Zero false merges** on a store the rule never saw.
- It misses that store's duplicate convention entirely. `1-to-1 Baking Flour, Gluten-Free, 25 lb,
  Bob's Red Mill` (LIPARI FOODS) and `1-to-1 Baking Flour, Gluten-Free, Bob's Red Mill`
  (KEHE - STORE) get different keys, because the size sits mid-title rather than as a trailing
  suffix. Arguably correct — a 25 lb sack and a retail bag are different purchasable items — but
  it confirms the rule is store-shaped.

**Token-free half validates:** chrome differencing, the cross-page boilerplate rule, family
grouping, title-based contamination detection. Most of the novel logic.

**Needs an admin token:** the metafield allowlist, quotability classification, and attribute
reachability — the actual deliverable.

To start the token-free half, flip `countrylife` to `enabled: true` with `crawl_scope: all`. Worth
doing before the reranker decision, since a third store may move recall more than reranking would.

---

## 7. Smaller items

- **`products/update` webhook on a metafield write.** Unknown whether it fires. Irrelevant to v0,
  required before any incremental re-ingestion design.
- **Eval table has no mode column.** Scoped rows are identifiable only because the handle is
  appended to the question text. An explicit column would be clearer.
- **`poc facts` has no `--why`.** It reports that an attribute is answerable without showing which
  assertion satisfied it. Useful when debugging the label gate in item 1.
- **Verify the skout assertion-count attribution.** The drop from 2,440 to roughly 2,080 after the
  full crawl was attributed in `FINDINGS.md` to sampling bias being removed — a larger hit-rate
  denominator. That was written before the group-chrome bug in item 1 was found, and that bug also
  suppressed assertions. The split between the two causes has not been measured.
- **`ruff` is not a project dependency** despite a `.ruff_cache` in the tree. There is no lint
  step. Either add it to the dev extra or delete the cache directory.

---

## 8. Repository state

The code work described above is committed as `e74427d` — "feat: update harness with separate
search paths and tests" — on `main`. That commit carries the full-catalogue crawl, the family
collapse, retrieval diagnostics, fact ages, the scoped/discovery split, and the two theme-spec
fixes listed as done in item 1.

Uncommitted at the time of writing: `README.md`, `docs/DESIGN.md`, `docs/FINDINGS.md` and this
file, all documentation.

The Postgres database holds the measured state the numbers above describe, including the crawl of
152 skout pages. It is a Docker volume, not in the repo — `docker compose up -d db` brings it
back, but a `docker compose down -v` would destroy it and require a full re-crawl and re-embed to
reproduce.
