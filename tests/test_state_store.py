"""Persistent state: atomicity, corruption recovery, schema tolerance."""

from __future__ import annotations

import json

from app.models import UsageSnapshot, utcnow
from app.storage.state_store import SCHEMA_VERSION, State, StateStore


def test_missing_file_yields_defaults(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = store.load()
    assert state.program_state == "STARTING"
    assert state.last_triggered_reset_id == ""


def test_round_trip(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = store.load()
    state.program_state = "WAITING_RESET"
    state.last_triggered_reset_id = "codex:five_hour:123"
    store.save()

    reloaded = StateStore(tmp_path / "state.json").load()
    assert reloaded.program_state == "WAITING_RESET"
    assert reloaded.last_triggered_reset_id == "codex:five_hour:123"
    assert reloaded.schema_version == SCHEMA_VERSION


def test_corrupt_file_is_preserved_not_deleted(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = StateStore(path)
    state = store.load()
    assert state.program_state == "STARTING"
    assert path.with_suffix(".json.corrupt").exists(), "corrupt state is evidence, keep it"


def test_unknown_keys_are_dropped(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"program_state": "WORKING", "future_field": 1}), encoding="utf-8")
    state = StateStore(path).load()
    assert state.program_state == "WORKING"
    assert not hasattr(state, "future_field")


def test_snapshot_persisted_without_raw_payload(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.load()
    snap = UsageSnapshot(
        available=False,
        five_hour_remaining_percent=0.0,
        source="test",
        timestamp=utcnow(),
        raw={"secret_ish": "should not be written"},
    )
    snap.reset_id = "test:five_hour:1"
    store.update_snapshot(snap)
    store.save()

    written = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "raw" not in written["last_usage_snapshot"]
    assert written["last_seen_reset_id"] == "test:five_hour:1"


def test_mark_triggered_dedupes():
    state = State()
    now = utcnow()
    state.mark_triggered("a", now)
    state.mark_triggered("a", now)
    state.mark_triggered("b", now)
    assert state.triggered_reset_ids == ["a", "b"]
    assert state.has_triggered("a")
    assert not state.has_triggered("c")


def test_atomic_write_leaves_no_temp_files(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.load()
    for index in range(25):
        store.state.restart_count = index
        store.save()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".state-")]
    assert leftovers == []
