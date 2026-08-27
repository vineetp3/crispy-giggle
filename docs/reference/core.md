# `core/` — pure domain logic

Algorithm-level specification for these stages: `docs/DESIGN.md` §5.3.

No IO, no config, no network. Each rule below cost a measurement to learn; several are
inverted-safety hazards rather than style preferences.

---

## `blocks` — chrome extraction and cross-page differencing

The core idea, validated on five live skout product pages: split each page into text runs
("blocks"), count how many pages each distinct block appears on, and drop the ones that
appear on most pages. Those are navigation, footer, mega-menu and banners.

**`chrome_threshold` must NOT be 1.0.** Different pages omit different sections, so requiring
a block to appear on *every* page leaks whole sections into every page's product region.
Measured on skout: `Where do you ship?` appears on 4 of 5 pages, `What does a Skout bar taste
like?` on 3 of 5. At threshold 1.0 the store-wide FAQ survived (1,569 words); at 0.8 it did
not (664 words). The config validator rejects 1.0 outright.

**A ratio threshold cannot catch copy repeated across a handful of pages.** Measured on remi's
30 crawled pages: 1,236 distinct blocks, 72 classed chrome at 0.8 (a 24-page cutoff), and
2,919 words sitting in blocks that appear on 2 or more pages but below it. That is where the
cross-page marketing copy lives — the doctor testimonial repeats on three products at
1,500–2,000 words each. `repeated_block_profile` applies an absolute page count instead, and
`profiling._two_level_chrome` uses it only for products alone on their template, where the
per-group pass has no sibling to difference against.

The page floor is 3, not 2. At 2 it strips spec text legitimately shared between two variants
of one product.

**The word floor is load-bearing and was added after a measured regression.** A page count
alone is not enough, because real attributes repeat across sibling products exactly like
boilerplate does. Applying the 3-page rule with no length guard cost remi its `compatibility`
attribute and cost skout both `dimensions` and `usage` — skout dropped from `theme 2` to
`theme 0`, which is the deliverable this whole stage exists to produce. Raising the page floor
did not fix it: at 5 pages remi recovered but skout did not.

Length separates the two cleanly. The copy this rule targets is long prose; attributes are
short `label: value` pairs. At 20 words both stores keep every attribute they had before the
rule, and remi's coverage still improves from 4.1% to 4.7%. Anything shorter than 20 words is
left alone no matter how often it repeats.

`inline_label` exists because a theme may render a spec as one text run —
`Material: Dental-grade polymer, BPA-free, and phthalate-free.` — rather than as a bold label
node followed by a value node. `label_for` only sees the second shape, so remi's night guards
looked like they had no material at all. The same four-word label cap and `looks_like_label`
guards apply, plus a numeric-value rule, so skout's `February: 2/12` shipping calendar and
`FIND IN A SKOUT BAR: 4.5` rating widget stay excluded.

---

## `matching` — value normalisation, page matching, contamination

In order of how much damage each caused.

**Bag-of-words containment over a whole page is not a render signal.** A 60-token candidate
built from common words clears an 0.8 token-overlap bar on any large page without being
present: `custom.product_faqs` scored 0.833 on a skout page with 10 of its 60 tokens absent
from the page entirely. Every match therefore has to survive `best_window_overlap`, which asks
whether the tokens co-occur inside one span rather than anywhere on the page. Removing that
gate silently reclassifies unrendered LLM enrichment as rendered, which is how it reaches a
shopper.

**The tolerance that gate must not break:** skout's theme renders `Protein [1g]` as
`Protein 1g`, so matching is on normalised tokens, never on substrings. Exact containment
rejects `custom.nutrients`, and a silently dropped structured field looks exactly like success.

`rich_text_field` values are ASTs whose `type` keys hold strings (`"root"`, `"paragraph"`,
`"text"`). A generic string-leaf walk harvests those as content, which pollutes embeddings and
makes `cands[0]` identical across every product. Rich text is therefore walked by node type,
not generically.

`detect_foreign_product_ids` is separate from `detect_contamination` only because it needs the
full catalogue id set, which does not exist until every page is fetched. It iterates a `set()`
of id strings, so which contaminated id its diagnostic names varies per process — cosmetic,
and the verdict is unaffected.

---

## `quotability` — may this be repeated to a shopper as fact?

**Decided by type and shape, NOT by render presence.** Rendering only tells you the theme
consumed the key — the page is rendered *from* the metafield, so a match says nothing about
whether a merchant vetted the value. skout's `custom.short_description` renders on every
sampled product and is generated marketing prose; `custom.nutrients` is a typed list of
checkable facts whose theme presence is incidental.

So: prose types and untyped `string` are never quotable; `json` is never quotable (skout's
`product_faqs` and `product_attributes` are json holding hedged generated sentences); and a
candidate over `quotable_max_tokens` is prose regardless of its declared type.

`is_commerce_constant` guards the theme path, which has no namespace, key or type to key on.
remi renders `Birthday Sale: 50% Off` as a spec pair on 22 products; it is a time-limited
promotion, not a property of the product. A bare percentage is not enough to decide —
`Formula: 3.8% Hydrogen Peroxide` is a real concentration — so it keys on discount language
rather than on the presence of a number.

`is_content_free` drops flags, colours and timestamps: values that exist but carry no product
information.

---

## `families` — collapsing duplicate listings

skout lists the same physical product up to three times: a base handle, a `-bundle` handle,
and a `skout-organic-` prefixed legacy listing. Ten products in the peanut-butter protein bar
family carry byte-identical `free_from` values, so five result slots can go to five spellings
of one bar while other flavours never surface.

**Collapsing happens at retrieval time, not index time, on purpose.** Every product stays
indexed and individually addressable, bundles stay retrievable, and the behaviour is one
toggle. Merging at index time would make the bundle listings unreachable and cost a full
re-index to undo.

The grouping key is the normalised title, not the `edges` table. `flavor_of` (208 rows on
skout), `bundle_prebuilt` and `bundle_extra` are merchant-declared bundle-to-member links — a
15-pack pointing at the three flavours inside it — which is a different relation from two
listings of one bar. Measured against the live catalogue, the title key collapses skout
172 → 121 and remi 48 → 44.

Canonical selection prefers a non-bundle listing, then the most quotable assertions, then the
shortest handle: `peanut-butter-protein-bar` (14 quotable) wins over
`peanut-butter-protein-bar-bundle` (12) and `peanut-butter-organic-protein-bar` (9).

A family ranks where its best member ranked, so the canonical adopts that member's fused
score. Without it the canonical carries its own lower score and the result list stops being
monotonic in rrf, which reads as a ranking bug.

---

## `attributes` — per-attribute reachability

Measures whether a **field** exists and how many products carry a value, not whether any given
product is answerable. A store can show `api` for allergens while 30 of its products carry no
declaration — `profile.declarations` is the per-product view and the two must be read together.

Matching is on key names and recovered labels, never on values. `custom.nutrients` is nutrition
because of its name; a key called `custom.product_blue_content` holding nutrition facts would
be missed. That is the price of a deterministic mapping, and it is why `_unmapped` is reported
rather than silently dropped.

`image` is the category the word-percentage coverage number cannot see at all. skout keeps
certifications and a nutrition panel in `file_reference` metafields, so that text exists
neither in the API nor in the page region, and coverage neither explains it nor counts it as
unreachable.

**Two shape inconsistencies to know about.** Within one attribute record, `theme` is a list of
label strings while `api` and `image` are lists of `{key, support}` dicts. And `_unmapped` is a
sentinel entry sharing the same dict with a different shape entirely — no `sources` or
`reachable`, and all three lists hold plain strings. Both are why `StoreProfile.attributes` is
typed as an open dict rather than a model.

---

## `documents` — building retrieval documents

**One document per trust class, not one per product.** Both classes must be retrievable —
unrendered enrichment is the most retrieval-useful content on some products — but the document
text is what an answer layer receives as grounding context, and a single mixed string carries
no marker separating a vetted nutrition panel from generated prose. The trust class has to live
on the chunk, because a class stored only on a sibling assertion row is not visible to whoever
reads `documents.text`.

**`free_from` never enters a document.** Its polarity is invisible to an embedding: writing
`Almonds; Cashews; Hazelnuts` for a product that contains none of them teaches the vector the
opposite of the fact. Polarity-bearing fields are filters, not prose, and negation is answered
in SQL.

Anything filterable stays a column. Filtering in SQL is exact; filtering by embedding
similarity is not.

Operates on Postgres rows, not `core.models.Product` — see the naming note in
`docs/ARCHITECTURE.md`.

---

## `models` — the shapes that cross boundaries

Field order **is** the JSON key order for `api.jsonl` and `profile.json`, so reordering a field
rewrites the artefact.

`IDENTITY_FIELDS` and `IDENTITY_ASSERTION_FIELDS` are deliberately different sets.
`IDENTITY_ASSERTION_FIELDS` (title, vendor, product_type) is what `merge` emits as assertions.
`IDENTITY_FIELDS` adds `handle` and is what `answering.stated` *excludes*, because a product's
own name is not an answer about it: without that exclusion the literal check passes `how many
tablets are in a pack` against the title `Deep Clean + Freshening Tablets`, which says nothing
about how many.

`attributes` and `template_constants` are typed as open dicts. Their entries are genuinely
polymorphic — see the `attributes` section above, and note that a `by_template` entry carries
an extra `block` key for single-product templates but not for templates with siblings. Strict
models would emit `null`s and change the artefact.

---

## `tuning` — the 28 thresholds

The single source of truth. Modules take these as parameters rather than redeclaring literals,
so an experiment is a config edit. Grouped by the stage each steers; see
`docs/ARCHITECTURE.md` for the operational-vs-algorithm split against `config/stores.yaml`.
