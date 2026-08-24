"""The report. This is the deliverable of the POC.

Everything else exists to produce or validate it: which metafield keys carry live
product content, what they are called in human terms, what was rejected and why, what
lives only in the theme, and how much of the visible product content the API covers.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table

from .artifacts import read_json
from .config import StoreConfig

console = Console()

REASON_NOTES = {
    "foreign_product_id": "value references a different product",
    "widget_markup": "value is rendered widget HTML, not content",
    "reference_type": "reference, captured as an edge instead",
    "excluded_namespace": "channel/SEO plumbing",
    "always_excluded": "internal-only field",
    "chrome_like": "appears on unrelated product pages",
    "stale_namespace": "app appears abandoned",
    "no_usable_value": "no displayable value on any product",
}


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
    pct = coverage.get("coverage_pct")
    console.print(
        f"coverage: [bold]{pct if pct is not None else 'n/a'}%[/bold] of product-region "
        f"words explained by API sources   "
        f"(residual {coverage.get('residual_words')} of {coverage.get('region_words_total')} words; "
        f"template constants {coverage.get('template_constant_words')}, "
        f"per-product unreachable {coverage.get('per_product_unreachable_words')})"
    )

    hist = chrome.get("frequency_histogram") or {}
    if hist:
        console.print(
            "block frequency across pages: "
            + "  ".join(f"{k}p:{v}" for k, v in sorted(hist.items(), key=lambda kv: int(kv[0])))
        )

    admitted = profile.get("allowlist") or []
    if admitted:
        table = Table(title=f"admitted keys ({len(admitted)})", show_lines=False)
        table.add_column("key", overflow="fold")
        table.add_column("label")
        table.add_column("type")
        table.add_column("reason")
        table.add_column("hit", justify="right")
        table.add_column("n", justify="right")
        for entry in admitted:
            rate = entry.get("hit_rate")
            table.add_row(
                f"{entry['namespace']}.{entry['key']}",
                entry.get("label") or "[dim]-[/dim]",
                entry.get("type") or "",
                entry.get("reason") or "",
                f"{rate:.2f}" if isinstance(rate, (int, float)) else "-",
                str(entry.get("support") or 0),
            )
        console.print(table)

    rejected = profile.get("rejected") or []
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
    if constants:
        table = Table(title="template constants (theme-resident, needs polling)")
        table.add_column("template")
        table.add_column("blocks", justify="right")
        table.add_column("sample", overflow="fold")
        for template, values in constants.items():
            sample = " / ".join(v[:60] for v in values[:3]) or "[dim]none[/dim]"
            table.add_row(str(template), str(len(values)), sample)
        console.print(table)

    per_product = (profile.get("template_constants") or {}).get(
        "per_product_theme_sample"
    ) or {}
    leftover = {h: b for h, b in per_product.items() if b}
    if leftover:
        console.print(
            f"[yellow]per-product theme content present on {len(leftover)} sampled "
            "page(s) -- these products need page polling, not just API reads[/yellow]"
        )
        for handle, blocks in list(leftover.items())[:2]:
            console.print(f"  [dim]{handle}[/dim]: " + " / ".join(b[:70] for b in blocks[:3]))


def summary_line(store: StoreConfig) -> str:
    try:
        profile = read_json(store.profile_path)
    except FileNotFoundError:
        return f"{store.slug}: no profile yet"
    coverage = (profile.get("coverage") or {}).get("coverage_pct")
    return (
        f"{store.slug}: {len(profile.get('allowlist') or [])} admitted, "
        f"{len(profile.get('rejected') or [])} rejected, coverage {coverage}%"
    )
