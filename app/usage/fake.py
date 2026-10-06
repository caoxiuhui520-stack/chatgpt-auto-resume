"""Test / offline providers.

Deliberately part of the shipped package rather than the test folder: running
the daemon against a scripted provider is the recommended way to exercise the
full pipeline end to end without waiting hours for a real quota reset.

    python -m app.main --provider fake --scenario exhausted_then_restored
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, TYPE_CHECKING

from app.models import UsageSnapshot
from app.usage.base import UsageProvider, register
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig

log = get_logger("usage.fake")


def _snapshot(
    *,
    five_hour_used: float,
    weekly_used: float,
    five_hour_reset_in_minutes: float = 300,
    weekly_reset_in_days: float = 7,
    source: str = "fake",
    plan: str = "plus",
    now: datetime | None = None,
) -> UsageSnapshot:
    now = now or datetime.now(timezone.utc)
    five_reset = now + timedelta(minutes=five_hour_reset_in_minutes)
    week_reset = now + timedelta(days=weekly_reset_in_days)
    available = five_hour_used < 100
    snap = UsageSnapshot(
        available=available,
        five_hour_remaining_percent=max(0.0, 100.0 - five_hour_used),
        weekly_remaining_percent=max(0.0, 100.0 - weekly_used),
        five_hour_reset_at=five_reset,
        weekly_reset_at=week_reset,
        source=source,
        timestamp=now,
        plan_type=plan,
        reached_type=None if available else "rate_limit_reached",
    )
    snap.reset_id = snap.compute_reset_id()
    return snap


class FakeUsageProvider(UsageProvider):
    """Programmable provider.

    Set :attr:`snapshot` directly, or install a callable with
    :meth:`set_script` to vary the answer per call.
    """

    name = "fake"
    description = "in-memory provider for tests and offline runs"

    def __init__(self, cfg: "AppConfig | None" = None) -> None:
        self.calls = 0
        self.snapshot: UsageSnapshot = _snapshot(five_hour_used=42.0, weekly_used=10.0)
        self._script: list[UsageSnapshot] | None = None
        self._fn: Callable[[int], UsageSnapshot] | None = None

    # -- configuration helpers --------------------------------------------

    def set_snapshot(self, snapshot: UsageSnapshot) -> None:
        self.snapshot = snapshot
        self._script = None
        self._fn = None

    def set_script(self, snapshots: list[UsageSnapshot], *, repeat_last: bool = True) -> None:
        self._script = list(snapshots)
        self._repeat_last = repeat_last
        self._fn = None

    def set_callable(self, fn: Callable[[int], UsageSnapshot]) -> None:
        self._fn = fn
        self._script = None

    # -- UsageProvider -----------------------------------------------------

    def get_usage(self) -> UsageSnapshot:
        self.calls += 1
        if self._fn is not None:
            return self._fn(self.calls)
        if self._script:
            if len(self._script) > 1:
                return self._script.pop(0)
            return self._script[0]
        return self.snapshot


@register("fake")
def _build_fake(cfg: "AppConfig") -> UsageProvider:  # noqa: ARG001
    import os

    provider = FakeUsageProvider()
    scenario = os.environ.get("AUTO_RESUME_SCENARIO", "")
    if scenario:
        provider.set_script(SCENARIOS[scenario]())
        log.info("fake provider loaded scenario %r (%d steps)", scenario, len(provider._script or []))
    return provider


# ---------------------------------------------------------------------------
# Scenarios - each returns a list of snapshots consumed one per poll.
# ---------------------------------------------------------------------------

def scenario_healthy() -> list[UsageSnapshot]:
    return [_snapshot(five_hour_used=20.0, weekly_used=5.0)]


def scenario_exhausted_then_restored() -> list[UsageSnapshot]:
    """Typical real-world flow: working -> exhausted -> reset -> restored."""
    now = datetime.now(timezone.utc)
    exhausted = _snapshot(five_hour_used=100.0, weekly_used=40.0, now=now)
    still_exhausted = _snapshot(five_hour_used=100.0, weekly_used=40.0, now=now)
    restored = _snapshot(
        five_hour_used=0.0,
        weekly_used=40.0,
        five_hour_reset_in_minutes=300,
        now=now + timedelta(minutes=5),
    )
    return [exhausted, still_exhausted, exhausted, restored]


def scenario_flapping() -> list[UsageSnapshot]:
    """100 -> 99 -> 100 inside the same window: must NOT be treated as a reset."""
    now = datetime.now(timezone.utc)
    a = _snapshot(five_hour_used=100.0, weekly_used=30.0, now=now)
    b = _snapshot(five_hour_used=99.0, weekly_used=30.0, now=now)  # same resetsAt
    c = _snapshot(five_hour_used=100.0, weekly_used=30.0, now=now)
    return [a, b, c]


def scenario_same_reset_id_twice() -> list[UsageSnapshot]:
    """The same new window appears twice - the second must be ignored."""
    now = datetime.now(timezone.utc)
    exhausted = _snapshot(five_hour_used=100.0, weekly_used=30.0, now=now)
    restored = _snapshot(
        five_hour_used=0.0, weekly_used=30.0, now=now + timedelta(minutes=5)
    )
    return [exhausted, restored, restored, restored]


SCENARIOS: dict[str, Callable[[], list[UsageSnapshot]]] = {
    "healthy": scenario_healthy,
    "exhausted_then_restored": scenario_exhausted_then_restored,
    "flapping": scenario_flapping,
    "same_reset_id_twice": scenario_same_reset_id_twice,
}
