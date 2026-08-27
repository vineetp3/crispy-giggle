"""The Typer app and the store-selection helper every command group shares.

Separate from cli.app so command modules can register without importing the entry point back.
"""

from __future__ import annotations

import typer

from pier39_poc.infra.config import ConfigError, StoreConfig, load_env, load_stores
from pier39_poc.presentation.console import console

app = typer.Typer(add_completion=False, help="Shopify catalogue ingestion feasibility POC")


def resolve_stores(only: str | None) -> list[StoreConfig]:
    load_env()
    try:
        stores = load_stores(only=only)
    except ConfigError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not stores:
        console.print("[yellow]no enabled stores selected[/yellow]")
        raise typer.Exit(1)
    return stores
