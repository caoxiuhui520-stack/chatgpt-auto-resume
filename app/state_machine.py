"""The daemon state machine.

States and the exact transition rules are specified in the project brief.
This module is deliberately free of I/O: it takes a :class:`UsageSnapshot`
plus a few boolean "world" facts and returns the next state. That makes the
whole decision surface unit-testable without a real ChatGPT or a real Codex.

Guarantee: :data:`ERROR` is never terminal. ``step()`` always allows a path
back to ``WORKING`` so a single bad poll cannot kill the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from app.models import UsageSnapshot, utcnow
from app.utils.logging_setup import get_logger

log = get_logger("state_machine")


class State(str, Enum):
    STARTING = "STARTING"
    WORKING = "WORKING"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    WAITING_RESET = "WAITING_RESET"
    READY_TO_RESUME = "READY_TO_RESUME"
    RESUMING = "RESUMING"
    COOLDOWN = "COOLDOWN"
    ERROR = "ERROR"


#: Which transitions are legal. Used for assertions in tests and to make the
#: intent of the design reviewable at a glance.
ALLOWED_TRANSITIONS: dict[State, set[State]] = {
    State.STARTING: {State.WORKING, State.QUOTA_EXHAUSTED, State.ERROR, State.STARTING},
    State.WORKING: {
        State.QUOTA_EXHAUSTED,
        State.READY_TO_RESUME,
        State.ERROR,
        State.WORKING,
        State.COOLDOWN,
    },
    State.QUOTA_EXHAUSTED: {
        State.WAITING_RESET,
        State.READY_TO_RESUME,
        State.WORKING,
        State.ERROR,
        State.QUOTA_EXHAUSTED,
    },
    State.WAITING_RESET: {
        State.READY_TO_RESUME,
        State.WORKING,
        State.ERROR,
        State.WAITING_RESET,
    },
    State.READY_TO_RESUME: {
        State.RESUMING,
        State.WORKING,
        State.QUOTA_EXHAUSTED,
        State.ERROR,
        State.READY_TO_RESUME,
    },
    State.RESUMING: {
        State.COOLDOWN,
        State.READY_TO_RESUME,
        State.ERROR,
        State.WORKING,
        State.RESUMING,
    },
    State.COOLDOWN: {State.WORKING, State.ERROR, State.COOLDOWN},
    # ERROR is recoverable by design - never a dead end.
    State.ERROR: {
        State.WORKING,
        State.QUOTA_EXHAUSTED,
        State.WAITING_RESET,
        State.READY_TO_RESUME,
        State.COOLDOWN,
        State.ERROR,
    },
}


@dataclass
class WorldFacts:
    """Everything the state machine needs to know about the outside world."""

    #: The provider read succeeded and reports quota available.
    quota_available: bool = False
    #: The provider read failed entirely this tick.
    provider_error: bool = False
    #: A genuinely new quota window has been observed (id differs from the
    #: previously seen one AND the previous window was exhausted).
    new_window_detected: bool = False
    #: All safety conditions for sending have passed.
    safe_to_resume: bool = False
    #: A resume attempt finished successfully.
    resume_succeeded: bool = False
    #: A resume attempt was attempted but failed.
    resume_failed: bool = False
    #: The cooldown timer has elapsed.
    cooldown_elapsed: bool = False
    #: This process has already attempted a resume for this window.
    already_attempted: bool = False


class StateMachine:
    def __init__(self, initial: State = State.STARTING) -> None:
        self._state = initial
        self._history: list[tuple[datetime, State, State, str]] = []
        self._consecutive_errors = 0

    # -- accessors ---------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "at": when.isoformat(),
                "from": old.value,
                "to": new.value,
                "reason": reason,
            }
            for when, old, new, reason in self._history[-limit:]
        ]

    # -- transitions -------------------------------------------------------

    def force(self, new: State, reason: str = "") -> None:
        self._set(new, reason)

    def _set(self, new: State, reason: str) -> None:
        if new is self._state:
            return
        allowed = ALLOWED_TRANSITIONS.get(self._state, set())
        if new not in allowed:
            log.warning(
                "illegal transition %s -> %s (%s); forcing anyway",
                self._state.value,
                new.value,
                reason,
            )
        old = self._state
        self._state = new
        self._history.append((utcnow(), old, new, reason))
        if len(self._history) > 500:
            self._history = self._history[-250:]
        log.info("state %s -> %s (%s)", old.value, new.value, reason or "no reason")
        if new is not State.ERROR:
            self._consecutive_errors = 0

    def step(self, snapshot: UsageSnapshot, facts: WorldFacts) -> State:
        """Advance one tick and return the new state."""
        if facts.provider_error and not snapshot.ok:
            self._consecutive_errors += 1
            # Stay put on the first few failures: a single flaky read must not
            # throw away the fact that we are waiting for a reset.
            if self._consecutive_errors >= 3:
                self._set(State.ERROR, "provider read failed repeatedly")
            return self._state

        state = self._state

        if state is State.STARTING:
            if not snapshot.ok:
                return state
            if facts.quota_available:
                self._set(State.WORKING, "quota available at startup")
            else:
                self._set(State.QUOTA_EXHAUSTED, "quota already exhausted at startup")
            return self._state

        if state is State.WORKING:
            if not facts.quota_available:
                self._set(State.QUOTA_EXHAUSTED, "5h quota exhausted")
            return self._state

        if state is State.QUOTA_EXHAUSTED:
            # A brief flap (used 100 -> 99 -> 100) must not be treated as a
            # reset. Only a genuinely new window does that.
            if facts.new_window_detected and facts.quota_available:
                self._set(State.READY_TO_RESUME, "new quota window detected")
            elif facts.quota_available:
                self._set(State.WORKING, "quota available again in same window")
            else:
                self._set(State.WAITING_RESET, "waiting for the window to reset")
            return self._state

        if state is State.WAITING_RESET:
            if facts.quota_available and facts.new_window_detected:
                self._set(State.READY_TO_RESUME, "quota restored, new window")
            elif not facts.quota_available:
                return state
            elif facts.quota_available and not facts.new_window_detected:
                # Quota came back inside the same window - nothing to resume.
                self._set(State.WORKING, "quota back within the same window")
            return self._state

        if state is State.READY_TO_RESUME:
            if not facts.quota_available:
                self._set(State.QUOTA_EXHAUSTED, "quota went away before we could send")
            elif facts.already_attempted:
                # Already handled this window in a previous life; return to
                # idle monitoring without sending anything.
                self._set(State.COOLDOWN, "this window was already resumed")
            elif facts.safe_to_resume:
                self._set(State.RESUMING, "all safety conditions passed")
            return self._state

        if state is State.RESUMING:
            if facts.resume_succeeded:
                self._set(State.COOLDOWN, "resume prompt sent")
            elif facts.resume_failed:
                self._set(State.READY_TO_RESUME, "resume attempt failed, will retry")
            return self._state

        if state is State.COOLDOWN:
            if facts.cooldown_elapsed:
                if facts.quota_available:
                    self._set(State.WORKING, "cooldown finished")
                else:
                    self._set(State.QUOTA_EXHAUSTED, "cooldown finished, quota exhausted")
            return self._state

        if state is State.ERROR:
            # Recover as soon as a read succeeds again.
            if snapshot.ok:
                if facts.quota_available:
                    self._set(State.WORKING, "recovered from error, quota available")
                else:
                    self._set(State.WAITING_RESET, "recovered from error, waiting for reset")
            return self._state

        return self._state
