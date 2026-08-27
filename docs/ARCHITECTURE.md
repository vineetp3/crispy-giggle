# Architecture

Where things live and why. Read this first if you are new to the repo; read
`docs/reference/` when you need to change a specific rule.

## Layers

Dependencies flow strictly downward. `core` imports nothing; nothing imports `cli`.

| layer | holds | may import |
|---|---|---|
| `core/` | pure domain logic and shared shapes: `models`, `tuning`, `quotability`, `blocks`, `matching`, `families`, `attributes`, `documents` | nothing |
| `infra/` | anything that talks to the outside: `config`, `db`, `artifacts`, `shopify_api`, `embeddings`, `llm` | `core` |
| `ingest/` | the pipeline stages: `crawl`, `labels`, `profiling`, `merge`, `indexing` | `core`, `infra` |
| `retrieval/` | answering a query: `search`, `answering` | `core`, `infra` |
| `evaluation/` | measurement harnesses, not product: `harness`, `chat` | `core`, `infra`, `ingest`, `retrieval` |
| `presentation/` | everything that formats for a human: `console`, `render`, `report` | all of the above |
| `cli/` | thin Typer orchestration, one module per command group | all of the above |

`ingest/` and `retrieval/` are independent siblings: neither may import the other.

No module in `core/` may read config, touch the network, or reach Postgres. That is what
keeps it unit-testable and is checked, not merely intended: the table above is declared as an
import-linter `layers` contract in `pyproject.toml` and enforced by `uv run lint-imports`.
The contract is `exhaustive`, so adding a subpackage without placing it in the order above
fails the check rather than going unnoticed.

The current graph is tighter than the table permits — `evaluation` imports only `retrieval`,
and `presentation` imports neither `evaluation` nor `retrieval`. The table is the policy, not
a snapshot; narrowing it is a decision to make deliberately, not by accident.

## The pipeline

Every stage reads the previous stage's files from disk and writes its own. No stage fetches
on another's behalf, which is what makes `profile` re-runnable fifty times against saved
pages while you tune thresholds.

```
fetch-api  --> data/<slug>/api.jsonl            (+ metafield_definitions.jsonl)
fetch-html --> data/<slug>/pages/*.html         (+ fetch_manifest.jsonl)
profile    --> data/<slug>/profile.json         reads api.jsonl + pages/
merge      --> Postgres: assertions, edges, constants
index      --> Postgres: documents + vectors
search / facts / eval / report                  read Postgres (+ live API)
```

`data/<slug>/run.json` records the **resolved** config for every stage run. A coverage
number without the threshold and page count that produced it is worthless a week later.

## Where do I change X?

| to change | edit |
|---|---|
| a threshold — any of the 28 | `core/tuning.py`, or override `tuning:` in `config/stores.yaml` |
| whether a value may be quoted to a shopper | `core/quotability.py` |
| how chrome is stripped from a page | `core/blocks.py` |
| which metafield keys are admitted | `ingest/profiling.py` |
| how a rendered `Label: Value` pair is classified | `ingest/labels.py` (policy protocol) |
| how duplicate listings collapse | `core/families.py` |
| ranking, fusion, filtering | `retrieval/search.py` |
| anything printed to a terminal | `presentation/render.py` or `presentation/report.py` |
| a per-store operational setting | top-level keys in `config/stores.yaml` |

## Configuration: two kinds, one rule

`config/stores.yaml` top-level keys are **operational** — crawl budget, fetch profile, API
version, model names. The `tuning:` block is **algorithm thresholds**: change one and the
same catalogue yields a different answer. Defaults and grouping live in `core/tuning.py`;
per-store overrides merge one level deep, so overriding `tuning.retrieval.rrf_k` leaves
every other group intact.

Values that name a taxonomy rather than set a level — `PROSE_TYPES`, `WIDGET_MARKERS`,
`ALWAYS_EXCLUDE`, `TRUST_CLASSES` — are **not** knobs. They stay next to the logic that
reads them; moving them to YAML would make the code harder to follow, not easier.

Secrets never appear in `stores.yaml`. Tokens come from one JSON blob keyed by store slug,
so adding a store is a config edit plus a token edit and nothing more.

## Two conventions that look like accidents

**Function-local imports in `cli/` and `retrieval/search.py` are deliberate.** `flashrank`
pulls in onnxruntime and numpy, and importing it at module scope makes `poc --help` slow for
every command that never reranks. Do not hoist them to the top of the file.

**`product` means two different things.** In `ingest/` and `infra/` it is a `core.models.Product`
— a catalogue record from `api.jsonl`. In `core/documents.py` and `ingest/indexing.py` it is a
Postgres row (`SELECT p.*`), which is why those two still take plain dicts.

## Verification

```bash
uv run pytest -q                 # the test suite
uv run ruff check src tests
uv run lint-imports              # the layer contract in pyproject.toml
```

One further property, which no linter can check:

- **Output stability.** `merge` and `profile` have no unit tests. The real regression net is
  running the pipeline on a real catalogue and diffing `profile.json` plus the output of
  `report`, `labels`, `facts`, `search` and `eval` against a saved copy. `profile.json` is
  byte-reproducible for a fixed input, so any diff is a real change.

Two known cosmetic nondeterminisms will show up in such a diff and are **not** regressions:
`matching.detect_foreign_product_ids` iterates a `set()` of id strings, so which contaminated
id its diagnostic names varies per process; and `search.assertions_for` orders only by
`(trust_class, field)`, so assertions tying on `field` come back in physical row order, which
shifts whenever `merge` re-inserts them.
