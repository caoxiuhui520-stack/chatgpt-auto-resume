"""Shared fixtures.

Tests drive the real :class:`~app.daemon.Daemon` with fake providers and a
fake ChatGPT controller, so the pipeline under test is the production one -
only the I/O edges are replaced.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.chatgpt.fake import FakeChatGptController
from app.config import AppConfig
from app.daemon import Daemon
from app.models import UsageSnapshot
from app.notification.base import NullNotifier
from app.resume.duplicate_guard import DuplicateGuard
from app.resume.resume_manager import ResumeManager
from app.resume.retry_manager import RetryManager
from app.runtime.health import HealthMonitor
from app.state_machine import State, StateMachine
from app.storage.state_store import StateStore
from app.usage.fake import FakeUsageProvider


class StubPromptConfig(AppConfig):
    """AppConfig with a deterministic in-memory prompt (no file needed)."""

    def read_prompt(self) -> str:  # type: ignore[override]
        return "CONTINUE_TEST_PROMPT"


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    c = AppConfig()
    c.dry_run = False
    c.poll_interval_seconds = 30
    c.resume.cooldown_minutes = 30
    c.resume.max_retries = 5
    c.resume.retry_backoff_seconds = [0, 30, 120, 300, 900]
    c.chatgpt.auto_start = True
    c.notifications.windows = False
    return c


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.json")


def make_snapshot(
    *,
    five_hour_used: float,
    weekly_used: float = 30.0,
    reset_in_minutes: float = 300,
    source: str = "fake",
    now: datetime | None = None,
) -> UsageSnapshot:
    now = now or datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)
    from app.usage.fake import _snapshot

    return _snapshot(
        five_hour_used=five_hour_used,
        weekly_used=weekly_used,
        five_hour_reset_in_minutes=reset_in_minutes,
        source=source,
        now=now,
    )


def make_daemon(
    cfg: AppConfig,
    store: StateStore,
    provider: FakeUsageProvider,
    controller: FakeChatGptController | None = None,
    *,
    initial_state: State = State.STARTING,
) -> tuple[Daemon, FakeChatGptController, NullNotifier, RetryManager]:
    controller = controller or FakeChatGptController(cfg)
    notifier = NullNotifier()
    sm = StateMachine(initial_state)
    guard = DuplicateGuard(store, cfg.resume.cooldown_minutes)
    retry = RetryManager(store, cfg.resume.max_retries, cfg.resume.retry_backoff_seconds)
    resume_manager = ResumeManager(cfg, store, controller, guard, retry, notifier, provider=provider)
    health = HealthMonitor(cfg.health.heartbeat_seconds, cfg.health.max_consecutive_failures)

    daemon = Daemon(
        cfg=cfg,
        provider=provider,
        controller=controller,
        store=store,
        notifier=notifier,
        state_machine=sm,
        guard=guard,
        retry=retry,
        resume_manager=resume_manager,
        health=health,
    )
    return daemon, controller, notifier, retry


def drive(daemon: Daemon, ticks: int) -> State:
    state = daemon.sm.state
    for _ in range(ticks):
        state = daemon.run_once()
    return state


@pytest.fixture
def fast_forward(monkeypatch):
    """Move the daemon's notion of 'now' forward on demand."""
    clock = {"now": datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)}

    def advance(minutes: float = 0, seconds: float = 0) -> datetime:
        clock["now"] += timedelta(minutes=minutes, seconds=seconds)
        return clock["now"]

    return clock, advance
