"""Crash-simulation tests for the durable two-phase send transaction.

The guarantee under test: **whatever point the process dies at, a restart
must never produce an automatic second send for the same quota window.**

Covered crash points (per the hardening brief):

1. crash before PREPARED           -> nothing happened; a normal send is fine
2. crash after PREPARED, before keyboard -> leftover PREPARED: refuse, UNCERTAIN
3. crash immediately after Enter   -> keystrokes happened, PREPARED on disk: refuse
4. crash before CONFIRMED          -> confirm lost, PREPARED on disk: refuse
5. restart with UNCERTAIN          -> same window: refuse; new window: fresh chance
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.chatgpt.fake import FakeChatGptController
from app.models import ErrorKind
from app.notification.base import Event
from app.state_machine import State
from app.storage.state_store import PendingSend, StateStore
from app.usage.fake import FakeUsageProvider
from tests.conftest import drive, make_daemon, make_snapshot

NOW = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)


def _pair(now: datetime = NOW) -> list:
    """[exhausted, restored-with-new-window] two-tick script."""
    return [
        make_snapshot(five_hour_used=100.0, reset_in_minutes=300, now=now),
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605, now=now),
    ]


def _restored(now: datetime = NOW):
    return make_snapshot(five_hour_used=0.0, reset_in_minutes=605, now=now)


def _make(cfg, store, controller=None, script=None, initial_state=State.STARTING):
    provider = FakeUsageProvider(cfg)
    provider.set_script(script if script is not None else _pair())
    return make_daemon(cfg, store, provider, controller, initial_state=initial_state)


def _reload(path) -> StateStore:
    fresh = StateStore(path)
    fresh.load()
    return fresh


# ---------------------------------------------------------------------------
# 1. No crash: PREPARED -> CONFIRMED, and the marker is cleaned up.
# ---------------------------------------------------------------------------


def test_normal_send_completes_transaction(cfg, store):
    daemon, controller, _notifier, _retry = _make(cfg, store)
    drive(daemon, 2)

    assert len(controller.sent_prompts) == 1
    assert store.state.pending_send_status == PendingSend.CONFIRMED
    assert store.state.pending_send_reset_id == ""
    assert store.state.has_triggered(_restored().reset_id)

    # Nothing pending survives on disk either.
    reloaded = _reload(store.path)
    assert reloaded.state.pending_send_status == PendingSend.CONFIRMED
    assert not reloaded.state.has_prepared_send()
    assert not reloaded.state.has_uncertain_send()


# ---------------------------------------------------------------------------
# 2. Crash after PREPARED but before any keystroke.
# ---------------------------------------------------------------------------


def test_crash_after_prepared_before_keyboard_never_resends(cfg, store):
    # Simulate the crash: PREPARED was persisted, then the process died.
    store.state.begin_pending_send(_restored().reset_id, NOW)
    store.save()

    store2 = _reload(store.path)
    assert store2.state.has_prepared_send()

    daemon, controller, notifier, retry = _make(cfg, store2)
    drive(daemon, 4)

    assert controller.send_attempts == 0, "a leftover PREPARED must block any new send"
    assert controller.sent_prompts == []
    assert store2.state.has_uncertain_send()
    assert retry.state.retry_count == 0, "UNCERTAIN must not consume the retry budget"
    assert any(event is Event.SEND_UNCERTAIN for event, _ in notifier.sent)
    # The marker survives on disk - only a genuinely new window may clear it.
    assert _reload(store.path).state.has_uncertain_send()


# ---------------------------------------------------------------------------
# 3. Crash immediately after Enter (keystrokes delivered, no commit).
# ---------------------------------------------------------------------------


class _CrashAfterEnter(FakeChatGptController):
    """Simulates the process dying between Enter and post-send verify."""

    def verify_sent(self, timeout: float = 3.0):  # noqa: ARG002
        raise RuntimeError("simulated process crash right after Enter")


def test_crash_immediately_after_enter_never_resends(cfg, store):
    crasher = _CrashAfterEnter(cfg)
    daemon, controller, _n, _r = _make(cfg, store, crasher)

    with pytest.raises(RuntimeError):
        drive(daemon, 2)

    # The keystrokes really happened in the previous life...
    assert len(controller.sent_prompts) == 1
    # ...and the disk still shows an unfinished PREPARED transaction.
    reloaded = _reload(store.path)
    assert reloaded.state.has_prepared_send()

    # Restart with a healthy controller: the same window must NOT be resent.
    daemon2, controller2, notifier2, retry2 = _make(cfg, reloaded)
    drive(daemon2, 4)

    assert controller2.sent_prompts == []
    assert reloaded.state.has_uncertain_send()
    assert retry2.state.retry_count == 0
    assert any(event is Event.SEND_UNCERTAIN for event, _ in notifier2.sent)


# ---------------------------------------------------------------------------
# 4. Crash after a positive verify but before CONFIRMED is persisted.
# ---------------------------------------------------------------------------


class _CrashOnConfirmStore(StateStore):
    """Dies at the exact moment the CONFIRMED state would be written."""

    def save(self, state=None) -> None:
        if self.state.pending_send_status == PendingSend.CONFIRMED:
            raise RuntimeError("simulated crash before CONFIRMED was persisted")
        super().save(state)


def test_crash_before_confirmed_never_resends(cfg, tmp_path):
    store = _CrashOnConfirmStore(tmp_path / "state.json")
    daemon, controller, _n, _r = _make(cfg, store)

    with pytest.raises(RuntimeError):
        drive(daemon, 2)

    assert len(controller.sent_prompts) == 1
    # In memory we reached CONFIRMED, but the disk still says PREPARED.
    reloaded = _reload(store.path)
    assert reloaded.state.has_prepared_send()

    daemon2, controller2, notifier2, retry2 = _make(cfg, reloaded)
    drive(daemon2, 4)

    assert controller2.sent_prompts == []
    assert reloaded.state.has_uncertain_send()
    assert retry2.state.retry_count == 0
    assert any(event is Event.SEND_UNCERTAIN for event, _ in notifier2.sent)


# ---------------------------------------------------------------------------
# 5. Restart with UNCERTAIN: per-window semantics.
# ---------------------------------------------------------------------------


def test_restart_with_uncertain_same_window_never_resends(cfg, store):
    store.state.begin_pending_send(_restored().reset_id, NOW)
    store.state.mark_pending_uncertain()
    store.save()

    store2 = _reload(store.path)
    assert store2.state.has_uncertain_send()

    daemon, controller, notifier, retry = _make(cfg, store2)
    drive(daemon, 5)

    assert controller.sent_prompts == []
    assert controller.send_attempts == 0
    assert store2.state.has_uncertain_send(), "the marker must not be cleared for the same window"
    assert retry.state.retry_count == 0


def test_uncertain_from_old_window_does_not_block_a_new_window(cfg, store):
    # An UNCERTAIN marker belongs to the window it was created in.
    store.state.begin_pending_send(_restored().reset_id, NOW)
    store.state.mark_pending_uncertain()
    store.save()

    store2 = _reload(store.path)

    # Five hours later: a genuinely new quota window.
    later = NOW + timedelta(hours=5, minutes=1)
    daemon, controller, _notifier, _retry = _make(cfg, store2, script=_pair(later))
    drive(daemon, 2)

    assert len(controller.sent_prompts) == 1, "a new window is a fresh chance"
    assert store2.state.pending_send_status == PendingSend.CONFIRMED
    assert store2.state.has_triggered(_restored(later).reset_id)


# ---------------------------------------------------------------------------
# Supporting behaviour around the transaction.
# ---------------------------------------------------------------------------


def test_unconfirmed_send_is_uncertain_and_never_retried(cfg, store):
    """POST_SEND_VERIFY found no positive evidence: mark UNCERTAIN, stop."""
    controller = FakeChatGptController(cfg)
    controller.confirmation = (False, "fake: no positive confirmation")
    daemon, controller, notifier, retry = _make(cfg, store, controller)

    drive(daemon, 5)

    assert len(controller.sent_prompts) == 1, "sent once, then never again"
    assert store.state.has_uncertain_send()
    assert store.state.pending_send_reset_id == _restored().reset_id
    assert retry.state.retry_count == 0, "SEND_UNCERTAIN must not consume retries"
    assert any(event is Event.SEND_UNCERTAIN for event, _ in notifier.sent)


def test_send_failure_aborts_transaction_and_allows_retry(cfg, store):
    """A classified failure BEFORE any keystroke clears PREPARED: retry is OK."""
    controller = FakeChatGptController(cfg, fail_with=ErrorKind.SEND_FAILED)
    daemon, controller, _notifier, retry = _make(cfg, store, controller)

    drive(daemon, 3)

    assert controller.sent_prompts == []
    assert store.state.pending_send_status == PendingSend.NONE, (
        "a failed send must not leave a pending transaction behind"
    )
    assert retry.state.retry_count > 0, "ordinary send failures still use the retry budget"


def test_focus_unverified_neither_sends_nor_consumes_retries(cfg, store):
    """FOCUS_UNVERIFIED: refuse to type, park the window, keep retry budget."""
    controller = FakeChatGptController(cfg, fail_with=ErrorKind.FOCUS_UNVERIFIED)
    daemon, controller, _notifier, retry = _make(cfg, store, controller)

    drive(daemon, 4)

    assert controller.sent_prompts == []
    assert store.state.pending_send_status == PendingSend.NONE
    assert retry.state.retry_count == 0
    assert daemon.sm.state in (State.COOLDOWN, State.WORKING)
