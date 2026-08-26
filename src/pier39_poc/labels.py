"""The label gate: is a rendered `Label: Value` pair a product spec or a storefront widget?

Both render identically once a page is reduced to visible text, and the same label means
opposite things on different stores. skout's `Pack Size` is a variant picker; remi's
`Quantity` is how many tablets are in the box. A single global regular expression cannot
separate them, which is why this decision is configured or classified per store rather
than hardcoded.

Three verdicts, not two. `uncertain` exists because the safe action for an unrecognised
label is to make it findable without making it quotable, so a label nobody has ruled on
lands in the retrieval corpus rather than being asserted to a shopper or thrown away.

Policies may only demote. `merge.is_quotable_theme_value` and `profile.is_commerce_constant`
still run afterwards and can reject anything a policy accepted; no policy can promote past
them. The manual allow and deny lists in `config/stores.yaml` are consulted before any
policy and override it, so a bad classifier verdict is correctable without a code change.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from . import llm
from .config import REPO_ROOT, StoreConfig

SPEC = "spec"
WIDGET = "widget"
UNCERTAIN = "uncertain"

VERDICTS = (SPEC, WIDGET, UNCERTAIN)

SPEC_LABELS_DIR = REPO_ROOT / "config" / "spec_labels"


def normalise(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip().lower())


class LabelPolicy(Protocol):
    name: str
    gates_template_constants: bool

    def verdict(self, store: StoreConfig, label: str, value: str) -> str: ...


@dataclass
class NonePolicy:
    """Rejects every per-product pair, reproducing behaviour before the gate existed."""

    name: str = "none"
    gates_template_constants: bool = False

    def verdict(self, store: StoreConfig, label: str, value: str) -> str:
        return WIDGET


def _manual_override(store: StoreConfig, label: str) -> str | None:
    key = normalise(label)
    if key in {normalise(x) for x in store.spec_label_deny}:
        return WIDGET
    if key in {normalise(x) for x in store.spec_label_allow}:
        return SPEC
    return None


def load_reference(slug: str, directory: Path | None = None) -> dict[str, str]:
    path = (directory or SPEC_LABELS_DIR) / f"{slug}.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for label, verdict in (raw.get("labels") or {}).items():
        if verdict not in VERDICTS:
            raise ValueError(f"{path}: {label!r} has verdict {verdict!r}, expected one of {VERDICTS}")
        out[normalise(label)] = verdict
    return out


@dataclass
class StaticPolicy:
    """Reads the hand-authored reference set in `config/spec_labels/<slug>.yaml`."""

    name: str = "static"
    gates_template_constants: bool = True
    directory: Path | None = None

    def verdict(self, store: StoreConfig, label: str, value: str) -> str:
        override = _manual_override(store, label)
        if override:
            return override
        return load_reference(store.slug, self.directory).get(normalise(label), UNCERTAIN)


PROMPT = """You classify labels taken from e-commerce product pages.

A SPEC label introduces a durable property of the product itself: what it is made of, how
much is in the package, what it is compatible with, how long it lasts. It stays true no
matter who views the page.

A WIDGET label belongs to the storefront's purchasing interface, not to the product. It
names a control the shopper operates and its value is whatever is currently selected:
quantity steppers, variant and pack pickers, subscription frequency selectors, and the
cart's own line-item headings. Page titles, marketing headings and promotional banners are
also WIDGET, because they are not properties of the product.

Answer UNCERTAIN only when the label could plausibly be either and the examples do not
settle it.

Store: {slug} sells {category}.
Label: {label}
Example values for this label: {examples}

Respond with exactly one word: SPEC, WIDGET or UNCERTAIN."""

STORE_CATEGORY = {
    "skout": "snack bars and other packaged foods",
    "remi": "dental products such as night guards, retainers and cleaning devices",
}

CLASSIFIER_MODEL = os.environ.get("PIER39_LABEL_MODEL", "gpt-5.5")


@dataclass
class ClassifierPolicy:
    """One model call per distinct label, cached on disk and committed.

    Only the label is sent, with up to three example values for disambiguation. Values are
    never sent for extraction: the model decides what a label means, it never produces a
    fact. That boundary is what keeps `DESIGN.md` §9's exclusion of LLM extraction intact
    while allowing LLM classification.

    A cached run makes no network call, so a profile that has been classified once stays
    reproducible offline. The cache is keyed by model as well as by label: two models
    disagree, and a cache that ignored the model would silently serve one model's verdicts
    for a run nominally using another, which would make any comparison between them
    meaningless.

    Reasoning models reject `max_tokens` and spend their budget before emitting anything, so
    the request is attempted in the modern shape first and falls back to the legacy one.
    """

    name: str = "llm"
    gates_template_constants: bool = True
    client: Any = None
    model: str = CLASSIFIER_MODEL
    # slug -> model -> label -> verdict. The model level is what lets a cache
    # written by one classifier survive a model change.
    _cache: dict[str, dict[str, dict[str, str]]] | None = None
    _dirty: set[str] | None = None

    def cache_path(self, store: StoreConfig) -> Path:
        return store.data_dir / "label_verdicts.json"

    LEGACY_CACHE_MODEL = "gpt-4o-mini"

    def _load_file(self, store: StoreConfig) -> dict[str, dict[str, str]]:
        if self._cache is None:
            self._cache = {}
        if store.slug not in self._cache:
            path = self.cache_path(store)
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if raw and all(isinstance(v, str) for v in raw.values()):
                raw = {self.LEGACY_CACHE_MODEL: raw}
            self._cache[store.slug] = raw
        return self._cache[store.slug]

    def _load(self, store: StoreConfig) -> dict[str, str]:
        return self._load_file(store).setdefault(self.model, {})

    def flush(self, store: StoreConfig) -> None:
        if self._cache is None or store.slug not in self._cache:
            return
        path = self.cache_path(store)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self._load_file(store), indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )

    def _ask(self, store: StoreConfig, label: str, examples: list[str]) -> str:
        self.client = llm.client_or_default(self.client)
        prompt = PROMPT.format(
            slug=store.slug,
            category=STORE_CATEGORY.get(store.slug, "consumer products"),
            label=label,
            examples="; ".join(e[:120] for e in examples[:3]) or "(none observed)",
        )
        answer = llm.complete(self.client, self.model, prompt).text.lower()
        for verdict in VERDICTS:
            if answer.startswith(verdict):
                return verdict
        return UNCERTAIN

    def verdict(self, store: StoreConfig, label: str, value: str) -> str:
        override = _manual_override(store, label)
        if override:
            return override
        cache = self._load(store)
        key = normalise(label)
        if key not in cache:
            cache[key] = self._ask(store, label, [value])
            self.flush(store)
        return cache[key]

    def warm(self, store: StoreConfig, examples: dict[str, list[str]]) -> dict[str, str]:
        cache = self._load(store)
        for label, values in sorted(examples.items()):
            key = normalise(label)
            if _manual_override(store, label):
                continue
            if key not in cache:
                cache[key] = self._ask(store, label, values)
        self.flush(store)
        return dict(cache)


def get_policy(name: str, client: Any = None) -> LabelPolicy:
    if name == "none":
        return NonePolicy()
    if name == "static":
        return StaticPolicy()
    if name == "llm":
        return ClassifierPolicy(client=client)
    raise ValueError(f"unknown label policy: {name!r}")
