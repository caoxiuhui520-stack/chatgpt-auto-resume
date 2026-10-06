"""End-to-end daemon behaviour with fakes.

The headline requirement of the whole project lives in this file:

    Any situation, the same quota-restoration cycle must never send twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import ErrorKind
from app.state_machine import State
from tests.conftest import drive, make_daemon, make_snapshot


def test_happy_path_sends_exactly_once(cfg, store):
    provider_script = [
        make_snapshot(five_hour_used=100.0),   # exhausted
        make_snapshot(five_hour_used=100.0),   # still exhausted
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605),  # restored, new window
        make_snapshot(five_hour_used=1.0, reset_in_minutes=605),  # same window, later poll
        make_snapshot(five_hour_used=2.0, reset_in_minutes=605),  # same window again
    ]
    from app.usage.fake import FakeUsageProvider

    provider = FakeUsageProvider(cfg)
    provider.set_script(provider_script)

    daemon, controller, _notifier, _retry = make_daemon(cfg, store, provider)
    drive(daemon, len(provider_script))

    assert len(controller.sent_prompts) == 1, "the prompt must be sent exactly once"
    assert store.state.last_triggered_reset_id


def test_flapping_availability_within_one_window_never_sends(cfg, store):
    """100 -> 99 -> 100 inside the same window is not a restoration."""
    from app.usage.fake import FakeUsageProvider

    now = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)
    same_window = [
        make_snapshot(five_hour_used=100.0, reset_in_minutes=300, now=now),
        make_snapshot(five_hour_used=99.0, reset_in_minutes=300, now=now),
        make_snapshot(five_hour_used=100.0, reset_in_minutes=300, now=now),
        make_snapshot(five_hour_used=98.0, reset_in_minutes=300, now=now),
    ]
    provider = FakeUsageProvider(cfg)
    provider.set_script(same_window)

    daemon, controller, _n, _r = make_daemon(cfg, store, provider)
    drive(daemon, len(same_window))

    assert controller.sent_prompts == [], "availability flapping must not trigger a resume"


def test_restart_after_send_does_not_resend(cfg, store, tmp_path):
    """The reboot scenario: state on disk must suppress a second send."""
    from app.usage.fake import FakeUsageProvider

    first = [
        make_snapshot(five_hour_used=100.0),
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605),
    ]
    provider = FakeUsageProvider(cfg)
    provider.set_script(first)
    daemon, controller, _n, _r = make_daemon(cfg, store, provider)
    drive(daemon, len(first))
    assert len(controller.sent_prompts) == 1
    triggered = store.state.last_triggered_reset_id

    # Simulate a reboot: brand new objects, same state file.
    from app.storage.state_store import StateStore

    store2 = StateStore(store.path)
    store2.load()
    assert store2.state.last_triggered_reset_id == triggered

    provider2 = FakeUsageProvider(cfg)
    provider2.set_script([first[-1], first[-1], first[-1]])
    daemon2, controller2, _n2, _r2 = make_daemon(cfg, store2, provider2, initial_state=State.WORKING)
    drive(daemon2, 3)
    assert controller2.sent_prompts == [], "a restart must not cause a duplicate send"


def test_busy_chatgpt_defers_without_consuming_retries(cfg, store):
    from app.chatgpt.fake import FakeChatGptController
    from app.usage.fake import FakeUsageProvider

    provider = FakeUsageProvider(cfg)
    provider.set_script([
        make_snapshot(five_hour_used=100.0),
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605),
    ])
    controller = FakeChatGptController(cfg, busy=True)
    daemon, controller, _n, retry = make_daemon(cfg, store, provider, controller)
    drive(daemon, 2)

    assert controller.sent_prompts == []
    assert retry.state.retry_count == 0, "busy is a waitable condition"
    assert daemon.sm.state in (State.READY_TO_RESUME, State.WAITING_RESET, State.QUOTA_EXHAUSTED)


def test_busy_then_idle_sends_once(cfg, store):
    from app.chatgpt.fake import FakeChatGptController
    from app.usage.fake import FakeUsageProvider

    provider = FakeUsageProvider(cfg)
    provider.set_script([
        make_snapshot(five_hour_used=100.0),
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605),
    ])
    controller = FakeChatGptController(cfg, busy=True)
    daemon, controller, _n, _r = make_daemon(cfg, store, provider, controller)

    drive(daemon, 2)
    assert controller.sent_prompts == []

    controller.busy = False
    drive(daemon, 1)
    assert len(controller.sent_prompts) == 1


def test_chatgpt_not_running_triggers_autostart(cfg, store):
    from app.chatgpt.fake import FakeChatGptController
    from app.usage.fake import FakeUsageProvider

    provider = FakeUsageProvider(cfg)
    provider.set_script([
        make_snapshot(five_hour_used=100.0),
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605),
    ])
    controller = FakeChatGptController(cfg, running=False, window_present=False)
    daemon, controller, _n, _r = make_daemon(cfg, store, provider, controller)
    drive(daemon, 2)

    assert controller.start_calls == 1, "auto_start must attempt a launch"
    assert len(controller.sent_prompts) == 1


def test_autostart_disabled_means_no_launch(cfg, store):
    from app.chatgpt.fake import FakeChatGptController
    from app.usage.fake import FakeUsageProvider

    cfg.chatgpt.auto_start = False
    provider = FakeUsageProvider(cfg)
    provider.set_script([
        make_snapshot(five_hour_used=100.0),
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605),
    ])
    controller = FakeChatGptController(cfg, running=False, window_present=False)
    daemon, controller, _n, _r = make_daemon(cfg, store, provider, controller)
    drive(daemon, 2)

    assert controller.start_calls == 0
    assert controller.sent_prompts == []


def test_send_failure_retries_then_gives_up(cfg, store):
    from app.chatgpt.fake import FakeChatGptController
    from app.usage.fake import FakeUsageProvider

    cfg.dry_run = False
    cfg.resume.max_retries = 3
    provider = FakeUsageProvider(cfg)
    provider.set_script([
        make_snapshot(five_hour_used=100.0),
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605),
    ])
    controller = FakeChatGptController(cfg, fail_with=ErrorKind.SEND_FAILED)
    daemon, controller, notifier, retry = make_daemon(cfg, store, provider, controller)
    drive(daemon, 6)

    assert controller.sent_prompts == []
    assert retry.retries_exhausted()
    events = {event for event, _ in notifier.sent}
    assert any(e.value == "max_retries" for e in events)


def test_dry_run_never_types(cfg, store):
    from app.chatgpt.fake import FakeChatGptController
    from app.usage.fake import FakeUsageProvider

    cfg.dry_run = True
    provider = FakeUsageProvider(cfg)
    provider.set_script([
        make_snapshot(five_hour_used=100.0),
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605),
    ])
    controller = FakeChatGptController(cfg)
    daemon, controller, _n, _r = make_daemon(cfg, store, provider, controller)
    drive(daemon, 2)

    assert controller.sent_prompts == []
    # A dry run must not mark the window as triggered, otherwise the real
    # reset would be swallowed.
    assert not store.state.has_triggered(provider._script[0].reset_id if provider._script else "")


def test_task_lock_blocks_wrong_conversation(cfg, store):
    from app.chatgpt.fake import FakeChatGptController
    from app.usage.fake import FakeUsageProvider

    cfg.task_lock.enabled = True
    cfg.task_lock.project = "huanyu"
    provider = FakeUsageProvider(cfg)
    provider.set_script([
        make_snapshot(five_hour_used=100.0),
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605),
    ])
    controller = FakeChatGptController(cfg, conversation="some-other-chat")
    daemon, controller, _n, _r = make_daemon(cfg, store, provider, controller)
    drive(daemon, 2)

    assert controller.sent_prompts == [], "the task lock must prevent a mis-targeted send"


def test_task_lock_allows_matching_conversation(cfg, store):
    from app.chatgpt.fake import FakeChatGptController
    from app.usage.fake import FakeUsageProvider

    cfg.task_lock.enabled = True
    cfg.task_lock.project = "huanyu"
    provider = FakeUsageProvider(cfg)
    provider.set_script([
        make_snapshot(five_hour_used=100.0),
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605),
    ])
    controller = FakeChatGptController(cfg, conversation="huanyu dashboard work")
    daemon, controller, _n, _r = make_daemon(cfg, store, provider, controller)
    drive(daemon, 2)

    assert len(controller.sent_prompts) == 1


def test_provider_failure_keeps_waiting_then_recovers(cfg, store):
    from app.chatgpt.fake import FakeChatGptController
    from app.usage.fake import FakeUsageProvider

    provider = FakeUsageProvider(cfg)
    provider.set_script([
        make_snapshot(five_hour_used=100.0),
        make_snapshot(five_hour_used=0.0, reset_in_minutes=605),
    ])
    controller = FakeChatGptController(cfg)
    daemon, controller, _n, _r = make_daemon(cfg, store, provider, controller)

    # Two provider errors in a row: state is preserved, no send.
    daemon.provider = _AlwaysFailingProvider()
    drive(daemon, 2)
    assert controller.sent_prompts == []

    daemon.provider = provider
    drive(daemon, 2)
    assert len(controller.sent_prompts) == 1


class _AlwaysFailingProvider:
    name = "broken"
    description = "always fails"

    def get_usage(self):
        from app.models import UsageSnapshot, utcnow

        return UsageSnapshot(source=self.name, error="boom", timestamp=utcnow(), stale=True)

    def close(self):
        pass

    def describe(self):
        return self.name
