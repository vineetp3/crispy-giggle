# Findings

**Measured:** 2026-08-25, second run, against live `skout` and `remi`.
**Spec:** `docs/DESIGN.md`. This document records what the runs measured and what the
implementation changed. Numbers here move on every run; the spec does not.

**These numbers are not comparable to the first 2026-08-25 run.** Three things changed between
them: both stores now crawl their full catalogue rather than a 40-page sample (skout went from 20
crawled pages to 152), products alone on their template get an extra cross-page boilerplate pass,
and duplicate listings are collapsed at retrieval. The first of those moves the denominator of
every hit rate, so key admissions and assertion counts shift for reasons that have nothing to do
with the store.

---

## 1. Store profiles

| | skout | remi |
|---|---|---|
| Products / published | 201 / 184 | 48 / 30 |
| Pages crawled | **152** (was 20) | 30 |
| Abandoned SKUs excluded | 32 | 0 |
| Products indexed | 169 | 48 |
| Metafield keys admitted / rejected | 22 / 49 | 17 / 64 |
| Keys classed `rendered` | 7 | 2 |
| Assertions (quotable / retrieval) | 2,185 (1,473 / 712) | 636 (384 / 252) |
| Theme-sourced assertions | 118 | 73 |
| Labelled theme constants | 23 | 56 |
| Coverage, word-weighted diagnostic | 11.6% | 4.8% |

skout's assertion count fell from 2,440 on the first run. That is the sampling bias being removed,
not content being lost: hit rate is scored against crawled products, and the denominator went from
20 pages to 152, so keys that cleared `allowlist_min_hit_rate` on a small sample no longer do.
Seven keys are now classed `rendered` against six before. remi's assertion count is unchanged at
606 while its coverage rose from 4.0% to 4.7% — the new boilerplate pass removed cross-page
marketing prose without touching a single attribute, which is exactly what it was for.

### Attribute reachability — the deliverable

| attribute | skout | remi |
|---|---|---|
| allergens | api | absent |
| nutrition | api + image | absent |
| ingredients | api + image | absent |
| materials | absent | theme |
| power | absent | theme |
| dimensions | theme | theme |
| care | absent | theme |
| compatibility | absent | theme |
| usage | api + theme | absent |
| certifications | image | absent |
| **totals** | api 4, theme 2, image 3, absent 4 | api 0, **theme 5**, image 0, absent 5 |

Unchanged from the first run, and holding it unchanged took a correction — see §3.

**remi cannot answer a single attribute from the API.** Materials, power, dimensions, care and
compatibility are all theme-resident, so remi requires scheduled page polling. skout has one
theme-only attribute and does not.

This is the number the coverage percentage was meant to produce and could not. Coverage is
word-weighted, so it tracks review volume as much as API completeness, and its denominator moves
with `chrome_threshold` and the page sample, which makes two stores' percentages incomparable.

`image` is a category the word count cannot see in either direction. skout keeps
`custom.nutrition_facts_image` on 123 products and `display.certification` on 158; that text is
in neither the API nor the page region, so coverage neither explains it nor counts it as
unreachable.

---

## 2. Success criteria

| # | Criterion | Result |
|---|---|---|
| 1 | `fetch-api` retrieves both catalogues on the admin token | **pass** — 201 and 48 products, 4,975 and 1,221 metafields |
| 2 | `fetch-html` escalates through remi's Cloudflare interstitial | **pass** — 30 of 30 pages |
| 3 | `custom.nutrients` admitted despite `Protein [1g]` → `Protein 1g` | **pass** — rendered, 7/8 |
| 4 | The three contaminated keys rejected | **pass** — see §3 on the reason code |
| 5 | At least three opaque fields get labels | **pass** — 26 labels; see §3 on the surface |
| 6 | remi's four spec labels captured as template constants | **pass** — all four with values |
| 7 | `report` emits reachability and coverage | **pass** |
| 8 | recall@5 ≥ 0.70, zero violations | **pass** — discovery 0.92 and 1.00, zero violations. Scoped answerability is reported separately and is not a recall number |

### Eval

Scored in two modes since the scoped/discovery split. They are different tasks and merging them
was hiding the most important thing in this document.

| | skout | remi |
|---|---|---|
| **discovery recall@5** — can the catalogue surface the product | **0.92** (23/25) | **1.00** (22/22) |
| **scoped answerability** — is the fact present and quotable on a known product | **0.87** (20/23) | **0.65** (13/20) |
| Constraint violations | **0** | **0** |

**The split inverts the earlier conclusion, and the earlier one was wrong.** Scored as
catalogue-wide retrieval, remi's attribute score was 0.92 against skout's 0.50, which read as
"remi is in better shape". Scored as answerability it is remi 0.65 against skout 0.87 — the
opposite. The old number was measuring whether the right product could be *found*, and remi aced
that because its vocabulary is distinctive: "tank" narrows remi to 7 of 48 products while
"calories" narrows skout to 48 of 171. It was never measuring whether remi could *answer*.

The corrected picture agrees with the store profiles rather than contradicting them. remi has
**0 API-reachable attributes** and depends entirely on theme content; skout has 4 and they are
densely populated.

**Scoped scoring immediately found a real extraction bug, which is the point of measuring it.**
remi's night-guard pages state `Material: Dental-grade polymer, BPA-free, and phthalate-free.`
in plain sight, and the pipeline reported no material at all. Two defects stacked:

- **Group chrome was discarded before template constants were extracted.** A block on every page
  of a template is either furniture or a spec shared by every product of that type, and the
  second was being thrown away. Any template group with two or more crawled pages lost its whole
  spec table; groups with one crawled page kept theirs. That is why `water-flosser` — alone on
  its template — accounted for 5 of remi's 8 passes, while the night guards, in a 3-page group,
  had nothing.
- **A spec rendered as one text run could not be read.** `label_for` only sees a label node
  followed by a value node. `Material: Dental-grade polymer` in a single run was invisible.

Fixing both moved remi from 0.40 to 0.65 and skout from 0.78 to 0.87, took remi from `theme 5` to
`theme 6` attributes (ingredients became reachable), and raised labelled theme constants to 56 on
remi and 23 on skout. Discovery recall did not move, which is the expected signature of a content
bug rather than a retrieval one.

**The fix leaked promotions, and needed its own guard.** Recovering group-chrome specs pulled in
`Birthday Sale: 50% Off` as a quotable fact on 22 remi products. `is_commerce_fact` could not
catch it — it keys on namespace, key and type, none of which a theme constant has.
`is_commerce_constant` keys on discount language instead of on the presence of a number, because
`Formula: 3.8% Hydrogen Peroxide` is a real concentration. Verified 0 promotional theme facts
quotable on either store.

**Two claims made here earlier were wrong, and the correction matters more than the numbers.**
This document previously called the missing tablet count and the missing removal-tool material
"genuine content gaps". Both facts are on the page:

- `deep-clean-freshening-tablets` renders `Quantity: 120 tablets (roughly 4 months of daily use)`.
  The pair extracts cleanly and is discarded because `quantity` sits on the not-a-label denylist
  in `blocks.py`, next to `select`, `filter` and `qty`, where it exists to reject the cart
  quantity picker.
- `mouth-night-guard-removal-tool` renders `Material: Food-grade material, BPA-free, and
  phthalate-free`. It is discarded because theme facts are only written from template constants —
  pairs shared across a template group — and a spec on a single product's page is classed
  per-product theme content and dropped.

So of remi's seven remaining scoped failures, at least two are extraction defects rather than
missing content, and the same is true of an unknown share of the rest. **Treat the current scoped
answerability numbers as a floor, not a measurement of what these stores can answer.**
`docs/PENDING.md` carries the analysis and the options.

A related distinction still holds and is worth keeping: store-level reachability and per-product
answerability are different things. `attributes.py` warns about the gap, and the scoped score is
the first thing to measure it.

`deep-clean-freshening-tablets` produced a **false pass** before identity fields were excluded:
the literal check matched the product's own title, `Deep Clean + Freshening Tablets`, which says
nothing about how many are in a pack. `title`, `vendor`, `product_type` and `handle` stay
quotable — they have to be — but they restate identity rather than assert a property, so
`ProductAnswer.stated` drops them before any attribute or literal check. That one fix moved remi
from 0.45 to 0.40, which is the number to trust. skout's two failures are shelf life
and pack size — and shelf life *does* exist as a theme constant, which means it is reachable but
not attached to the products the question names.

Discovery recall is now measured on a cleaner question set and reads 0.92 / 1.00, against 0.82 /
0.97 when the scoped questions were polluting it.

**Every one of these numbers is RRF only.** The reranker has never executed — see §4.

---

## 3. Where the implementation differs from the original spec

Each of these was forced by measurement, not preference.

**Matching needs locality, not just overlap.** Token overlap over a whole page is not a render
signal. `custom.product_faqs` scored 0.833 on a skout page carrying none of it, with 10 of its
60 tokens absent entirely. Requiring the tokens to co-occur inside one window drops it to 0.00
while `custom.nutrients` holds at 0.88. On remi, 82 of 144 matches were non-contiguous before
the gate.

**Hit rate must be scored against crawled products.** Scoring against every product carrying a
value caps the rate at crawled/total: on a 20-page sample `custom.nutrients` read 7/48 = 0.15
against a 0.8 bar, so no key could ever be classed `rendered` and everything fell through to
`partially_rendered`, which downstream read as quotable.

**Quotability is decided by type and shape, not by render presence.** The page is rendered from
the metafield, so a match proves only that the theme consumed the key. skout's
`custom.short_description` renders on 18 of 18 sampled products and is generated marketing prose;
`custom.nutrients` is a typed list of checkable facts whose theme presence is incidental.

**Freshness does not gate quotability.** Median metafield age on skout is 1,138 days for
`custom.nutrients`, 1,020 for `filter.contains` and 1,286 for `filter.curated`. An age cliff
empties the quotable set rather than making it safer. What v0 does instead is render the source
date on every quotable fact, in `search` and in `report`, so a two-year-old allergen declaration
is visibly two years old. `free_from` averages 719 days across 130 products and reaches 1,098.

**Cross-page boilerplate removal needs a length guard, not just a page count.** This one was
caught by regression rather than foresight. Products alone on their template get no per-group
differencing pass, so a 3-page absolute floor was added for them. Shipped without a length guard
it destroyed real attributes: remi lost `compatibility` and skout lost both `dimensions` and
`usage`, dropping from `theme 2` to `theme 0`. Real attributes repeat across sibling products in
exactly the same way boilerplate does, and raising the page floor does not separate them — at 5
pages remi recovered and skout did not. Length does: the target copy is a 1,500–2,000 word
testimonial, while attributes are short `label: value` pairs. Requiring **both** 3 pages and 20
words keeps every attribute on both stores and still lifts remi's coverage from 4.0% to 4.7%.

**Criterion 4's reason code.** `product_seo.seo_tags` carries no product ID, GID or URL, which is
why the original spec recorded it as undetectable. It is caught by a same-store title check
instead: `foreign_product_title`, at 12 of 32 values.

**Criterion 5's surface.** The criterion named metafield keys. Of 26 labels recovered, 24 are
theme-resident — remi's `Material`, `Battery life`, `Power`, `Tank capacity`, `Tank volume`,
`Treatment duration` and ten more. Restricting the criterion to metafields measured the wrong
surface.

**Coverage fell from 20.2% / 10.3% to 10.6% / 4.0%,** and the new figures are the first honest
ones. Two defects inflated the old numbers: the rich-text AST leak put `root`, `paragraph` and
`text` into the explained-token set, and the residual pass used bag-of-words coverage with no
window, so remi's `30 days per charge with daily use` scored 5/7 against `descriptionHtml` on
the scattered words `30 daily days use with` and the battery spec was silently deleted. Do not
compare these numbers to earlier ones.

---

## 4. What has never run

| | why it matters |
|---|---|
| **The reranker** | `COHERE_API_KEY` is the literal placeholder `xxx` from `.env.example`; Cohere returns 401; `search._rerank` swallows it. Every recall figure is RRF only, and the decision to put Cohere in v0 is untested. |
| **The Storefront read** | `PIER39_SHOPIFY_STOREFRONT_TOKENS` is unset, so every live read used the Admin fallback. The market-pricing justification for that change is unverified. |
| ~~The sampler floor~~ | Closed. It could never have been exercised: `profile_pages: 40` cannot reach `GROUP_FLOOR = 3` across skout's 28 template groups. Both stores now crawl in full — skout at 152 pages, 0 failures — and no group with 3 or more products has fewer than 3 crawled pages. |

The first two no longer fail silently: `search` reports a failed rerank or live read, and the CLI
says explicitly that an empty result under `--max-price` may mean the lookup died rather than that
nothing matched. Verified by pointing the Admin client at a bogus token. They still degrade rather
than raise, which is right for a shopper query.

The first two fail silently by design — degrading rather than erroring is right for resilience
and wrong for confidence. `eval --compare-rerank` now detects and refuses to report a delta when
the reranker did not execute; the Storefront fallback has no equivalent detector.

**Whether reranking belongs in v0 at all is now an open question, not an assumption.** Recall
without it is 0.79 and 0.97, and at 170 and 48 products RRF over vector plus full-text puts the
answer inside the top-50 nearly always — a cross-encoder is reordering a set that already
contains it. `DESIGN.md` §10 records the decision rule, the finding that OpenAI has no rerank
endpoint, and the candidate alternatives with their dependency weights.

---

## 5. Data-quality findings, per store

**Commerce facts were being stored and quoted.** remi's `water-flosser` carried seven quotable
price assertions, all written 2026-01-23: six copies of `55.00` across `custom.current_price`,
`banner_pricing`, `smarterr_app_price`, `smarterr_single_price`, `smartrr_otp_price` and
`price_promotion_text`, plus a `yellow_badge_save_amount` of `$55.00` labelled a saving while
equal to full price. Six remi products held 8 price rows with 6 distinct values each. Thirteen
remi keys and two skout keys are now rejected as `commerce_fact`.

**31 of skout's 184 published products are abandoned records.** Priced 0.00 with
`inventoryQuantity` at -770, -101, -14; five of eight sampled shadow a live twin under a legacy
handle (`lemon-zest-protein-bar` at 0.00 beside `skout-organic-lemon-zest-protein-bar` at
35.99). They were 40% of a sampled candidate pool. Negative inventory alone does not identify
them: remi runs continue-selling and has 23 of 30 products at negative quantity, all buyable.

**skout's live catalogue splits three ways:** 58.2% buyable, 17.0% abandoned, 24.7% transient
stockout. remi: 100% buyable.

**remi holds zero free-from declarations** across all 30 published products, so every allergen
negation query there correctly returns nothing. skout can answer for 152 of 184; the other 30
are excluded from negation queries rather than admitted on absent evidence.

**Review counts conflict across apps.** skout's peanut-butter cookie reports 72
(`reviews.rating_count`), 63 (`stamped.reviews_count`) and 4.8 (`okendo.summaryData`); remi
reports 51, 627 and 1193 for one product. Both products now get no review count. 73 of skout's
products keep a count because only one app answers.

**Text-only contamination is real and partly ambiguous.** skout's `global.description_tag`
describes a different product on 19 of 145 values; `custom.short_description` on 9 of 91. Both
sit below the 25% rejection bar and are admitted with a review flag, because at low rates
flavour-family overlap and genuine cross-sell copy are indistinguishable from contamination.

**skout's theme presents non-product content as `label: value` pairs.** A shipping calendar
(`February: 2/12`), a rating widget (`FIND IN A SKOUT BAR: 4.5`) and colon-terminated prose
(`We also ship internationally to: Australia`). All are excluded, by a four-word label cap and a
numeric-value rule.

**remi's own spec table is mislabelled.** `Tank capacity:` is paired with `Cordless and
portable, no sink required`. The extraction is faithful; the source is wrong. Worth raising with
the publisher.

---

## 6. Verification

90 tests, covering block extraction and differencing, the cross-page rule and its length guard,
value normalisation, locality matching, contamination in all four forms, attribute reachability,
family collapse and canonical selection, retrieval diagnostics, product-scoped attribute
lookup, the three-way free-from outcome, inline theme labels, template-constant recovery, the
theme commerce guard, and eval scoring.

Asserted against the live database after the final run:

- 0 commerce assertions stored, either store
- 0 quotable booleans, hex colours or unix timestamps
- 0 abandoned SKUs reachable through search
- 0 undeclared products returned by any negation query
- 0 constraint violations across 67 eval questions
- every template group with 3 or more sellable products has at least 3 crawled pages

**One exception to a previously absolute claim.** This file used to assert 0 quotable values
containing a currency amount. There is now exactly 1, and it is not a leak through the metafield
path: it is a remi product *title*, `Night Guard Cleaning + Teeth Whitening Foam (SALES DISCOUNT)
$15`, authored that way by the merchant. Titles must stay quotable, so the honest statement is
that no price *metafield* reaches a quotable assertion, and that a merchant can still put a price
in a name. Worth raising with the publisher for the same reason as the mislabelled tank capacity
in §5.

---

## 7. The label gate, and the LLM classifier arm

Measured 2026-08-25, both pilot stores, full catalogues, no reranking.

### What was actually broken

`docs/PENDING.md` §1 recorded two open causes. Investigation found a third, which was the
one that mattered, and corrected the account of the second.

**The denylist.** `blocks.NOT_A_LABEL_PATTERNS` rejected any label beginning `quantity` or
`qty`. remi's tablets render `Quantity: 120 tablets (roughly 4 months of daily use)`, a real
specification. Removed from the global list; the existing numeric-value check still rejects
a bare integer, so a quantity stepper does not become a fact.

**A missing numeric guard.** The `label_for` path applied no numeric check to the value, so
count and date blocks paired with adjacent headings. Removing four junk skout pairs,
including `This item = 354` and `December = 12/19`, cost nothing.

**Eligibility, which was the real cause.** Labelled pairs were only ever formed from
*residual* blocks — those not already explained by an admitted metafield or the description
prose. A specification whose text also appears in `description_html` was therefore never
turned into a typed pair, even though the page renders an explicit label for it. remi's
removal tool is the clearest case: `Material: Food-grade material, BPA-free, and
phthalate-free` is on the page **and** inside the description, so it was dropped and the
product had no `materials` attribute. The label is precisely the structure that turns prose
into a checkable fact, so discarding a labelled pair because the prose already contains the
sentence discards the only thing worth having.

This corrects `PENDING.md` §1b. Per-product specs were not lost because `merge` writes only
template constants; most of remi's are in singleton template groups and were already stored.
They were lost at extraction.

### The gate

Extraction now recovers labelled pairs from the whole product region and deduplicates them
against the template constants already emitted. That yields 8 net-new pairs on remi and 86
on skout, and 38 distinct labels across both stores — small enough to hand-label.

Three verdicts. `spec` may become quotable, `uncertain` becomes a retrieval assertion,
`widget` is not stored. Unrecognised labels are `uncertain`, so the default for anything
nobody has ruled on is findable but never repeated to a shopper as fact. The deterministic
guards still run afterwards; no policy can promote past them.

### The regression guard was already breached

`PENDING.md` specified that `This item`, `Pack Size` and `Delivery Frequency` must never be
quotable on skout. They already were, before any of this work, as **template constants** —
101 `Pack Size`, 10 `Size` and 2 `Delivery Frequency` quotable assertions. The gate
therefore had to cover template constants too, demote-only, so that a label nobody has
ruled on keeps its existing behaviour.

### The three arms

| arm | remi scoped | skout scoped | remi assertions | skout assertions | widget labels quotable on skout |
|---|---|---|---|---|---|
| `none` (control) | 0.70 | 0.87 | 640 (388 q) | 2,184 (1,480 q) | yes, 124 |
| `static` | **0.75** | 0.78 | 641 (389 q) | 2,062 (1,356 q) | no |
| `llm` | 0.70 | 0.87 | 631 (378 q) | 2,187 (1,480 q) | yes, 111 |

Discovery recall@5 was unchanged in every arm — 1.00 on remi, 0.92 on skout — and constraint
violations stayed 0 throughout. The control is not the 0.65 / 0.87 recorded earlier: the
three extraction fixes alone lifted remi from 0.65 to 0.70 before any policy ran.

### The classifier lost, and lost on the case it was meant to win

`gpt-4o-mini`, one call per distinct label, cached and committed. Scored against the
hand-authored reference sets:

| store | agreement | widget precision | widget recall | pairs affected by a disagreement |
|---|---|---|---|---|
| remi | 22/30 | 0.50 | 0.60 | 15 of 65 |
| skout | 4/8 | 0.67 | 0.67 | 48 of 105 |

It read skout's `Pack Size` and `Size` as specifications, which is what reintroduced the
breach, and read remi's `Quantity` as a widget — the single case the whole item existed to
fix. It also demoted `Power` and `Tank capacity` on remi. Its errors are not random: it
tracks how a label *sounds* rather than what the store does with it, which is the same
failure mode as the global regular expression it was meant to replace.

The pre-registered rule was that the classifier must beat the reference set by more than the
metric's resolution. It matched the ungated control on both stores instead. `--label-policy
llm` stays available and off by default.

**One caveat on the comparison.** A single model and prompt were tested. The result shows
this classifier does not beat a hand-authored list on two stores where the list was authored
by someone who had read the pages; it does not show that no classifier could. The
generalisation question — a store nobody has looked at — is untouched by this measurement
and belongs with third-store validation.

### skout's fall from 0.87 to 0.78 is a judgement, not a defect

Two questions of the form *how many bars come in a pack* were previously answered by `Pack
Size`, the variant picker. Suppressing it removes those answers. A picker does list the
purchasable sizes, so calling that answer wrong is a position, not a fact. The reference set
takes the conservative one: a control's current selection is not a durable property of the
product. Reversing it for this store is one line —
`spec_label_allow: ["Pack Size"]` in `config/stores.yaml` — and would make 101 picker values
quotable again. That trade belongs with whoever owns the risk of quoting them.
