"""Recall@k, plus the safety measurement recall cannot make.

Gotchas:

Recall alone scores a negation query as a pass when it returns the expected products AND
a peanut-containing cookie alongside them. Constraint queries are therefore scored on
whether EVERY result satisfies the constraint, checked against the database rather than a
hand-written forbid list -- a fixture can be passed by omitting the awkward product, a
database check cannot. Which of many valid products ranks first is relevance, reported
as its own number so the two cannot be traded off.

Duplicate listings are collapsed by `search`, so an expected handle can arrive as a
sibling of the canonical hit rather than as the canonical itself. Expectations are matched
against both. Safety is NOT: `undeclared_returns` and `forbid_handles` are checked against
the returned canonical handles only, because those are what an answer layer would quote.

A question with `expect_empty` passes only when nothing comes back. remi carries no
free-from declarations at all, so its negation queries must return nothing; without this
the harness cannot tell "correctly refused" from "found nothing useful".

Two modes, scored separately, because they are not the same task. A discovery question
("a fruity snack bar for a toddler") is answered by ranking the catalogue, and recall@5 is
the right measure. A scoped question ("is it BPA free") arrives with the product already
known -- from a product page, or the previous turn -- and the only question is whether the
fact is present and quotable. Scoring the second as the first measures vocabulary
distinctiveness: "tank" narrows remi to 7 of 48 products and "calories" narrows skout to 48
of 171, which is most of why remi looked better at attributes than skout. Half the original
attribute questions contain the word "it", which is the tell -- there is no "it" in a
catalogue-wide search.

Recall at n=10 has a standard error near 0.15, so 0.70 is indistinguishable from 0.55.
The question sets are sized to make the number mean something, and `compare` exists
because a reranker whose effect is smaller than the metric's resolution is unfalsifiable.
"""

from __future__ import annotations

from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

from . import db
from .config import REPO_ROOT, StoreConfig
from .answering import ProductNotFound, answer_for_product
from .matching import FREE_FROM_FIELD
from .search import search

console = Console()
QUESTIONS_DIR = REPO_ROOT / "config" / "questions"


def load_questions(slug: str) -> list[dict[str, Any]]:
    path = QUESTIONS_DIR / f"{slug}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no questions file: {path}")
    payload = yaml.safe_load(path.read_text()) or {}
    questions = payload.get("questions") or []
    if not questions:
        raise ValueError(f"{path} has no questions")
    return questions


def undeclared_returns(slug: str, handles: list[str], terms: list[str]) -> list[str]:
    """Returned products that do not declare freedom from every excluded term.

    This is the real safety property and it is checked against the database rather than a
    hand-written forbid list, so it cannot be passed by omitting an awkward product from
    the fixture.
    """
    if not handles or not terms:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT p.handle, count(DISTINCT t.term) AS declared
            FROM products p
            JOIN stores s ON s.id = p.store_id
            CROSS JOIN unnest(%(terms)s::text[]) AS t(term)
            LEFT JOIN field_assertions fa
              ON fa.product_id = p.id
             AND fa.field = %(field)s
             AND fa.value ILIKE '%%' || t.term || '%%'
            WHERE s.slug = %(slug)s AND p.handle = ANY(%(handles)s)
            GROUP BY p.handle
            """,
            {
                "terms": sorted({t.strip().lower() for t in terms if t.strip()}),
                "handles": handles,
                "slug": slug,
                "field": FREE_FROM_FIELD,
            },
        ).fetchall()
    wanted = len({t.strip().lower() for t in terms if t.strip()})
    covered = {r["handle"] for r in rows if int(r["declared"]) == wanted}
    return sorted(set(handles) - covered)


def _evaluate_scoped(
    question: dict[str, Any], store: StoreConfig, handle: str
) -> dict[str, Any]:
    """Score one question against one known product.

    The product is a parameter, not something to infer. Passing means the fact is present
    AND quotable for that product -- not that it ranked well, which is what the
    catalogue-wide path measures and what these questions were never asking.

    One case per (question, product). All-or-nothing across a question's whole scope hides
    the useful half of the answer: `what material is it made of` is answerable on remi's
    water-flosser and not on either night guard, and merging those into one failure loses
    exactly the thing worth acting on.
    """
    attribute = question.get("expect_attribute")
    literal = (question.get("expect_value_contains") or "").strip().lower()
    exclude = question.get("exclude_terms") or []

    unsafe: list[str] = []
    try:
        answer = answer_for_product(store, handle, exclude_terms=exclude, live=False)
    except ProductNotFound:
        return {
            "q": f"{question['q']}  [{handle}]",
            "kind": question.get("kind", "attribute"),
            "mode": "scoped",
            "ok": False,
            "rank": None,
            "violations": [],
            "relevant": False,
            "has_expectation": bool(attribute or literal),
            "top": "product not found",
            "returned": 0,
            "reranked": False,
        }

    found = True
    why = ""
    if attribute:
        found = answer.can_answer(attribute)
        why = "" if found else f"no quotable {attribute}"
    if found and literal:
        found = any(literal in (a["value"] or "").lower() for a in answer.stated)
        why = "" if found else f"no quotable value containing {literal!r}"

    for outcome in answer.free_from:
        if not outcome.has_declaration:
            unsafe.append(f"{handle}: no free-from declaration for {outcome.term}")

    ok = found and not unsafe
    return {
        "q": f"{question['q']}  [{handle}]",
        "kind": question.get("kind", "attribute"),
        "mode": "scoped",
        "ok": ok,
        "rank": None,
        "violations": unsafe,
        "relevant": ok,
        "has_expectation": bool(attribute or literal),
        "top": why or handle,
        "returned": len(answer.quotable),
        "reranked": False,
    }


def _evaluate_one(
    question: dict[str, Any], store: StoreConfig, top_k: int, rerank: bool
) -> dict[str, Any]:
    expected = set(question.get("expect_handles") or [])
    forbidden = set(question.get("forbid_handles") or [])
    expect_empty = bool(question.get("expect_empty"))
    exclude = question.get("exclude_terms") or []

    results = search(
        question["q"],
        store,
        slug=store.slug,
        top_k=top_k,
        exclude_terms=exclude,
        rerank=rerank,
        live_prices=False,
    )
    handles = [r.handle for r in results]
    listed = handles + [s for r in results for s in r.siblings]
    reranked = any(r.rerank_score is not None for r in results)
    unsafe = sorted(
        (set(handles) & forbidden)
        | set(undeclared_returns(store.slug, handles, exclude))
    )

    if expect_empty:
        ok = not handles
    elif exclude:
        ok = bool(handles) and not unsafe
    elif expected:
        ok = any(h in expected for h in listed)
    else:
        ok = not unsafe

    return {
        "q": question["q"],
        "kind": question.get("kind", "general"),
        "ok": ok,
        "rank": next(
            (
                n
                for n, r in enumerate(results, 1)
                if r.handle in expected or any(s in expected for s in r.siblings)
            ),
            None,
        ),
        "violations": unsafe,
        "relevant": bool(expected) and any(h in expected for h in listed),
        "has_expectation": bool(expected),
        "top": handles[0] if handles else None,
        "returned": len(handles),
        "reranked": reranked,
    }


def run(store: StoreConfig, top_k: int = 5, rerank: bool = True, quiet: bool = False) -> dict[str, Any]:
    questions = load_questions(store.slug)
    outcomes: list[dict[str, Any]] = []
    for q in questions:
        if q.get("scope"):
            outcomes.extend(_evaluate_scoped(q, store, h) for h in q["scope"])
        else:
            outcomes.append(_evaluate_one(q, store, top_k, rerank))

    hits = sum(1 for o in outcomes if o["ok"])
    violating = [o for o in outcomes if o["violations"]]
    recall = hits / len(outcomes) if outcomes else 0.0
    reranked_any = any(o["reranked"] for o in outcomes)
    discovery_outcomes = [o for o in outcomes if o.get("mode") != "scoped"]
    scoped_outcomes = [o for o in outcomes if o.get("mode") == "scoped"]
    scored = [o for o in outcomes if o["has_expectation"]]
    relevance = (
        sum(1 for o in scored if o["relevant"]) / len(scored) if scored else 0.0
    )

    per_kind: dict[str, list[bool]] = {}
    for outcome in outcomes:
        per_kind.setdefault(outcome["kind"], []).append(outcome["ok"])

    if not quiet:
        table = Table(title=f"eval: {store.slug} (recall@{top_k}, rerank={rerank})")
        table.add_column("#", justify="right")
        table.add_column("question", overflow="fold")
        table.add_column("kind")
        table.add_column("hit", justify="center")
        table.add_column("rank", justify="right")
        table.add_column("top result", overflow="fold")
        for i, outcome in enumerate(outcomes, 1):
            mark = "[green]yes[/green]" if outcome["ok"] else "[red]no[/red]"
            if outcome["violations"]:
                mark = "[red]VIOLATION[/red]"
            table.add_row(
                str(i),
                outcome["q"],
                outcome["kind"],
                mark,
                str(outcome["rank"]) if outcome["rank"] else "-",
                outcome["top"] or "[dim]nothing[/dim]",
            )
        console.print(table)

        discovery = [o for o in outcomes if o.get("mode") != "scoped"]
        scoped = [o for o in outcomes if o.get("mode") == "scoped"]
        if scoped:
            d_ok = sum(1 for o in discovery if o["ok"])
            s_ok = sum(1 for o in scoped if o["ok"])
            console.print(
                f"discovery recall@{top_k}: [bold]"
                f"{(d_ok / len(discovery) if discovery else 0):.2f}[/bold] "
                f"({d_ok}/{len(discovery)})  -- can the catalogue surface the product"
            )
            console.print(
                f"scoped answerability: [bold]{(s_ok / len(scoped)):.2f}[/bold] "
                f"({s_ok}/{len(scoped)})  -- is the fact present and quotable on a known product"
            )
        console.print(f"combined: [bold]{recall:.2f}[/bold] ({hits}/{len(outcomes)})")
        console.print(
            f"relevance@{top_k} (expected handle in results, where one was named): "
            f"[bold]{relevance:.2f}[/bold] "
            f"({sum(1 for o in scored if o['relevant'])}/{len(scored)})"
        )
        for kind, results in sorted(per_kind.items()):
            rate = sum(results) / len(results)
            colour = "green" if rate >= 0.7 else "yellow" if rate > 0 else "red"
            console.print(
                f"  {kind}: [{colour}]{rate:.2f}[/{colour}] ({sum(results)}/{len(results)})"
            )

        if violating:
            console.print(
                f"[red]{len(violating)} constraint violation(s) -- a returned product "
                "contradicts the query's exclusion. This is a safety failure, not a "
                "ranking miss:[/red]"
            )
            for outcome in violating:
                console.print(f"  [red]{outcome['q']}[/red] -> {outcome['violations']}")
        else:
            console.print("[green]0 constraint violations[/green]")

        if rerank and not reranked_any:
            console.print(
                "[red]the reranker did not run[/red] -- `search._rerank` degrades to the "
                "fused order on any exception, so a bad COHERE_API_KEY looks exactly like "
                "a reranker that changed nothing. These numbers are RRF only."
            )

        if recall < 0.70:
            console.print(
                "[yellow]below the 0.70 bar in DESIGN.md section 2. The bar is "
                "arbitrary; what matters is that it is measured.[/yellow]"
            )

    return {
        "recall": recall,
        "discovery_recall": (
            sum(1 for o in discovery_outcomes if o["ok"]) / len(discovery_outcomes)
            if discovery_outcomes else None
        ),
        "scoped_answerability": (
            sum(1 for o in scoped_outcomes if o["ok"]) / len(scoped_outcomes)
            if scoped_outcomes else None
        ),
        "relevance": relevance,
        "reranked": reranked_any,
        "hits": hits,
        "total": len(outcomes),
        "violations": sum(len(o["violations"]) for o in outcomes),
        "violating_questions": len(violating),
        "by_kind": {k: sum(v) / len(v) for k, v in per_kind.items()},
    }


def compare(store: StoreConfig, top_k: int = 5) -> dict[str, Any]:
    """Same questions with and without the cross-encoder, so its effect is measured."""
    with_rerank = run(store, top_k=top_k, rerank=True, quiet=True)
    without = run(store, top_k=top_k, rerank=False, quiet=True)
    delta = with_rerank["recall"] - without["recall"]
    resolution = 1.0 / with_rerank["total"] if with_rerank["total"] else 1.0

    table = Table(title=f"rerank A/B: {store.slug} (recall@{top_k})")
    table.add_column("configuration")
    table.add_column("recall", justify="right")
    table.add_column("hits", justify="right")
    table.add_column("violations", justify="right")
    for name, result in (("with rerank", with_rerank), ("without rerank", without)):
        table.add_row(
            name,
            f"{result['recall']:.3f}",
            f"{result['hits']}/{result['total']}",
            str(result["violations"]),
        )
    console.print(table)

    if not with_rerank["reranked"]:
        console.print(
            "[red]INVALID COMPARISON: the reranker never executed[/red] -- both arms are "
            "RRF only, so the delta below is structurally zero and measures nothing. "
            "`search._rerank` swallows every exception; check COHERE_API_KEY."
        )
        return {"with_rerank": with_rerank, "without_rerank": without, "delta": None}

    verdict = (
        "below the metric's resolution -- not measurable at this question count"
        if abs(delta) < resolution
        else ("helps" if delta > 0 else "hurts")
    )
    console.print(
        f"delta: [bold]{delta:+.3f}[/bold] against a resolution of {resolution:.3f} "
        f"(1 question) -- {verdict}"
    )
    return {"with_rerank": with_rerank, "without_rerank": without, "delta": delta}
