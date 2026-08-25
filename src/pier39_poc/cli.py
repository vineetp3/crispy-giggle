"""CLI. One subcommand per pipeline stage; every stage reads the previous one's files."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import db, evaluate
from . import labels as labels_stage
from . import index as index_stage
from . import merge as merge_stage
from . import profile as profile_stage
from . import report as report_stage
from .artifacts import read_json, record_stage, write_jsonl
from .config import ConfigError, StoreConfig, load_env, load_stores, token_for
from .crawl import GROUP_FLOOR, fetch_pages, floor_shortfall, select_pages
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
        shortfall = floor_shortfall(cfg, products)
        if shortfall:
            budget, reachable = shortfall
            console.print(
                f"  [yellow]profile_pages={budget} cannot reach the {GROUP_FLOOR}-page "
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
def merge_cmd(
    store: Optional[str] = typer.Option(None, "--store"),
    label_policy: str = typer.Option(
        "none",
        "--label-policy",
        help="gate for per-product theme spec pairs: none | static | llm",
    ),
) -> None:
    for cfg in _stores(store):
        try:
            policy = labels_stage.get_policy(label_policy)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        counts = merge_stage.run(cfg, policy)
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
    no_group: bool = typer.Option(
        False, "--no-group", help="do not collapse duplicate listings of one product"
    ),
) -> None:
    from .search import Diagnostics, search

    stores = _stores(store)
    cfg = stores[0]
    slug = cfg.slug if store else None
    terms = [t.strip() for t in (exclude or "").split(",") if t.strip()]

    diag = Diagnostics()
    hits = search(
        query, cfg, slug=slug, top_k=top_k, exclude_terms=terms,
        rerank=not no_rerank, live_prices=not no_live,
        max_price=max_price, in_stock_only=in_stock,
        group_families=not no_group, diagnostics=diag,
    )
    _print_diagnostics(diag, filtered=max_price is not None or in_stock)
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
        if hit.siblings:
            console.print(f"   [dim]also listed as: {', '.join(hit.siblings)}[/dim]")
        for row in hit.matched_fields[:6]:
            mark = "Q" if row["trust_class"] == "quotable" else "r"
            label = row.get("label") or row["field"]
            console.print(
                f"   [{mark}] {label}: {str(row['value'])[:110]}{_as_of(row)}"
            )
        if hit.online_store_url:
            console.print(f"   [blue]{hit.online_store_url}[/blue]")
        console.print()


def _as_of(row: dict) -> str:
    stamp = row.get("source_updated_at")
    if stamp is None or row.get("trust_class") != "quotable":
        return ""
    return f"  [dim](as of {stamp.date().isoformat()})[/dim]"


def _print_diagnostics(diag, filtered: bool) -> None:
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


@app.command("facts")
def facts_cmd(
    handle: str = typer.Argument(..., help="product handle; scope expands to its family"),
    store: Optional[str] = typer.Option(None, "--store"),
    exclude: Optional[str] = typer.Option(None, "--exclude", help="comma-separated terms"),
    no_live: bool = typer.Option(False, "--no-live"),
    attribute: Optional[str] = typer.Option(None, "--attribute", help="check one attribute"),
) -> None:
    from .answering import ProductNotFound, answer_for_product
    from .attributes import ATTRIBUTES
    from .search import Diagnostics

    cfg = _stores(store)[0]
    terms = [t.strip() for t in (exclude or "").split(",") if t.strip()]
    diag = Diagnostics()
    try:
        answer = answer_for_product(
            cfg, handle, exclude_terms=terms, live=not no_live, diagnostics=diag
        )
    except ProductNotFound as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    _print_diagnostics(diag, filtered=False)
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

    names = [attribute] if attribute else list(ATTRIBUTES)
    console.print()
    for name in names:
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


@app.command("labels")
def labels_cmd(
    store: Optional[str] = typer.Option(None, "--store"),
    as_yaml: bool = typer.Option(False, "--yaml", help="emit a reference-set skeleton"),
) -> None:
    for cfg in _stores(store):
        profile = read_json(cfg.profile_path)
        per_product = (profile.get("template_constants") or {}).get("per_product") or {}
        stats: dict[str, dict[str, Any]] = {}
        for handle, pairs in per_product.items():
            for pair in pairs:
                label = pair.get("label")
                if not label:
                    continue
                entry = stats.setdefault(
                    label, {"pairs": 0, "handles": set(), "examples": []}
                )
                entry["pairs"] += 1
                entry["handles"].add(handle)
                if len(entry["examples"]) < 3:
                    entry["examples"].append(pair["value"])

        reference = labels_stage.load_reference(cfg.slug)
        if as_yaml:
            print(f"# {cfg.slug}: hand-authored label reference set")
            print("labels:")
            for label in sorted(stats):
                current = reference.get(labels_stage.normalise(label), "uncertain")
                print(f"  {json.dumps(label)}: {current}")
            continue

        table = Table(title=f"{cfg.slug}: per-product theme labels")
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
                reference.get(labels_stage.normalise(label), "-"),
                (entry["examples"][0] or "")[:58],
            )
        console.print(table)
        console.print(
            f"[dim]{sum(e['pairs'] for e in stats.values())} pairs, "
            f"{len(stats)} distinct labels[/dim]"
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
