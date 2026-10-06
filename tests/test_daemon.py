"""Daemon internals: window-change detection, fallbacks, adaptive polling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import UsageSnapshot, utcnow
from app.state_machine import State
from tests.conftest import make_daemon, make_snapshot

NOW = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)


def _snap(available: bool, reset_in_minutes: float, used: float | None = None):
    return make_snapshot(
        five_hour_used=(0.0 if available else 100.0) if used is None else used,
        reset_in_minutes=reset_in_minutes,
        now=NOW,
    )


def test_new_window_requires_a_forward_move(cfg, store):
    from app.usage.fake import FakeUsageProvider

    provider = FakeUsageProvider(cfg)
    daemon, _c, _n, _r = make_daemon(cfg, store, provider)

    prev = _snap(False, 300)
    # Same window: quota readable but the reset did not move.
    same = _snap(True, 300)
    assert daemon._new_window_detected(same, prev) is False

    # Forward move of 5+ minutes: a new window.
    moved = _snap(True, 305)
    assert daemon._new_window_detected(moved, prev) is True


def test_no_previous_snapshot_never_counts_as_restoration(cfg, store):
    from app.usage.fake import FakeUsageProvider

    daemon, _c, _n, _r = make_daemon(cfg, store, FakeUsageProvider(cfg))
    assert daemon._new_window_detected(_snap(True, 300), None) is False


def test_still_exhausted_never_counts(cfg, store):
    from app.usage.fake import FakeUsageProvider

    daemon, _c, _n, _r = make_daemon(cfg, store, FakeUsageProvider(cfg))
    assert daemon._new_window_detected(_snap(False, 300), _snap(False, 300)) is False


def test_forward_move_of_less_than_five_minutes_is_drift(cfg, store):
    from app.usage.fake import FakeUsageProvider

    daemon, _c, _n, _r = make_daemon(cfg, store, FakeUsageProvider(cfg))
    drifted = _snap(True, 302)
    assert daemon._new_window_detected(drifted, _snap(False, 300)) is False


def test_unknown_previous_reset_still_counts(cfg, store):
    from app.usage.fake import FakeUsageProvider

    daemon, _c, _n, _r = make_daemon(cfg, store, FakeUsageProvider(cfg))
    prev = UsageSnapshot(available=False, five_hour_remaining_percent=0.0,
                         five_hour_reset_at=None, source="fake", timestamp=NOW)
    prev.reset_id = "fake:five_hour:1"
    cur = _snap(True, 300)
    assert daemon._new_window_detected(cur, prev) is True


def test_fallback_provider_is_used_when_primary_fails(cfg, store):
    from app.usage.fake import FakeUsageProvider

    good = FakeUsageProvider(cfg)
    good.set_script([make_snapshot(five_hour_used=42.0)])

    daemon, _c, _n, _r = make_daemon(cfg, store, _BrokenProvider())
    daemon.fallbacks = [good]

    snapshot = daemon.read_usage()
    assert snapshot.ok
    assert snapshot.source == "fake"
    assert snapshot.five_hour_remaining_percent == 58.0


def test_all_providers_failing_marks_the_snapshot_stale(cfg, store):
    daemon, _c, _n, _r = make_daemon(cfg, store, _BrokenProvider())
    daemon.fallbacks = [_BrokenProvider()]
    snapshot = daemon.read_usage()
    assert not snapshot.ok
    assert snapshot.stale


def test_interval_is_base_when_working(cfg, store):
    from app.usage.fake import FakeUsageProvider

    cfg.poll_interval_seconds = 30
    daemon, _c, _n, _r = make_daemon(cfg, store, FakeUsageProvider(cfg))
    daemon.sm.force(State.WORKING, "test")
    assert daemon.next_interval() == 30.0


def test_interval_speeds_up_near_the_reset(cfg, store):
    from app.usage.fake import FakeUsageProvider

    cfg.poll_interval_seconds = 30
    daemon, _c, _n, _r = make_daemon(cfg, store, FakeUsageProvider(cfg))
    daemon.sm.force(State.WAITING_RESET, "test")

    soon = utcnow() + timedelta(minutes=2)
    daemon.store.state.last_usage_snapshot = {
        "five_hour_reset_at": soon.isoformat(),
        "available": False,
    }
    assert daemon.next_interval() < 30.0

    far = utcnow() + timedelta(hours=4)
    daemon.store.state.last_usage_snapshot = {
        "five_hour_reset_at": far.isoformat(),
        "available": False,
    }
    assert daemon.next_interval() == 30.0


def test_interval_speeds_up_after_the_advertised_reset(cfg, store):
    from app.usage.fake import FakeUsageProvider

    cfg.poll_interval_seconds = 60
    daemon, _c, _n, _r = make_daemon(cfg, store, FakeUsageProvider(cfg))
    daemon.sm.force(State.WAITING_RESET, "test")
    daemon.store.state.last_usage_snapshot = {
        "five_hour_reset_at": (utcnow() - timedelta(minutes=5)).isoformat(),
        "available": False,
    }
    assert daemon.next_interval() <= 15.0


def test_heartbeat_emits_on_schedule(cfg, store):
    from app.runtime.health import HealthMonitor

    health = HealthMonitor(heartbeat_seconds=300)
    now = utcnow()
    assert health.tick(now) is False          # first tick primes the clock
    assert health.tick(now + timedelta(seconds=10)) is False
    assert health.tick(now + timedelta(seconds=400)) is True


def test_health_alert_after_threshold_failures(cfg, store):
    from app.runtime.health import HealthMonitor

    health = HealthMonitor(max_consecutive_failures=3)
    assert health.record_provider_failure("boom") is False
    assert health.record_provider_failure("boom") is False
    assert health.record_provider_failure("boom") is True
    health.record_provider_success()
    assert health.consecutive_provider_failures == 0


class _BrokenProvider:
    name = "broken"
    description = "always fails"

    def get_usage(self):
        return UsageSnapshot(source=self.name, error="boom", timestamp=utcnow(), stale=True)

    def close(self):
        pass

    def describe(self):
        return self.name
