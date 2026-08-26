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
because a reranker's effect has to be measured rather than assumed. It is decided by a
paired t-test over the two arms (ranx), not by asking whether the delta clears one
question's worth of the metric -- that older rule could not tell a small real effect
from noise, nor say whether the same questions moved.
"""

from __future__ import annotations

from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

from . import db
from .answering import ProductNotFound, answer_for_product
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
        "expected": sorted(expected),
        "listed": listed,
        "top": handles[0] if handles else None,
        "returned": len(handles),
        "reranked": reranked,
    }



RELEVANCE_METRIC = "hit_rate@{k}"


def _ir_outcomes(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the outcomes that are an IR task: a question that named handles.

    Scoped outcomes also carry `has_expectation`, but they ask whether a fact is
    quotable on a product the question already named -- there is no ranking and no
    relevant-document set, so they are scored as they always were and never reach ranx.
    """
    return [o for o in outcomes if o.get("expected")]


def _ir_pair(outcomes: list[dict[str, Any]], top_k: int) -> tuple[dict, dict]:
    """`(qrels, run)` for ranx, from the outcomes the harness already produced.

    The metric is `hit_rate@k`, not `recall@k`. This harness has always scored a
    discovery question as satisfied when ANY named handle comes back -- several
    questions name up to five interchangeable products, and finding one is the whole
    expectation. `recall@k` would divide by the number named and score 1-of-3 as 0.33,
    silently redefining the measurement. `hit_rate@k` is what the hand-computed
    `relevance` has always been.

    `listed` carries family siblings as well as the hits themselves, because a collapsed
    duplicate listing still counts as surfacing the product. It is NOT re-truncated to
    `top_k`: `search` already returned only `top_k` hits, and the siblings hang off those
    hits rather than occupying ranks of their own. Cutting the list again would discard
    sibling matches the harness has always counted. Positions are scored by descending
    rank so ranx sees the order the shopper saw.
    """
    qrels: dict[str, dict[str, int]] = {}
    run: dict[str, dict[str, float]] = {}
    for i, outcome in enumerate(_ir_outcomes(outcomes)):
        qid = f"q{i}"
        qrels[qid] = {h: 1 for h in outcome["expected"]}
        listed = list(outcome.get("listed") or [])
        scores = {h: float(len(listed) - n) for n, h in enumerate(listed)}
        # ranx rejects a query with no retrieved documents; an empty result set is a
        # real outcome here, so it is represented by a document that matches nothing.
        run[qid] = scores or {"__none__": 0.0}
    return qrels, run


def ir_relevance(outcomes: list[dict[str, Any]], top_k: int) -> float | None:
    """`relevance@k` over the handle-naming questions, via ranx."""
    qrels, run = _ir_pair(outcomes, top_k)
    if not qrels:
        return None
    import warnings

    from ranx import Qrels, Run
    from ranx import evaluate as ranx_evaluate

    # k spans the whole candidate list: the top_k cut happened in `search`, and a
    # smaller k here would re-truncate collapsed siblings out of the measurement.
    k = max((len(docs) for docs in run.values()), default=top_k) or top_k
    with warnings.catch_warnings():
        # ranx's numba kernels warn about a uint64->int64 cast on every call. It is
        # internal to the metric and says nothing about this harness's data.
        warnings.simplefilter("ignore")
        metric = RELEVANCE_METRIC.format(k=k)
        # One metric in, one score out. ranx returns a {metric: score} dict only when
        # asked for several, so unwrap defensively rather than assume the scalar.
        scored = ranx_evaluate(Qrels(qrels), Run(run), metric)
    if isinstance(scored, dict):
        return float(scored[metric])
    return float(scored)


def blended_relevance(outcomes: list[dict[str, Any]], top_k: int) -> tuple[float, int, int]:
    """The reported `relevance@k`: ranx for the ranked half, as-scored for the rest.

    Returns `(rate, relevant, scored)`. The two halves are combined by count rather
    than averaged, which is what the hand-computed figure has always been.
    """
    scored = [o for o in outcomes if o["has_expectation"]]
    if not scored:
        return 0.0, 0, 0
    ranked = _ir_outcomes(outcomes)
    ir_value = ir_relevance(outcomes, top_k)
    ir_hits = int(round((ir_value or 0.0) * len(ranked)))
    other_hits = sum(
        1 for o in scored if not o.get("expected") and o["relevant"]
    )
    relevant = ir_hits + other_hits
    return relevant / len(scored), relevant, len(scored)


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
    relevance, relevant_count, scored_count = blended_relevance(outcomes, top_k)

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
            f"({relevant_count}/{scored_count})"
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
                "fused order on any exception, so a failed rerank looks exactly like "
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
        "outcomes": outcomes,
        "violations": sum(len(o["violations"]) for o in outcomes),
        "violating_questions": len(violating),
        "by_kind": {k: sum(v) / len(v) for k, v in per_kind.items()},
    }



SIGNIFICANCE_MAX_P = 0.05


def rerank_significance(
    with_outcomes: list[dict[str, Any]],
    without_outcomes: list[dict[str, Any]],
    top_k: int,
    max_p: float = SIGNIFICANCE_MAX_P,
) -> dict[str, Any] | None:
    """Paired t-test over the two arms, on the questions that name handles.

    Replaces the old `resolution = 1 / n` rule, which asked whether a delta cleared
    one question's worth of the metric. That is a question-count heuristic, not a test:
    it cannot separate a real small effect from noise, and it says nothing about whether
    the same questions moved. ranx pairs the arms per query and reports a p-value plus
    win/tie/loss, so DESIGN.md 10's "is the reranker worth keeping" is decided rather
    than eyeballed. A NaN p-value means the arms scored identically on every query.
    """
    qrels_map, run_with = _ir_pair(with_outcomes, top_k)
    _, run_without = _ir_pair(without_outcomes, top_k)
    if not qrels_map or set(run_with) != set(run_without):
        return None

    import warnings

    from ranx import Qrels, Run
    from ranx import compare as ranx_compare

    k = max((len(d) for d in run_with.values()), default=top_k) or top_k
    metric = RELEVANCE_METRIC.format(k=k)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        report = ranx_compare(
            Qrels(qrels_map),
            runs=[
                Run(run_with, name="with_rerank"),
                Run(run_without, name="without_rerank"),
            ],
            metrics=[metric],
            max_p=max_p,
        ).to_dict()

    with_side = report.get("with_rerank", {})
    p_value = with_side.get("comparisons", {}).get("without_rerank", {}).get(metric)
    wtl = with_side.get("win_tie_loss", {}).get("without_rerank", {}).get(metric, {})
    return {
        "metric": metric,
        "p_value": p_value,
        "max_p": max_p,
        "wins": wtl.get("W", 0),
        "ties": wtl.get("T", 0),
        "losses": wtl.get("L", 0),
        "with_score": with_side.get("scores", {}).get(metric),
        "without_score": report.get("without_rerank", {}).get("scores", {}).get(metric),
    }


def compare(store: StoreConfig, top_k: int = 5) -> dict[str, Any]:
    """Same questions with and without the cross-encoder, so its effect is measured."""
    with_rerank = run(store, top_k=top_k, rerank=True, quiet=True)
    without = run(store, top_k=top_k, rerank=False, quiet=True)
    delta = with_rerank["recall"] - without["recall"]

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
            "`search._rerank` swallows every exception; check `rerank_model` names a checkpoint flashrank can fetch."
        )
        return {"with_rerank": with_rerank, "without_rerank": without, "delta": None}

    sig = rerank_significance(
        with_rerank.get("outcomes") or [], without.get("outcomes") or [], top_k
    )
    console.print(f"delta (combined): [bold]{delta:+.3f}[/bold]")

    if sig is None:
        console.print(
            "[yellow]no ranked questions to test[/yellow] -- every question is scoped, "
            "so there is no ordering for a reranker to change."
        )
        return {
            "with_rerank": with_rerank,
            "without_rerank": without,
            "delta": delta,
            "significance": None,
        }

    p_value = sig["p_value"]
    unmoved = sig["wins"] == 0 and sig["losses"] == 0
    console.print(
        f"{sig['metric']}: with {sig['with_score']:.3f} vs without "
        f"{sig['without_score']:.3f} -- "
        f"{sig['wins']}W / {sig['ties']}T / {sig['losses']}L"
    )

    if unmoved:
        console.print(
            "[yellow]the reranker changed no question's outcome[/yellow] -- it ran, and "
            "every query scored the same in both arms. No paired test can separate "
            "these; on DESIGN.md 10's rule the reranker is not earning its place."
        )
    elif p_value is None or p_value != p_value:  # NaN
        console.print(
            f"[yellow]no usable p-value[/yellow] at max_p={sig['max_p']} -- "
            "treat the delta as unmeasured."
        )
    elif p_value <= sig["max_p"]:
        console.print(
            f"[green]significant[/green]: paired t-test p={p_value:.4f} "
            f"<= {sig['max_p']} -- the reranker {'helps' if delta >= 0 else 'hurts'}."
        )
    else:
        console.print(
            f"[yellow]not significant[/yellow]: paired t-test p={p_value:.4f} "
            f"> {sig['max_p']} -- the difference is noise at this question count."
        )

    return {
        "with_rerank": with_rerank,
        "without_rerank": without,
        "delta": delta,
        "significance": sig,
    }
