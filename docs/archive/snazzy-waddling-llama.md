> **ARCHIVED — completed work order.** The library-offload work described here was done.
> Kept for the decisions it records. Current state: `docs/ARCHITECTURE.md` and
> `docs/reference/`.

# Offload hand-rolled code to libraries — Tier 1 + Tier 2

## Context

A scan of `pier39-discovery-poc` found that ~9% of its 6,357 source lines reimplement
things a library already does. The other ~91% is store-specific judgement (chrome
differencing, the metafield allowlist, the label gate, quotability classification) that no
library encodes and that this work must not touch.

Two of the offloads are not merely tidying. `search._rerank` is Cohere-only, has never
executed, and blocks the single largest open decision in `DESIGN.md` §10 — whether a
reranker belongs in v0 at all. `llm.complete` tries three request shapes and records which
one won nowhere, which is `PENDING.md` §3c and the reason two chat-replay runs are not
necessarily comparable. Both are closed here.

Intended outcome: fewer hand-rolled paths, `--compare-rerank` runnable with no vendor
account, and request-shape reporting — with every existing measurement reproducible.

## Decisions taken

| Question | Call |
|---|---|
| `ranx` (~300 MB: numba, pandas, seaborn, fastparquet, ir-datasets) and `litellm` (pins `openai<3.0.0`; 3.3.1 installed) | **Take them.** Weight and the openai downgrade were raised and accepted. |
| Reranker backend | **`flashrank` direct** — torch-free, onnxruntime, ~34 MB model. No `rerankers` wrapper layer. |
| `undeclared_returns` vacuous-SQL bug | **Out of scope.** Separate task, so any metric movement here is unambiguously attributable to a library swap. |
| `config.py` migration depth | **Full** — `StoreConfig` → pydantic v2 model, env/secrets → `pydantic-settings`. |

## Explicitly out of scope

- The `undeclared_returns` LEFT JOIN bug (tracked separately).
- Any change to `blocks.py` HTML parsing. Measured against lxml on all 45 fixture and live
  pages: identical distinct block set, zero divergence. The regex path is empirically
  correct on this corpus.
- `matching.best_window_overlap` → rapidfuzz. Every threshold in the repo is calibrated
  against its exact semantics; swapping it invalidates every recorded measurement.
- `chat.py` sentence splitting. Both known defects are already passing regression tests.
- Behaviour change of any kind. This refactor is mechanical. If a number moves, that is a
  defect in the refactor, not a finding.

---

## Phase 0 — dependency gate (do first; everything else depends on it)

`litellm` requires `openai>=2.20,<3.0.0`. The venv currently has **openai 3.3.1**. Adding
litellm downgrades it.

1. `pyproject.toml`: change `openai>=1.60` → `openai>=2.20,<3`; add `litellm`, `ranx`,
   `flashrank`, `pydantic-settings`, `tenacity`. Add `ruff` to the `dev` extra (there is a
   `.ruff_cache` in the tree with no dependency — `PENDING.md` §7).
2. `uv sync --extra dev`, then run the existing suite: `uv run pytest -q` (baseline: 131
   passing).
3. **Gate — smoke-test the openai downgrade before writing any other code.** The only
   direct openai consumer that survives this work is `embeddings.py`
   (`client.embeddings.create(model=, input=, dimensions=)`, then `response.data`,
   `item.index`, `item.embedding`). Confirm it still works against openai 2.x with a
   one-batch live call.
4. Record the venv size before and after. `DESIGN.md` §10 treats dependency weight as a
   live constraint (623 MB today); the new figure belongs in the decisions register.

**Abort condition:** if `embeddings.py` cannot work under openai 2.x, stop and re-open the
litellm decision rather than working around it.

---

## Phase 1 — already-installed libraries, zero test impact

None of these files are imported by any test, so there is no safety net. Verify by running
the pipeline (see Verification), not by running pytest.

**`search.py` — `pgvector.psycopg.register_vector`**
`pgvector>=0.3` is a declared direct dependency in `pyproject.toml` that is never imported.
`search.vector_literal()` (`search.py:101`, used at `:123`) hand-formats
`"[" + ",".join(repr(float(v))) + "]"`. Register the adapter in `db.connect()`
(`db.py:17`) and pass the vector as a native parameter. Delete `vector_literal`.

**`embeddings.py` — numpy and tiktoken** (both already installed via crawl4ai)
- `l2_normalise` (`:47`) is a `math.sqrt` loop → `numpy.linalg.norm`.
- `_batches` (`:54`) uses `MAX_CHARS_PER_REQUEST = 400_000` as a proxy for a **300,000
  token** API limit. Replace with real `tiktoken` counting. Keep `MAX_INPUTS_PER_REQUEST`.
- Keep `EmbeddingStats.first_batch_norms` and its logging — that measurement is what
  `DESIGN.md` §11 cites for the pgvector operator-class choice.

**`db.py` — psycopg3 `executemany`** (no new dependency)
Five loops issue one round-trip per row: `upsert_variants:91`, `replace_assertions:160`,
`replace_edges:188`, `replace_rejected_keys:201`, `replace_template_constants:217`.
Convert to `executemany`. **Preserve the per-row `ON CONFLICT … DO UPDATE` semantics** and,
in `replace_assertions`, the delete-not-in query that follows the loop — that pair is what
makes assertions a set-diff rather than an upsert, which `merge` depends on to withdraw a
dropped review count.

---

## Phase 2 — `config.py` → pydantic v2 + pydantic-settings

`StoreConfig` (215 lines: dataclass + `validate()` + defaults merge + env secrets) becomes
a pydantic v2 `BaseModel`; `token_for` / `storefront_token_for` / `require_env` /
`database_url` move onto a `BaseSettings`.

Port `validate()` (`config.py:71-91`) to field validators, keeping every rule and its
message — especially the `chrome_threshold >= 1.0` rejection, which cites `DESIGN.md` §5.3
and is load-bearing.

**Two hazards, both from `tests/test_profile.py`:**

1. **`config.DATA_ROOT` must remain a module-level global dereferenced lazily at
   property-access time** (`config.py:92-114`). `tests/test_profile.py:151` does
   `monkeypatch.setattr(config, "DATA_ROOT", tmp_path)`. If a pydantic `computed_field` or
   a settings field resolves the data root eagerly at construction, that monkeypatch
   silently stops working, the fixture writes into the real `data/skout/`, and all ten
   tests at `test_profile.py:217-282` break. Keep `data_dir`/`pages_dir`/`api_path`/
   `profile_path`/`manifest_path`/`fetch_manifest_path` as properties reading the global.
2. **Field names, defaults and keyword construction must not change.**
   `tests/test_labels.py:39-40` builds 15 stores as
   `StoreConfig(slug=…, domain=…, **kwargs)` relying on every other field defaulting;
   `tests/test_profile.py:153-159` passes exactly `profile_pages=5` (the `>= 5` boundary)
   and `chrome_threshold=0.8`. Validation now runs at construction rather than on an
   explicit `validate()` call — confirm both boundary values still pass.

Note `StoreConfig.profile_pages` defaults to `20` in code but `40` in `config/stores.yaml`
and `DESIGN.md` §4.1. Carry the `20` forward unchanged; the discrepancy is a documentation
matter, not this refactor's.

`load_stores`, `token_for`, `storefront_token_for`, `require_env` and `database_url` have
**zero test coverage** — free to restructure, and correspondingly unverified. Cover them
with the manual checks in Verification.

---

## Phase 3 — `llm.py` → litellm

`complete()` (`llm.py:28-55`) tries three request shapes blind and reports none. Replace
with `litellm.completion`, which normalises `max_tokens` / `max_completion_tokens` /
`reasoning_effort` across model families.

**Return which shape was used.** This is the point of the change (`PENDING.md` §3c). Have
`complete` return the text plus the resolved parameters — `litellm.get_supported_openai_params`
and `drop_params` expose what was actually sent — and surface it on `chat.Turn.to_log()`
so `chat_replay.jsonl` records it per turn.

Rewrite the module docstring. Its entire body describes the three-attempt fallback and
becomes false.

**Breaks two tests** (`tests/test_labels.py`), both via `StubClient` (`:121-137`), a
hand-built fake of `client.chat.completions.create` reached through
`ClassifierPolicy` → `labels.py:186,193`:

- `test_classifier_caches_one_call_per_label:140` — asserts `client.calls == 1`
- `test_classifier_falls_back_to_uncertain_on_an_unparseable_answer:152`

litellm's `completion()` is a module-level function, not a client object, so `StubClient`
is bypassed entirely. Use litellm's native `mock_response` parameter, and count invocations
by monkeypatching `litellm.completion`. The assertions worth preserving are exactly the
current ones: **one model call per distinct label** (the cache contract) and **an
unparseable answer degrades to `UNCERTAIN`**, never to `SPEC`.

`test_classifier_never_overrides_a_manual_deny:160` asserts `client.calls == 0`; it needs
only a countable stub in whatever new form.

`ClassifierPolicy.client` (`labels.py`) is the injection seam. Keep an injectable seam of
some kind — the cache-per-label contract is only testable through it.

---

## Phase 4 — `search._rerank` → flashrank

Goal per `DESIGN.md` §10: make `eval --compare-rerank` runnable with **no vendor account**,
so the reranker decision can be settled by measurement.

`_rerank` (`search.py:340`) is one function with one call site (`search.py:226`). Give it
two backends selected by a prefix on the existing `rerank_model` config value:
`cohere:rerank-v4.0-fast` and `flashrank:ms-marco-MiniLM-L-12-v2`. **A bare value keeps
meaning Cohere**, so `config/stores.yaml` works unchanged.

Preserve the existing contract exactly:
- degrade to the input list — same objects, same order — on any exception, never raise
- set `diag.rerank_failed` and `diag.rerank_error` via `_brief` (`search.py:333`)
- accept `diag=None` without raising
- read only `hit.text` (sliced to 4000 chars) and write only `hit.rerank_score`

Add flashrank model download/caching to the setup path so a first run is not a silent
network call inside a shopper query.

**Breaks two tests** (`tests/test_search_diagnostics.py:18` and `:38`). Both inject a fake
`cohere` module into `sys.modules` whose `ClientV2` raises, relying on `_rerank` doing
`import cohere` inside its `try`. With a second backend that mechanism no longer induces the
failure. Rewrite as a parametrised pair over both backends, inducing failure at each
backend's entry point. The contract assertions carry over verbatim. `FakeHit` (`:11-15`)
gains fields only if flashrank needs doc ids.

Update the `search.py` module docstring — it names Cohere specifically.

---

## Phase 5 — `evaluate.py` → ranx

`evaluate.run()` and its metric math (`evaluate.py:229-238`) have **zero test coverage**, so
nothing in the suite guards this. Number parity is the acceptance criterion instead.

Most of what this harness measures has no IR equivalent — scoped answerability, constraint
satisfaction, `expect_empty`, violations. Do **not** force those through ranx. Two things
map cleanly:

1. **`relevance@5` and the `expect_handles` half of discovery recall.** Build `qrels`
   (`{question: {handle: 1}}`) and `run` (`{question: {handle: score}}`) from the existing
   outcomes and compute via `ranx.evaluate(qrels, run, "recall@5")`. Cross-check against
   today's hand-computed value — **they must be identical**, and that equality is the
   acceptance test for this phase.
2. **`compare()`'s significance rule.** Today `resolution = 1.0 / total` (`evaluate.py:342`)
   is an eyeballed one-question threshold. Replace with `ranx.compare(qrels, runs=[…],
   metrics=[…], max_p=…)` and its paired t-test, so `DESIGN.md` §10's "delta below 0.03 →
   drop reranking" rule is decided by a real test.

**Keep as-is:** `_evaluate_one`, `_evaluate_scoped`, `undeclared_returns`, the two-mode
split, and the violation reporting.

**Hard constraint — do not break the two evaluate tests** (`tests/test_profile.py:320` and
`:367`). They rebind `ev.search` and `ev.undeclared_returns` directly (manual save/restore,
not `monkeypatch`), so both must remain **module-level globals of `evaluate`** resolved as
globals at call time. Moving `undeclared_returns` into a class, into `db.py`, or into a
local import inside `_evaluate_one` breaks the patch *silently* — the real function runs and
the test errors on a DB connection. Also: `FakeStore` is a bare class with only `.slug`, so
nothing in `_evaluate_one` or a ranx-based `run()` may type-check its store argument or
touch another attribute.

Preserve `_evaluate_one`'s returned keys `ok`, `relevant`, `violations` with today's
semantics, and the branch ordering that makes `expect_empty` short-circuit **before** the
`exclude` branch (`evaluate.py:190-193`).

---

## Verification

Run in order. Phases 1, 2 and 5 have little or no unit coverage, so the pipeline run is the
real test.

```bash
uv sync --extra dev
uv run pytest -q                          # expect 131 passing, minus the 4 rewritten
uv run ruff check src/ tests/             # new lint step
```

**Offline, no token** — exercises config, profile, blocks, matching end to end:
```bash
uv run poc seed-fixtures
uv run poc profile --store skout
uv run poc report  --store skout
```

**Against the live database** (`docker compose up -d db`; it currently holds skout 171
products / 2,183 assertions and remi 48 / 639 — the `PENDING.md` snapshot):
```bash
uv run poc stores                          # Phase 2: resolved config unchanged
uv run poc report --store remi             # Phase 2
uv run poc search "protein bar" --store skout --max-price 30   # Phases 1, 4
uv run poc facts water-flosser --store remi
uv run poc eval --store remi               # Phase 5: numbers must not move
uv run poc eval --store skout
```

**Parity checks — the acceptance criteria.** Capture these *before* starting and diff after:

| Check | Expected |
|---|---|
| `poc eval --store remi` / `--store skout` | discovery recall, scoped answerability, relevance, violations all **identical** |
| ranx recall@5 vs hand-computed | **identical**, on both stores |
| `poc report` on both stores | attribute reachability, admitted/rejected key counts **identical** |
| `poc index --force` embedding L2 norms | still 0.9997–1.0001 (`DESIGN.md` §11) |
| `poc merge` assertion counts | skout 2,183 / remi 639, same quotable-retrieval split |

**Phase 4 payoff — the point of the exercise:**
```bash
uv run poc eval --store remi --compare-rerank    # with rerank_model: flashrank:...
uv run poc eval --store skout --compare-rerank
```
This is the first time either arm has actually executed. A non-zero delta with a real
p-value settles `DESIGN.md` §10.

**Phase 3 payoff:**
```bash
uv run poc chat-replay --store remi --limit 8
```
Confirm each turn in `data/remi/chat_replay.jsonl` now records the resolved request shape.
remi's baseline is groundedness 0.85 with zero invalid citations across 42 turns — a
`--limit 8` run should not contradict it.

## Follow-ups this work does not do

- `undeclared_returns` vacuous SQL (deliberately deferred above).
- `DESIGN.md` §3 decisions register and §10 open items need updating once the reranker A/B
  produces a number, and §9's dependency-weight note needs the new venv size.
- `PENDING.md` §2 (reranking) and §3c (request shape) close; §7's `ruff` item closes.
- `README.md` model table lists `rerank-v4.0-fast — has never run`; update after Phase 4.
- Docstrings in `llm.py`, `search.py` and `config.py` describe mechanisms this work
  replaces and will be false until rewritten.
