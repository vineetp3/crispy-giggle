"""A grounded answer layer, built to strengthen the evals rather than to ship a product.

The end product is a storefront chatbot with its own service. This is a playground, and its
value is that it produces questions and a groundedness number that nothing else measures.

One answer function, two callers. The REPL and the batch harness both go through `answer`,
so they cannot disagree about what the model was shown. That is the whole reason the
function exists separately from either.

**Citations are the point.** The model is required to tag every claim with the id of the
assertion that supports it. Verification is then code, not judgement: the id must exist, it
must belong to a product in scope, and it must be `quotable`. A claim with no citation, or
one citing a retrieval-tier assertion, is ungrounded and counted as such. This is the actual
product risk the quotable/retrieval split exists to manage, and until now nothing measured
whether the split survives contact with a model.

**Retrieval is unchanged and routing is absent.** Scope arrives as a parameter, exactly as a
product page would supply it in production. The model is never asked to infer which product
is meant, because a wrong answer would then be ambiguous between bad routing and bad
retrieval, and it is bad retrieval that is being measured.

`DESIGN.md` §9 keeps LLM calls out of ingestion. This module is imported by the CLI only,
never by a stage, so that line stays honest.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import llm
from .answering import ProductAnswer, ProductNotFound, answer_for_product
from .config import StoreConfig
from .search import Diagnostics, Hit, search

CHAT_MODEL = os.environ.get("PIER39_CHAT_MODEL", "gpt-5.5")

MAX_RETRIEVAL_SHOWN = 12

SYSTEM = """You are a shopping assistant for an online store. Answer only from the FACTS
below.

Every FACT carries an id in square brackets. Each sentence of your answer that states
anything about a product must end with the ids that support it, like [a:1234] or
[a:1234][a:1237]. A sentence with no id will be treated as unsupported and shown to a
reviewer as a failure.

FACTS are split into two tiers and the difference matters:

QUOTABLE facts are vetted and may be stated to the shopper as fact.
BACKGROUND facts are retrieved prose. Use them to decide what is relevant or to say that
something is described somewhere, but never state a BACKGROUND fact as a checkable claim
about the product, and never cite one as support for a specific attribute.

If the FACTS do not answer the question, say so plainly and say what is missing. Do not
guess, do not use outside knowledge about these products or brands, and do not invent ids.
Keep the answer to a few sentences."""

CITATION_RE = re.compile(r"\[a:(\d+)\]")

# A sentence runs to its terminator and then swallows any citation group that follows it,
# so `... 36 Pack.[a:532][a:544] I cannot confirm ...` is two sentences and the second is
# correctly seen as uncited. Splitting on whitespace after the terminator instead merges
# them, which lets an unsupported claim hide behind the citations of the claim before it.
#
# A full stop between two digits is a decimal point, not a terminator. Without that guard a
# 4.9 rating or a 6.7 oz tank shatters into `It has a 4.` and `9 oz tank ...`, and the
# fragments read as uncited claims -- which is most of what an early version of this scorer
# was actually counting.
_TERMINATOR = r"(?:(?<!\d)\.(?!\d)|[!?])+"
_SENTENCE_RE = re.compile(
    r"(?:[^.!?]|(?<=\d)\.(?=\d))+" + _TERMINATOR + r"(?:\s*\[a:\d+\])*"
)


@dataclass
class Citation:
    assertion_id: int
    valid: bool
    reason: str = ""
    label: str | None = None
    value: str | None = None


@dataclass
class Turn:
    question: str
    mode: str
    store_slug: str
    handle: str | None
    text: str
    shown_quotable: list[dict[str, Any]] = field(default_factory=list)
    shown_retrieval: list[dict[str, Any]] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)
    diagnostics: Diagnostics | None = None
    error: str | None = None
    request_shape: dict[str, Any] = field(default_factory=dict)

    @property
    def sentences(self) -> list[str]:
        """Sentences, with trailing citation groups attached to what they support.

        Models put the ids after the full stop -- `... phthalate-free. [a:74281]` -- so a
        naive split leaves every claim looking uncited and every citation looking like a
        sentence of its own. A fragment that is nothing but citations belongs to the
        sentence before it.
        """
        text = (self.text or "").strip()
        if not text:
            return []
        out: list[str] = []
        cursor = 0
        for match in _SENTENCE_RE.finditer(text):
            part = match.group(0).strip()
            if part:
                out.append(part)
            cursor = match.end()
        tail = text[cursor:].strip()
        if tail:
            out.append(tail)
        return out

    @property
    def uncited_sentences(self) -> list[str]:
        return [s for s in self.sentences if not CITATION_RE.search(s)]

    @property
    def outcome(self) -> str:
        """One of `grounded`, `ungrounded`, `uncited` or `error`.

        `uncited` means the answer carried no citations at all. That bundles two things
        this scorer deliberately does not try to separate: a correct refusal, where the
        facts genuinely did not cover the question, and an answer that simply asserted
        things without support. Telling them apart needs to know whether the question was
        answerable, which the eval files express as an expectation and a free-typed REPL
        turn does not carry.

        Folding them either way would corrupt the number. Counting them grounded lets a
        model score perfectly by refusing everything; counting them ungrounded punishes the
        one safe response available when the catalogue is silent. So they are excluded from
        the ratio and reported alongside their uncited sentences, which is what a reviewer
        needs in order to classify them by eye.
        """
        if self.error:
            return "error"
        if not self.citations:
            return "uncited"
        if all(c.valid for c in self.citations) and not self.uncited_sentences:
            return "grounded"
        return "ungrounded"

    @property
    def grounded(self) -> bool:
        return self.outcome == "grounded"

    def to_question_yaml(self) -> dict[str, Any]:
        """The shape `config/questions/*.yaml` uses, so promoting a turn is a copy."""
        out: dict[str, Any] = {"q": self.question, "kind": "attribute"}
        if self.handle:
            out["scope"] = [self.handle]
        else:
            out["expect_handles"] = [h.handle for h in self.hits[:3]]
        return out

    def to_log(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "mode": self.mode,
            "store": self.store_slug,
            "handle": self.handle,
            "answer": self.text,
            "outcome": self.outcome,
            "grounded": self.grounded,
            "citations": [
                {"id": c.assertion_id, "valid": c.valid, "reason": c.reason}
                for c in self.citations
            ],
            "uncited_sentences": self.uncited_sentences,
            "shown_quotable_ids": [a.get("id") for a in self.shown_quotable],
            "shown_retrieval_ids": [a.get("id") for a in self.shown_retrieval],
            "hits": [h.handle for h in self.hits],
            "question_yaml": self.to_question_yaml(),
            "request_shape": self.request_shape,
            "error": self.error,
        }


def _fact_line(assertion: dict[str, Any]) -> str:
    label = assertion.get("label") or assertion.get("field")
    value = (assertion.get("value") or "").strip()
    aid = assertion.get("id")
    return f"[a:{aid}] {label}: {value}"


def _render_facts(
    quotable: list[dict[str, Any]], retrieval: list[dict[str, Any]]
) -> str:
    blocks = ["QUOTABLE FACTS"]
    if quotable:
        blocks.extend(_fact_line(a) for a in quotable)
    else:
        blocks.append("(none)")
    blocks.append("")
    blocks.append("BACKGROUND FACTS")
    if retrieval:
        blocks.extend(_fact_line(a) for a in retrieval)
    else:
        blocks.append("(none)")
    return "\n".join(blocks)


def _verify(
    cited: list[int],
    quotable: list[dict[str, Any]],
    retrieval: list[dict[str, Any]],
) -> list[Citation]:
    by_id = {int(a["id"]): a for a in quotable if a.get("id") is not None}
    background = {int(a["id"]): a for a in retrieval if a.get("id") is not None}
    out: list[Citation] = []
    for aid in cited:
        if aid in by_id:
            row = by_id[aid]
            out.append(
                Citation(aid, True, "", row.get("label") or row.get("field"), row.get("value"))
            )
        elif aid in background:
            row = background[aid]
            out.append(
                Citation(
                    aid,
                    False,
                    "cites a background fact as if it were quotable",
                    row.get("label") or row.get("field"),
                    row.get("value"),
                )
            )
        else:
            out.append(Citation(aid, False, "no such assertion was shown"))
    return out


def _scoped(
    store: StoreConfig, question: str, handle: str, live: bool
) -> tuple[list[dict], list[dict], list[Hit], Diagnostics, ProductAnswer | None]:
    diag = Diagnostics()
    try:
        product = answer_for_product(store, handle, live=live, diagnostics=diag)
    except ProductNotFound:
        return [], [], [], diag, None
    return list(product.quotable), list(product.retrieval), [], diag, product


def _discovery(
    store: StoreConfig, question: str, top_k: int, live: bool
) -> tuple[list[dict], list[dict], list[Hit], Diagnostics, None]:
    diag = Diagnostics()
    hits = search(
        question,
        store,
        slug=store.slug,
        top_k=top_k,
        live_prices=live,
        diagnostics=diag,
    )
    quotable: list[dict[str, Any]] = []
    retrieval: list[dict[str, Any]] = []
    seen: set[int] = set()
    for hit in hits:
        for row in hit.matched_fields:
            aid = row.get("id")
            if aid is None or int(aid) in seen:
                continue
            seen.add(int(aid))
            tagged = dict(row)
            tagged["label"] = f"{hit.handle} -- {row.get('label') or row.get('field')}"
            (quotable if row["trust_class"] == "quotable" else retrieval).append(tagged)
    return quotable, retrieval, hits, diag, None


def answer(
    store: StoreConfig,
    question: str,
    handle: str | None = None,
    top_k: int = 5,
    live: bool = False,
    client: Any = None,
    model: str = CHAT_MODEL,
) -> Turn:
    """The single grounded call. Both the REPL and the batch harness use this."""
    mode = "scoped" if handle else "discovery"
    if handle:
        quotable, retrieval, hits, diag, product = _scoped(store, question, handle, live)
        if product is None:
            return Turn(
                question, mode, store.slug, handle, "",
                error=f"no product with handle {handle!r} in {store.slug}",
            )
    else:
        quotable, retrieval, hits, diag, _ = _discovery(store, question, top_k, live)

    retrieval = retrieval[:MAX_RETRIEVAL_SHOWN]
    prompt = (
        f"{SYSTEM}\n\n"
        f"{_render_facts(quotable, retrieval)}\n\n"
        f"SHOPPER QUESTION: {question}\n\nANSWER:"
    )
    turn = Turn(
        question=question,
        mode=mode,
        store_slug=store.slug,
        handle=handle,
        text="",
        shown_quotable=quotable,
        shown_retrieval=retrieval,
        hits=hits,
        diagnostics=diag,
    )
    try:
        completion = llm.complete(
            llm.client_or_default(client), model, prompt, budget=1200
        )
        turn.text = completion.text
        turn.request_shape = completion.params
    except Exception as exc:
        turn.error = f"{type(exc).__name__}: {exc}"
        return turn

    cited = [int(m) for m in CITATION_RE.findall(turn.text)]
    turn.citations = _verify(cited, quotable, retrieval)
    return turn


def replay(
    store: StoreConfig,
    questions: list[dict[str, Any]],
    top_k: int = 5,
    client: Any = None,
    model: str = CHAT_MODEL,
) -> dict[str, Any]:
    """Run a question list through the SAME answer function and score groundedness."""
    turns: list[Turn] = []
    for question in questions:
        scope = question.get("scope") or []
        if scope:
            turns.extend(
                answer(store, question["q"], handle=h, top_k=top_k, client=client, model=model)
                for h in scope
            )
        else:
            turns.append(
                answer(store, question["q"], top_k=top_k, client=client, model=model)
            )

    counts = {"grounded": 0, "ungrounded": 0, "uncited": 0, "error": 0}
    for turn in turns:
        counts[turn.outcome] += 1
    citations = [c for t in turns for c in t.citations]
    attempted = counts["grounded"] + counts["ungrounded"]
    return {
        "turns": turns,
        "total": len(turns),
        "errors": counts["error"],
        "grounded": counts["grounded"],
        "ungrounded": counts["ungrounded"],
        "uncited": counts["uncited"],
        "groundedness": counts["grounded"] / attempted if attempted else 0.0,
        "citations": len(citations),
        "invalid_citations": len([c for c in citations if not c.valid]),
        "uncited_sentences": sum(len(t.uncited_sentences) for t in turns),
    }


def append_log(path, turn: Turn) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(turn.to_log(), ensure_ascii=False, default=str) + "\n")
