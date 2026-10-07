"""The daemon.

One poll = one call to :meth:`Daemon.run_once`. Keeping the tick as a plain
method (no sleeping inside) is what makes the whole thing testable: a test can
drive 500 simulated ticks in milliseconds.

Quota-restoration detection follows the brief's rule exactly: it is
``previous.available == False and current.available == True`` **plus** evidence
that a genuinely new window appeared (the reset timestamp moved forward, or the
window identity changed). Availability flapping inside one window must never
trigger a resume.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.models import ErrorKind, UNCERTAIN_ERRORS, UsageSnapshot, utcnow
from app.notification.base import Event
from app.state_machine import State, StateMachine, WorldFacts
from app.utils.logging_setup import get_logger
from app.utils.time_utils import humanize_delta

if TYPE_CHECKING:  # pragma: no cover
    from app.chatgpt.base import ChatGptController
    from app.config import AppConfig
    from app.notification.base import Notifier
    from app.resume.resume_manager import ResumeManager
    from app.resume.retry_manager import RetryManager
    from app.runtime.health import HealthMonitor
    from app.storage.state_store import StateStore
    from app.usage.base import UsageProvider

log = get_logger("daemon")

#: A window change is only believed when the reset timestamp moves at least
#: this far. Small drift between polls of the same window is ignored.
NEW_WINDOW_MIN_ADVANCE = timedelta(minutes=5)

#: Poll faster than the configured interval once the reset is imminent.
FAST_POLL_SECONDS = 15.0
IMMINENT_POLL_SECONDS = 20.0
IMMINENT_RESET_SECONDS = 600.0


class Daemon:
    def __init__(
        self,
        cfg: "AppConfig",
        provider: "UsageProvider",
        controller: "ChatGptController",
        store: "StateStore",
        notifier: "Notifier",
        state_machine: StateMachine,
        guard,
        retry: "RetryManager",
        resume_manager: "ResumeManager",
        health: "HealthMonitor",
        fallback_providers: list["UsageProvider"] | None = None,
    ) -> None:
        self.cfg = cfg
        self.provider = provider
        self.fallbacks = fallback_providers or []
        self.controller = controller
        self.store = store
        self.notifier = notifier
        self.sm = state_machine
        self.guard = guard
        self.retry = retry
        self.resume_manager = resume_manager
        self.health = health

        self._stop = threading.Event()
        self._notified: set[str] = set()
        self.prompt = cfg.read_prompt()
        self.tick_count = 0

    # -- lifecycle ---------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:  # pragma: no cover - exercised manually
        log.info(
            "daemon starting: provider=%s dry_run=%s poll=%ss",
            self.provider.name,
            self.cfg.dry_run,
            self.cfg.poll_interval_seconds,
        )
        self._notify(Event.RESTARTED, self._startup_message())
        while not self._stop.is_set():
            started = time.time()
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - the daemon must survive
                log.exception("unhandled error in the main loop")
                self._notify(Event.EXCEPTION, f"Unhandled error: {type(exc).__name__}: {exc}")
                self.sm.force(State.ERROR, f"exception: {type(exc).__name__}")

            interval = self.next_interval()
            elapsed = time.time() - started
            wait = max(1.0, interval - elapsed)
            log.debug("next poll in %.0fs", wait)
            if self._stop.wait(wait):
                break

        self.close()
        log.info("daemon stopped")

    def close(self) -> None:
        for provider in [self.provider, *self.fallbacks]:
            try:
                provider.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.controller.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.notifier.close()
        except Exception:  # noqa: BLE001
            pass
        self.store.save()

    # -- one tick ----------------------------------------------------------

    def run_once(self) -> State:
        self.tick_count += 1
        now = utcnow()
        self.health.tick(now)

        snapshot = self.read_usage()

        prev = self._previous_snapshot()
        facts = self._world_facts(snapshot, prev, now)

        # Notifications derived from the transition we are about to take.
        self._notify_transitions(snapshot, prev, now)

        if facts.provider_error and not snapshot.ok:
            if self.health.record_provider_failure(snapshot.error or "unknown"):
                self._notify(
                    Event.HEALTH_WARNING,
                    f"Usage provider failed repeatedly. Last error: {snapshot.error}",
                )
        else:
            self.health.record_provider_success()

        previous_state = self.sm.state
        new_state = self.sm.step(snapshot, facts)

        # Collapse READY_TO_RESUME -> RESUMING inside the same tick. A restore
        # should act immediately rather than waiting one more poll interval,
        # and every safety gate is re-evaluated by ResumeManager anyway.
        if new_state is State.READY_TO_RESUME and facts.safe_to_resume:
            new_state = self.sm.step(snapshot, facts)

        self._persist(snapshot, new_state, previous_state, now)

        # Handle the resume attempt as a second phase of the same tick.
        if new_state is State.RESUMING:
            self._do_resume(snapshot, now)

        return self.sm.state

    # -- usage reading -----------------------------------------------------

    def read_usage(self) -> UsageSnapshot:
        snapshot = self.provider.get_usage()
        if snapshot.ok:
            return snapshot

        log.debug("primary provider failed (%s), trying fallbacks", snapshot.error)
        for provider in self.fallbacks:
            candidate = provider.get_usage()
            if candidate.ok:
                log.info("fallback provider %s supplied usage data", provider.name)
                return candidate
            log.debug("fallback provider %s also failed: %s", provider.name, candidate.error)

        # Everyone failed: keep the previous snapshot's shape but mark it stale
        # so nothing is decided on it.
        stale = UsageSnapshot(
            available=False,
            source=snapshot.source,
            error=snapshot.error or "all providers failed",
            timestamp=utcnow(),
            stale=True,
        )
        prev = self._previous_snapshot()
        if prev is not None:
            stale.five_hour_remaining_percent = prev.five_hour_remaining_percent
            stale.weekly_remaining_percent = prev.weekly_remaining_percent
            stale.five_hour_reset_at = prev.five_hour_reset_at
            stale.weekly_reset_at = prev.weekly_reset_at
            stale.reset_id = prev.reset_id
        return stale

    def _previous_snapshot(self) -> UsageSnapshot | None:
        raw = self.store.state.last_usage_snapshot
        if not raw:
            return None
        try:
            return UsageSnapshot.from_dict(raw)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not restore the previous snapshot: %s", exc)
            return None

    # -- facts -------------------------------------------------------------

    def _new_window_detected(
        self, snapshot: UsageSnapshot, prev: UsageSnapshot | None
    ) -> bool:
        if prev is None:
            # First observation of this process. If the state file says we
            # already handled this window, the guard will catch it later; this
            # method must not invent a "restoration".
            return False
        if prev.available or not snapshot.available:
            return False

        # Evidence 1: the reset timestamp moved forward by a believable amount.
        # This is the primary test, and it deliberately subsumes the window id:
        # a reset_id change of only one or two minutes is poll-to-poll drift on
        # the *same* window, and treating it as a new window would fire a
        # resume into an account that is still exhausted.
        if snapshot.five_hour_reset_at and prev.five_hour_reset_at:
            if snapshot.five_hour_reset_at - prev.five_hour_reset_at >= NEW_WINDOW_MIN_ADVANCE:
                return True

        # Evidence 2: we did not know the previous reset time, but the quota is
        # available now and previously it was not. Weak but useful after a
        # state-file migration.
        if prev.five_hour_reset_at is None and snapshot.five_hour_reset_at is not None:
            return True

        log.debug(
            "quota became available but the window did not move forward "
            "(prev_reset=%s cur_reset=%s) - not a restoration",
            prev.five_hour_reset_at,
            snapshot.five_hour_reset_at,
        )
        return False

    def _world_facts(
        self, snapshot: UsageSnapshot, prev: UsageSnapshot | None, now: datetime
    ) -> WorldFacts:
        cooling, _ = self.guard.in_cooldown(now)
        facts = WorldFacts(
            quota_available=bool(snapshot.ok and snapshot.available),
            provider_error=not snapshot.ok,
            new_window_detected=self._new_window_detected(snapshot, prev),
            already_attempted=self.guard.already_triggered(snapshot.reset_id),
            cooldown_elapsed=not cooling,
        )
        # safe_to_resume is decided in _do_resume (it needs live UI state);
        # here we only gate on the things the state machine already knows.
        facts.safe_to_resume = facts.quota_available and not facts.already_attempted
        return facts

    # -- transitions / notifications --------------------------------------

    def _notify_transitions(
        self, snapshot: UsageSnapshot, prev: UsageSnapshot | None, now: datetime
    ) -> None:
        state = self.sm.state
        key_base = snapshot.reset_id or "unknown"

        if state in (State.QUOTA_EXHAUSTED, State.WAITING_RESET):
            key = f"exhausted:{key_base}"
            if key not in self._notified:
                self._notified.add(key)
                self._notify(
                    Event.QUOTA_EXHAUSTED,
                    f"5h quota exhausted (weekly {self._pct(snapshot.weekly_remaining_percent)} left)."
                    f" Reset in {humanize_delta(snapshot.five_hour_reset_at, now)}.",
                )
                if snapshot.five_hour_reset_at:
                    self._notify(
                        Event.RESET_ESTIMATED,
                        f"Estimated reset at "
                        f"{snapshot.five_hour_reset_at.astimezone().strftime('%H:%M')} "
                        f"({humanize_delta(snapshot.five_hour_reset_at, now)}).",
                    )

        if state in (State.READY_TO_RESUME, State.RESUMING):
            key = f"restored:{key_base}"
            if key not in self._notified:
                self._notified.add(key)
                self._notify(
                    Event.QUOTA_RESTORED,
                    f"Quota restored (5h {self._pct(snapshot.five_hour_remaining_percent)} left).",
                )
                self._notify(
                    Event.RESUME_PREPARING,
                    "Checking ChatGPT, the target conversation and the Work state.",
                )

        # Prune the dedup set so it cannot grow without bound.
        if len(self._notified) > 200:
            self._notified = set(list(self._notified)[-100:])

    def _pct(self, value: float | None) -> str:
        return "?" if value is None else f"{value:.0f}%"

    def _notify(self, event: Event, message: str) -> None:
        try:
            self.notifier.send(event, message)
        except Exception as exc:  # noqa: BLE001
            log.debug("notification raised: %s", exc)

    def _startup_message(self) -> str:
        state = self.store.state
        return (
            f"Daemon started (dry_run={self.cfg.dry_run}, provider={self.provider.name}, "
            f"restart #{state.restart_count})."
        )

    # -- resume ------------------------------------------------------------

    def _do_resume(self, snapshot: UsageSnapshot, now: datetime) -> None:
        result = self.resume_manager.attempt(self.prompt, snapshot, now=now)

        succeeded = result.success
        detail = result.detail or result.error or ""

        if not succeeded and result.error == ErrorKind.DRY_RUN:
            # Nothing was typed. Move through COOLDOWN so we do not print the
            # same line every 30 seconds, but leave the window un-triggered.
            self.sm.force(State.COOLDOWN, "dry run completed")
            return

        if succeeded:
            self.health.record_controller_success()
            self._notify(
                Event.RESUME_SENT,
                f"Continue prompt delivered to ChatGPT ({detail}).",
            )
            self.sm.force(State.COOLDOWN, "resume sent")
            return

        if result.error in UNCERTAIN_ERRORS:
            # SEND_UNCERTAIN / FOCUS_UNVERIFIED: the prompt may already be in
            # the conversation, or we refused to type because focus could not
            # be proven. Either way there must be no automatic retry, and the
            # retry budget must NOT be consumed (this is not a transient
            # failure). The resume manager already persisted UNCERTAIN and
            # notified; park in COOLDOWN until a genuinely new window arrives.
            log.error(
                "send uncertain (%s: %s); no automatic retry for this window",
                result.error,
                detail,
            )
            self.sm.force(State.COOLDOWN, "send uncertain - manual check required")
            return

        if result.error == ErrorKind.PROMPT_RESOLUTION_FAILED:
            # A preset binding/rendering failure is a config problem, not a
            # transient one. Never send; notify and pause this window so the
            # user can fix the preset. No automatic retry loop.
            self._notify(
                Event.RESUME_FAILED,
                f"续跑 Prompt 解析失败，本额度窗口不会发送：{detail}。请到 GUI 检查 Prompt 预设。",
            )
            log.error("prompt resolution failed; pausing window: %s", detail)
            self.sm.force(State.COOLDOWN, "prompt resolution failed")
            return

        if result.error in (ErrorKind.CHATGPT_BUSY, ErrorKind.CHATGPT_NOT_RUNNING,
                            ErrorKind.WINDOW_NOT_FOUND, ErrorKind.TASK_LOCK_MISMATCH):
            log.info("resume deferred: %s (%s)", result.error, detail)
            self.sm.force(State.READY_TO_RESUME, f"deferred: {result.error}")
            return

        decision = self.retry.record_failure(result.error or ErrorKind.UNKNOWN, now)
        if decision.give_up:
            self._notify(
                Event.MAX_RETRIES,
                f"Gave up after {self.retry.max_retries} attempts ({result.error}: {detail}). "
                "Auto-resume is paused for this window. No further prompt will be sent.",
            )
            self.sm.force(State.COOLDOWN, "retries exhausted")
            return

        self._notify(
            Event.RESUME_FAILED,
            f"Attempt failed ({result.error}: {detail}). "
            f"Retry {self.retry.state.retry_count}/{self.retry.max_retries} "
            f"in {decision.delay_seconds:.0f}s.",
        )
        self.sm.force(State.READY_TO_RESUME, f"retry scheduled: {result.error}")

    # -- persistence -------------------------------------------------------

    def _persist(
        self,
        snapshot: UsageSnapshot,
        new_state: State,
        previous_state: State,
        now: datetime,
    ) -> None:
        state = self.store.state
        if snapshot.ok:
            self.store.update_snapshot(snapshot)
        state.program_state = new_state.value
        if new_state is not previous_state:
            state.last_state_change = now.isoformat()
        self.store.save()

    # -- scheduling --------------------------------------------------------

    def next_interval(self, snapshot: UsageSnapshot | None = None) -> float:
        base = float(self.cfg.poll_interval_seconds)
        state = self.sm.state

        if state not in (State.WAITING_RESET, State.QUOTA_EXHAUSTED):
            return base

        reset_at = None
        raw = self.store.state.last_usage_snapshot or {}
        value = raw.get("five_hour_reset_at")
        if value:
            try:
                reset_at = datetime.fromisoformat(value)
            except ValueError:
                reset_at = None

        if reset_at is None:
            return base

        remaining = (reset_at - utcnow()).total_seconds()
        if remaining <= 0:
            # The advertised reset time has passed: poll fast until the
            # provider actually reports fresh quota.
            return min(base, FAST_POLL_SECONDS)
        if remaining <= IMMINENT_RESET_SECONDS:
            return min(base, IMMINENT_POLL_SECONDS)
        return base
