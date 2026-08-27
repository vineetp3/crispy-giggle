"""Commands for retrieval: `search` ranks the catalogue, `facts` answers about a known product.

Runs after index. Heavy imports are function-local on purpose: docs/reference/presentation.md
"""

from __future__ import annotations

import typer

from pier39_poc.cli.context import app, resolve_stores
from pier39_poc.presentation import render
from pier39_poc.presentation.console import console


@app.command("search", help="Rank the catalogue for a discovery query.")
def search_cmd(
    query: str = typer.Argument(...),
    store: str | None = typer.Option(None, "--store"),
    top_k: int = typer.Option(5, "--top-k"),
    exclude: str | None = typer.Option(None, "--exclude", help="comma-separated terms"),
    no_rerank: bool = typer.Option(False, "--no-rerank"),
    no_live: bool = typer.Option(False, "--no-live", help="skip the live price call"),
    max_price: float | None = typer.Option(
        None, "--max-price", help="applied to live price, not a stored column"
    ),
    in_stock: bool = typer.Option(False, "--in-stock", help="live availability only"),
    no_group: bool = typer.Option(
        False, "--no-group", help="do not collapse duplicate listings of one product"
    ),
) -> None:
    from pier39_poc.retrieval.search import Diagnostics, prepare_rerank, search

    stores = resolve_stores(store)
    cfg = stores[0]
    slug = cfg.slug if store else None
    terms = [t.strip() for t in (exclude or "").split(",") if t.strip()]
    if not no_rerank:
        prepare_rerank(cfg.rerank_model)

    diag = Diagnostics()
    hits = search(
        query, cfg, slug=slug, top_k=top_k, exclude_terms=terms,
        rerank=not no_rerank, live_prices=not no_live,
        max_price=max_price, in_stock_only=in_stock,
        group_families=not no_group, diagnostics=diag,
    )
    render.search_diagnostics(diag, filtered=max_price is not None or in_stock)
    if not hits:
        render.no_results()
        return
    render.search_hits(hits)


@app.command("facts", help="Return what is quotable about a product you already have.")
def facts_cmd(
    handle: str = typer.Argument(..., help="product handle; scope expands to its family"),
    store: str | None = typer.Option(None, "--store"),
    exclude: str | None = typer.Option(None, "--exclude", help="comma-separated terms"),
    no_live: bool = typer.Option(False, "--no-live"),
    attribute: str | None = typer.Option(None, "--attribute", help="check one attribute"),
) -> None:
    from pier39_poc.core.attributes import ATTRIBUTES
    from pier39_poc.retrieval.answering import ProductNotFound, answer_for_product
    from pier39_poc.retrieval.search import Diagnostics

    cfg = resolve_stores(store)[0]
    terms = [t.strip() for t in (exclude or "").split(",") if t.strip()]
    diag = Diagnostics()
    try:
        answer = answer_for_product(
            cfg, handle, exclude_terms=terms, live=not no_live, diagnostics=diag
        )
    except ProductNotFound as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    render.search_diagnostics(diag, filtered=False)
    render.product_answer(answer, [attribute] if attribute else list(ATTRIBUTES))
