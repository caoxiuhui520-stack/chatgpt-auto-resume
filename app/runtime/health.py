"""Long-run health monitoring.

A daemon that silently stops polling is worse than one that crashes loudly. Two
things are watched:

* **heartbeat** - a DEBUG line every N seconds proves the loop is still turning.
* **consecutive failures** - repeated provider or controller errors raise a
  notification so the user finds out without reading logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models import utcnow
from app.utils.logging_setup import get_logger

log = get_logger("health")


@dataclass
class HealthMonitor:
    heartbeat_seconds: int = 300
    max_consecutive_failures: int = 5

    last_heartbeat: datetime | None = None
    ticks: int = 0
    consecutive_provider_failures: int = 0
    consecutive_controller_failures: int = 0
    alerts_sent: list[str] = field(default_factory=list)

    # -- heartbeat ---------------------------------------------------------

    def tick(self, now: datetime | None = None) -> bool:
        """Called once per loop. Returns True when a heartbeat was emitted."""
        now = now or utcnow()
        self.ticks += 1
        if self.last_heartbeat is None:
            self.last_heartbeat = now
            return False
        if now - self.last_heartbeat >= timedelta(seconds=self.heartbeat_seconds):
            self.last_heartbeat = now
            log.debug(
                "daemon alive: ticks=%d, provider_failures=%d, controller_failures=%d",
                self.ticks,
                self.consecutive_provider_failures,
                self.consecutive_controller_failures,
            )
            return True
        return False

    # -- failure tracking --------------------------------------------------

    def record_provider_failure(self, detail: str = "") -> bool:
        """Returns True when the alert threshold was crossed."""
        self.consecutive_provider_failures += 1
        if self.consecutive_provider_failures == self.max_consecutive_failures:
            self.alerts_sent.append(f"provider:{detail}")
            log.error(
                "usage provider failed %d times in a row; raising an alert",
                self.consecutive_provider_failures,
            )
            return True
        return False

    def record_provider_success(self) -> None:
        if self.consecutive_provider_failures >= self.max_consecutive_failures:
            log.info("usage provider recovered")
        self.consecutive_provider_failures = 0

    def record_controller_failure(self, detail: str = "") -> bool:
        self.consecutive_controller_failures += 1
        if self.consecutive_controller_failures == self.max_consecutive_failures:
            self.alerts_sent.append(f"controller:{detail}")
            log.error(
                "ChatGPT controller failed %d times in a row; raising an alert",
                self.consecutive_controller_failures,
            )
            return True
        return False

    def record_controller_success(self) -> None:
        self.consecutive_controller_failures = 0

    def snapshot(self) -> dict[str, object]:
        return {
            "ticks": self.ticks,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "provider_failures": self.consecutive_provider_failures,
            "controller_failures": self.consecutive_controller_failures,
        }
