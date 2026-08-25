"""Configuration loading: stores.yaml + defaults merge + secrets from env.

Secrets never appear in stores.yaml. Tokens come from a single JSON blob keyed by
store slug, so adding a store is a config edit and a token edit, nothing more.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "stores.yaml"
DATA_ROOT = REPO_ROOT / "data"

TOKENS_ENV = "PIER39_SHOPIFY_TOKENS"
STOREFRONT_TOKENS_ENV = "PIER39_SHOPIFY_STOREFRONT_TOKENS"

CRAWL_SCOPES = ("none", "sample", "template_representatives", "all")
SAMPLING_MODES = ("by_template", "by_product_type", "random", "first_n", "explicit")
FETCH_PROFILES = ("plain", "stealth", "undetected")


class ConfigError(RuntimeError):
    pass


@dataclass
class StoreConfig:
    slug: str
    domain: str
    enabled: bool = True

    admin_api_version: str = "2026-01"

    profile_pages: int = 20
    crawl_scope: str = "sample"
    max_pages: int = 250

    sampling: str = "by_template"
    sampling_seed: int = 1739
    explicit_handles: list[str] = field(default_factory=list)

    fetch_profile: str = "plain"
    concurrency: int = 4
    delay_seconds: tuple[float, float] = (1.0, 3.0)
    page_timeout_ms: int = 45000

    chrome_threshold: float = 0.8
    min_block_chars: int = 3
    containment_threshold: float = 0.8
    allowlist_min_support: int = 3
    allowlist_min_hit_rate: float = 0.8

    storefront_api_version: str = "2026-01"
    market_country: str = "US"

    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1024
    rerank_model: str = "rerank-v4.0-fast"

    def validate(self) -> None:
        if self.crawl_scope not in CRAWL_SCOPES:
            raise ConfigError(f"{self.slug}: crawl_scope must be one of {CRAWL_SCOPES}")
        if self.sampling not in SAMPLING_MODES:
            raise ConfigError(f"{self.slug}: sampling must be one of {SAMPLING_MODES}")
        if self.fetch_profile not in FETCH_PROFILES:
            raise ConfigError(f"{self.slug}: fetch_profile must be one of {FETCH_PROFILES}")
        if not 0.0 < self.chrome_threshold <= 1.0:
            raise ConfigError(f"{self.slug}: chrome_threshold must be in (0, 1]")
        if self.chrome_threshold >= 1.0:
            raise ConfigError(
                f"{self.slug}: chrome_threshold of 1.0 leaks store-wide sections into "
                "every product region. See DESIGN.md 5.3. Use 0.8."
            )
        if self.sampling == "explicit" and not self.explicit_handles:
            raise ConfigError(f"{self.slug}: sampling=explicit needs explicit_handles")
        if self.profile_pages < 5:
            raise ConfigError(
                f"{self.slug}: profile_pages below 5 makes differencing unreliable"
            )

    @property
    def data_dir(self) -> Path:
        return DATA_ROOT / self.slug

    @property
    def pages_dir(self) -> Path:
        return self.data_dir / "pages"

    @property
    def api_path(self) -> Path:
        return self.data_dir / "api.jsonl"

    @property
    def profile_path(self) -> Path:
        return self.data_dir / "profile.json"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "run.json"

    @property
    def fetch_manifest_path(self) -> Path:
        return self.data_dir / "fetch_manifest.jsonl"

    def base_url(self) -> str:
        return f"https://{self.domain}"

    def graphql_url(self) -> str:
        return f"https://{self.domain}/admin/api/{self.admin_api_version}/graphql.json"

    def storefront_url(self) -> str:
        return f"https://{self.domain}/api/{self.storefront_api_version}/graphql.json"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_env() -> None:
    load_dotenv(REPO_ROOT / ".env")


def load_stores(
    config_path: Path | None = None, only: str | None = None
) -> list[StoreConfig]:
    path = config_path or DEFAULT_CONFIG
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")

    raw = yaml.safe_load(path.read_text()) or {}
    defaults: dict[str, Any] = raw.get("defaults") or {}
    entries: list[dict[str, Any]] = raw.get("stores") or []
    if not entries:
        raise ConfigError(f"no stores defined in {path}")

    known = set(StoreConfig.__dataclass_fields__)
    out: list[StoreConfig] = []
    for entry in entries:
        merged = {**defaults, **entry}
        unknown = set(merged) - known
        if unknown:
            raise ConfigError(f"unknown config keys for {entry.get('slug')}: {sorted(unknown)}")
        if "delay_seconds" in merged and merged["delay_seconds"] is not None:
            lo, hi = merged["delay_seconds"]
            merged["delay_seconds"] = (float(lo), float(hi))
        store = StoreConfig(**merged)
        store.validate()
        out.append(store)

    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        unknown_slugs = wanted - {s.slug for s in out}
        if unknown_slugs:
            raise ConfigError(f"unknown store slug(s): {sorted(unknown_slugs)}")
        return [s for s in out if s.slug in wanted]

    return [s for s in out if s.enabled]


def token_for(slug: str) -> str:
    blob = os.environ.get(TOKENS_ENV)
    if not blob:
        raise ConfigError(
            f"{TOKENS_ENV} is not set. Copy .env.example to .env and fill it in."
        )
    try:
        tokens = json.loads(blob)
    except ValueError as exc:
        raise ConfigError(f"{TOKENS_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(tokens, dict):
        raise ConfigError(f"{TOKENS_ENV} must be a JSON object keyed by store slug")
    token = tokens.get(slug)
    if not token:
        raise ConfigError(
            f"no token for store '{slug}' in {TOKENS_ENV}. "
            f"Available: {sorted(tokens)}"
        )
    return str(token)


def storefront_token_for(slug: str) -> str | None:
    blob = os.environ.get(STOREFRONT_TOKENS_ENV)
    if not blob:
        return None
    try:
        tokens = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(tokens, dict):
        return None
    token = tokens.get(slug)
    return token if isinstance(token, str) and token else None


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def database_url() -> str:
    return os.environ.get(
        "DATABASE_URL", "postgresql://pier39:pier39@localhost:5433/discovery"
    )
