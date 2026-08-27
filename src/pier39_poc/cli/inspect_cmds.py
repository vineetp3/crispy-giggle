"""Read-only commands over what the pipeline produced: report, eval, labels, compare-labels.

`report` is the deliverable. Details: docs/reference/presentation.md
"""

from __future__ import annotations

from typing import Any

import typer

from pier39_poc.cli.context import app, resolve_stores
from pier39_poc.core.models import StoreProfile
from pier39_poc.evaluation import harness
from pier39_poc.ingest import labels
from pier39_poc.ingest.profiling import load_profile
from pier39_poc.presentation import render, report
from pier39_poc.presentation.console import console


@app.command("report", help="The deliverable: per-store attribute reachability and admitted keys.")
def report_cmd(store: str | None = typer.Option(None, "--store")) -> None:
    for cfg in resolve_stores(store):
        report.render(cfg)


@app.command("eval", help="Score retrieval against config/questions: recall, relevance, violations.")
def eval_cmd(
    store: str | None = typer.Option(None, "--store"),
    top_k: int = typer.Option(5, "--top-k"),
    no_rerank: bool = typer.Option(False, "--no-rerank"),
    compare_rerank: bool = typer.Option(
        False, "--compare-rerank", help="run with and without the cross-encoder"
    ),
) -> None:
    from pier39_poc.retrieval.search import prepare_rerank

    results = {}
    for cfg in resolve_stores(store):
        if compare_rerank or not no_rerank:
            prepare_rerank(cfg.rerank_model)
        if compare_rerank:
            comparison = harness.compare(cfg, top_k=top_k)
            render.eval_comparison(comparison, top_k)
            results[cfg.slug] = comparison["with_rerank"]
        else:
            result = harness.run(cfg, top_k=top_k, rerank=not no_rerank)
            render.eval_result(result)
            results[cfg.slug] = result
    render.eval_summary_json(results)


def _label_stats(profile: StoreProfile) -> dict[str, dict[str, Any]]:
    tc = profile.template_constants
    sources = list(tc.per_product.items())
    sources += list(tc.by_template.items())
    stats: dict[str, dict[str, Any]] = {}
    for handle, pairs in sources:
        for pair in pairs:
            label = pair.get("label")
            if not label:
                continue
            entry = stats.setdefault(label, {"pairs": 0, "handles": set(), "examples": []})
            entry["pairs"] += 1
            entry["handles"].add(handle)
            if len(entry["examples"]) < 3:
                entry["examples"].append(pair["value"])
    return stats


@app.command("labels", help="Inventory the theme labels this store renders, to hand-label.")
def labels_cmd(
    store: str | None = typer.Option(None, "--store"),
    as_yaml: bool = typer.Option(False, "--yaml", help="emit a reference-set skeleton"),
) -> None:
    for cfg in resolve_stores(store):
        stats = _label_stats(load_profile(cfg))
        reference = labels.load_reference(cfg.slug)
        if as_yaml:
            render.label_reference_yaml(cfg.slug, stats, reference)
            continue
        render.label_inventory(cfg.slug, stats, reference)


def _label_examples(profile: StoreProfile) -> tuple[dict[str, list[str]], dict[str, int]]:
    tc = profile.template_constants
    groups = list(tc.per_product.values())
    groups += list(tc.by_template.values())
    examples: dict[str, list[str]] = {}
    pairs_per_label: dict[str, int] = {}
    for pairs in groups:
        for pair in pairs:
            label = pair.get("label")
            if not label:
                continue
            examples.setdefault(label, []).append(pair["value"])
            pairs_per_label[label] = pairs_per_label.get(label, 0) + 1
    return examples, pairs_per_label


def _score_against_reference(
    examples: dict[str, list[str]],
    pairs_per_label: dict[str, int],
    reference: dict[str, str],
    verdicts: dict[str, str],
) -> tuple[list[tuple[str, int, str, str, bool]], dict[str, Any]]:
    rows: list[tuple[str, int, str, str, bool]] = []
    agree = scored = tp = fp = fn = 0
    for label in sorted(examples, key=lambda x: -pairs_per_label[x]):
        key = labels.normalise(label)
        ref = reference.get(key)
        got = verdicts.get(key, labels.UNCERTAIN)
        if ref is None:
            continue
        scored += 1
        same = ref == got
        agree += 1 if same else 0
        if ref == labels.WIDGET and got == labels.WIDGET:
            tp += 1
        elif ref != labels.WIDGET and got == labels.WIDGET:
            fp += 1
        elif ref == labels.WIDGET and got != labels.WIDGET:
            fn += 1
        rows.append((label, pairs_per_label[label], ref, got, same))

    pairs_wrong = sum(
        pairs_per_label[lbl]
        for lbl in examples
        if reference.get(labels.normalise(lbl))
        and reference[labels.normalise(lbl)]
        != verdicts.get(labels.normalise(lbl), labels.UNCERTAIN)
    )
    metrics = {
        "agree": agree,
        "scored": scored,
        "precision": tp / (tp + fp) if (tp + fp) else None,
        "recall": tp / (tp + fn) if (tp + fn) else None,
        "pairs_wrong": pairs_wrong,
        "pairs_total": sum(pairs_per_label.values()),
    }
    return rows, metrics


@app.command("compare-labels", help="Score the llm label policy against the hand-authored reference set.")
def compare_labels_cmd(store: str | None = typer.Option(None, "--store")) -> None:
    for cfg in resolve_stores(store):
        examples, pairs_per_label = _label_examples(load_profile(cfg))
        reference = labels.load_reference(cfg.slug)
        if not reference:
            console.print(f"[yellow]{cfg.slug}: no reference set, nothing to score[/yellow]")
            continue

        verdicts = labels.ClassifierPolicy().warm(cfg, examples)
        rows, metrics = _score_against_reference(
            examples, pairs_per_label, reference, verdicts
        )
        render.label_comparison(cfg.slug, rows, metrics)
