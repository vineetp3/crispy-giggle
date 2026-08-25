"""Recall@k, plus the safety measurement recall cannot make.

Gotchas:

Recall alone scores a negation query as a pass when it returns the expected products AND
a peanut-containing cookie alongside them. Constraint queries are therefore scored on
whether EVERY result satisfies the constraint, checked against the database rather than a
hand-written forbid list -- a fixture can be passed by omitting the awkward product, a
database check cannot. Which of many valid products ranks first is relevance, reported
as its own number so the two cannot be traded off.

A question with `expect_empty` passes only when nothing comes back. remi carries no
free-from declarations at all, so its negation queries must return nothing; without this
the harness cannot tell "correctly refused" from "found nothing useful".

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
        ok = any(h in expected for h in handles)
    else:
        ok = not unsafe

    return {
        "q": question["q"],
        "kind": question.get("kind", "general"),
        "ok": ok,
        "rank": next((n for n, h in enumerate(handles, 1) if h in expected), None),
        "violations": unsafe,
        "relevant": bool(expected) and any(h in expected for h in handles),
        "has_expectation": bool(expected),
        "top": handles[0] if handles else None,
        "returned": len(handles),
        "reranked": reranked,
    }


def run(store: StoreConfig, top_k: int = 5, rerank: bool = True, quiet: bool = False) -> dict[str, Any]:
    questions = load_questions(store.slug)
    outcomes = [_evaluate_one(q, store, top_k, rerank) for q in questions]

    hits = sum(1 for o in outcomes if o["ok"])
    violating = [o for o in outcomes if o["violations"]]
    recall = hits / len(outcomes) if outcomes else 0.0
    reranked_any = any(o["reranked"] for o in outcomes)
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

        console.print(f"recall@{top_k}: [bold]{recall:.2f}[/bold] ({hits}/{len(outcomes)})")
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
