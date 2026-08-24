"""Artefact IO and the run manifest.

Every stage reads the previous stage's files from disk and writes its own. No stage
fetches on behalf of another. That is what makes profiling re-runnable fifty times
without re-crawling.

The manifest records the *resolved* config for each stage run. A coverage number
without the threshold and page count that produced it is worthless a week later.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import StoreConfig


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def ensure_dirs(store: StoreConfig) -> None:
    store.pages_dir.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# jsonl
# --------------------------------------------------------------------------- #

def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"missing artefact: {path}")
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_products(store: StoreConfig) -> list[dict[str, Any]]:
    return list(read_jsonl(store.api_path))


# --------------------------------------------------------------------------- #
# json
# --------------------------------------------------------------------------- #

def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"missing artefact: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #

def page_paths(store: StoreConfig, handle: str) -> tuple[Path, Path]:
    return store.pages_dir / f"{handle}.html", store.pages_dir / f"{handle}.md"


def write_page(store: StoreConfig, handle: str, raw_html: str, markdown: str) -> str:
    ensure_dirs(store)
    html_path, md_path = page_paths(store, handle)
    html_path.write_text(raw_html, encoding="utf-8")
    md_path.write_text(markdown or "", encoding="utf-8")
    return sha256(raw_html)


def read_page_html(store: StoreConfig, handle: str) -> str | None:
    html_path, _ = page_paths(store, handle)
    if not html_path.exists():
        return None
    return html_path.read_text(encoding="utf-8", errors="ignore")


def crawled_handles(store: StoreConfig) -> list[str]:
    if not store.pages_dir.exists():
        return []
    return sorted(p.stem for p in store.pages_dir.glob("*.html"))


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #

def record_stage(store: StoreConfig, stage: str, detail: dict[str, Any]) -> None:
    """Append a stage record to the store's run manifest, with the resolved config."""
    manifest: dict[str, Any] = {}
    if store.manifest_path.exists():
        manifest = read_json(store.manifest_path)
    manifest.setdefault("slug", store.slug)
    manifest.setdefault("stages", {})
    manifest["stages"][stage] = {
        "at": now_iso(),
        "resolved_config": store.as_dict(),
        **detail,
    }
    write_json(store.manifest_path, manifest)
