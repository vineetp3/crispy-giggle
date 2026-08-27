# scripts/

Output verification. Nothing here is part of the package; it exists because the pipeline's
real regression surface is its output, not its unit tests.

The layer rules are not here — they are import-linter contracts in `pyproject.toml`, run
with `uv run lint-imports`.

## `verify_output.sh`

`merge` and `profile` have no unit tests, and the CLI can break in ways `ruff` and the
whole test suite pass straight through — a stale import in a command module, for example.
This runs the full chain on the real catalogues in `data/` and diffs 24 outputs against a
saved copy.

```bash
docker compose up -d db
scripts/verify_output.sh capture   # once, before you start changing things
scripts/verify_output.sh check     # after every change
```

Needs a populated `data/` and a reachable Postgres. `profile.json` is byte-reproducible
for a fixed input, so any diff is a real change.

`canon_output.py` neutralises two known cosmetic nondeterminisms before diffing; see its
docstring. Both are pre-existing and neither affects a verdict.
