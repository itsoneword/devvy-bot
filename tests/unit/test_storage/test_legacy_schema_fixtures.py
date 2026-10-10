"""Schema regression guard (dv-5e4f Phase 0.1 fixture, Phase 1 Alembic).

The committed fixtures under tests/fixtures/legacy_schema/ are the structural
snapshot of the pre-Alembic schema (bot v26, data v4, llm_analytics,
channel_intel) -- the schema prod ran when Alembic took over. They never
change. Invariants, all permanent (they keep holding after new revisions):

1. The frozen legacy replay (src/storage/schema/legacy.py) still produces
   exactly that snapshot (byte-for-byte, incl. normalized CREATE text).
2. The Alembic baseline revision is structurally identical to it -- same
   columns/order/types/defaults/PKs/AUTOINCREMENT/CHECKs/FKs/indexes, same
   FTS/trigger/view SQL. (CREATE TABLE text is not compared verbatim:
   Alembic creates each table in one statement where legacy grew it by
   ALTER.) This is what makes stamping a bridged legacy DB correct.
3. A fresh DB built through the app's own init path (head) is structurally
   identical to a legacy DB bridged and upgraded to head: fresh installs and
   upgraded installs converge.
"""

import json
from pathlib import Path

import pytest

from scripts.schema_snapshot import snapshot
from src.storage.schema.legacy import build_legacy
from src.storage.schema.migrate import baseline_snapshot, ensure_schema
from src.storage.schema.structure import diff
from tests.helpers.db_builders import (
    DB_NAMES,
    build_fresh_bot_db,
    build_fresh_channel_intel_db,
    build_fresh_data_db,
    build_fresh_llm_analytics_db,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "legacy_schema"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("name", DB_NAMES)
def test_frozen_legacy_replay_matches_fixture_exactly(tmp_path, name):
    path = tmp_path / f"{name}.db"
    build_legacy(path, name)
    assert snapshot(path) == _load_fixture(name)


@pytest.mark.parametrize("name", DB_NAMES)
def test_alembic_baseline_matches_fixture(name):
    assert diff(baseline_snapshot(name), _load_fixture(name), "baseline") == []


def _bridged_head(tmp_path: Path, name: str) -> dict:
    path = tmp_path / "legacy" / f"{name}.db"
    path.parent.mkdir()
    build_legacy(path, name)
    ensure_schema(name, path)
    return snapshot(path)


async def test_fresh_bot_db_matches_bridged_legacy(tmp_path):
    path = tmp_path / "bot.db"
    manager = await build_fresh_bot_db(path)
    await manager.close()
    assert diff(snapshot(path), _bridged_head(tmp_path, "bot"), "fresh") == []


async def test_fresh_data_db_matches_bridged_legacy(tmp_path):
    path = tmp_path / "data.db"
    manager = await build_fresh_data_db(path)
    await manager.close()
    assert diff(snapshot(path), _bridged_head(tmp_path, "data"), "fresh") == []


async def test_fresh_llm_analytics_db_matches_bridged_legacy(tmp_path):
    path = tmp_path / "llm_analytics.db"
    build_fresh_llm_analytics_db(path)
    assert diff(snapshot(path), _bridged_head(tmp_path, "llm_analytics")) == []


async def test_fresh_channel_intel_db_matches_bridged_legacy(tmp_path):
    path = tmp_path / "channel_intel.db"
    await build_fresh_channel_intel_db(path)
    assert diff(snapshot(path), _bridged_head(tmp_path, "channel_intel")) == []


@pytest.mark.parametrize("name", ["bot", "data", "llm_analytics", "channel_intel"])
def test_fixture_file_exists(name):
    assert (FIXTURES / f"{name}.json").exists()
