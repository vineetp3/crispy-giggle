"""Load config/stores.yaml, merge defaults, read secrets from the environment.

The bottom of the stack; every stage takes a StoreConfig. Gotchas: docs/reference/infra.md
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pier39_poc.core.tuning import Tuning

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO_ROOT / "config" / "stores.yaml"
DATA_ROOT = REPO_ROOT / "data"

TOKENS_ENV = "PIER39_SHOPIFY_TOKENS"
STOREFRONT_TOKENS_ENV = "PIER39_SHOPIFY_STOREFRONT_TOKENS"

CRAWL_SCOPES = ("none", "sample", "template_representatives", "all")
SAMPLING_MODES = ("by_template", "by_product_type", "random", "first_n", "explicit")
FETCH_PROFILES = ("plain", "stealth", "undetected")


class ConfigError(RuntimeError):
    pass


class StoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    slug: str
    domain: str
    enabled: bool = True

    admin_api_version: str = "2026-01"

    profile_pages: int = 20
    crawl_scope: str = "sample"
    max_pages: int = 250

    sampling: str = "by_template"
    sampling_seed: int = 1739
    explicit_handles: list[str] = Field(default_factory=list)

    fetch_profile: str = "plain"
    concurrency: int = 4
    delay_seconds: tuple[float, float] = (1.0, 3.0)
    page_timeout_ms: int = 45000
    escalation_cooldown_seconds: float = 30.0
    final_retry_delay_seconds: float = 60.0

    tuning: Tuning = Tuning()

    spec_label_allow: list[str] = Field(default_factory=list)
    spec_label_deny: list[str] = Field(default_factory=list)

    storefront_api_version: str = "2026-01"
    market_country: str = "US"

    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1024
    rerank_model: str = "ms-marco-MiniLM-L-12-v2"

    @model_validator(mode="after")
    def _check(self) -> StoreConfig:
        if self.crawl_scope not in CRAWL_SCOPES:
            raise ConfigError(f"{self.slug}: crawl_scope must be one of {CRAWL_SCOPES}")
        if self.sampling not in SAMPLING_MODES:
            raise ConfigError(f"{self.slug}: sampling must be one of {SAMPLING_MODES}")
        if self.fetch_profile not in FETCH_PROFILES:
            raise ConfigError(f"{self.slug}: fetch_profile must be one of {FETCH_PROFILES}")
        if not 0.0 < self.tuning.blocks.chrome_threshold <= 1.0:
            raise ConfigError(f"{self.slug}: chrome_threshold must be in (0, 1]")
        if self.tuning.blocks.chrome_threshold >= 1.0:
            raise ConfigError(
                f"{self.slug}: chrome_threshold of 1.0 leaks store-wide sections into "
                "every product region. See DESIGN.md 5.3. Use 0.8."
            )
        if self.sampling == "explicit" and not self.explicit_handles:
            raise ConfigError(f"{self.slug}: sampling=explicit needs explicit_handles")
        if self.escalation_cooldown_seconds < 0 or self.final_retry_delay_seconds < 0:
            raise ConfigError(f"{self.slug}: cooldown/retry delays must not be negative")
        if self.profile_pages < 5:
            raise ConfigError(
                f"{self.slug}: profile_pages below 5 makes differencing unreliable"
            )
        return self

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
        return self.model_dump()


def load_env() -> None:
    load_dotenv(REPO_ROOT / ".env")


def _merge_tuning(defaults: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    merged = {**defaults, **entry}
    base = defaults.get("tuning") or {}
    override = entry.get("tuning") or {}
    if base or override:
        groups = {**base}
        for group, values in override.items():
            groups[group] = {**(base.get(group) or {}), **(values or {})}
        merged["tuning"] = groups
    return merged


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

    known = set(StoreConfig.model_fields)
    out: list[StoreConfig] = []
    for entry in entries:
        merged = _merge_tuning(defaults, entry)
        unknown = set(merged) - known
        if unknown:
            raise ConfigError(f"unknown config keys for {entry.get('slug')}: {sorted(unknown)}")
        if "delay_seconds" in merged and merged["delay_seconds"] is not None:
            lo, hi = merged["delay_seconds"]
            merged["delay_seconds"] = (float(lo), float(hi))
        out.append(StoreConfig(**merged))

    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        unknown_slugs = wanted - {s.slug for s in out}
        if unknown_slugs:
            raise ConfigError(f"unknown store slug(s): {sorted(unknown_slugs)}")
        return [s for s in out if s.slug in wanted]

    return [s for s in out if s.enabled]


DEFAULT_DATABASE_URL = "postgresql://pier39:pier39@localhost:5433/discovery"


class Secrets(BaseSettings):

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    shopify_tokens: str | None = Field(default=None, validation_alias=TOKENS_ENV)
    storefront_tokens: str | None = Field(
        default=None, validation_alias=STOREFRONT_TOKENS_ENV
    )
    database_url: str = Field(
        default=DEFAULT_DATABASE_URL, validation_alias="DATABASE_URL"
    )


def _token_map(blob: str, env_name: str, strict: bool) -> dict[str, Any] | None:
    try:
        tokens = json.loads(blob)
    except ValueError as exc:
        if not strict:
            return None
        raise ConfigError(f"{env_name} is not valid JSON: {exc}") from exc
    if not isinstance(tokens, dict):
        if not strict:
            return None
        raise ConfigError(f"{env_name} must be a JSON object keyed by store slug")
    return tokens


def token_for(slug: str) -> str:
    blob = Secrets().shopify_tokens
    if not blob:
        raise ConfigError(
            f"{TOKENS_ENV} is not set. Copy .env.example to .env and fill it in."
        )
    tokens = _token_map(blob, TOKENS_ENV, strict=True)
    assert tokens is not None
    token = tokens.get(slug)
    if not token:
        raise ConfigError(
            f"no token for store '{slug}' in {TOKENS_ENV}. "
            f"Available: {sorted(tokens)}"
        )
    return str(token)


def storefront_token_for(slug: str) -> str | None:
    blob = Secrets().storefront_tokens
    if not blob:
        return None
    tokens = _token_map(blob, STOREFRONT_TOKENS_ENV, strict=False)
    if tokens is None:
        return None
    token = tokens.get(slug)
    return token if isinstance(token, str) and token else None


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def database_url() -> str:
    return Secrets().database_url
