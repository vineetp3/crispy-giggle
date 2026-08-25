"""CLI. One subcommand per pipeline stage; every stage reads the previous one's files."""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import db, evaluate
from . import index as index_stage
from . import merge as merge_stage
from . import profile as profile_stage
from . import report as report_stage
from .artifacts import record_stage, write_jsonl
from .config import ConfigError, StoreConfig, load_env, load_stores, token_for
from .crawl import fetch_pages, select_pages
from .shopify_api import PRODUCTS_QUERY, AdminClient, ShopifyError

app = typer.Typer(add_completion=False, help="Shopify catalogue ingestion feasibility POC")
console = Console()


def _stores(only: Optional[str]) -> list[StoreConfig]:
    load_env()
    try:
        stores = load_stores(only=only)
    except ConfigError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(2)
    if not stores:
        console.print("[yellow]no enabled stores selected[/yellow]")
        raise typer.Exit(1)
    return stores


@app.command("init-db")
def init_db() -> None:
    load_env()
    db.init_db()
    console.print("[green]schema applied[/green]")


@app.command("show-query")
def show_query() -> None:
    console.print(PRODUCTS_QUERY)


@app.command("stores")
def list_stores(store: Optional[str] = typer.Option(None, "--store")) -> None:
    table = Table(title="stores")
    for column in ("slug", "domain", "scope", "profile_pages", "fetch", "threshold"):
        table.add_column(column)
    for cfg in _stores(store):
        table.add_row(
            cfg.slug, cfg.domain, cfg.crawl_scope, str(cfg.profile_pages),
            cfg.fetch_profile, str(cfg.chrome_threshold),
        )
    console.print(table)


@app.command("fetch-api")
def fetch_api(
    store: Optional[str] = typer.Option(None, "--store"),
    limit: Optional[int] = typer.Option(None, "--limit", help="stop after N products"),
) -> None:
    for cfg in _stores(store):
        try:
            token = token_for(cfg.slug)
        except ConfigError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)

        console.print(f"[bold]{cfg.slug}[/bold]: fetching from {cfg.graphql_url()}")
        try:
            with AdminClient(cfg, token) as client:
                definitions = client.metafield_definitions()
                products = []
                for product in client.products():
                    products.append(product)
                    if limit and len(products) >= limit:
                        break
                sellable = client.sellability(
                    [p for p in products if p.get("online_store_url")]
                )
        except ShopifyError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)

        for product in products:
            product["sellable"] = sellable.get(str(product["product_id"]), False)

        write_jsonl(cfg.api_path, products)
        write_jsonl(cfg.data_dir / "metafield_definitions.jsonl", definitions)

        published = sum(1 for p in products if p.get("online_store_url"))
        abandoned = [
            p["handle"] for p in products
            if p.get("online_store_url") and not p.get("sellable")
        ]
        metafields = sum(len(p.get("metafields") or []) for p in products)
        has_template = sum(1 for p in products if p.get("template_suffix"))

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


@app.command("fetch-html")
def fetch_html(
    store: Optional[str] = typer.Option(None, "--store"),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    from .artifacts import load_products

    for cfg in _stores(store):
        products = load_products(cfg)
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


@app.command("profile")
def profile_cmd(store: Optional[str] = typer.Option(None, "--store")) -> None:
    for cfg in _stores(store):
        payload = profile_stage.build_profile(cfg)
        coverage = (payload.get("coverage") or {}).get("coverage_pct")
        console.print(
            f"[bold]{cfg.slug}[/bold]: {len(payload['allowlist'])} admitted, "
            f"{len(payload['rejected'])} rejected, coverage {coverage}% "
            f"-> {cfg.profile_path}"
        )


@app.command("merge")
def merge_cmd(store: Optional[str] = typer.Option(None, "--store")) -> None:
    for cfg in _stores(store):
        counts = merge_stage.run(cfg)
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


@app.command("index")
def index_cmd(
    store: Optional[str] = typer.Option(None, "--store"),
    force: bool = typer.Option(False, "--force", help="re-embed unchanged documents"),
) -> None:
    for cfg in _stores(store):
        counts = index_stage.run(cfg, force=force)
        console.print(
            f"[bold]{cfg.slug}[/bold]: {counts['documents']} documents embedded, "
            f"{counts['skipped_unchanged']} unchanged"
        )
        console.print(f"  embeddings: {counts['embedding_stats']}")


@app.command("search")
def search_cmd(
    query: str = typer.Argument(...),
    store: Optional[str] = typer.Option(None, "--store"),
    top_k: int = typer.Option(5, "--top-k"),
    exclude: Optional[str] = typer.Option(None, "--exclude", help="comma-separated terms"),
    no_rerank: bool = typer.Option(False, "--no-rerank"),
    no_live: bool = typer.Option(False, "--no-live", help="skip the live price call"),
    max_price: Optional[float] = typer.Option(
        None, "--max-price", help="applied to live price, not a stored column"
    ),
    in_stock: bool = typer.Option(False, "--in-stock", help="live availability only"),
) -> None:
    from .search import search

    stores = _stores(store)
    cfg = stores[0]
    slug = cfg.slug if store else None
    terms = [t.strip() for t in (exclude or "").split(",") if t.strip()]

    hits = search(
        query, cfg, slug=slug, top_k=top_k, exclude_terms=terms,
        rerank=not no_rerank, live_prices=not no_live,
        max_price=max_price, in_stock_only=in_stock,
    )
    if not hits:
        console.print("[yellow]no results[/yellow]")
        return

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
        for row in hit.matched_fields[:6]:
            mark = "Q" if row["trust_class"] == "quotable" else "r"
            label = row.get("label") or row["field"]
            console.print(f"   [{mark}] {label}: {str(row['value'])[:110]}")
        if hit.online_store_url:
            console.print(f"   [blue]{hit.online_store_url}[/blue]")
        console.print()


@app.command("report")
def report_cmd(store: Optional[str] = typer.Option(None, "--store")) -> None:
    for cfg in _stores(store):
        report_stage.render(cfg)


@app.command("eval")
def eval_cmd(
    store: Optional[str] = typer.Option(None, "--store"),
    top_k: int = typer.Option(5, "--top-k"),
    no_rerank: bool = typer.Option(False, "--no-rerank"),
    compare_rerank: bool = typer.Option(
        False, "--compare-rerank", help="run with and without the cross-encoder"
    ),
) -> None:
    results = {}
    for cfg in _stores(store):
        if compare_rerank:
            results[cfg.slug] = evaluate.compare(cfg, top_k=top_k)["with_rerank"]
        else:
            results[cfg.slug] = evaluate.run(cfg, top_k=top_k, rerank=not no_rerank)
    console.print(
        json.dumps(
            {
                k: {"recall": v["recall"], "violations": v["violations"]}
                for k, v in results.items()
            },
            indent=2,
        )
    )


@app.command("seed-fixtures")
def seed_fixtures() -> None:
    import shutil
    import sys

    from .config import REPO_ROOT

    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from test_profile import DESCRIPTION, FIXTURES, HANDLES, IDS, _metafields

    load_env()
    cfg = load_stores(only="skout")[0]
    cfg.pages_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for handle in HANDLES:
        shutil.copy(FIXTURES / f"{handle}.html", cfg.pages_dir / f"{handle}.html")
        rows.append(
            {
                "id": f"gid://shopify/Product/{IDS[handle]}",
                "product_id": IDS[handle],
                "handle": handle,
                "title": f"Skout Organic {handle.replace('-', ' ').title()} Soft Baked Cookies",
                "vendor": "Skout Organic",
                "product_type": "Soft Baked Cookies",
                "tags": ["cr-ignore"],
                "status": "ACTIVE",
                "online_store_url": f"https://www.skoutorganic.com/products/{handle}",
                "description_html": DESCRIPTION if handle == "peanut-butter" else "<p>A cookie.</p>",
                "template_suffix": None,
                "collections": [{"handle": "soft-baked-cookies", "title": "Soft-Baked Cookies"}],
                "metafields": _metafields(handle),
                "variants": [
                    {
                        "id": f"gid://shopify/ProductVariant/4052559196{i}",
                        "title": f"{i} Boxes",
                        "sku": None,
                        "selectedOptions": [{"name": "Pack Size", "value": f"{i} Boxes"}],
                    }
                    for i in (3, 6)
                ],
            }
        )

    write_jsonl(cfg.api_path, rows)
    console.print(
        f"[green]seeded[/green] {len(rows)} products and {len(HANDLES)} pages "
        f"-> {cfg.data_dir}\n"
        "[yellow]synthetic: coverage from this seed is not meaningful[/yellow]"
    )


@app.command("run")
def run_all(
    store: Optional[str] = typer.Option(None, "--store"),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    init_db()
    fetch_api(store=store, limit=limit)
    fetch_html(store=store, limit=None)
    profile_cmd(store=store)
    merge_cmd(store=store)
    index_cmd(store=store, force=False)
    report_cmd(store=store)


if __name__ == "__main__":
    app()
