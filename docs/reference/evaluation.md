# `evaluation/` — measurement harnesses, not product

Harness specification: `docs/DESIGN.md` §8.

Neither module is shipped behaviour. Both exist to produce numbers nothing else measures.

---

## `harness` — recall@k, plus the safety measurement recall cannot make

**Recall alone scores a negation query as a pass** when it returns the expected products AND a
peanut-containing cookie alongside them. Constraint queries are therefore scored on whether
EVERY result satisfies the constraint, checked against the database rather than a hand-written
forbid list — a fixture can be passed by omitting the awkward product, a database check cannot.
Which of many valid products ranks first is relevance, reported as its own number so the two
cannot be traded off.

`undeclared_returns` is the real safety property, and it is checked against the database for
exactly that reason.

Duplicate listings are collapsed by `search`, so an expected handle can arrive as a sibling of
the canonical hit rather than as the canonical itself. Expectations are matched against both.
**Safety is not**: `undeclared_returns` and `forbid_handles` are checked against the returned
canonical handles only, because those are what an answer layer would quote.

A question with `expect_empty` passes only when nothing comes back. remi carries no free-from
declarations at all, so its negation queries must return nothing; without this the harness cannot
tell "correctly refused" from "found nothing useful".

### Two modes, scored separately

A discovery question ("a fruity snack bar for a toddler") is answered by ranking the catalogue,
and recall@5 is the right measure. A scoped question ("is it BPA free") arrives with the product
already known, and the only question is whether the fact is present and quotable.

Scoring the second as the first measures vocabulary distinctiveness: "tank" narrows remi to 7 of
48 products and "calories" narrows skout to 48 of 171, which is most of why remi looked better at
attributes than skout. Half the original attribute questions contain the word "it" — the tell.

`_evaluate_scoped` produces **one case per (question, product)**. All-or-nothing across a
question's whole scope hides the useful half of the answer: `what material is it made of` is
answerable on remi's water-flosser and not on either night guard, and merging those into one
failure loses exactly the thing worth acting on.

### Why a t-test, not a threshold

Recall at n=10 has a standard error near 0.15, so 0.70 is indistinguishable from 0.55. The
question sets are sized to make the number mean something, and `compare` exists because a
reranker's effect has to be measured rather than assumed.

`rerank_significance` replaces an older `resolution = 1/n` rule that asked whether a delta
cleared one question's worth of the metric. That is a question-count heuristic, not a test: it
cannot separate a real small effect from noise, and it says nothing about whether the same
questions moved. ranx pairs the arms per query and reports a p-value plus win/tie/loss. A NaN
p-value means the arms scored identically on every query.

### The ranx bridge

`_ir_outcomes` keeps only outcomes that are an IR task — a question that named handles. Scoped
outcomes also carry `has_expectation`, but they ask whether a fact is quotable on a product the
question already named; there is no ranking and no relevant-document set, so they are scored as
they always were and never reach ranx.

`_ir_pair` builds `(qrels, run)`. **The metric is `hit_rate@k`, not `recall@k`.** This harness has
always scored a discovery question as satisfied when ANY named handle comes back — several
questions name up to five interchangeable products, and finding one is the whole expectation.
`recall@k` would divide by the number named and score 1-of-3 as 0.33, silently redefining the
measurement.

`listed` carries family siblings as well as the hits themselves, because a collapsed duplicate
listing still counts as surfacing the product. It is **not** re-truncated to `top_k`: `search`
already returned only `top_k` hits, and the siblings hang off those hits rather than occupying
ranks of their own. Cutting the list again would discard sibling matches the harness has always
counted. Positions are scored by descending rank so ranx sees the order the shopper saw.

Two mechanical notes: ranx rejects a query with no retrieved documents, so an empty result set —
a real outcome here — is represented by a document that matches nothing; and `k` spans the whole
candidate list, because the `top_k` cut already happened in `search` and a smaller `k` here would
re-truncate collapsed siblings out of the measurement.

`blended_relevance` combines the two halves by count rather than averaging them, which is what
the hand-computed figure has always been.

`run` and `compare` return data only. All rendering lives in `presentation.render`.

---

## `chat` — a grounded answer layer, built to strengthen the evals

The end product is a storefront chatbot with its own service. This is a playground, and its value
is that it produces questions and a groundedness number that nothing else measures.

**One answer function, two callers.** The REPL and the batch harness both go through `answer`, so
they cannot disagree about what the model was shown. That is the whole reason the function exists
separately from either.

**Citations are the point.** The model is required to tag every claim with the id of the assertion
that supports it. Verification is then code, not judgement: the id must exist, it must belong to a
product in scope, and it must be `quotable`. A claim with no citation, or one citing a
retrieval-tier assertion, is ungrounded and counted as such. This is the actual product risk the
quotable/retrieval split exists to manage.

**Retrieval is unchanged and routing is absent.** Scope arrives as a parameter, exactly as a
product page would supply it in production. The model is never asked to infer which product is
meant, because a wrong answer would then be ambiguous between bad routing and bad retrieval — and
it is bad retrieval that is being measured.

LLM calls stay out of ingestion. This module is imported by the CLI only, never by a stage.

### Sentence splitting, which is subtler than it looks

Models put the ids after the full stop — `... phthalate-free. [a:74281]` — so a naive split leaves
every claim looking uncited and every citation looking like a sentence of its own. A fragment that
is nothing but citations belongs to the sentence before it.

A sentence therefore runs to its terminator and then swallows any citation group that follows, so
`... 36 Pack.[a:532][a:544] I cannot confirm ...` is two sentences and the second is correctly
seen as uncited. Splitting on whitespace after the terminator instead merges them, which lets an
unsupported claim hide behind the citations of the claim before it.

**A full stop between two digits is a decimal point, not a terminator.** Without that guard a 4.9
rating or a 6.7 oz tank shatters into `It has a 4.` and `9 oz tank ...`, and the fragments read as
uncited claims — which is most of what an early version of this scorer was actually counting.

### The four outcomes

`grounded`, `ungrounded`, `uncited`, `error`.

`uncited` means the answer carried no citations at all. That bundles two things this scorer
deliberately does not try to separate: a correct refusal, where the facts genuinely did not cover
the question, and an answer that simply asserted things without support. Telling them apart needs
to know whether the question was answerable, which the eval files express as an expectation and a
free-typed REPL turn does not carry.

Folding them either way would corrupt the number. Counting them grounded lets a model score
perfectly by refusing everything; counting them ungrounded punishes the one safe response
available when the catalogue is silent. So they are excluded from the ratio and reported alongside
their uncited sentences, which is what a reviewer needs in order to classify them by eye.

`to_question_yaml` emits the shape `config/questions/*.yaml` uses, so promoting a REPL turn into
the eval set is a copy.
