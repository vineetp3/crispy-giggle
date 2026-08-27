"""Terminal rendering for every command: hits, facts, chat turns, eval results.

The only module that formats for a human, so the layers below return data.
Details: docs/reference/presentation.md
"""

from __future__ import annotations

import json
from typing import Any

from rich.table import Table

from pier39_poc.ingest import labels
from pier39_poc.presentation.console import console


def stores_table(configs: list[Any]) -> None:
    table = Table(title="stores")
    for column in ("slug", "domain", "scope", "profile_pages", "fetch", "threshold"):
        table.add_column(column)
    for cfg in configs:
        table.add_row(
            cfg.slug, cfg.domain, cfg.crawl_scope, str(cfg.profile_pages),
            cfg.fetch_profile, str(cfg.tuning.blocks.chrome_threshold),
        )
    console.print(table)


def as_of(row: dict) -> str:
    stamp = row.get("source_updated_at")
    if stamp is None or row.get("trust_class") != "quotable":
        return ""
    return f"  [dim](as of {stamp.date().isoformat()})[/dim]"


def search_diagnostics(diag, filtered: bool) -> None:
    if diag.live_read_failed:
        console.print(
            f"[red]the live price and stock read failed[/red] -- {diag.live_read_error}."
        )
        if filtered:
            console.print(
                "[red]  --max-price and --in-stock reject any product whose price could "
                "not be confirmed, so an empty result here means the lookup died, not "
                "that nothing matched.[/red]"
            )
    if diag.rerank_failed:
        console.print(
            f"[red]the reranker did not run[/red] -- {diag.rerank_error}. Results are "
            "the fused order only."
        )


def no_results() -> None:
    console.print("[yellow]no results[/yellow]")


def search_hits(hits: list[Any]) -> None:
    for n, hit in enumerate(hits, 1):
        header = f"[bold]{n}. {hit.title}[/bold]  [dim]{hit.store_slug}/{hit.handle}[/dim]"
        console.print(header)
        scores = f"rrf={hit.rrf:.4f} vec={hit.vector_rank} lex={hit.lexical_rank}"
        if hit.rerank_score is not None:
            scores += f" rerank={hit.rerank_score:.3f}"
        ground = "groundable" if hit.groundable else "match-only"
        console.print(f"   [dim]{scores}  chunk={hit.chunk_key} ({ground})[/dim]")
        if hit.live:
            console.print(
                f"   [green]live[/green]: {hit.live['available']}/{hit.live['variants']} "
                f"available, price {hit.live['min_price']}-{hit.live['max_price']}"
            )
        if hit.siblings:
            console.print(f"   [dim]also listed as: {', '.join(hit.siblings)}[/dim]")
        for row in hit.matched_fields[:6]:
            mark = "Q" if row["trust_class"] == "quotable" else "r"
            label = row.get("label") or row["field"]
            console.print(
                f"   [{mark}] {label}: {str(row['value'])[:110]}{as_of(row)}"
            )
        if hit.online_store_url:
            console.print(f"   [blue]{hit.online_store_url}[/blue]")
        console.print()


def product_answer(answer: Any, attribute_names: list[str]) -> None:
    console.print(f"[bold]{answer.title}[/bold]  [dim]{answer.store_slug}/{answer.handle}[/dim]")
    if answer.family:
        console.print(f"   [dim]also listed as: {', '.join(answer.family)}[/dim]")
    if answer.live:
        console.print(
            f"   [green]live[/green]: {answer.live['available']}/{answer.live['variants']} "
            f"available, price {answer.live['min_price']}-{answer.live['max_price']}"
        )

    for outcome in answer.free_from:
        if not outcome.has_declaration:
            console.print(
                f"   [yellow]{outcome.term}: NO free-from declaration on this product -- "
                f"unknown, not 'free of it'[/yellow]"
            )
        elif outcome.declared_free:
            console.print(f"   [green]{outcome.term}: declared free of it[/green]")
        else:
            console.print(f"   [red]{outcome.term}: declares free-from, and does NOT list it[/red]")

    console.print()
    for name in attribute_names:
        rows = answer.answers(name)
        mark = "[green]YES[/green]" if rows else "[dim]no [/dim]"
        detail = "; ".join(
            f"{r.get('label') or r['field']}: {str(r['value'])[:48]}" for r in rows[:2]
        )
        console.print(f"   {mark} {name:15} {detail}")

    console.print(
        f"\n   [dim]{len(answer.quotable)} quotable / {len(answer.retrieval)} retrieval "
        f"assertions across {len(answer.family) + 1} listing(s), "
        f"{len(answer.documents)} document(s)[/dim]"
    )


def chat_turn(turn: Any, show_facts: bool, show_diag: bool) -> None:
    if turn.error:
        console.print(f"[red]{turn.error}[/red]")
        return

    console.print()
    console.print(turn.text)
    console.print()

    if show_facts:
        table = Table(title="what the model was shown", show_lines=False)
        table.add_column("id", justify="right")
        table.add_column("tier")
        table.add_column("label")
        table.add_column("value", overflow="fold")
        cited = {c.assertion_id for c in turn.citations}
        for row in turn.shown_quotable:
            marker = " *" if row.get("id") in cited else ""
            table.add_row(
                f"{row.get('id')}{marker}",
                "[green]quotable[/green]",
                str(row.get("label") or row.get("field")),
                (row.get("value") or "")[:70],
            )
        for row in turn.shown_retrieval:
            marker = " *" if row.get("id") in cited else ""
            table.add_row(
                f"{row.get('id')}{marker}",
                "[yellow]background[/yellow]",
                str(row.get("label") or row.get("field")),
                (row.get("value") or "")[:70],
            )
        console.print(table)
        console.print("[dim]* was cited[/dim]")

    bad = [c for c in turn.citations if not c.valid]
    uncited = turn.uncited_sentences
    verdict = {
        "grounded": "[green]grounded[/green]",
        "ungrounded": "[red]NOT grounded[/red]",
        "uncited": "[yellow]no citations -- refusal, or unsupported[/yellow]",
        "error": "[red]error[/red]",
    }[turn.outcome]
    console.print(
        f"{verdict}  {len(turn.citations) - len(bad)}/{len(turn.citations)} citations valid"
        + (f", {len(uncited)} uncited sentence(s)" if uncited else "")
    )
    for citation in bad:
        console.print(f"  [red]a:{citation.assertion_id}[/red] -- {citation.reason}")
    for sentence in uncited:
        console.print(f"  [red]no citation[/red] -- {sentence[:90]}")

    if show_diag:
        if turn.hits:
            hit_table = Table(title="retrieval", show_lines=False)
            hit_table.add_column("handle")
            hit_table.add_column("vec", justify="right")
            hit_table.add_column("lex", justify="right")
            hit_table.add_column("rrf", justify="right")
            hit_table.add_column("rerank", justify="right")
            for hit in turn.hits:
                hit_table.add_row(
                    hit.handle,
                    "-" if hit.vector_rank is None else str(hit.vector_rank),
                    "-" if hit.lexical_rank is None else str(hit.lexical_rank),
                    f"{hit.rrf:.4f}",
                    "-" if hit.rerank_score is None else f"{hit.rerank_score:.3f}",
                )
            console.print(hit_table)
        if turn.diagnostics is not None:
            search_diagnostics(turn.diagnostics, filtered=False)


def replay_scores(slug: str, model: str, result: dict[str, Any]) -> None:
    table = Table(title=f"{slug}: groundedness ({model})")
    table.add_column("#", justify="right")
    table.add_column("mode")
    table.add_column("question", overflow="fold")
    table.add_column("cites", justify="right")
    table.add_column("ok", justify="center")
    marks = {
        "grounded": "[green]y[/green]",
        "ungrounded": "[red]n[/red]",
        "uncited": "[yellow]-[/yellow]",
        "error": "[red]![/red]",
    }
    for i, turn in enumerate(result["turns"], 1):
        bad = len([c for c in turn.citations if not c.valid])
        mark = marks[turn.outcome]
        label = turn.question if not turn.handle else f"{turn.question}  [{turn.handle}]"
        table.add_row(
            str(i),
            turn.mode,
            label[:70],
            f"{len(turn.citations) - bad}/{len(turn.citations)}",
            mark,
        )
    console.print(table)
    console.print(
        f"[bold]{slug}[/bold]: groundedness {result['groundedness']:.2f} "
        f"({result['grounded']} grounded / {result['ungrounded']} ungrounded), "
        f"{result['uncited']} uncited, {result['errors']} error(s)"
    )
    console.print(
        f"[dim]{result['invalid_citations']} invalid citation(s), "
        f"{result['uncited_sentences']} uncited sentence(s). Answers with no citations are "
        f"excluded from the ratio: a correct refusal and an unsupported assertion are "
        f"not distinguishable without knowing the question was answerable.[/dim]"
    )


def label_reference_yaml(slug: str, stats: dict[str, Any], reference: dict[str, str]) -> None:
    print(f"# {slug}: hand-authored label reference set")
    print("labels:")
    for label in sorted(stats):
        current = reference.get(labels.normalise(label), "uncertain")
        print(f"  {json.dumps(label)}: {current}")


def label_inventory(slug: str, stats: dict[str, Any], reference: dict[str, str]) -> None:
    table = Table(title=f"{slug}: per-product theme labels")
    table.add_column("label")
    table.add_column("pairs", justify="right")
    table.add_column("products", justify="right")
    table.add_column("reference")
    table.add_column("example")
    for label in sorted(stats, key=lambda x: -stats[x]["pairs"]):
        entry = stats[label]
        table.add_row(
            label,
            str(entry["pairs"]),
            str(len(entry["handles"])),
            reference.get(labels.normalise(label), "-"),
            (entry["examples"][0] or "")[:58],
        )
    console.print(table)
    console.print(
        f"[dim]{sum(e['pairs'] for e in stats.values())} pairs, "
        f"{len(stats)} distinct labels[/dim]"
    )


def label_comparison(slug: str, rows: list[tuple[str, int, str, str, bool]], metrics: dict[str, Any]) -> None:
    table = Table(title=f"{slug}: llm vs reference")
    table.add_column("label")
    table.add_column("pairs", justify="right")
    table.add_column("reference")
    table.add_column("llm")
    table.add_column("agree")
    for label, pairs, ref, got, same in rows:
        table.add_row(label, str(pairs), ref, got, "yes" if same else "[red]no[/red]")
    console.print(table)

    precision = metrics["precision"]
    recall = metrics["recall"]
    console.print(
        f"[bold]{slug}[/bold]: agreement {metrics['agree']}/{metrics['scored']}"
        + (f", widget precision {precision:.2f}" if precision is not None else "")
        + (f", widget recall {recall:.2f}" if recall is not None else "")
    )
    console.print(
        f"[dim]{metrics['pairs_wrong']} of {metrics['pairs_total']} pairs "
        f"affected by a disagreement[/dim]"
    )


def eval_result(result: dict[str, Any]) -> None:
    top_k = result["top_k"]
    outcomes = result["outcomes"]

    table = Table(
        title=f"eval: {result['slug']} (recall@{top_k}, rerank={result['rerank_requested']})"
    )
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

    if result["scoped_total"]:
        d_total = result["discovery_total"]
        d_ok = result["discovery_hits"]
        s_ok = result["scoped_hits"]
        console.print(
            f"discovery recall@{top_k}: [bold]"
            f"{(d_ok / d_total if d_total else 0):.2f}[/bold] "
            f"({d_ok}/{d_total})  -- can the catalogue surface the product"
        )
        console.print(
            f"scoped answerability: [bold]{(s_ok / result['scoped_total']):.2f}[/bold] "
            f"({s_ok}/{result['scoped_total']})  -- is the fact present and quotable on a known product"
        )
    console.print(f"combined: [bold]{result['recall']:.2f}[/bold] ({result['hits']}/{result['total']})")
    console.print(
        f"relevance@{top_k} (expected handle in results, where one was named): "
        f"[bold]{result['relevance']:.2f}[/bold] "
        f"({result['relevant_count']}/{result['scored_count']})"
    )
    for kind, (kind_hits, kind_total) in sorted(result["by_kind_counts"].items()):
        rate = kind_hits / kind_total
        colour = "green" if rate >= 0.7 else "yellow" if rate > 0 else "red"
        console.print(
            f"  {kind}: [{colour}]{rate:.2f}[/{colour}] ({kind_hits}/{kind_total})"
        )

    violating = result["violating"]
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

    if result["rerank_requested"] and not result["reranked"]:
        console.print(
            "[red]the reranker did not run[/red] -- `search._rerank` degrades to the "
            "fused order on any exception, so a failed rerank looks exactly like "
            "a reranker that changed nothing. These numbers are RRF only."
        )

    if result["recall"] < 0.70:
        console.print(
            "[yellow]below the 0.70 bar in DESIGN.md section 2. The bar is "
            "arbitrary; what matters is that it is measured.[/yellow]"
        )


def eval_comparison(result: dict[str, Any], top_k: int) -> None:
    with_rerank = result["with_rerank"]
    without = result["without_rerank"]

    table = Table(title=f"rerank A/B: {with_rerank['slug']} (recall@{top_k})")
    table.add_column("configuration")
    table.add_column("recall", justify="right")
    table.add_column("hits", justify="right")
    table.add_column("violations", justify="right")
    for name, arm in (("with rerank", with_rerank), ("without rerank", without)):
        table.add_row(
            name,
            f"{arm['recall']:.3f}",
            f"{arm['hits']}/{arm['total']}",
            str(arm["violations"]),
        )
    console.print(table)

    if not with_rerank["reranked"]:
        console.print(
            "[red]INVALID COMPARISON: the reranker never executed[/red] -- both arms are "
            "RRF only, so the delta below is structurally zero and measures nothing. "
            "`search._rerank` swallows every exception; check `rerank_model` names a checkpoint flashrank can fetch."
        )
        return

    delta = result["delta"]
    console.print(f"delta (combined): [bold]{delta:+.3f}[/bold]")

    sig = result["significance"]
    if sig is None:
        console.print(
            "[yellow]no ranked questions to test[/yellow] -- every question is scoped, "
            "so there is no ordering for a reranker to change."
        )
        return

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
    elif p_value is None or p_value != p_value:
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


def eval_summary_json(results: dict[str, dict[str, Any]]) -> None:
    console.print(
        json.dumps(
            {
                k: {"recall": v["recall"], "violations": v["violations"]}
                for k, v in results.items()
            },
            indent=2,
        )
    )
