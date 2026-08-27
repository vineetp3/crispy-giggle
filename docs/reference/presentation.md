# `presentation/` and `cli/`

CLI specification: `docs/DESIGN.md` §7.

Everything that formats for a human lives here, so every layer below returns data. That
separation is also the seam an HTTP layer would reuse: `harness.run`, `search` and
`answer_for_product` all return plain results with no printing in them.

---

## `console` — one `Console`

A single instance so width, colour and capture behave identically everywhere. There were three
before; a captured or redirected run behaved differently depending on which module printed.

---

## `render` — the only module that formats

Search hits, scoped facts, chat turns, the label inventory and comparison, eval results and the
A/B comparison.

`label_reference_yaml` uses bare `print()` rather than `console.print()` on purpose: the output is
meant to be redirected into a YAML file, and rich would wrap it and interpret markup.

---

## `report` — the deliverable of the POC

**The attribute table is the headline, not the coverage percentage.** Coverage is word-weighted,
so it measures review volume as much as API completeness: remi's five words of
`Material: BPA-free, food-safe plastic` count the same as five words of a review, and the
denominator moves with `chrome_threshold` and the page sample, which makes two stores'
percentages incomparable. It is kept as a diagnostic only.

`per_product_theme_counts` is the complete map; `per_product_theme_sample` is a truncated, ranked
view of it. Counting the sample and printing that as the finding reported "5 sampled pages" for
remi when the real figure was every page analysed.

`sources` is filtered and coerced to `str` before joining: it comes from profile JSON, so a null
or non-string would otherwise reach `join()` and raise `TypeError` rather than render.

`_quotable_age_table` catches bare `Exception` and returns `None`, so an unreachable database
silently drops the whole age table rather than saying so. Every other degradation path in this
repo announces itself.

---

## `cli/` — thin Typer orchestration

| module | commands |
|---|---|
| `context` | the Typer `app` and `resolve_stores`, separate so command modules can register without importing the assembly point back |
| `ingest_cmds` | `init-db` `show-query` `stores` `fetch-api` `fetch-html` `profile` `merge` `index` |
| `query_cmds` | `search` `facts` |
| `inspect_cmds` | `report` `eval` `labels` `compare-labels` |
| `chat_cmds` | `chat` `chat-replay` |
| `workflow_cmds` | `seed-fixtures` `run` — convenience wrappers, not stages |
| `app` | the entry point; walks `COMMAND_GROUPS` in order |

**`COMMAND_GROUPS` order is `poc --help` order.** Typer lists commands in registration order, not
alphabetically, and importing a command module is what registers its commands. A plain
`from pier39_poc.cli import a, b, c` would be alphabetised by isort and silently reorder the help
output, which is why `app.py` walks an explicit tuple through `import_module`.

`workflow_cmds` is registered last so the real pipeline stages appear first.

**Command help text lives in the `@app.command(help=...)` argument, not in a docstring.** Typer
renders a command's docstring as its help, so a docstring here would be user-facing output rather
than documentation — and this repo keeps prose out of the code.

Heavy retrieval imports are function-local so `poc --help` does not load onnxruntime, numpy and
flashrank. Do not hoist them.

`seed-fixtures` reads `tests/fixtures/skout/seed_catalogue.json` as **data**. It previously
inserted `tests/` onto `sys.path` and imported `test_profile`, which made a shipped entry point
depend on the test suite — and the two had already drifted on variant ids.
