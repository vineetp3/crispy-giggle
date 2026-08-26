# Pending work

**Last updated:** 2026-08-25 (label gate closed).

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
uv run pytest -q                   # 131 tests, no token needed
```

`.env` holds `PIER39_SHOPIFY_TOKENS` and `OPENAI_API_KEY`. Nothing else needs a credential:
reranking runs locally.
`PIER39_SHOPIFY_STOREFRONT_TOKENS` is unset.

**State as of this writing** — measured 2026-08-25 with the `static` label policy, which is
the `merge` default. Re-derive it with `poc report` and `poc eval` rather than trusting it — it
is a snapshot, and the pipeline has been re-run since.

| | skout | remi |
|---|---|---|
| Products indexed | 171 | 48 |
| Assertions (quotable / retrieval) | 2,183 (1,473 / 710) | 639 (387 / 252) |
| Theme-sourced assertions | 116 | 76 |
| Attribute reachability | api 4, theme 2, image 3, absent 4 | theme 6, absent 4 |
| Discovery recall@5 | 0.92 | 1.00 |
| Scoped answerability | 0.87 | 0.75 (was 0.65) |
| Constraint violations | 0 | 0 |

skout holds its 0.87 because `Pack Size` and `Size` were deliberately kept quotable; see §1a.
`Delivery Frequency` and `This item` are suppressed, which is what the regression guard now
means.

---

## 1. Theme spec extraction — the label gate *(closed 2026-08-25)*

Closed. The rules and their justifications are in `docs/DESIGN.md` §5.3; the §9 amendment
permitting label classification is there too. Summary of what changed and what it cost:

- Three extraction defects fixed. The `quantity` denylist entry is gone, the `label_for`
  path gained a numeric guard, and labelled pairs are now recovered from the whole product
  region rather than only from residual blocks.
- **The third was the real cause, and this document had it wrong.** Per-product specs were
  not lost because `merge` writes only template constants. They were lost at extraction: a
  spec whose text also appeared in `description_html` was classed as already explained and
  never became a typed pair. remi's `Material: Food-grade material, BPA-free, and
  phthalate-free` is the clearest case.
- A per-store label policy now decides spec, widget or uncertain, applied at merge so the
  arms differ by one flag. Unrecognised labels become retrieval assertions, never quotable.
- remi scoped answerability 0.65 → 0.75. skout holds 0.87. Discovery recall and constraint
  violations unchanged on both stores.
- `Delivery Frequency` and `This item` are no longer quotable on skout. `Pack Size` and
  `Size` deliberately still are; see §1a.

Two questions this raised, both open:

**1a. Does a variant picker answer "how many bars come in a pack"? Decided for skout, open
in general.** Suppressing `Pack Size` and `Size` cost two scoped answers and dropped skout
to 0.78. The call was made to keep them quotable, so skout holds 0.87 and 111 picker values
are quotable. `Delivery Frequency` and `This item` remain suppressed.

What stays open is the general rule. The decision was made per store, by a person, on
evidence — which is the mechanism working as intended, but it does not scale to a store
nobody has read. A third store will pose the same question with no one to answer it, which
is the strongest argument for either a better classifier or an explicit rule about what a
selector may be quoted for. Reverse for skout by setting both labels back to `widget` in
`config/spec_labels/skout.yaml`.

**1b. The reference sets are one reader's judgement on two stores.** `config/spec_labels/`
holds 38 hand-authored verdicts. Precision figures measured against them are agreement with
that judgement, not correctness. A second reader labelling the same 38 independently would
show how much of the classifier's disagreement is model error and how much is genuine
ambiguity. Cheap, and it has not been done.

The classifier arm was built, measured twice and left off by default. `gpt-4o-mini` did
not beat the hand-authored sets and read remi's `Quantity` as a widget, the case the item
existed to fix. `gpt-5.5` reproduced every confident judgement in both reference sets, 29 of
29, and promoted no widget to a specification. The earlier conclusion was therefore about
the model, not about classification. It stays off by default because a cached deterministic
file is cheaper and reviewable, but its case for a store nobody has read — item 6 — is now
considerably stronger than the first run suggested.

---

## 2. Reranking — measured, and it earns nothing *(closed 2026-08-26)*

`_rerank` used to call a hosted reranking service whose credential was never set. The important
part is not that it failed but *how*: `rerank` defaults to `True`, so it was called on **every**
search and swallowed the auth error into the fused order every time. "Never ran" and "ran and
changed nothing" produced identical output, which is why no recall figure before this date could
distinguish them.

It now runs a local ONNX cross-encoder in-process through `flashrank`, named by `rerank_model`
as a bare checkpoint. There is no credential to be silently wrong, no per-query network call once
the checkpoint is cached, and the arm is runnable by anyone who clones the repo.

**The measurement, run on 2026-08-26 at `top_k=5`.** Both arms executed; `evaluate`'s
"INVALID COMPARISON" guard did not fire.

| store | with rerank | without | delta | paired t-test | win/tie/loss |
|---|---|---|---|---|---|
| remi | 0.881 (37/42) | 0.881 (37/42) | +0.000 | n/a — identical on every query | 0W / 20T / 0L |
| skout | 0.896 (43/48) | 0.896 (43/48) | +0.000 | p = 1.0000 | 2W / 20T / 2L |

remi's reranker changed no question's outcome at all. skout's moved four questions and the wins
exactly cancelled the losses. Under `DESIGN.md` §10's rule — delta below 0.03 — reranking is
dropped from v0; `DESIGN.md` §3's register carries the decision.

Two things had changed before the measurement, and both still hold for any revisit:

- Duplicate collapse means the top-5 now holds distinct products rather than several spellings of
  one bar. A cross-encoder could never have fixed that crowding, so an A/B before the collapse was
  measuring the wrong thing.
- Scoped questions no longer touch the ranking path at all. If most production traffic is
  product-scoped, a reranker only ever affects discovery queries, which narrows its case.

**Still open, deliberately:** the code still reranks by default. The decision above says it is not
earning its place, but `rerank` remains `True` in `search.search` and `flashrank` is the default
backend, so every query still pays a local cross-encoder pass. Flipping that default — or removing
the stage — is a behaviour change and was not made alongside the measurement that motivates it.

Revisit when a store's catalogue is large enough for first-stage recall to fail; the arm is now
one flag away rather than one vendor account away.

---

## 3. Chat layer — built, with a measured baseline *(2026-08-25)*

Built as the agreed A+D shape. `poc chat` is a REPL and `poc chat-replay` is a batch
harness, both going through one `chat.answer`, so they cannot disagree about what the model
was shown. The scorer's own defects are encoded as named regression tests in
`tests/test_chat.py`, which is a better record than prose because a test fails when the knowledge
is lost.

`src/pier39_poc/chat.py` and `src/pier39_poc/llm.py` are imported by the CLI only, never by
an ingest stage, so `DESIGN.md` §9 stays honest.

**remi baseline: groundedness 0.85, and zero invalid citations across 42 turns.** The model
never invented an assertion id and never cited a background fact as quotable. That is the
first evidence that the quotable/retrieval split holds when a model is put in front of it.

What remains open:

**3a. skout has no baseline.** One command, 48 model calls.

**3b. Reasoning effort is hardcoded `low` and unexposed.** It was chosen for one-word label
classification and carried into grounded answering, which is a much harder task, without
being revisited. Every number recorded is at `low`. Exposing `--effort` and re-running would
show whether the remaining uncited sentences are a model limitation or a setting.

**3c. `llm.complete` hides which request shape succeeded.** *(closed 2026-08-26)* It tried
three shapes blind and recorded none, so a model that rejected `reasoning_effort` ran at its own
default and nothing said so. `complete` now resolves the shape up front from litellm's capability
table — the same three shapes, in the same order, chosen rather than discovered by failing — and
returns it alongside the text as a `Completion`. `chat.Turn.to_log` writes it per turn, so
`chat_replay.jsonl` records what was actually sent and two runs are comparable. A `--limit 8`
remi replay on 2026-08-26 logged `max_completion_tokens=1200, reasoning_effort=low` on every
turn.

**3d. Re-scoring requires re-calling the model.** The answers are already logged and the
scorer is pure text analysis, so a `--rescore` flag reading `chat_replay.jsonl` would make
iterating on the metric free. The §8 figures were recovered that way with a throwaway
script.

**3e. A hedge is scored as an uncited claim.** *"I don't have the tank size in the available
facts"* states no product fact but is counted against the answer. Four of remi's six
ungrounded turns are this. The 0.85 is therefore a floor, and the fix is either a
non-claim detector or an expectation in the question file.

**3f. Promote-to-eval was deliberately not built.** Turns are logged with a `question_yaml`
field already in the right shape, so the remaining work is a command that appends it.

**Still rejected: an agentic REPL with tools.** Routing would add a second failure mode on
top of the one being measured. Revisit now that A+D has a baseline to compare against.

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
- **Verify the skout assertion-count attribution.** *(recommend dropping)* The drop from 2,440 to
  roughly 2,080 after the full crawl was attributed to sampling bias being removed — a larger
  hit-rate denominator. That was written before the group-chrome bug in item 1
  was found, and that bug also suppressed assertions. Three further extraction changes have landed
  since (the `quantity` denylist removal, the `label_for` numeric guard, and full-region labelled
  pair recovery), so the original number can no longer be reproduced and the split between causes
  cannot be recovered — only re-derived from scratch. The archaeology is not worth the answer.
- ~~**`ruff` is not a project dependency**~~ *(closed 2026-08-26)* — added to the `dev` extra with
  a `[tool.ruff]` block (`E,F,I,UP,B`, line length 100). The tree is clean: `uv run ruff check
  src/ tests/` passes. `[tool.pyright]` was added alongside it, pinning `venvPath`/`venv` so an
  editor resolves imports against this project's `.venv` rather than whatever is activated in the
  shell; that check is clean too, with four rule-scoped `# pyright: ignore` suppressions that each
  carry a one-line reason.

Added 2026-08-25, from building the label gate and the chat layer. Rough order of value:

- **`--rescore` on `poc chat-replay`.** The highest-value item here. Turn answers are already in
  `data/<slug>/chat_replay.jsonl` and the scorer is pure text analysis over that text, so
  re-scoring should never call a model. It currently does, which made iterating on the
  groundedness metric expensive — three scorer defects were found by measuring, and each fix meant
  re-running 42 model calls. The remi baseline was ultimately recovered from the log with a
  throwaway script, which is the proof this wants to be a flag.
- **Record which request shape `llm.complete` used.** It attempts three — modern with
  `reasoning_effort`, modern without, then legacy `temperature`/`max_tokens` — and records none.
  A model that rejects `reasoning_effort` therefore runs at its own default effort and nothing
  says so, meaning two runs are not necessarily comparable. Same silent-degradation pattern as the
  reranker's 401 and the Storefront fallback, both of which were worth surfacing.
- **Expose reasoning effort.** `--effort` on `chat` and `chat-replay` plus an environment
  variable. It is hardcoded `low` in `llm.py`, chosen while probing one-word label classification
  and carried into grounded answering — a much harder task — without being revisited. Every
  measurement recorded anywhere is at `low`. Raising it also needs the completion budget raised,
  since reasoning tokens are drawn from the same allowance and an exhausted budget returns empty,
  which `complete` treats as a failed attempt and silently retries in another shape.
- **A hedge is scored as an uncited claim.** *"I don't have the tank size in the available facts"*
  states no product fact but counts against the answer. Four of remi's six ungrounded turns are
  this shape, so the 0.85 baseline is a floor. Fixing it needs either a non-claim detector or an
  expectation carried in the question file, and the second is the more honest of the two.
- **Record which client served the live price read.** `Diagnostics` tracks whether the read
  failed but not whether Storefront or the Admin fallback answered it, so a base-market price is
  indistinguishable from a market-correct one in any result. Related: `attach_live` fetches the
  Storefront `currencyCode` and then discards it, so one of the three stated reasons for
  preferring Storefront currently buys nothing even when a token exists.
- **Move `labels.STORE_CATEGORY` into `config/stores.yaml`.** A dictionary keyed by store slug,
  hardcoded in the module, supplying the classifier its one line of store context. It will not
  survive a third store, which is item 6.
- **Promote-to-eval command.** Chat turns already log a `question_yaml` field in the shape
  `config/questions/*.yaml` uses, with `scope` attached when a product was held. The remaining
  work is a command that appends it to the store's question file.

---

## 8. Repository state

The label-gate work is committed on the `worktree-label-gate` branch: extraction fixes, the
policy layer, the classifier, the three-arm comparison, and 20 new tests. The chat layer
(`chat.py`, `llm.py`, two CLI commands, 21 tests) is **uncommitted** in the working tree at
the time of writing. The suite is 131 tests and still needs no token.

The documentation described in earlier revisions of this file as uncommitted was committed
in `93e50fc`; that note was stale and is corrected here.

The Postgres database holds the measured state above and is settled on the `static` policy,
which is now the `merge` default. It is a Docker volume, not in the repo — `docker compose up
-d db` brings it back, but a `docker compose down -v` would destroy it and require a full
re-crawl and re-embed to reproduce. `poc init-db` is idempotent and carries the
`template_constants.handle` migration.
