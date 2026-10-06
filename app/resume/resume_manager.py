"""Resume orchestration.

Order of operations matters and is intentional:

1. **Re-read the quota** (layer 4 of duplicate protection). The state may have
   changed between "we decided to resume" and "we are about to type".
2. **Duplicate guard** (layers 1+2). Cheap, and it protects against a crash
   that happened after the previous send.
3. **Task lock**. Only continue the conversation we were told to continue.
4. **App / window / busy checks**. Never type into a working app.
5. **Send**, then **persist** the fact that we sent.

Every early exit returns a classified error so the caller can tell
"the world isn't ready" (wait) apart from "the send failed" (retry).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from app.models import ErrorKind, ResumeResult, UsageSnapshot, utcnow
from app.resume.duplicate_guard import DuplicateGuard
from app.resume.retry_manager import RetryManager
from app.storage.state_store import StateStore
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.chatgpt.base import ChatGptController
    from app.config import AppConfig
    from app.notification.base import Notifier
    from app.usage.base import UsageProvider

log = get_logger("resume")


class ResumeManager:
    def __init__(
        self,
        cfg: "AppConfig",
        store: StateStore,
        controller: "ChatGptController",
        guard: DuplicateGuard,
        retry: RetryManager,
        notifier: "Notifier | None" = None,
        provider: "UsageProvider | None" = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.controller = controller
        self.guard = guard
        self.retry = retry
        self.notifier = notifier
        self.provider = provider
        self.last_result: ResumeResult | None = None

    # -- task lock ---------------------------------------------------------

    def task_lock_ok(self, conversation_title: str) -> tuple[bool, str]:
        lock = self.cfg.task_lock
        if not lock.enabled:
            return True, "task lock disabled"

        if not conversation_title:
            # Conservative: with the lock on, an unreadable conversation is
            # not permission to send.
            return False, "task lock enabled but the active conversation title is unreadable"

        title = conversation_title.lower()
        if lock.project and lock.project.lower() not in title:
            return False, f"conversation {conversation_title!r} is not project {lock.project!r}"
        if lock.conversation and lock.conversation.lower() not in title:
            return (
                False,
                f"conversation {conversation_title!r} does not match "
                f"{lock.conversation!r}",
            )
        if lock.goal_hash:
            import hashlib

            digest = hashlib.sha256(conversation_title.encode("utf-8")).hexdigest()[:16]
            if digest != lock.goal_hash:
                return False, "goal_hash mismatch"
        return True, "task lock satisfied"

    # -- main entry point --------------------------------------------------

    def attempt(
        self,
        prompt: str,
        snapshot: UsageSnapshot,
        *,
        dry_run: bool | None = None,
        now: datetime | None = None,
    ) -> ResumeResult:
        now = now or utcnow()
        dry = self.cfg.dry_run if dry_run is None else dry_run

        # ---- layer 4: re-read the quota right before acting ---------------
        fresh = snapshot
        if self.provider is not None:
            fresh = self.provider.get_usage()
            if not fresh.ok:
                result = ResumeResult(
                    False,
                    ErrorKind.PROVIDER_READ_FAILED,
                    f"re-read failed: {fresh.error}",
                )
                self._finish(result)
                return result
            if not fresh.available:
                result = ResumeResult(
                    False, ErrorKind.CHATGPT_BUSY, "quota is no longer available at send time"
                )
                self._finish(result)
                return result
            if fresh.reset_id and snapshot.reset_id and fresh.reset_id != snapshot.reset_id:
                result = ResumeResult(
                    False,
                    ErrorKind.CHATGPT_BUSY,
                    "quota window changed while preparing the resume",
                )
                self._finish(result)
                return result

        # ---- layers 1 + 2 -------------------------------------------------
        decision = self.guard.check(fresh, now)
        if not decision.allowed:
            result = ResumeResult(False, ErrorKind.UNKNOWN, f"guard: {decision.reason}")
            self._finish(result)
            return result

        # ---- task lock ----------------------------------------------------
        title = ""
        if self.cfg.task_lock.enabled:
            self.controller.find_window()
            title = self.controller.conversation_title()
        ok, why = self.task_lock_ok(title)
        if not ok:
            result = ResumeResult(False, ErrorKind.TASK_LOCK_MISMATCH, why)
            self._finish(result)
            return result

        # ---- app state ----------------------------------------------------
        if not self.controller.is_running():
            if not dry and self.cfg.chatgpt.auto_start:
                log.info("ChatGPT is not running; attempting to launch it")
                if not self.controller.start():
                    result = ResumeResult(
                        False,
                        ErrorKind.CHATGPT_NOT_RUNNING,
                        "ChatGPT Desktop could not be launched",
                    )
                    self._finish(result)
                    return result
            elif not dry:
                result = ResumeResult(
                    False, ErrorKind.CHATGPT_NOT_RUNNING, "ChatGPT Desktop is not running"
                )
                self._finish(result)
                return result

        if self.controller.find_window() is None:
            result = ResumeResult(False, ErrorKind.WINDOW_NOT_FOUND, "no ChatGPT main window")
            self._finish(result)
            return result

        busy = self.controller.is_busy()
        if busy is not False:
            detail = "busy" if busy else "busy state unknown"
            result = ResumeResult(False, ErrorKind.CHATGPT_BUSY, detail)
            self._finish(result)
            return result

        # ---- send ---------------------------------------------------------
        if dry:
            log.info("DRY_RUN: Would send prompt (reset_id=%s)", fresh.reset_id)
            self.guard.commit_dry_run(fresh.reset_id)
            result = ResumeResult(False, ErrorKind.DRY_RUN, "dry run: nothing was typed")
            self._finish(result)
            return result

        result = self.controller.send_prompt(prompt, dry_run=False)
        if result.success:
            self.guard.commit(fresh.reset_id, now)
            self.store.state.resumed_count += 1
            self.store.save()
            self.retry.reset()
            log.info("resume prompt sent successfully (reset_id=%s)", fresh.reset_id)
        else:
            log.warning("resume attempt failed: %s - %s", result.error, result.detail)

        self._finish(result)
        return result

    def _finish(self, result: ResumeResult) -> None:
        self.last_result = result
