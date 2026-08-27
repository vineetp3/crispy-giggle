# `ingest/` — the pipeline stages

Algorithm-level specification for these stages: `docs/DESIGN.md` §5.1–5.4.

`crawl` → `profiling` → `merge` → `indexing`, with `labels` as the gate `profiling` and
`merge` both consult.

---

## `crawl` — page fetching, selection, escalation

One crawl run per store, because Crawl4AI's `RateLimiter` applies a random inter-request delay
rather than a per-domain control — crawling two stores in one run would not let you give remi
gentler settings than skout.

Escalation exists because remi returns HTTP 403 with a Cloudflare "Just a moment" interstitial
to plain requests while its `.js` endpoint returns 200. Headless Chromium passes. The ladder is
`plain → stealth → undetected`, and the profile that succeeded is recorded per page.

"Pages per store" is three settings, because they answer different questions: `profile_pages`
(the sample used to derive the profile), `crawl_scope` (`none` / `sample` /
`template_representatives` / `all`), and `max_pages` (a hard ceiling). `crawl_scope: none` is
meaningful and correct for a store whose API already holds everything.

Measured 2026-08-25: skout's full catalogue is 152 pages in 5m43s with 0 failures. Chromium
rendering dominates at ~10s/page; raw HTTP is 0.6–0.9s. Sampling was never a cost control —
`profile_pages` drives differencing quality — so both pilot stores run `crawl_scope: all`.

`floor_shortfall` warns when `profile_pages` cannot reach the 3-page floor for every template
group, because below that groups get 1–2 pages and differencing degrades.

`arun_many` returns `CrawlResultContainer | AsyncGenerator`; we never stream, so it is the
container, which is iterable. Pyright cannot narrow the union, hence the suppression there.

---

## `labels` — is a rendered `Label: Value` pair a spec or a widget?

Both render identically once a page is reduced to visible text, and **the same label means
opposite things on different stores**: skout's `Pack Size` is a variant picker; remi's
`Quantity` is how many tablets are in the box. A single global regular expression cannot
separate them, which is why this decision is configured or classified per store rather than
hardcoded.

**Three verdicts, not two.** `uncertain` exists because the safe action for an unrecognised
label is to make it findable without making it quotable, so a label nobody has ruled on lands
in the retrieval corpus rather than being asserted to a shopper or thrown away.

**Policies may only demote.** `core.quotability.is_quotable_theme_value` and
`is_commerce_constant` still run afterwards and can reject anything a policy accepted; no
policy can promote past them. The manual allow and deny lists in `config/stores.yaml` are
consulted before any policy and override it, so a bad classifier verdict is correctable without
a code change.

This is the one **experimentation seam** in the repo worth copying: a `LabelPolicy` protocol,
three implementations (`NonePolicy` reproduces behaviour before the gate existed and exists as
a control; `StaticPolicy` reads `config/spec_labels/<slug>.yaml`; `ClassifierPolicy` calls a
model), and `get_policy(name)` wired to `merge --label-policy`.

`ClassifierPolicy` sends **only the label**, with up to three example values for
disambiguation. Values are never sent for extraction: the model decides what a label means, it
never produces a fact. That boundary is what keeps the exclusion of LLM extraction intact while
allowing LLM classification.

Its cache is keyed by model as well as by label. Two models disagree, and a cache that ignored
the model would silently serve one model's verdicts for a run nominally using another, making
any comparison between them meaningless. A cached run makes no network call, so a profile
classified once stays reproducible offline.

`gpt-5.5` reproduced every confident judgement in both reference sets; `gpt-4o-mini` did not.
It stays off by default because a reviewed file beats a model call for two stores someone has
already read.

---

## `profiling` — where does this store's product content live?

**`support` and `observed` are different denominators and must not be conflated.** `support`
counts every product carrying a usable value; `observed` counts only those whose page was
fetched. A hit rate over `support` has an arithmetic ceiling of crawled/total — on a 20-page
skout sample `custom.nutrients` reported 7/48 = 0.15 against a 0.8 bar, so no key could ever be
classed `rendered` and everything fell through to `partially_rendered`, which downstream read as
quotable. Render verdicts use `observed`; retrieval value uses `support`.

**There is no chrome guard on metafield keys.** It double-counted across products (a key on 5
products with 4 sibling pages scored 20 against a threshold of 4) and wrongly rejected
`custom.nutrients`, `filter.ingredients` and `custom.product_faqs`. Page chrome is handled by
differencing, and metafields are product-scoped by construction. What replaced it is the
recorded `identical value on every product` diagnostic, which must hash **all** candidates
rather than the first: `cands[0]` is `root` for every rich-text field, so hashing the first made
the diagnostic fire on everything.

**Render presence promotes; it does not gate.** skout's `custom.product_attributes`,
`custom.product_faqs` and all three `custom.description_*` render nowhere and are the most
retrieval-useful content on the product. Contamination and freshness filter; rendering only
informs the trust class, and `merge` decides that.

### The evidence-collection pass

`_collect_evidence` walks every product × metafield once and accumulates a `KeyEvidence` per
`(namespace, key)`. Three write semantics there are load-bearing and easy to break:

- the metafield **type is first-sighting-wins** (`setdefault`);
- a **rejection is last-write-wins**, and beats accumulated support from other products —
  `_verdict_for` checks it before anything else, so one contaminated product rejects the key;
- the **foreign-title detail is first-write-wins**, so the first example is the one reported.

`_early_rejection` runs the cascade that needs only the raw value (contamination, excluded
namespaces, reference types, widget markup); `_value_rejection` runs the two that need parsed
candidates (commerce facts, content-free values).

---

## `merge` — API and page evidence into field assertions

**`filter.contains` is a FREE-FROM list, not a contains list.** It is renamed to `free_from`
here so no downstream reader can invert the allergen filter. Proved on skout: the peanut-butter
bar omits `Peanut`; the lemon-poppyseed cookie includes it.

Quotability is decided in `core.quotability`, by type and shape rather than render presence —
see `docs/reference/core.md`.

**Freshness is recorded (`source_updated_at`) but deliberately does NOT gate quotability.**
Median metafield age on skout is over 1,000 days for `custom.nutrients`, `filter.contains` and
`filter.curated`; an age cliff would empty the quotable set rather than make it safer. Decay
needs a re-confirmation loop that v0 does not have.

`rendered` is per product, never per key. A key at an 0.85 hit rate does not render on the other
15%, and a product whose page was never fetched has no render evidence at all.

**Conflicts are dropped, never reconciled.** skout's peanut-butter cookie reports 72, 63 and 4.8
across three review namespaces and remi reports 51, 627 and 1193 for one product, so neither
gets a review count at all.

A theme constant is quotable only when it carries a recovered label. A labelled pair is
markup-evidenced (`Material: BPA-free, food-safe plastic`); an unlabelled one is whatever
survived intersecting a template group's residual, and on skout's heterogeneous `_default`
group that includes blog titles, "1 year ago" and per-variant pricing.

---

## `indexing` — build documents, embed, load

The content-hash gate is what makes re-running cheap: an unchanged document skips both the
write and the embedding call. It is keyed on `(product, chunk)`, so splitting a product into a
quotable and a retrieval chunk means a change to one does not re-embed the other.

A product can legitimately have no quotable chunk. Empty documents are skipped rather than
written, so **absence of a quotable chunk is the signal** that nothing about that product may be
stated as fact.

The embedding zip is `strict`: one vector per pending document, by construction. If that ever
stops holding, truncating would silently store embeddings against the wrong documents — a
corrupt index no test would notice.

Operates on Postgres rows, not `core.models.Product`.
