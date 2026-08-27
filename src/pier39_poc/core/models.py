"""The shapes that cross module boundaries: products, assertions, the store profile.

Written by infra.shopify_api (api.jsonl) and ingest.profiling (profile.json); read by every
stage above. Field order IS the JSON key order. Gotchas: docs/reference/core.md
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

IDENTITY_FIELDS = frozenset({"title", "vendor", "product_type", "handle"})

IDENTITY_ASSERTION_FIELDS = ("title", "vendor", "product_type")


class Metafield(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    namespace: str = ""
    key: str = ""
    type: str = ""
    value: str | None = None
    updatedAt: str | None = None
    createdAt: str | None = None


class Variant(BaseModel):
    id: str | None = None
    title: str | None = None
    sku: str | None = None
    selectedOptions: list[dict[str, Any]] = []


class Product(BaseModel):
    id: str | None = None
    product_id: str = ""
    handle: str = ""
    title: str = ""
    vendor: str = ""
    product_type: str = ""
    tags: list[str] = []
    status: str = ""
    published_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    online_store_url: str | None = None
    description_html: str = ""
    template_suffix: str | None = None
    seo: dict[str, Any] = {}
    collections: list[dict[str, Any]] = []
    metafields: list[Metafield] = []
    variants: list[Variant] = []
    sellable: bool = True


class Assertion(BaseModel):
    field: str
    label: str | None
    value: str
    source: str
    source_kind: str
    rendered: bool
    trust_class: str
    source_updated_at: str | None = None


class KeyVerdictRecord(BaseModel):
    namespace: str
    key: str
    type: str
    admitted: bool
    reason: str
    hit_rate: float | None
    support: int
    observed: int
    matches: int
    label: str | None
    labels_seen: list[list[Any]]
    label_observations: int
    matched_handles: list[str]
    detail: str


class Declarations(BaseModel):
    fields: list[str]
    published: int
    with_declaration: int
    without_declaration: int
    handles_without_sample: list[str]


class ChromeSummary(BaseModel):
    threshold: float
    blocks: int
    set_hash: str
    frequency_histogram: dict[int, int]


class ThemeBlockCount(BaseModel):
    blocks: int
    words: int


class TemplateConstants(BaseModel):
    by_template: dict[str, list[dict[str, Any]]]
    per_product: dict[str, list[dict[str, Any]]]
    per_product_theme_counts: dict[str, ThemeBlockCount]
    per_product_theme_sample: dict[str, list[str]]


class Coverage(BaseModel):
    region_words_total: int
    residual_words: int
    template_constant_words: int
    per_product_unreachable_words: int
    coverage_pct: float | None


class StoreProfile(BaseModel):
    slug: str
    pages_analysed: int
    products_total: int
    products_published: int
    declarations: Declarations
    description_rendered_handles: list[str]
    chrome: ChromeSummary
    region_words: dict[str, int]
    attributes: dict[str, Any]
    allowlist: list[KeyVerdictRecord]
    rejected: list[KeyVerdictRecord]
    template_constants: TemplateConstants
    coverage: Coverage
