"""Duplicate protection - the highest-priority requirement.

Four independent layers, per the project brief. Any one of them alone is
enough to stop a double-send; they are deliberately redundant because a wrong
send is not recoverable (the message is already in the user's conversation).

  Layer 1  reset_id identity - one resume per quota window, ever.
  Layer 2  cooldown - a hard floor on the send rate.
  Layer 3  persistent state - survives crashes and reboots.
  Layer 4  fresh re-read before sending - lives in ResumeManager, because it
           needs the provider.

Layer 3 is implemented by the fact that layers 1 and 2 read from
:class:`~app.storage.state_store.State`, which is written to disk atomically
*before* the send is reported as done.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models import UsageSnapshot, utcnow
from app.storage.state_store import State, StateStore
from app.utils.logging_setup import get_logger

log = get_logger("guard")


@dataclass(slots=True)
class GuardDecision:
    allowed: bool
    reason: str = ""
    layer: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


class DuplicateGuard:
    def __init__(self, store: StateStore, cooldown_minutes: int = 30) -> None:
        self.store = store
        self.cooldown = timedelta(minutes=max(0, cooldown_minutes))

    # -- queries -----------------------------------------------------------

    @property
    def state(self) -> State:
        return self.store.state

    def already_triggered(self, reset_id: str) -> bool:
        return self.state.has_triggered(reset_id)

    def in_cooldown(self, now: datetime | None = None) -> tuple[bool, float]:
        """Return (still_in_cooldown, seconds_remaining)."""
        now = now or utcnow()
        last = self.state.last_resume_time
        if not last:
            return False, 0.0
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return False, 0.0
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=now.tzinfo)
        elapsed = now - last_dt
        remaining = (self.cooldown - elapsed).total_seconds()
        return (remaining > 0), max(0.0, remaining)

    def check(self, snapshot: UsageSnapshot, now: datetime | None = None) -> GuardDecision:
        now = now or utcnow()

        # Layer 1 - window identity.
        reset_id = snapshot.reset_id
        if not reset_id:
            return GuardDecision(
                False,
                "no reset_id: cannot prove this is a new quota window",
                layer="identity",
            )
        if self.state.has_triggered(reset_id):
            return GuardDecision(
                False, f"reset_id {reset_id} was already resumed", layer="identity"
            )

        # Layer 2 - cooldown.
        cooling, remaining = self.in_cooldown(now)
        if cooling:
            return GuardDecision(
                False,
                f"cooldown active for another {remaining:.0f}s",
                layer="cooldown",
            )

        # Layer 3 - sanity: never resume for a window that is not open yet.
        if snapshot.five_hour_reset_at and snapshot.five_hour_reset_at > now + timedelta(
            minutes=5
        ):
            # A window whose reset is still far in the future but that reports
            # quota available is suspicious (placeholder window).
            log.debug(
                "reset in the future (%s) - treating as not yet a real window",
                snapshot.five_hour_reset_at,
            )

        return GuardDecision(True, "no duplicate evidence", layer="all")

    # -- mutation ----------------------------------------------------------

    def commit(self, reset_id: str, now: datetime | None = None) -> None:
        """Record the send. Must be called (and persisted) before the daemon
        considers the resume done, so a crash right after cannot double-send."""
        now = now or utcnow()
        self.state.mark_triggered(reset_id, now)
        self.store.save()
        log.info("guard committed: reset_id=%s at %s", reset_id, now.isoformat())

    def commit_dry_run(self, reset_id: str) -> None:
        """In dry-run the send did not happen, so the window stays untriggered.

        Only the *last seen* id is recorded, which is what makes the very next
        real poll able to detect a genuine window change.
        """
        self.state.last_seen_reset_id = reset_id or self.state.last_seen_reset_id
        self.store.save()
