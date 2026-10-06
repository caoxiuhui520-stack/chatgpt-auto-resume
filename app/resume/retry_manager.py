"""Retry policy for resume attempts.

The brief requires the error classes to be handled differently, otherwise the
backoff misbehaves:

* **Waitable** errors (ChatGPT busy / not running / window not found / task
  lock mismatch) do NOT consume a retry slot. They mean "the world is not
  ready", so the daemon returns to ``READY_TO_RESUME`` and simply polls again.
  Counting these would exhaust five retries in two minutes just because the
  user's machine was busy.
* **Hard** errors (send failed, composer not found, provider read failed) do
  consume a slot and follow the documented schedule:
  0s -> 30s -> 2m -> 5m -> 15m -> stop and notify.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models import ErrorKind, WAITABLE_ERRORS, utcnow
from app.storage.state_store import State, StateStore
from app.utils.logging_setup import get_logger

log = get_logger("retry")

DEFAULT_BACKOFF = (0, 30, 120, 300, 900)


@dataclass(slots=True)
class RetryDecision:
    kind: str = "retry"  # "retry" | "wait" | "give_up"
    delay_seconds: float = 0.0
    reason: str = ""

    @property
    def give_up(self) -> bool:
        return self.kind == "give_up"


class RetryManager:
    def __init__(
        self,
        store: StateStore,
        max_retries: int = 5,
        backoff_seconds: list[int] | None = None,
        reset_id: str = "",
    ) -> None:
        self.store = store
        self.max_retries = max(1, max_retries)
        self.backoff = list(backoff_seconds or DEFAULT_BACKOFF)
        self.reset_id = reset_id

    @property
    def state(self) -> State:
        return self.store.state

    def start_window(self, reset_id: str) -> None:
        """Begin (or adopt) retry bookkeeping for a quota window."""
        if self.reset_id != reset_id or self.state.retry_reset_id != reset_id:
            self.reset_id = reset_id
            self.state.retry_reset_id = reset_id
            self.state.retry_count = 0
            self.state.next_retry_at = None
            self.store.save()

    def reset(self) -> None:
        self.state.reset_retry()
        self.store.save()

    def delay_for(self, attempt: int) -> float:
        """Delay before retry number ``attempt`` (0-based)."""
        if not self.backoff:
            return 0.0
        if attempt < len(self.backoff):
            return float(self.backoff[attempt])
        return float(self.backoff[-1])

    def record_failure(self, error_kind: str, now: datetime | None = None) -> RetryDecision:
        now = now or utcnow()

        if error_kind in WAITABLE_ERRORS:
            log.info(
                "waitable failure (%s); staying in READY_TO_RESUME, no retry consumed",
                error_kind,
            )
            return RetryDecision("wait", 0.0, error_kind)

        self.state.retry_count += 1
        attempt = self.state.retry_count - 1

        if self.state.retry_count > self.max_retries:
            log.error(
                "max retries (%d) exceeded for reset_id=%s; giving up and notifying",
                self.max_retries,
                self.reset_id or "(none)",
            )
            self.store.save()
            return RetryDecision("give_up", 0.0, f"max_retries reached ({error_kind})")

        delay = self.delay_for(attempt)
        self.state.next_retry_at = (now + timedelta(seconds=delay)).isoformat()
        self.store.save()
        log.warning(
            "resume failure %s: retry %d/%d in %.0fs",
            error_kind,
            self.state.retry_count,
            self.max_retries,
            delay,
        )
        return RetryDecision("retry", delay, error_kind)

    def ready_to_retry(self, now: datetime | None = None) -> bool:
        raw = self.state.next_retry_at
        if not raw:
            return True
        now = now or utcnow()
        try:
            target = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if target.tzinfo is None:
            target = target.replace(tzinfo=now.tzinfo)
        return now >= target

    def retries_exhausted(self) -> bool:
        return self.state.retry_count > self.max_retries

    def describe(self) -> str:
        return (
            f"retry_count={self.state.retry_count}/{self.max_retries} "
            f"reset_id={self.reset_id or '(none)'}"
        )


__all__ = ["RetryManager", "RetryDecision", "ErrorKind", "DEFAULT_BACKOFF"]
