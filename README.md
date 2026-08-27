# pier39-discovery-poc

Feasibility POC for the product-discovery chatbot's catalogue ingestion: pull Shopify
metafields and rendered storefront HTML, work out where each publisher's product content
actually lives, load it into Postgres with provenance, and query it.

The deliverable is `poc report` — the per-store profile, headed by which attributes each store
can answer and from which source. `poc search` exists to validate it.

**No current measurement is written down in this repository.** Numbers go stale the moment the
pipeline is re-run, so they are printed on demand instead. Run the commands below and read the
output.

## Documentation

| read | for |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | where the code lives, the layer rules, "where do I change X" |
| [`docs/DESIGN.md`](docs/DESIGN.md) | the build specification: success criteria, per-stage algorithms, schema |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | settled decisions and the empirical facts this build rests on |
| [`docs/PENDING.md`](docs/PENDING.md) | unfinished work, with the options and tradeoffs for each |
| [`docs/reference/`](docs/reference/) | per-module gotchas and the measurements behind them |
| [`docs/archive/`](docs/archive/) | historical: superseded notes and completed work orders |

Start with `ARCHITECTURE.md` if you are changing code, `DESIGN.md` if you are changing behaviour.

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
- `PIER39_SHOPIFY_STOREFRONT_TOKENS` — optional, same shape. Used for the answer-time price and
  stock read. Without it that read falls back to the Admin API, which has no market context and
  shares the ingestion rate-limit bucket.

Non-secret settings live in `config/stores.yaml`; per-store label reference sets live in
`config/spec_labels/<slug>.yaml`.

## Run it without a token

The five real skout product pages are committed as fixtures, so the two stages carrying the
novel logic can be exercised immediately:

```bash
uv run poc seed-fixtures
uv run poc profile --store skout
uv run poc report  --store skout
uv run pytest -q
```

That seed is synthetic — five products, no review content — so **its coverage number is not
meaningful**. It proves the pipeline runs.

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

Or `uv run poc run --store skout` to chain everything. `uv run poc --help` lists every command.

## Querying it

Retrieval has two paths, because they answer different questions:

```bash
uv run poc search "a fruity snack bar for a toddler" --store skout   # discovery
uv run poc facts water-flosser --store remi                          # product already known
```

`search` ranks the catalogue. `facts` takes a product you already have — from a product page, or
the previous turn — expands it to its family, and returns what is quotable about it. It embeds
nothing and makes no model call.

```bash
uv run poc search "cookies without peanuts" --store skout --exclude peanut
uv run poc search "protein bar" --store skout --max-price 30 --in-stock
uv run poc search "protein bar" --store skout --no-group
uv run poc eval --store skout --compare-rerank
```

Three behaviours worth knowing before you read a result:

- **`--exclude` is a whitelist join, not an exclusion scan.** A product with no free-from
  declaration is not an answer to a negation query. For allergens `facts` answers in three
  states, never an empty list: declared free of it, declares and does not list it, or **no
  declaration at all** — the last must never be reported as "free of it".
- **`--max-price` and `--in-stock` filter on a live read**, because price and stock are never
  stored. They reject anything whose price could not be confirmed, so an empty result may mean
  the lookup failed. Search says so when that happens.
- **Duplicate listings are collapsed** into a single result with its other listings shown
  beneath, so five slots hold five products rather than five spellings of one bar. `--no-group`
  turns that off.

Every command takes `--store`, which overrides the `enabled` flag; most take `--limit`.

## Trying the answer layer by hand

```bash
uv run poc chat --store remi                     # discovery mode
uv run poc chat --store remi --product water-flosser
uv run poc chat-replay --store remi --limit 8    # same answer function, batch
```

Each turn prints the answer, every assertion the model was shown with its id and tier, which ids
it cited, whether the answer is grounded, and the retrieval diagnostics. In the REPL,
`/product <handle>` scopes to a product, `/discovery` clears it, `/quit` leaves. `--no-facts` and
`--no-diagnostics` quieten it; `--model` compares models.

The model must cite an assertion id for every claim, and each citation is checked in code: the id
must exist, have been shown, and be `quotable`.

## Stages and models

| stage | reads | writes |
|---|---|---|
| `fetch-api` | admin token | `api.jsonl`, `metafield_definitions.jsonl` |
| `fetch-html` | `api.jsonl` | `pages/*.html`, `fetch_manifest.jsonl` |
| `profile` | `api.jsonl` + `pages/` | `profile.json` |
| `merge` | `api.jsonl` + `profile.json` + `spec_labels/` | Postgres: assertions, edges, constants |
| `index` | Postgres | Postgres: documents + vectors |
| `search` / `facts` / `eval` | Postgres + live API | stdout |
| `labels` / `compare-labels` | `profile.json` | stdout |
| `chat` / `chat-replay` | Postgres + OpenAI | REPL / groundedness |

| what | setting | default |
|---|---|---|
| embeddings | `embedding_model` in `config/stores.yaml` | `text-embedding-3-large`, 1024 dims |
| reranking | `rerank_model` | `ms-marco-MiniLM-L-12-v2` — local ONNX cross-encoder via `flashrank`. No key, no network at query time once cached |
| label classifier | `PIER39_LABEL_MODEL` | `gpt-5.5` |
| chat and replay | `PIER39_CHAT_MODEL`, or `--model` | `gpt-5.5` |

No model is called during ingestion: `merge` defaults to the deterministic `static` label
policy. `merge --label-policy none|static|llm` selects the gate; see
[`docs/reference/ingest.md`](docs/reference/ingest.md).

## Two things the schema deliberately does not store

**Price and inventory.** There are no such columns, and prices held in metafields are rejected
too. One derived read is permitted at ingestion — a sellability verdict per product. The verdict
is stored; neither input is. It exists because 31 of skout's 184 published products are
abandoned records priced 0.00.

**Anything unvetted as quotable.** Every assertion is `quotable` or `retrieval`, and each product
gets a separate document per class so the distinction survives into whatever an answer layer
reads. Quotability is decided by type and shape, not by whether the value renders: the page is
rendered *from* the metafield, so a match proves the theme consumed the key and nothing about
whether a merchant vetted it.

## Status

All eight criteria in `docs/DESIGN.md` §2 pass against live runs on both stores.

Two items are worth knowing: the **reranker** was measured on 2026-08-26 and earns nothing (the
delta was below the metric's resolution on both stores; `docs/PENDING.md` §2 has the numbers),
and the **Storefront read** has never executed for want of a token, so live reads use the Admin
fallback. Both report their own failure instead of degrading in silence.
