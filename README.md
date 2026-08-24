# pier39-discovery-poc

Feasibility POC for the product-discovery chatbot's catalogue ingestion: pull Shopify
metafields and rendered storefront HTML, work out where each publisher's product content
actually lives, load it into Postgres with provenance, and query it.

**Read `DESIGN.md` first.** It is the build specification and the decisions register.

The deliverable is `poc report` — the per-store profile. `poc search` exists to validate it.

---

## Setup

```bash
uv sync --extra dev
cp .env.example .env          # then fill it in
docker compose up -d db
uv run poc init-db
```

`.env` needs:

- `PIER39_SHOPIFY_TOKENS` — one JSON blob keyed by store slug, e.g.
  `{"skout":"shpat_...","remi":"shpat_..."}`. Needs `read_products`.
- `OPENAI_API_KEY` — embeddings (`text-embedding-3-large`, 1024 dims).
- `COHERE_API_KEY` — reranking. Optional; search degrades to the fused order without it.

Store settings live in `config/stores.yaml` and contain no secrets.

---

## Run it without a token

The five real skout product pages are committed as fixtures, so the two stages that
carry the novel logic can be exercised immediately:

```bash
uv run poc seed-fixtures
uv run poc profile --store skout
uv run poc report  --store skout
```

That seed is synthetic — five products, no review content — so **its coverage number is
not meaningful**. It proves the pipeline runs.

```bash
uv run pytest -q          # 42 tests against the real fixtures
```

---

## Run it with a token

```bash
uv run poc show-query                    # inspect the GraphQL before spending a call
uv run poc fetch-api  --store skout      # -> data/skout/api.jsonl
uv run poc fetch-html --store skout      # -> data/skout/pages/
uv run poc profile    --store skout      # -> data/skout/profile.json
uv run poc merge      --store skout      # -> Postgres
uv run poc index      --store skout      # -> embeddings
uv run poc report     --store skout      # the deliverable
uv run poc eval       --store skout      # recall@5
```

Or `uv run poc run --store skout` to chain everything.

Then try it:

```bash
uv run poc search "cookies without peanuts" --store skout --exclude peanut
uv run poc search "how long does the battery last" --store remi
```

Every command takes `--store` (which overrides the `enabled` flag) and most take
`--limit`. `uv run poc stores` prints the resolved config.

---

## What each stage does

| Stage | Reads | Writes |
|---|---|---|
| `fetch-api` | admin token | `api.jsonl`, `metafield_definitions.jsonl` |
| `fetch-html` | `api.jsonl` | `pages/*.html`, `pages/*.md`, `fetch_manifest.jsonl` |
| `profile` | `api.jsonl` + `pages/` | `profile.json` |
| `merge` | `api.jsonl` + `profile.json` | Postgres: assertions, edges, constants |
| `index` | Postgres | Postgres: documents + vectors |
| `search` / `eval` | Postgres + live API | stdout |

Stages never fetch on each other's behalf. That is what makes `profile` re-runnable
against saved pages while you tune thresholds.

`data/<slug>/run.json` records the **resolved** config for every stage run. A coverage
number without the threshold and page count that produced it is worthless a week later.

---

## Configuration worth knowing

`chrome_threshold` defaults to `0.8` and **must not be 1.0**. Requiring a block to
appear on every sampled page leaks store-wide sections into every product region,
because different pages omit different sections. Measured on five skout pages: at 1.0,
1,569 words survive and include the store-wide FAQ; at 0.8, 664 words survive and the
survivors are the real product region. The config validator rejects 1.0 outright.

"Pages per store" is three settings: `profile_pages` (sample used to derive the profile),
`crawl_scope` (`none` / `sample` / `template_representatives` / `all`), and `max_pages`
(hard ceiling). `crawl_scope: none` is meaningful and correct for a store whose API
already holds everything.

---

## Two things the schema deliberately does not store

**Price and inventory.** There are no such columns. They are read live at query time via
`search`, because remi carries six stale copies of its own price in metafields, one of
them labelled a saving while equal to the full price.

**Anything unvetted as quotable.** Every assertion is `quotable` or `retrieval`.
Retrieval material feeds the embedding and matching but must never be quoted to a
shopper. Unrendered LLM-generated enrichment lands in `retrieval` only.

---

## Status

Verified without a token, against real fixtures:

- block differencing, with the measured thresholds (`tests/test_blocks.py`)
- value normalisation, token-subset matching, contamination detection (`tests/test_matching.py`)
- the whole `profile` stage, covering DESIGN.md criteria 3 and 4 (`tests/test_profile.py`)

Not yet verified — needs a token, or a real run:

- `fetch-api` against a live shop, including whether `Product.templateSuffix` exists
- `fetch-html` escalation against remi's Cloudflare interstitial
- criterion 5, label recovery: no labels were recovered from the skout seed. remi is the
  store with `Material:` / `Battery life:` labels, so this needs remi's pages
- whether OpenAI returns unit-normalised vectors (`index` measures and logs it)
- `merge`, `index`, `search`, `eval` end to end against Postgres

See `DESIGN.md` section 12 for the open items.
