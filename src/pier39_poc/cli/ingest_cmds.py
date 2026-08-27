"""Commands for the pipeline stages: init-db, fetch-api, fetch-html, profile, merge, index.

Thin orchestration; the work lives in pier39_poc.ingest. Runs before query_cmds.
"""

from __future__ import annotations

import asyncio

import typer

from pier39_poc.cli.context import app, resolve_stores
from pier39_poc.core.models import Product
from pier39_poc.core.tuning import DEFAULTS
from pier39_poc.infra import db
from pier39_poc.infra.artifacts import record_stage, write_jsonl
from pier39_poc.infra.config import ConfigError, load_env, token_for
from pier39_poc.infra.shopify_api import PRODUCTS_QUERY, AdminClient, ShopifyError
from pier39_poc.ingest import indexing, labels, merge, profiling
from pier39_poc.ingest.crawl import fetch_pages, floor_shortfall, select_pages
from pier39_poc.presentation import render
from pier39_poc.presentation.console import console


@app.command("init-db", help="Apply sql/schema.sql to Postgres. Idempotent.")
def init_db() -> None:
    load_env()
    db.init_db()
    console.print("[green]schema applied[/green]")


@app.command("show-query", help="Print the products GraphQL query before spending an API call.")
def show_query() -> None:
    console.print(PRODUCTS_QUERY)


@app.command("stores", help="Print the resolved per-store config.")
def list_stores(store: str | None = typer.Option(None, "--store")) -> None:
    render.stores_table(resolve_stores(store))


@app.command("fetch-api", help="Pull the catalogue: metafields, variants and a sellability verdict.")
def fetch_api(
    store: str | None = typer.Option(None, "--store"),
    limit: int | None = typer.Option(None, "--limit", help="stop after N products"),
) -> None:
    for cfg in resolve_stores(store):
        try:
            token = token_for(cfg.slug)
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc

        console.print(f"[bold]{cfg.slug}[/bold]: fetching from {cfg.graphql_url()}")
        try:
            with AdminClient(cfg, token) as client:
                definitions = client.metafield_definitions()
                products: list[Product] = []
                for product in client.products():
                    products.append(product)
                    if limit and len(products) >= limit:
                        break
                sellable = client.sellability(
                    [p for p in products if p.online_store_url]
                )
        except ShopifyError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        for product in products:
            product.sellable = sellable.get(product.product_id, False)

        write_jsonl(cfg.api_path, [p.model_dump() for p in products])
        write_jsonl(cfg.data_dir / "metafield_definitions.jsonl", definitions)

        published = sum(1 for p in products if p.online_store_url)
        abandoned = [
            p.handle for p in products
            if p.online_store_url and not p.sellable
        ]
        metafields = sum(len(p.metafields) for p in products)
        has_template = sum(1 for p in products if p.template_suffix)

        record_stage(cfg, "fetch-api", {
            "products": len(products),
            "published": published,
            "metafields": metafields,
            "definitions": len(definitions),
            "template_suffix_present": has_template,
            "abandoned_sku": len(abandoned),
            "abandoned_sku_sample": sorted(abandoned)[:20],
        })
        console.print(
            f"  {len(products)} products ({published} published), {metafields} metafields, "
            f"{len(definitions)} definitions -> {cfg.api_path}"
        )
        if abandoned:
            console.print(
                f"  [yellow]{len(abandoned)} of {published} published products are "
                f"abandoned SKUs (no priced or available variant); excluded from crawling "
                f"and indexing[/yellow]"
            )
        if has_template == 0:
            console.print(
                "  [yellow]no templateSuffix values seen; template grouping will fall "
                "back to product_type[/yellow]"
            )


@app.command("fetch-html", help="Crawl the selected product pages, escalating the fetch profile on a block.")
def fetch_html(
    store: str | None = typer.Option(None, "--store"),
    limit: int | None = typer.Option(None, "--limit"),
) -> None:
    from pier39_poc.infra.artifacts import load_products

    for cfg in resolve_stores(store):
        products = load_products(cfg)
        shortfall = floor_shortfall(cfg, products)
        if shortfall:
            budget, reachable = shortfall
            console.print(
                f"  [yellow]profile_pages={budget} cannot reach the {DEFAULTS.crawl.group_floor}-page "
                f"floor for every template group; {reachable} pages would be needed, so "
                f"groups get 1-2 pages and differencing degrades. See DESIGN.md 5.2."
                "[/yellow]"
            )
        targets = select_pages(cfg, products)
        if limit:
            targets = targets[:limit]
        if not targets:
            console.print(f"[bold]{cfg.slug}[/bold]: nothing to fetch (crawl_scope={cfg.crawl_scope})")
            continue

        console.print(f"[bold]{cfg.slug}[/bold]: fetching {len(targets)} page(s)")
        outcomes = asyncio.run(fetch_pages(cfg, targets))
        ok = [o for o in outcomes if o.ok]
        bad = [o for o in outcomes if not o.ok]
        profiles = sorted({o.profile_used for o in ok})
        console.print(f"  {len(ok)} ok, {len(bad)} failed; profiles used: {profiles or '-'}")
        for outcome in bad[:5]:
            console.print(f"  [red]{outcome.handle}[/red]: {outcome.error} (status {outcome.status})")


@app.command("profile", help="Derive where this store's product content lives -> profile.json.")
def profile_cmd(store: str | None = typer.Option(None, "--store")) -> None:
    for cfg in resolve_stores(store):
        profile = profiling.build_profile(cfg)
        console.print(
            f"[bold]{cfg.slug}[/bold]: {len(profile.allowlist)} admitted, "
            f"{len(profile.rejected)} rejected, "
            f"coverage {profile.coverage.coverage_pct}% "
            f"-> {cfg.profile_path}"
        )


@app.command("merge", help="Turn API and page evidence into field assertions and load Postgres.")
def merge_cmd(
    store: str | None = typer.Option(None, "--store"),
    label_policy: str = typer.Option(
        "static",
        "--label-policy",
        help="gate for per-product theme spec pairs: none | static | llm",
    ),
) -> None:
    for cfg in resolve_stores(store):
        try:
            policy = labels.get_policy(label_policy)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
        counts = merge.run(cfg, policy)
        console.print(
            f"[bold]{cfg.slug}[/bold]: {counts['products']} products, "
            f"{counts['assertions']} assertions "
            f"({counts['quotable']} quotable / {counts['retrieval']} retrieval), "
            f"{counts['edges']} edges"
            + (
                f", {counts['abandoned_skipped']} abandoned SKUs skipped"
                if counts.get("abandoned_skipped")
                else ""
            )
        )


@app.command("index", help="Build retrieval documents, embed them, and load them into Postgres.")
def index_cmd(
    store: str | None = typer.Option(None, "--store"),
    force: bool = typer.Option(False, "--force", help="re-embed unchanged documents"),
) -> None:
    for cfg in resolve_stores(store):
        counts = indexing.run(cfg, force=force)
        console.print(
            f"[bold]{cfg.slug}[/bold]: {counts['documents']} documents embedded, "
            f"{counts['skipped_unchanged']} unchanged"
        )
        console.print(f"  embeddings: {counts['embedding_stats']}")
