"""Persistent state.

Durability matters more than speed here: the whole point is that a crash or a
reboot must not cause a duplicate resume. Writes are therefore atomic
(temp file in the same directory + ``os.replace`` + ``fsync``), and the file
carries a ``schema_version`` so a future format change can migrate instead of
silently mis-reading.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.models import UsageSnapshot
from app.utils.logging_setup import get_logger

log = get_logger("storage")

SCHEMA_VERSION = 1

#: Keep a bounded history of triggered reset ids as a second line of defence
#: in case the single `last_triggered_reset_id` field is ever clobbered.
MAX_TRIGGERED_HISTORY = 50


@dataclass
class State:
    """Everything the daemon must remember across restarts."""

    schema_version: int = SCHEMA_VERSION

    # --- duplicate protection (the highest-priority requirement) ----------
    last_seen_reset_id: str = ""
    last_triggered_reset_id: str = ""
    triggered_reset_ids: list[str] = field(default_factory=list)
    last_resume_time: str | None = None

    # --- retry bookkeeping -------------------------------------------------
    retry_count: int = 0
    retry_reset_id: str = ""
    next_retry_at: str | None = None

    # --- program state -----------------------------------------------------
    program_state: str = "STARTING"
    last_state_change: str | None = None

    # --- last observed usage ----------------------------------------------
    last_usage_snapshot: dict[str, Any] | None = None

    # --- misc telemetry ----------------------------------------------------
    started_at: str | None = None
    restart_count: int = 0
    resumed_count: int = 0

    # Non-persisted guards -------------------------------------------------
    _attempted_this_process: bool = field(default=False, compare=False)

    # -- helpers -----------------------------------------------------------

    def has_triggered(self, reset_id: str) -> bool:
        if not reset_id:
            return False
        return reset_id == self.last_triggered_reset_id or reset_id in self.triggered_reset_ids

    def mark_triggered(self, reset_id: str, when: datetime) -> None:
        self.last_triggered_reset_id = reset_id
        self.last_resume_time = when.isoformat()
        if reset_id and reset_id not in self.triggered_reset_ids:
            self.triggered_reset_ids.append(reset_id)
        if len(self.triggered_reset_ids) > MAX_TRIGGERED_HISTORY:
            self.triggered_reset_ids = self.triggered_reset_ids[-MAX_TRIGGERED_HISTORY:]

    def reset_retry(self) -> None:
        self.retry_count = 0
        self.retry_reset_id = ""
        self.next_retry_at = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "last_seen_reset_id": self.last_seen_reset_id,
            "last_triggered_reset_id": self.last_triggered_reset_id,
            "triggered_reset_ids": list(self.triggered_reset_ids),
            "last_resume_time": self.last_resume_time,
            "retry_count": self.retry_count,
            "retry_reset_id": self.retry_reset_id,
            "next_retry_at": self.next_retry_at,
            "program_state": self.program_state,
            "last_state_change": self.last_state_change,
            "last_usage_snapshot": self.last_usage_snapshot,
            "started_at": self.started_at,
            "restart_count": self.restart_count,
            "resumed_count": self.resumed_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "State":
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        kwargs = {k: v for k, v in (d or {}).items() if k in known}
        state = cls(**kwargs)
        if not isinstance(state.triggered_reset_ids, list):
            state.triggered_reset_ids = []
        # A stale/incompatible schema is tolerated: unknown fields are dropped
        # and missing fields take defaults.
        state.schema_version = SCHEMA_VERSION
        return state


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._state = State()

    # -- public API --------------------------------------------------------

    def load(self) -> State:
        if not self.path.exists():
            log.info("no state file yet, starting fresh: %s", self.path)
            return self._state

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Do not delete the corrupt file - it is evidence.
            backup = self.path.with_suffix(".json.corrupt")
            try:
                self.path.replace(backup)
                log.error("state file unreadable (%s); moved to %s", exc, backup)
            except OSError:
                log.error("state file unreadable (%s); could not back it up", exc)
            self._state = State()
            return self._state

        self._state = State.from_dict(raw)
        log.info(
            "state loaded: program_state=%s last_triggered_reset_id=%s retry_count=%d",
            self._state.program_state,
            self._state.last_triggered_reset_id or "(none)",
            self._state.retry_count,
        )
        return self._state

    @property
    def state(self) -> State:
        return self._state

    def save(self, state: State | None = None) -> None:
        if state is not None:
            self._state = state
        payload = json.dumps(self._state.to_dict(), indent=2, ensure_ascii=False)
        self._atomic_write(payload)

    def update_snapshot(self, snapshot: UsageSnapshot) -> None:
        self._state.last_usage_snapshot = snapshot.to_dict()
        self._state.last_seen_reset_id = snapshot.reset_id or self._state.last_seen_reset_id

    # -- internals ---------------------------------------------------------

    def _atomic_write(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".state-", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
