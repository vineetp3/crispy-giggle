"""Recall@k over the question sets, plus the safety measurement recall cannot make.

Scores retrieval; returns data only. Gotchas: docs/reference/evaluation.md
"""

from __future__ import annotations

from typing import Any

import yaml

from pier39_poc.core.matching import FREE_FROM_FIELD
from pier39_poc.core.tuning import DEFAULTS
from pier39_poc.infra import db
from pier39_poc.infra.config import REPO_ROOT, StoreConfig
from pier39_poc.retrieval.answering import ProductNotFound, answer_for_product
from pier39_poc.retrieval.search import search

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
    return [o for o in outcomes if o.get("expected")]


def _ir_pair(outcomes: list[dict[str, Any]], top_k: int) -> tuple[dict, dict]:
    qrels: dict[str, dict[str, int]] = {}
    run: dict[str, dict[str, float]] = {}
    for i, outcome in enumerate(_ir_outcomes(outcomes)):
        qid = f"q{i}"
        qrels[qid] = {h: 1 for h in outcome["expected"]}
        listed = list(outcome.get("listed") or [])
        scores = {h: float(len(listed) - n) for n, h in enumerate(listed)}
        run[qid] = scores or {"__none__": 0.0}
    return qrels, run


def ir_relevance(outcomes: list[dict[str, Any]], top_k: int) -> float | None:
    qrels, run = _ir_pair(outcomes, top_k)
    if not qrels:
        return None
    import warnings

    from ranx import Qrels, Run
    from ranx import evaluate as ranx_evaluate

    k = max((len(docs) for docs in run.values()), default=top_k) or top_k
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        metric = RELEVANCE_METRIC.format(k=k)
        scored = ranx_evaluate(Qrels(qrels), Run(run), metric)
    if isinstance(scored, dict):
        return float(scored[metric])
    return float(scored)


def blended_relevance(outcomes: list[dict[str, Any]], top_k: int) -> tuple[float, int, int]:
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


def run(store: StoreConfig, top_k: int = 5, rerank: bool = True) -> dict[str, Any]:
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

    discovery_hits = sum(1 for o in discovery_outcomes if o["ok"])
    scoped_hits = sum(1 for o in scoped_outcomes if o["ok"])

    return {
        "slug": store.slug,
        "top_k": top_k,
        "rerank_requested": rerank,
        "recall": recall,
        "discovery_recall": (
            discovery_hits / len(discovery_outcomes) if discovery_outcomes else None
        ),
        "scoped_answerability": (
            scoped_hits / len(scoped_outcomes) if scoped_outcomes else None
        ),
        "discovery_hits": discovery_hits,
        "discovery_total": len(discovery_outcomes),
        "scoped_hits": scoped_hits,
        "scoped_total": len(scoped_outcomes),
        "relevance": relevance,
        "relevant_count": relevant_count,
        "scored_count": scored_count,
        "reranked": reranked_any,
        "hits": hits,
        "total": len(outcomes),
        "outcomes": outcomes,
        "violations": sum(len(o["violations"]) for o in outcomes),
        "violating": violating,
        "violating_questions": len(violating),
        "by_kind": {k: sum(v) / len(v) for k, v in per_kind.items()},
        "by_kind_counts": {k: (sum(v), len(v)) for k, v in per_kind.items()},
    }


def rerank_significance(
    with_outcomes: list[dict[str, Any]],
    without_outcomes: list[dict[str, Any]],
    top_k: int,
    max_p: float = DEFAULTS.evaluation.significance_max_p,
) -> dict[str, Any] | None:
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
    with_rerank = run(store, top_k=top_k, rerank=True)
    without = run(store, top_k=top_k, rerank=False)

    if not with_rerank["reranked"]:
        return {
            "with_rerank": with_rerank,
            "without_rerank": without,
            "delta": None,
            "significance": None,
        }

    return {
        "with_rerank": with_rerank,
        "without_rerank": without,
        "delta": with_rerank["recall"] - without["recall"],
        "significance": rerank_significance(
            with_rerank.get("outcomes") or [], without.get("outcomes") or [], top_k
        ),
    }
