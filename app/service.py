"""Application service - the single façade between the GUI and the daemon.

The GUI talks only to this object. It owns the daemon, the discovery provider,
the config and the test-send store, and exposes:

* ``poll_once()``  - advance the daemon one tick and return an :class:`AppStatus`
* ``snapshot()``   - read-only status without stepping the daemon
* ``set_target`` / ``save_prompt`` / ``apply`` - configuration changes
* ``run_test_send`` - the supervised test send (blocking; call off the UI thread)

Nothing here imports Qt, so it stays unit-testable and the GUI is a thin shell.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import AppConfig, load_config, save_config
from app.discovery.models import ConversationInfo
from app.discovery.provider import LocalChatGPTDiscoveryProvider
from app.resume.test_send import TestSendResult, TestSendStore, run_test_send
from app.status import AppStatus, UsageView
from app.target import TargetResolver
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.daemon import Daemon

log = get_logger("service")


class AppService:
    """Owns the runtime objects and mediates every GUI action."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = config_path
        self.cfg: AppConfig = load_config(config_path)
        self.daemon: "Daemon | None" = None
        self.discovery = LocalChatGPTDiscoveryProvider()
        self.test_store = TestSendStore(Path(self.cfg.data_dir) / "test_send.json")
        self._started_at: float | None = None
        self._last_snapshot: AppStatus | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Build the daemon (spawns the provider child, off the UI thread)."""
        if self.daemon is not None:
            return
        from app.main import build_daemon

        self.cfg = load_config(self.config_path)  # reload in case config changed
        self.daemon = build_daemon(self.cfg)
        self.daemon.store.load()
        self._started_at = time.monotonic()
        log.info("service started (dry_run=%s)", self.cfg.dry_run)

    def stop(self) -> None:
        if self.daemon is not None:
            self.daemon.close()
            self.daemon = None
        self._started_at = None

    @property
    def running(self) -> bool:
        return self.daemon is not None

    # -- polling -----------------------------------------------------------

    def poll_once(self) -> AppStatus:
        if self.daemon is None:
            self.start()
        assert self.daemon is not None
        try:
            self.daemon.run_once()
        except Exception as exc:  # noqa: BLE001 - a tick must not kill the service
            log.exception("poll tick failed")
        return self.snapshot()

    def next_interval(self) -> float:
        if self.daemon is None:
            return float(self.cfg.poll_interval_seconds)
        return self.daemon.next_interval()

    # -- status ------------------------------------------------------------

    def snapshot(self) -> AppStatus:
        cfg = self.cfg
        status = AppStatus()
        status.daemon_running = self.running
        status.uptime_seconds = time.monotonic() - self._started_at if self._started_at else 0.0

        # send mode / arming
        from app.main import assess_send_mode

        try:
            mode, reason = assess_send_mode(cfg)
        except SystemExit as exc:
            mode, reason = "monitor", str(exc)
        status.real_send_armed = mode == "armed"
        status.send_mode = "armed" if mode == "armed" else ("dry_run" if not cfg.dry_run else "monitor")
        status.dry_run = cfg.dry_run
        status.armed_reason = reason

        if self.daemon is not None:
            status.state = self.daemon.sm.state.value
            state = self.daemon.store.state
            status.pending_transaction = state.pending_send_status
            status.last_resume_time = state.last_resume_time
            status.resumed_count = state.resumed_count
            status.started_at = state.started_at
            raw = state.last_usage_snapshot or {}
            status.usage = UsageView(
                five_hour_remaining_percent=raw.get("five_hour_remaining_percent"),
                weekly_remaining_percent=raw.get("weekly_remaining_percent"),
                five_hour_reset_at=raw.get("five_hour_reset_at"),
                weekly_reset_at=raw.get("weekly_reset_at"),
                available=bool(raw.get("available")),
                source=raw.get("source", ""),
                error=raw.get("error"),
            )
            status.provider_status = "ok" if status.usage and not status.usage.error else "error"
            # live ChatGPT state (cheap, cached controller)
            try:
                status.chatgpt_running = self.daemon.controller.is_running()
            except Exception:  # noqa: BLE001
                status.chatgpt_running = None
            try:
                status.active_conversation_title = self.daemon.controller.conversation_title()
            except Exception:  # noqa: BLE001
                status.active_conversation_title = ""

        # discovery
        status.discovery_available = self.discovery.available
        status.conversation_count = len(self.discovery.list_conversations())
        try:
            current = self.discovery.get_current_conversation()
            status.current_conversation_id = current.id if current else ""
        except Exception:  # noqa: BLE001
            current = None

        # target
        target = cfg.target
        status.target_configured = bool(target.conversation_id or target.conversation_title.strip())
        status.target = {
            "project_id": target.project_id,
            "project_name": target.project_name,
            "conversation_id": target.conversation_id,
            "conversation_title": target.conversation_title,
        }
        if status.target_configured:
            try:
                match = TargetResolver().resolve(
                    target,
                    status.current_conversation_id,
                    status.active_conversation_title,
                    self.discovery,
                )
                status.target_match = match.to_dict()
            except Exception:  # noqa: BLE001
                status.target_match = None

        # test send
        status.test_send_passed = self.test_store.passed
        status.test_send_last = {
            "status": self.test_store.record.get("status"),
            "conversation_id": self.test_store.record.get("conversation_id"),
            "channel": self.test_store.record.get("channel"),
            "confirmation": self.test_store.record.get("confirmation"),
            "timestamp": self.test_store.record.get("timestamp"),
        }

        self._last_snapshot = status
        return status

    # -- configuration -----------------------------------------------------

    def _persist(self) -> None:
        save_config(self.cfg, self.config_path or getattr(self.cfg, "_source_path", None))

    def set_target(
        self,
        conversation_id: str = "",
        conversation_title: str = "",
        project_id: str = "",
        project_name: str = "",
    ) -> None:
        self.cfg.target.conversation_id = (conversation_id or "").strip()
        self.cfg.target.conversation_title = (conversation_title or "").strip()
        self.cfg.target.project_id = (project_id or "").strip()
        self.cfg.target.project_name = (project_name or "").strip()
        # Choosing a target implies the task lock is wanted.
        self.cfg.task_lock.enabled = True
        self._persist()

    def clear_target(self) -> None:
        self.cfg.target = type(self.cfg.target)()
        self.cfg.task_lock.enabled = False
        self._persist()

    def set_prompt(self, text: str) -> None:
        path = self.cfg.prompt_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        import os
        import tempfile

        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".prompt-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def prompt_text(self) -> str:
        return self.cfg.read_prompt()

    def apply(self, **changes: object) -> None:
        """Apply simple scalar/nested config changes and persist atomically."""
        for dotted, value in changes.items():
            obj = self.cfg
            parts = dotted.split(".")
            for part in parts[:-1]:
                obj = getattr(obj, part)
            setattr(obj, parts[-1], value)
        self._persist()

    def arm(self, armed: bool) -> None:
        self.cfg.real_send.armed = bool(armed)
        if armed:
            self.cfg.dry_run = False
        self._persist()

    def set_dry_run(self, dry_run: bool) -> None:
        self.cfg.dry_run = bool(dry_run)
        if dry_run:
            self.cfg.real_send.armed = False
        self._persist()

    # -- test send ---------------------------------------------------------

    def run_test_send(self) -> TestSendResult:
        if self.daemon is None:
            self.start()
        assert self.daemon is not None
        return run_test_send(
            self.daemon.controller, self.cfg, self.discovery, self.test_store
        )
