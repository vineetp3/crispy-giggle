"""Per-attribute reachability: can this store answer a question of this kind, and from where.

Built by ingest.profiling, printed by presentation.report as the deliverable's headline table.
Gotchas and their measurements: docs/reference/core.md
"""

from __future__ import annotations

import re
from typing import Any

from pier39_poc.core.models import KeyVerdictRecord

ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "allergens": ("contains", "allergen", "free_from", "nut_free", "gluten", "dairy"),
    "nutrition": ("nutrient", "nutrition", "calorie", "protein", "macro"),
    "ingredients": ("ingredient", "formula", "composition"),
    "materials": ("material", "fabric", "bpa", "made_of", "made from"),
    "power": ("battery", "charge", "power", "voltage", "rechargeable"),
    "dimensions": ("dimension", "size", "capacity", "volume", "weight", "length", "tank"),
    "care": ("care", "wash", "clean", "storage", "shelf life", "treatment", "duration"),
    "compatibility": ("compatible", "fits", "works with", "suitable"),
    "usage": ("usage", "how to", "instruction", "direction", "frequency", "use_case"),
    "certifications": ("certif", "organic", "vegan", "verified", "badge", "award"),
}

SOURCE_ORDER = ("api", "theme", "image")


def _matches(text: str, needles: tuple[str, ...]) -> bool:
    lowered = re.sub(r"[_\-.]+", " ", (text or "").lower())
    return any(n.replace("_", " ") in lowered for n in needles)


def build(
    allowlist: list[KeyVerdictRecord],
    template_constants: dict[str, list[dict[str, Any]]],
    reference_keys: dict[str, int],
    per_product_labels: set[str] | None = None,
) -> dict[str, Any]:
    theme_labels = sorted(
        {c["label"] for blocks in template_constants.values() for c in blocks if c.get("label")}
        | set(per_product_labels or set())
    )

    out: dict[str, Any] = {}
    mapped_api: set[str] = set()
    mapped_theme: set[str] = set()
    mapped_image: set[str] = set()

    for name, needles in ATTRIBUTES.items():
        api = [
            {"key": f"{e.namespace}.{e.key}", "support": e.support}
            for e in allowlist
            if _matches(f"{e.namespace}.{e.key}", needles)
            or _matches(e.label or "", needles)
        ]
        theme = [label for label in theme_labels if _matches(label, needles)]
        image = [
            {"key": key, "products": count}
            for key, count in sorted(reference_keys.items())
            if _matches(key, needles)
        ]
        mapped_api.update(e["key"] for e in api)
        mapped_theme.update(theme)
        mapped_image.update(e["key"] for e in image)

        sources = [
            label
            for label, present in (("api", api), ("theme", theme), ("image", image))
            if present
        ]
        out[name] = {
            "sources": sources,
            "reachable": bool(sources),
            "api": api,
            "theme": theme,
            "image": image,
        }

    out["_unmapped"] = {
        "api": sorted(
            {
                f"{e.namespace}.{e.key}"
                for e in allowlist
                if f"{e.namespace}.{e.key}" not in mapped_api
            }
        ),
        "theme": [label for label in theme_labels if label not in mapped_theme],
        "image": sorted(k for k in reference_keys if k not in mapped_image),
    }
    return out


def summary(attributes: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys((*SOURCE_ORDER, "absent"), 0)
    for name, entry in attributes.items():
        if name.startswith("_"):
            continue
        if not entry["sources"]:
            counts["absent"] += 1
            continue
        for source in SOURCE_ORDER:
            if source in entry["sources"]:
                counts[source] += 1
    return counts
