"""Shared fixtures. The seed catalogue is DATA, not code, so the CLI can read it too.

`tests/fixtures/skout/` holds five real skout product pages plus `seed_catalogue.json`,
the synthetic api.jsonl that pairs with them. `poc seed-fixtures` reads the same file, so
the two cannot drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pier39_poc.core.tuning import BlockTuning, ProfilingTuning, Tuning
from pier39_poc.infra.config import StoreConfig

TESTS_ROOT = Path(__file__).parent
REPO_ROOT = TESTS_ROOT.parent
FIXTURES = TESTS_ROOT / "fixtures" / "skout"
SEED_CATALOGUE = FIXTURES / "seed_catalogue.json"
SPEC_LABELS = REPO_ROOT / "config" / "spec_labels"


def seed_products() -> list[dict]:
    return json.loads(SEED_CATALOGUE.read_text(encoding="utf-8"))


def seed_handles() -> list[str]:
    return [p["handle"] for p in seed_products()]


@pytest.fixture()
def seed_catalogue() -> list[dict]:
    return seed_products()


@pytest.fixture()
def seeded_handles() -> list[str]:
    return seed_handles()


@pytest.fixture()
def store(tmp_path, monkeypatch) -> StoreConfig:
    """A store whose data dir is a tmp dir seeded with the real fixture pages."""
    import pier39_poc.infra.config as config

    # StoreConfig path properties read config.DATA_ROOT at access time.
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)

    cfg = StoreConfig(
        slug="skout",
        domain="www.skoutorganic.com",
        profile_pages=5,
        tuning=Tuning(
            blocks=BlockTuning(chrome_threshold=0.8),
            profiling=ProfilingTuning(allowlist_min_support=3),
        ),
    )
    cfg.pages_dir.mkdir(parents=True, exist_ok=True)

    for handle in seed_handles():
        (cfg.pages_dir / f"{handle}.html").write_text(
            (FIXTURES / f"{handle}.html").read_text(errors="ignore"), encoding="utf-8"
        )

    from pier39_poc.infra.artifacts import write_jsonl

    write_jsonl(cfg.api_path, seed_products())
    return cfg
