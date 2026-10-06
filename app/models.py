"""Unified data models.

Everything that crosses a module boundary speaks in these types. The usage
providers are responsible for translating whatever the local Codex surface
returns into a :class:`UsageSnapshot`; nothing else in the program is allowed
to know about raw JSON-RPC payloads.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# A "short" window is the rolling 5h quota window. Anything longer is the
# weekly window. Mirrors the convention used by the reference monitor
# implementations (duration > 12h => weekly).
SHORT_WINDOW_MAX_MINUTES = 12 * 60
FIVE_HOUR_WINDOW_MINUTES = 5 * 60
WEEKLY_WINDOW_MINUTES = 7 * 24 * 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def from_unix(ts: int | float | None) -> datetime | None:
    """Parse a unix *seconds* timestamp. Tolerates milliseconds by heuristic.

    Returns None for missing/sentinel values - never raises.
    """
    if ts is None:
        return None
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # Some surfaces return milliseconds.
    if value > 10_000_000_000:
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def normalize_to_minute(dt: datetime | None) -> datetime | None:
    """Truncate to the minute.

    ``resetsAt`` drifts by a second or two between polls for the *same*
    window. Truncating to the minute makes window identity stable.
    """
    if dt is None:
        return None
    return dt.replace(second=0, microsecond=0)


@dataclass(slots=True)
class UsageWindow:
    """A single rate-limit window (5h or weekly)."""

    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: datetime | None = None
    label: str = ""

    @property
    def remaining_percent(self) -> float | None:
        if self.used_percent is None:
            return None
        return max(0.0, min(100.0, 100.0 - self.used_percent))

    @property
    def is_short_window(self) -> bool:
        if self.window_minutes is None:
            return self.label == "five_hour"
        return self.window_minutes <= SHORT_WINDOW_MAX_MINUTES

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_percent": self.used_percent,
            "remaining_percent": self.remaining_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UsageWindow":
        resets = d.get("resets_at")
        return cls(
            used_percent=d.get("used_percent"),
            window_minutes=d.get("window_minutes"),
            resets_at=datetime.fromisoformat(resets) if resets else None,
            label=d.get("label", ""),
        )


@dataclass(slots=True)
class UsageSnapshot:
    """Provider-independent view of the current quota state.

    ``available`` is the single most important flag: it means "there is quota
    left to work with right now". A transition False -> True is what the state
    machine treats as a genuine quota reset.
    """

    available: bool = False
    five_hour_remaining_percent: float | None = None
    weekly_remaining_percent: float | None = None
    five_hour_reset_at: datetime | None = None
    weekly_reset_at: datetime | None = None
    reset_id: str = ""
    source: str = "unknown"
    timestamp: datetime = field(default_factory=utcnow)

    # Extra context, non-authoritative.
    plan_type: str | None = None
    reached_type: str | None = None
    error: str | None = None
    stale: bool = False
    raw: dict[str, Any] | None = None

    # -- derived helpers ---------------------------------------------------

    @property
    def five_hour_used_percent(self) -> float | None:
        if self.five_hour_remaining_percent is None:
            return None
        return max(0.0, 100.0 - self.five_hour_remaining_percent)

    @property
    def ok(self) -> bool:
        """True when the snapshot carries usable data (no read failure)."""
        return self.error is None and self.five_hour_remaining_percent is not None

    def seconds_until_five_hour_reset(self) -> float | None:
        if not self.five_hour_reset_at:
            return None
        return (self.five_hour_reset_at - utcnow()).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("five_hour_reset_at", "weekly_reset_at", "timestamp"):
            value = d.get(key)
            if isinstance(value, datetime):
                d[key] = value.isoformat()
        # Never persist the raw provider payload; it can contain identifiers.
        d.pop("raw", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UsageSnapshot":
        def _dt(key: str) -> datetime | None:
            v = d.get(key)
            return datetime.fromisoformat(v) if v else None

        return cls(
            available=bool(d.get("available", False)),
            five_hour_remaining_percent=d.get("five_hour_remaining_percent"),
            weekly_remaining_percent=d.get("weekly_remaining_percent"),
            five_hour_reset_at=_dt("five_hour_reset_at"),
            weekly_reset_at=_dt("weekly_reset_at"),
            reset_id=d.get("reset_id", "") or "",
            source=d.get("source", "unknown"),
            timestamp=_dt("timestamp") or utcnow(),
            plan_type=d.get("plan_type"),
            reached_type=d.get("reached_type"),
            error=d.get("error"),
            stale=bool(d.get("stale", False)),
        )

    @staticmethod
    def make_reset_id(source: str, window: str, resets_at: datetime | None) -> str:
        """Stable identity for one quota window.

        ``{source}:{window}:{unix_seconds}`` - the same shape the reference
        implementations use. Because ``resets_at`` is normalised to the minute,
        two polls of the same window produce the same id.
        """
        if resets_at is None:
            return ""
        normalized = normalize_to_minute(resets_at)
        assert normalized is not None
        return f"{source}:{window}:{int(normalized.timestamp())}"

    def compute_reset_id(self) -> str:
        return self.make_reset_id(self.source, "five_hour", self.five_hour_reset_at)

    def goal_hash(self, goal: str) -> str:
        return hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class ResumeResult:
    """Outcome of one resume attempt, consumed by the retry manager."""

    success: bool
    error: str | None = None
    detail: str = ""


class ErrorKind:
    """Error taxonomy. Different handling per class was an explicit design
    lesson from the reference projects - collapsing them all into one bucket
    makes retry/backoff behave badly."""

    PROVIDER_READ_FAILED = "provider_read_failed"
    CHATGPT_NOT_RUNNING = "chatgpt_not_running"
    WINDOW_NOT_FOUND = "window_not_found"
    INPUT_NOT_FOUND = "input_not_found"
    CHATGPT_BUSY = "chatgpt_busy"
    SEND_FAILED = "send_failed"
    TASK_LOCK_MISMATCH = "task_lock_mismatch"
    FOCUS_UNVERIFIED = "focus_unverified"
    SEND_UNCERTAIN = "send_uncertain"
    DRY_RUN = "dry_run"
    UNKNOWN = "unknown"


#: Errors that should never be answered with an immediate retry; they mean
#: "the world is not ready yet", so we go back to waiting instead.
WAITABLE_ERRORS = frozenset(
    {
        ErrorKind.CHATGPT_BUSY,
        ErrorKind.CHATGPT_NOT_RUNNING,
        ErrorKind.WINDOW_NOT_FOUND,
        ErrorKind.TASK_LOCK_MISMATCH,
    }
)

#: Errors after which a message may already be in the conversation. These
#: must NEVER trigger an automatic retry - resending is worse than missing.
UNCERTAIN_ERRORS = frozenset(
    {
        ErrorKind.SEND_UNCERTAIN,
        ErrorKind.FOCUS_UNVERIFIED,
    }
)
