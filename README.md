# pier39-discovery-poc

Feasibility POC for the product-discovery chatbot's catalogue ingestion: pull Shopify
metafields and rendered storefront HTML, work out where each publisher's product content
actually lives, load it into Postgres with provenance, and query it.

**Read `docs/DESIGN.md` first.** It is the build specification and the decisions register.
`docs/FINDINGS.md` records what the live runs measured, and `docs/PENDING.md` is the unfinished
work with the options and tradeoffs for each.

The deliverable is `poc report` — the per-store profile, headed by which attributes each store
can answer and from which source. `poc search` exists to validate it.

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
- `COHERE_API_KEY` — reranking. Optional; search degrades to the fused order without it, and
  that degradation is silent. `poc eval --compare-rerank` reports when the reranker did not run.
- `PIER39_SHOPIFY_STOREFRONT_TOKENS` — optional, same JSON-blob shape. Used for the answer-time
  price and stock read. Without it that read falls back to the Admin API, which has no market
  context and shares the ingestion rate-limit bucket.

Store settings live in `config/stores.yaml` and contain no secrets.

`config/spec_labels/<slug>.yaml` holds the per-store label reference set: for each label the
theme renders as a `Label: Value` pair, whether it is a product **spec**, a storefront
**widget** such as a variant or subscription picker, or **uncertain**. Only `spec` may become
quotable; `uncertain` is stored as retrieval, findable but never repeated to a shopper as
fact; `widget` is not stored. The distinction cannot be made globally — skout's `Pack Size`
is a variant picker and remi's `Quantity` is how many tablets are in the box. See
`docs/FINDINGS.md` §7.

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
uv run pytest -q          # 131 tests against the real fixtures
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
uv run poc search "protein bar" --store skout --max-price 30 --in-stock
uv run poc search "protein bar" --store skout --no-group
uv run poc eval --store skout --compare-rerank
```

Retrieval has two paths, because they are different questions:

```bash
uv run poc search "a fruity snack bar for a toddler" --store skout   # discovery
uv run poc facts water-flosser --store remi                          # product already known
uv run poc facts peanut-butter-protein-bar --store skout --exclude peanut
```

`search` ranks the catalogue. `facts` takes a product you already have — from a product page, or
the previous turn — expands it to its family, and returns what is quotable about it. It embeds
nothing and makes no model call. For allergens it answers in three states, never an empty list:
declared free of it, declares and does not list it, or **no declaration at all** — the last must
never be reported as "free of it".

`--exclude` is a whitelist join, not an exclusion scan: a product with no free-from declaration
is not an answer to a negation query. `--max-price` and `--in-stock` filter on the live read,
because price and stock are never stored — and they reject anything whose price could not be
confirmed, so an empty result may mean the lookup failed. Search says so when that happens.

Duplicate listings of one product are collapsed into a single result with its other listings
shown beneath it, so five slots hold five products rather than five spellings of one bar.
`--no-group` turns that off. Quotable facts are printed with the date their source was last
updated.

Every command takes `--store` (which overrides the `enabled` flag) and most take
`--limit`. `uv run poc stores` prints the resolved config.

---

## What each stage does

| Stage | Reads | Writes |
|---|---|---|
| `fetch-api` | admin token | `api.jsonl`, `metafield_definitions.jsonl` |
| `fetch-html` | `api.jsonl` | `pages/*.html`, `pages/*.md`, `fetch_manifest.jsonl` |
| `profile` | `api.jsonl` + `pages/` | `profile.json` |
| `merge` | `api.jsonl` + `profile.json` + `spec_labels/` | Postgres: assertions, edges, constants |
| `index` | Postgres | Postgres: documents + vectors |
| `search` / `eval` | Postgres + live API | stdout |
| `labels` | `profile.json` | stdout: the label inventory to hand-label |
| `compare-labels` | `profile.json` + OpenAI | stdout: classifier scored against the reference set |
| `chat` | Postgres + OpenAI | REPL; turns to `data/<slug>/chat_turns.jsonl` |
| `chat-replay` | Postgres + OpenAI | groundedness over the eval questions |

### Models

| what | setting | default |
|---|---|---|
| embeddings | `embedding_model` in `config/stores.yaml` | `text-embedding-3-large`, 1024 dims |
| reranking | `rerank_model` in `config/stores.yaml` | `rerank-v4.0-fast` — has never run |
| label classifier | `PIER39_LABEL_MODEL` | `gpt-5.5` |
| chat and replay | `PIER39_CHAT_MODEL`, or `--model` | `gpt-5.5` |

Reasoning effort is `low` everywhere and is not yet exposed as a flag. No model is called
during ingestion: `merge` defaults to the deterministic `static` label policy.

### Trying it by hand

```bash
uv run poc chat --store remi                     # discovery mode
uv run poc chat --store remi --product water-flosser
```

Each turn prints the answer, every assertion the model was shown with its id and tier, which
ids it cited, whether the answer is grounded, and the retrieval diagnostics. In the REPL,
`/product <handle>` scopes to a product, `/discovery` clears it, `/quit` leaves. Add
`--no-facts` or `--no-diagnostics` to quieten it, `--model` to compare models.

The model must cite an assertion id for every claim, and each citation is checked in code:
the id must exist, have been shown, and be `quotable`. Answers carrying no citations are
reported separately rather than scored, because a correct refusal and an unsupported
assertion are not distinguishable without knowing the question was answerable.

```bash
uv run poc chat-replay --store remi --limit 8    # same answer function, batch
```

Drop `--limit` for the full set; that is 42 model calls on remi.

`merge --label-policy none|static|llm` selects the label gate. `static` is the default and
reads the reference set. `llm` classifies each distinct label once, caches the verdict in
`data/<slug>/label_verdicts.json` keyed by model, and is off by default. `gpt-5.5` (the
default, override with `PIER39_LABEL_MODEL`) reproduced every confident judgement in both
reference sets; `gpt-4o-mini` did not. It stays off because a reviewed file beats a model
call for two stores someone has already read. `none` reproduces behaviour before the gate existed
and exists as a control.

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

**Price and inventory.** There are no such columns, and prices held in *metafields* are
rejected too. remi carries six stale copies of its own price across six keys, plus one
labelled a saving while equal to full price. One derived read is permitted at ingestion —
a sellability verdict per product, from `availableForSale` and `price`. The verdict is
stored; neither input is. It exists because 31 of skout's 184 published products are
abandoned records priced 0.00.

**Anything unvetted as quotable.** Every assertion is `quotable` or `retrieval`, and each
product gets a separate document per class so the distinction survives into whatever an
answer layer reads. Quotability is decided by type and shape, not by whether the value
renders: the page is rendered *from* the metafield, so a match proves the theme consumed
the key and nothing about whether a merchant vetted it.

---

## Status

All eight criteria in `docs/DESIGN.md` §2 pass against live runs on both stores. Measured
results, per-store profiles and data-quality findings are in `docs/FINDINGS.md`.

Two things have never executed, and every number should be read with them in mind:

- **the reranker** — `COHERE_API_KEY` is a placeholder, so all recall figures are RRF only.
  Whether a reranker belongs in v0 at all is still open
- **the Storefront read** — no storefront token, so live reads used the Admin fallback

Both now report their own failure instead of degrading in silence.

See `docs/DESIGN.md` §10 for the open items, and its "Closed since the 2026-08-25 runs" list for
what has been resolved.
