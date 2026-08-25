"""CLI. One subcommand per pipeline stage; every stage reads the previous one's files."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import chat as chat_stage
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
        "static",
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
        tc = profile.get("template_constants") or {}
        sources = list((tc.get("per_product") or {}).items())
        sources += list((tc.get("by_template") or {}).items())
        stats: dict[str, dict[str, Any]] = {}
        for handle, pairs in sources:
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


@app.command("compare-labels")
def compare_labels_cmd(store: Optional[str] = typer.Option(None, "--store")) -> None:
    """Score the llm label policy against the hand-authored reference set."""
    for cfg in _stores(store):
        profile = read_json(cfg.profile_path)
        tc = profile.get("template_constants") or {}
        groups = list((tc.get("per_product") or {}).values())
        groups += list((tc.get("by_template") or {}).values())
        examples: dict[str, list[str]] = {}
        pairs_per_label: dict[str, int] = {}
        for pairs in groups:
            for pair in pairs:
                label = pair.get("label")
                if not label:
                    continue
                examples.setdefault(label, []).append(pair["value"])
                pairs_per_label[label] = pairs_per_label.get(label, 0) + 1

        reference = labels_stage.load_reference(cfg.slug)
        if not reference:
            console.print(f"[yellow]{cfg.slug}: no reference set, nothing to score[/yellow]")
            continue

        classifier = labels_stage.ClassifierPolicy()
        verdicts = classifier.warm(cfg, examples)

        table = Table(title=f"{cfg.slug}: llm vs reference")
        table.add_column("label")
        table.add_column("pairs", justify="right")
        table.add_column("reference")
        table.add_column("llm")
        table.add_column("agree")

        agree = 0
        scored = 0
        tp = fp = fn = 0
        for label in sorted(examples, key=lambda x: -pairs_per_label[x]):
            key = labels_stage.normalise(label)
            ref = reference.get(key)
            got = verdicts.get(key, labels_stage.UNCERTAIN)
            if ref is None:
                continue
            scored += 1
            same = ref == got
            agree += 1 if same else 0
            if ref == labels_stage.WIDGET and got == labels_stage.WIDGET:
                tp += 1
            elif ref != labels_stage.WIDGET and got == labels_stage.WIDGET:
                fp += 1
            elif ref == labels_stage.WIDGET and got != labels_stage.WIDGET:
                fn += 1
            table.add_row(
                label,
                str(pairs_per_label[label]),
                ref,
                got,
                "yes" if same else "[red]no[/red]",
            )
        console.print(table)

        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        console.print(
            f"[bold]{cfg.slug}[/bold]: agreement {agree}/{scored}"
            + (f", widget precision {precision:.2f}" if precision is not None else "")
            + (f", widget recall {recall:.2f}" if recall is not None else "")
        )
        pairs_wrong = sum(
            pairs_per_label[lbl]
            for lbl in examples
            if reference.get(labels_stage.normalise(lbl))
            and reference[labels_stage.normalise(lbl)]
            != verdicts.get(labels_stage.normalise(lbl), labels_stage.UNCERTAIN)
        )
        console.print(
            f"[dim]{pairs_wrong} of {sum(pairs_per_label.values())} pairs "
            f"affected by a disagreement[/dim]"
        )


def _render_turn(turn, show_facts: bool, show_diag: bool) -> None:
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
            _print_diagnostics(turn.diagnostics, filtered=False)


@app.command("chat")
def chat_cmd(
    store: Optional[str] = typer.Option(None, "--store"),
    product: Optional[str] = typer.Option(None, "--product", help="hold a product in scope"),
    top_k: int = typer.Option(5, "--top-k"),
    live: bool = typer.Option(False, "--live", help="read live price and stock per turn"),
    show_facts: bool = typer.Option(True, "--facts/--no-facts"),
    show_diag: bool = typer.Option(True, "--diagnostics/--no-diagnostics"),
    model: str = typer.Option(chat_stage.CHAT_MODEL, "--model"),
) -> None:
    """Grounded REPL. Answers cite assertion ids and every citation is verified."""
    stores = _stores(store)
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

        turn = chat_stage.answer(
            cfg, question, handle=handle, top_k=top_k, live=live, model=model
        )
        _render_turn(turn, show_facts, show_diag)
        chat_stage.append_log(log_path, turn)


@app.command("chat-replay")
def chat_replay_cmd(
    store: Optional[str] = typer.Option(None, "--store"),
    top_k: int = typer.Option(5, "--top-k"),
    model: str = typer.Option(chat_stage.CHAT_MODEL, "--model"),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    """Run the eval question set through the SAME answer function and score groundedness."""
    for cfg in _stores(store):
        questions = evaluate.load_questions(cfg.slug)
        if limit:
            questions = questions[:limit]
        result = chat_stage.replay(cfg, questions, top_k=top_k, model=model)

        table = Table(title=f"{cfg.slug}: groundedness ({model})")
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
            f"[bold]{cfg.slug}[/bold]: groundedness {result['groundedness']:.2f} "
            f"({result['grounded']} grounded / {result['ungrounded']} ungrounded), "
            f"{result['uncited']} uncited, {result['errors']} error(s)"
        )
        console.print(
            f"[dim]{result['invalid_citations']} invalid citation(s), "
            f"{result['uncited_sentences']} uncited sentence(s). Answers with no citations are "
            f"excluded from the ratio: a correct refusal and an unsupported assertion are "
            f"not distinguishable without knowing the question was answerable.[/dim]"
        )
        log_path = cfg.data_dir / "chat_replay.jsonl"
        for turn in result["turns"]:
            chat_stage.append_log(log_path, turn)
        console.print(f"[dim]turns logged to {log_path}[/dim]")


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
