"""Commands for the grounded answer layer: the REPL and the batch replay.

Both go through evaluation.chat.answer. Details: docs/reference/evaluation.md
"""

from __future__ import annotations

import typer

from pier39_poc.cli.context import app, resolve_stores
from pier39_poc.evaluation import chat, harness
from pier39_poc.presentation import render
from pier39_poc.presentation.console import console


@app.command("chat", help="Grounded REPL. Answers cite assertion ids and every citation is verified.")
def chat_cmd(
    store: str | None = typer.Option(None, "--store"),
    product: str | None = typer.Option(None, "--product", help="hold a product in scope"),
    top_k: int = typer.Option(5, "--top-k"),
    live: bool = typer.Option(False, "--live", help="read live price and stock per turn"),
    show_facts: bool = typer.Option(True, "--facts/--no-facts"),
    show_diag: bool = typer.Option(True, "--diagnostics/--no-diagnostics"),
    model: str = typer.Option(chat.CHAT_MODEL, "--model"),
) -> None:
    stores = resolve_stores(store)
    if len(stores) != 1:
        console.print("[red]choose one store with --store[/red]")
        raise typer.Exit(2)
    cfg = stores[0]
    handle = product
    log_path = cfg.data_dir / "chat_turns.jsonl"

    console.print(f"[bold]{cfg.slug}[/bold] chat, model {model}")
    console.print(
        "[dim]/product <handle> to scope, /discovery to clear, /quit to leave. "
        f"turns logged to {log_path}[/dim]"
    )
    while True:
        scope = handle or "discovery"
        try:
            question = typer.prompt(f"\n[{scope}]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nbye")
            return
        if not question:
            continue
        if question in ("/quit", "/exit"):
            return
        if question == "/discovery":
            handle = None
            continue
        if question.startswith("/product"):
            parts = question.split(maxsplit=1)
            handle = parts[1].strip() if len(parts) > 1 else None
            continue

        turn = chat.answer(
            cfg, question, handle=handle, top_k=top_k, live=live, model=model
        )
        render.chat_turn(turn, show_facts, show_diag)
        chat.append_log(log_path, turn)


@app.command("chat-replay", help="Run the eval questions through the same answer function and score groundedness.")
def chat_replay_cmd(
    store: str | None = typer.Option(None, "--store"),
    top_k: int = typer.Option(5, "--top-k"),
    model: str = typer.Option(chat.CHAT_MODEL, "--model"),
    limit: int | None = typer.Option(None, "--limit"),
) -> None:
    for cfg in resolve_stores(store):
        questions = harness.load_questions(cfg.slug)
        if limit:
            questions = questions[:limit]
        result = chat.replay(cfg, questions, top_k=top_k, model=model)

        render.replay_scores(cfg.slug, model, result)

        log_path = cfg.data_dir / "chat_replay.jsonl"
        for turn in result["turns"]:
            chat.append_log(log_path, turn)
        console.print(f"[dim]turns logged to {log_path}[/dim]")
