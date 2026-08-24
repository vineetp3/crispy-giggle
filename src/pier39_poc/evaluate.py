"""Recall@k against hand-written questions.

Without this the POC is a demo. With it, a threshold or prompt change can be judged.
Questions must include negation and attribute cases, because those are what the design
is betting on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

from .config import REPO_ROOT, StoreConfig
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


def run(store: StoreConfig, top_k: int = 5, rerank: bool = True) -> dict[str, Any]:
    questions = load_questions(store.slug)

    table = Table(title=f"eval: {store.slug} (recall@{top_k})")
    table.add_column("#", justify="right")
    table.add_column("question", overflow="fold")
    table.add_column("kind")
    table.add_column("hit", justify="center")
    table.add_column("rank", justify="right")
    table.add_column("top result", overflow="fold")

    hits = 0
    per_kind: dict[str, list[bool]] = {}

    for i, question in enumerate(questions, 1):
        text = question["q"]
        expected = set(question.get("expect_handles") or [])
        kind = question.get("kind", "general")
        exclude = question.get("exclude_terms") or []

        results = search(
            text,
            store,
            slug=store.slug,
            top_k=top_k,
            exclude_terms=exclude,
            rerank=rerank,
            live_prices=False,
        )
        handles = [r.handle for r in results]
        found_at = next((n for n, h in enumerate(handles, 1) if h in expected), None)
        ok = found_at is not None

        hits += int(ok)
        per_kind.setdefault(kind, []).append(ok)

        table.add_row(
            str(i),
            text,
            kind,
            "[green]yes[/green]" if ok else "[red]no[/red]",
            str(found_at) if found_at else "-",
            handles[0] if handles else "[dim]nothing[/dim]",
        )

    console.print(table)

    recall = hits / len(questions) if questions else 0.0
    console.print(f"recall@{top_k}: [bold]{recall:.2f}[/bold] ({hits}/{len(questions)})")
    for kind, outcomes in sorted(per_kind.items()):
        rate = sum(outcomes) / len(outcomes)
        colour = "green" if rate >= 0.7 else "yellow" if rate > 0 else "red"
        console.print(f"  {kind}: [{colour}]{rate:.2f}[/{colour}] ({sum(outcomes)}/{len(outcomes)})")

    if recall < 0.70:
        console.print(
            "[yellow]below the 0.70 bar in DESIGN.md section 2. The bar is arbitrary; "
            "what matters is that it is measured.[/yellow]"
        )

    return {"recall": recall, "hits": hits, "total": len(questions), "by_kind": per_kind}
