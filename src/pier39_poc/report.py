"""The report. This is the deliverable of the POC.

Gotchas:

The attribute table is the headline, not the coverage percentage. Coverage is
word-weighted, so it measures review volume as much as API completeness: remi's five
words of `Material: BPA-free, food-safe plastic` count the same as five words of a
review, and the denominator moves with `chrome_threshold` and the page sample, which
makes two stores' percentages incomparable. It is kept as a diagnostic only.

`per_product_theme_counts` is the complete map and `per_product_theme_sample` is a
truncated, ranked view of it. Counting the sample and printing that as the finding
reported "5 sampled pages" for remi when the real figure was every page analysed.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table

from .artifacts import read_json
from .attributes import SOURCE_ORDER, summary
from .config import StoreConfig
from .profile import FOREIGN_TITLE_REJECT_RATE

console = Console()

REASON_NOTES = {
    "foreign_product_id": "value references a different product",
    "widget_markup": "value is rendered widget HTML, not content",
    "reference_type": "reference, captured as an edge instead",
    "excluded_namespace": "channel/SEO/app plumbing",
    "always_excluded": "internal-only field",
    "commerce_fact": "price or inventory; read live, never stored",
    "foreign_product_title": "most values describe a different product in this store",
    "no_content_value": "flag, colour or timestamp; no product information",
    "stale_namespace": "app appears abandoned",
    "no_render_evidence": "no sampled page carried a value for this key",
    "low_render_evidence": "too few sampled pages to judge rendering",
    "no_usable_value": "no displayable value on any product",
}

SOURCE_STYLE = {
    "api": "[green]api[/green]",
    "theme": "[yellow]theme[/yellow]",
    "image": "[magenta]image[/magenta]",
}


def _attribute_table(profile: dict[str, Any]) -> Table | None:
    attributes = profile.get("attributes") or {}
    if not attributes:
        return None
    table = Table(title="attribute reachability -- can this store answer, and from where")
    table.add_column("attribute")
    table.add_column("reachable via")
    table.add_column("evidence", overflow="fold")
    for name, entry in attributes.items():
        if name.startswith("_"):
            continue
        # Filtered and coerced: `sources` comes from profile JSON, so a null or a
        # non-string would otherwise reach join() and raise TypeError rather than
        # render. Typing it concretely also makes SOURCE_STYLE.get return str.
        sources: list[str] = [str(s) for s in (entry.get("sources") or []) if s]
        via = " + ".join(SOURCE_STYLE.get(s, s) for s in sources) or "[red]absent[/red]"
        evidence = (
            [f"{e['key']} (n={e['support']})" for e in entry.get("api") or []]
            + list(entry.get("theme") or [])
            + [f"{e['key']} (n={e['products']}, image)" for e in entry.get("image") or []]
        )
        table.add_row(name, via, ", ".join(evidence[:4]) or "[dim]-[/dim]")
    return table


def render(store: StoreConfig) -> None:
    profile: dict[str, Any] = read_json(store.profile_path)

    console.rule(f"[bold]{store.slug}[/bold]  ({store.domain})")

    coverage = profile.get("coverage") or {}
    chrome = profile.get("chrome") or {}
    console.print(
        f"products: [bold]{profile.get('products_total')}[/bold] "
        f"({profile.get('products_published')} published)   "
        f"pages analysed: [bold]{profile.get('pages_analysed')}[/bold]   "
        f"chrome blocks: {chrome.get('blocks')} @ threshold {chrome.get('threshold')}"
    )

    attributes = profile.get("attributes") or {}
    if attributes:
        counts = summary(attributes)
        theme_only = sum(
            1
            for name, entry in attributes.items()
            if not name.startswith("_") and entry.get("sources") == ["theme"]
        )
        console.print(
            "attributes: "
            + "  ".join(
                f"{SOURCE_STYLE.get(s, s)} {counts[s]}" for s in SOURCE_ORDER
            )
            + f"  [red]absent[/red] {counts['absent']}"
        )
        if theme_only:
            console.print(
                f"[yellow]{theme_only} attribute(s) are theme-only -- this store needs "
                "scheduled page polling, not just API reads[/yellow]"
            )
        else:
            console.print(
                "[green]no attribute is theme-only -- the API alone answers what this "
                "store can answer[/green]"
            )

    table = _attribute_table(profile)
    if table is not None:
        console.print(table)

    decl = profile.get("declarations") or {}
    if decl:
        missing = decl.get("without_declaration") or 0
        published = decl.get("published") or 0
        have = decl.get("with_declaration") or 0
        if not have:
            console.print(
                f"[red]allergen negation: unanswerable for this store[/red] -- 0 of "
                f"{published} published products carry a free-from declaration "
                f"({', '.join(decl.get('fields') or [])}), so every negation query "
                "correctly returns nothing"
            )
        else:
            console.print(
                f"allergen negation: answerable for [bold]{have}[/bold] of {published} "
                f"published products; [{'red' if missing else 'green'}]{missing} "
                f"undeclared[/{'red' if missing else 'green'}] and excluded from any "
                "negation query"
            )

    pct = coverage.get("coverage_pct")
    console.print(
        f"[dim]coverage (diagnostic only, word-weighted): "
        f"{pct if pct is not None else 'n/a'}% -- residual "
        f"{coverage.get('residual_words')} of {coverage.get('region_words_total')} words; "
        f"template constants {coverage.get('template_constant_words')}, "
        f"per-product unreachable {coverage.get('per_product_unreachable_words')}[/dim]"
    )

    hist = {int(k): v for k, v in (chrome.get("frequency_histogram") or {}).items()}
    if hist:
        pages = profile.get("pages_analysed") or 0
        cutoff = (chrome.get("threshold") or 0) * pages
        near = sum(v for k, v in hist.items() if 0.6 * pages <= k < cutoff)
        console.print(
            "[dim]block frequency across pages: "
            + "  ".join(f"{k}p:{v}" for k, v in sorted(hist.items()))
            + "[/dim]"
        )
        if near:
            console.print(
                f"[dim]threshold sensitivity: {near} distinct block(s) sit between 60% "
                f"and the {chrome.get('threshold')} cutoff and survive into every product "
                "region; lowering the threshold would reclassify them[/dim]"
            )

    admitted = profile.get("allowlist") or []
    if admitted:
        table = Table(title=f"admitted keys ({len(admitted)})")
        table.add_column("key", overflow="fold")
        table.add_column("label")
        table.add_column("type")
        table.add_column("reason")
        table.add_column("hit", justify="right")
        table.add_column("obs", justify="right")
        table.add_column("n", justify="right")
        for entry in admitted:
            rate = entry.get("hit_rate")
            table.add_row(
                f"{entry['namespace']}.{entry['key']}",
                entry.get("label") or "[dim]-[/dim]",
                entry.get("type") or "",
                entry.get("reason") or "",
                f"{rate:.2f}" if isinstance(rate, (int, float)) else "-",
                str(entry.get("observed") or 0),
                str(entry.get("support") or 0),
            )
        console.print(table)

    rejected = profile.get("rejected") or []
    commerce = [r for r in rejected if r.get("reason") == "commerce_fact"]
    if commerce:
        console.print(
            f"[bold]commerce keys rejected ({len(commerce)})[/bold] -- price and "
            "inventory are read live at query time, never stored:"
        )
        console.print(
            "  " + ", ".join(f"{r['namespace']}.{r['key']}" for r in commerce)
        )

    review = [e for e in admitted if "REVIEW:" in (e.get("detail") or "")]
    if review:
        console.print(
            f"[yellow]{len(review)} admitted key(s) need human review[/yellow] -- some "
            "values describe a different product in this store, but not enough of them "
            f"to clear the {int(FOREIGN_TITLE_REJECT_RATE * 100)}% rejection bar. "
            "Flavour-family overlap and genuine cross-sell copy look the same here."
        )
        for entry in review:
            note = (entry.get("detail") or "").split("REVIEW: ")[-1]
            console.print(
                f"  [dim]{entry['namespace']}.{entry['key']}[/dim]: {note[:100]}"
            )

    if rejected:
        table = Table(title=f"rejected keys ({len(rejected)})")
        table.add_column("key", overflow="fold")
        table.add_column("reason")
        table.add_column("note / detail", overflow="fold")
        for entry in sorted(rejected, key=lambda e: (e.get("reason") or "", e["key"])):
            reason = entry.get("reason") or ""
            note = entry.get("detail") or REASON_NOTES.get(reason, "")
            table.add_row(f"{entry['namespace']}.{entry['key']}", reason, note[:90])
        console.print(table)

    constants = (profile.get("template_constants") or {}).get("by_template") or {}
    labelled = {
        tpl: [c for c in blocks if c.get("label")] for tpl, blocks in constants.items()
    }
    if constants:
        table = Table(title="template constants (theme-resident, needs polling)")
        table.add_column("template")
        table.add_column("blocks", justify="right")
        table.add_column("labelled", justify="right")
        table.add_column("spec pairs", overflow="fold")
        for template, blocks in sorted(
            constants.items(), key=lambda kv: -len(labelled.get(kv[0]) or [])
        ):
            pairs = labelled.get(template) or []
            sample = " / ".join(
                f"{c['label']}: {c['value'][:44]}" for c in pairs[:3]
            ) or "[dim]none labelled[/dim]"
            table.add_row(str(template), str(len(blocks)), str(len(pairs)), sample)
        console.print(table)

    counts = (profile.get("template_constants") or {}).get(
        "per_product_theme_counts"
    ) or {}
    with_content = {h: c for h, c in counts.items() if c.get("blocks")}
    if with_content:
        total_words = sum(c["words"] for c in with_content.values())
        console.print(
            f"[yellow]per-product theme content on {len(with_content)} of "
            f"{len(counts)} analysed page(s), {total_words} words -- these products need "
            "page polling, not just API reads[/yellow]"
        )
        sample = (profile.get("template_constants") or {}).get(
            "per_product_theme_sample"
        ) or {}
        for handle, blocks in list(sample.items())[:3]:
            words = with_content.get(handle, {}).get("words", 0)
            console.print(
                f"  [dim]{handle} ({words}w)[/dim]: "
                + " / ".join(b[:60] for b in blocks[:3])
            )

    ages = _quotable_age_table(store)
    if ages is not None:
        console.print(ages)
        console.print(
            "[yellow]nothing expires. An age cliff would empty the quotable set rather "
            "than make it safer -- see DESIGN.md 10. Age does not separate stale from "
            "stable-and-correct; only re-confirmation does.[/yellow]"
        )


def _quotable_age_table(store: StoreConfig) -> Table | None:
    from . import db

    try:
        with db.connect() as conn:
            rows = conn.execute(
                """
                SELECT fa.field,
                       count(*) AS n,
                       min(fa.source_updated_at) AS oldest,
                       max(fa.source_updated_at) AS newest
                FROM field_assertions fa
                JOIN products p ON p.id = fa.product_id
                JOIN stores s ON s.id = p.store_id
                WHERE s.slug = %s
                  AND fa.trust_class = 'quotable'
                  AND fa.source_updated_at IS NOT NULL
                GROUP BY fa.field
                ORDER BY min(fa.source_updated_at)
                LIMIT 15
                """,
                (store.slug,),
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None

    table = Table(title="age of quotable material -- how old is the oldest fact we would state")
    table.add_column("field", overflow="fold")
    table.add_column("n", justify="right")
    table.add_column("oldest")
    table.add_column("newest")
    for row in rows:
        table.add_row(
            row["field"],
            str(row["n"]),
            row["oldest"].date().isoformat(),
            row["newest"].date().isoformat(),
        )
    return table


def summary_line(store: StoreConfig) -> str:
    try:
        profile = read_json(store.profile_path)
    except FileNotFoundError:
        return f"{store.slug}: no profile yet"
    attributes = profile.get("attributes") or {}
    counts = summary(attributes) if attributes else {}
    coverage = (profile.get("coverage") or {}).get("coverage_pct")
    reach = (
        "  ".join(f"{s}:{counts[s]}" for s in (*SOURCE_ORDER, "absent") if s in counts)
        or "no attribute data"
    )
    return (
        f"{store.slug}: {len(profile.get('allowlist') or [])} admitted, "
        f"{len(profile.get('rejected') or [])} rejected | attributes {reach} "
        f"| coverage {coverage}%"
    )
