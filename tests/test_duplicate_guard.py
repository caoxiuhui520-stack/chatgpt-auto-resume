"""Duplicate protection: the same quota window must only ever fire once."""

from __future__ import annotations

from datetime import timedelta

from app.models import utcnow
from app.resume.duplicate_guard import DuplicateGuard
from app.resume.retry_manager import RetryManager
from app.storage.state_store import StateStore
from tests.conftest import make_snapshot


def test_first_window_is_allowed(store: StateStore):
    guard = DuplicateGuard(store, cooldown_minutes=30)
    snap = make_snapshot(five_hour_used=100.0)
    assert guard.check(snap).allowed


def test_same_reset_id_is_refused(store: StateStore):
    guard = DuplicateGuard(store, cooldown_minutes=0)
    snap = make_snapshot(five_hour_used=0.0)
    guard.commit(snap.reset_id)
    decision = guard.check(snap)
    assert not decision.allowed
    assert decision.layer == "identity"


def test_different_reset_id_is_allowed(store: StateStore):
    guard = DuplicateGuard(store, cooldown_minutes=0)
    first = make_snapshot(five_hour_used=0.0, reset_in_minutes=300)
    guard.commit(first.reset_id)
    second = make_snapshot(five_hour_used=0.0, reset_in_minutes=605)
    assert second.reset_id != first.reset_id
    assert guard.check(second).allowed


def test_cooldown_blocks_then_releases(store: StateStore):
    guard = DuplicateGuard(store, cooldown_minutes=30)
    snap = make_snapshot(five_hour_used=0.0)
    now = utcnow()
    guard.commit(snap.reset_id, now)

    other = make_snapshot(five_hour_used=0.0, reset_in_minutes=700)
    decision = guard.check(other, now + timedelta(minutes=5))
    assert not decision.allowed and decision.layer == "cooldown"

    assert guard.check(other, now + timedelta(minutes=31)).allowed


def test_missing_reset_id_is_refused(store: StateStore):
    guard = DuplicateGuard(store, cooldown_minutes=0)
    snap = make_snapshot(five_hour_used=0.0)
    snap.reset_id = ""
    decision = guard.check(snap)
    assert not decision.allowed and decision.layer == "identity"


def test_persistence_survives_a_reload(store: StateStore):
    guard = DuplicateGuard(store, cooldown_minutes=0)
    snap = make_snapshot(five_hour_used=0.0)
    guard.commit(snap.reset_id)

    reloaded = StateStore(store.path)
    reloaded.load()
    guard2 = DuplicateGuard(reloaded, cooldown_minutes=0)
    assert not guard2.check(snap).allowed, "a restart must not reset duplicate protection"


def test_triggered_history_is_bounded(tmp_path):
    store = StateStore(tmp_path / "state.json")
    guard = DuplicateGuard(store, cooldown_minutes=0)
    for index in range(120):
        snap = make_snapshot(five_hour_used=0.0, reset_in_minutes=300 + index * 5)
        guard.commit(snap.reset_id)
    assert len(guard.state.triggered_reset_ids) <= 50


# -- retry manager ---------------------------------------------------------


def test_waitable_errors_do_not_consume_retries(store: StateStore):
    retry = RetryManager(store, max_retries=5, backoff_seconds=[0, 30, 120, 300, 900])
    retry.start_window("w1")
    for _ in range(20):
        decision = retry.record_failure("chatgpt_busy")
        assert decision.kind == "wait"
    assert retry.state.retry_count == 0
    assert not retry.retries_exhausted()


def test_hard_errors_follow_the_backoff_schedule(store: StateStore):
    retry = RetryManager(store, max_retries=3, backoff_seconds=[0, 30, 120])
    retry.start_window("w1")
    delays = [retry.record_failure("send_failed").delay_seconds for _ in range(3)]
    assert delays == [0.0, 30.0, 120.0]
    assert retry.record_failure("send_failed").give_up


def test_start_window_resets_counters(store: StateStore):
    retry = RetryManager(store, max_retries=5, backoff_seconds=[0, 30])
    retry.start_window("w1")
    retry.record_failure("send_failed")
    assert retry.state.retry_count == 1
    retry.start_window("w2")
    assert retry.state.retry_count == 0
