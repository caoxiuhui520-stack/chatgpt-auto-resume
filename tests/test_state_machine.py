"""State machine transitions, including the 'ERROR is not terminal' rule."""

from __future__ import annotations

from app.models import UsageSnapshot, utcnow
from app.state_machine import ALLOWED_TRANSITIONS, State, StateMachine, WorldFacts


def snap(available: bool, ok: bool = True) -> UsageSnapshot:
    s = UsageSnapshot(
        available=available,
        five_hour_remaining_percent=100.0 if available else 0.0,
        source="test",
        timestamp=utcnow(),
        error=None if ok else "read failed",
    )
    s.reset_id = "test:five_hour:1000"
    return s


def test_startup_into_working_when_quota_available():
    sm = StateMachine(State.STARTING)
    assert sm.step(snap(True), WorldFacts(quota_available=True)) is State.WORKING


def test_startup_into_exhausted_when_no_quota():
    sm = StateMachine(State.STARTING)
    assert sm.step(snap(False), WorldFacts(quota_available=False)) is State.QUOTA_EXHAUSTED


def test_working_to_exhausted_to_waiting_reset():
    sm = StateMachine(State.WORKING)
    assert sm.step(snap(False), WorldFacts(quota_available=False)) is State.QUOTA_EXHAUSTED
    assert sm.step(snap(False), WorldFacts(quota_available=False)) is State.WAITING_RESET


def test_waiting_reset_to_ready_only_on_a_new_window():
    sm = StateMachine(State.WAITING_RESET)
    # Quota readable but the window did not change: stay put.
    assert sm.step(snap(True), WorldFacts(quota_available=True)) is State.WORKING
    sm = StateMachine(State.WAITING_RESET)
    assert (
        sm.step(snap(True), WorldFacts(quota_available=True, new_window_detected=True))
        is State.READY_TO_RESUME
    )


def test_ready_to_resume_to_resuming_then_cooldown():
    sm = StateMachine(State.READY_TO_RESUME)
    assert sm.step(snap(True), WorldFacts(quota_available=True, safe_to_resume=True)) is State.RESUMING
    assert sm.step(snap(True), WorldFacts(quota_available=True, resume_succeeded=True)) is State.COOLDOWN


def test_ready_to_resume_skips_when_already_attempted():
    sm = StateMachine(State.READY_TO_RESUME)
    state = sm.step(snap(True), WorldFacts(quota_available=True, already_attempted=True))
    assert state is State.COOLDOWN


def test_cooldown_returns_to_working_after_elapse():
    sm = StateMachine(State.COOLDOWN)
    assert sm.step(snap(True), WorldFacts(quota_available=True, cooldown_elapsed=True)) is State.WORKING


def test_error_is_recoverable_not_terminal():
    sm = StateMachine(State.WORKING)
    # Repeated provider failure drives ERROR.
    facts = WorldFacts(provider_error=True)
    for _ in range(3):
        sm.step(snap(False, ok=False), facts)
    assert sm.state is State.ERROR
    # A successful read recovers.
    assert sm.step(snap(True), WorldFacts(quota_available=True)) is State.WORKING


def test_error_state_has_outgoing_transitions():
    assert ALLOWED_TRANSITIONS[State.ERROR], "ERROR must never be a dead end"
    assert State.WORKING in ALLOWED_TRANSITIONS[State.ERROR]


def test_history_records_transitions():
    sm = StateMachine(State.STARTING)
    sm.step(snap(True), WorldFacts(quota_available=True))
    history = sm.history()
    assert history and history[-1]["to"] == "WORKING"
