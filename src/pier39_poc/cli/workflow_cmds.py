"""Convenience wrappers, not stages: `run` chains the pipeline, `seed-fixtures` fakes a catalogue.

Registered last so `poc --help` lists the real stages first.
"""

from __future__ import annotations

import typer

from pier39_poc.cli.context import app
from pier39_poc.cli.ingest_cmds import (
    fetch_api,
    fetch_html,
    index_cmd,
    init_db,
    merge_cmd,
    profile_cmd,
)
from pier39_poc.cli.inspect_cmds import report_cmd
from pier39_poc.infra.artifacts import write_jsonl
from pier39_poc.infra.config import load_env, load_stores
from pier39_poc.presentation.console import console


@app.command("seed-fixtures", help="Write a synthetic five-product skout catalogue so stages run without a token.")
def seed_fixtures() -> None:
    import json
    import shutil

    from pier39_poc.infra.config import REPO_ROOT

    fixtures = REPO_ROOT / "tests" / "fixtures" / "skout"
    rows = json.loads((fixtures / "seed_catalogue.json").read_text(encoding="utf-8"))

    load_env()
    cfg = load_stores(only="skout")[0]
    cfg.pages_dir.mkdir(parents=True, exist_ok=True)

    for row in rows:
        shutil.copy(fixtures / f"{row['handle']}.html", cfg.pages_dir / f"{row['handle']}.html")

    write_jsonl(cfg.api_path, rows)
    console.print(
        f"[green]seeded[/green] {len(rows)} products and {len(rows)} pages "
        f"-> {cfg.data_dir}\n"
        "[yellow]synthetic: coverage from this seed is not meaningful[/yellow]"
    )


@app.command("run", help="Chain init-db, fetch-api, fetch-html, profile, merge, index and report.")
def run_all(
    store: str | None = typer.Option(None, "--store"),
    limit: int | None = typer.Option(None, "--limit"),
) -> None:
    init_db()
    fetch_api(store=store, limit=limit)
    fetch_html(store=store, limit=None)
    profile_cmd(store=store)
    merge_cmd(store=store)
    index_cmd(store=store, force=False)
    report_cmd(store=store)
