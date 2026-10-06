"""Offline scenario isolation + interrupted-state sanitisation.

A fake-provider run must never write into the production state file, and a
persisted RESUMING state (interrupted resume) must never survive a restart.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from app.config import AppConfig, save_config
from app.state_machine import State
from app.storage.state_store import StateStore


class _TmpPathsConfig(AppConfig):
    """AppConfig whose data directory lives in tmp_path."""

    @property
    def state_path(self) -> Path:  # type: ignore[override]
        return self._tmp / "state.json"  # type: ignore[attr-defined]

    @property
    def log_dir(self) -> Path:  # type: ignore[override]
        return self._tmp / "logs"  # type: ignore[attr-defined]


@pytest.fixture
def cfg(tmp_path) -> AppConfig:
    c = _TmpPathsConfig()
    c._tmp = tmp_path  # type: ignore[attr-defined]
    c.usage.provider = "fake"
    c.usage.fallback_providers = []
    c.dry_run = True
    return c


def test_fake_once_uses_isolated_state_file(cfg, tmp_path):
    from app import main as app_main

    args = Namespace(
        command="once", config=str(tmp_path / "config.yaml"), provider="fake",
        chatgpt="fake", dry_run=None, ticks=2, log_level="ERROR",
        state=None, label=None, save=False,
    )
    app_main.cmd_once(cfg, args)

    # The production-style state file must NOT exist; the isolated one must.
    assert not (tmp_path / "state.json").exists()
    assert (tmp_path / "state-scenario.json").exists()


def test_explicit_state_override_is_respected(cfg, tmp_path):
    from app import main as app_main

    custom = tmp_path / "custom-state.json"
    args = Namespace(
        command="once", config=str(tmp_path / "config.yaml"), provider="fake",
        chatgpt="fake", dry_run=None, ticks=1, log_level="ERROR",
        state=str(custom), label=None, save=False,
    )
    app_main.cmd_once(cfg, args)
    assert custom.exists()
    assert not (tmp_path / "state-scenario.json").exists()


def test_resuming_state_is_sanitised_on_restart(cfg, tmp_path):
    state_path = tmp_path / "state.json"
    store = StateStore(state_path)
    store.state.program_state = State.RESUMING.value
    store.save()

    from app.main import build_daemon

    daemon = build_daemon(cfg, controller_name="fake", store=store)
    assert daemon.sm.state is State.STARTING, (
        "a persisted RESUMING state must restart the evaluation from STARTING"
    )
