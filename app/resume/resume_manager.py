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
from app.prompts.manager import PromptResolutionError
from app.resume.duplicate_guard import DuplicateGuard
from app.resume.retry_manager import RetryManager
from app.storage.state_store import StateStore
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.chatgpt.base import ChatGptController
    from app.config import AppConfig
    from app.discovery.provider import ConversationDiscoveryProvider
    from app.notification.base import Notifier
    from app.prompts.manager import PromptPresetManager
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
        discovery: "ConversationDiscoveryProvider | None" = None,
        presets: "PromptPresetManager | None" = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.controller = controller
        self.guard = guard
        self.retry = retry
        self.notifier = notifier
        self.provider = provider
        self.discovery = discovery
        self.presets = presets
        self.last_result: ResumeResult | None = None

    # -- task lock ---------------------------------------------------------

    def task_lock_ok(
        self, conversation_title: str, current_id: str = ""
    ) -> tuple[bool, str]:
        lock = self.cfg.task_lock
        if not lock.enabled:
            return True, "task lock disabled"

        # Prefer the structured target (conversation_id based) when present.
        target = self.cfg.target
        if target.conversation_id or target.conversation_title.strip():
            from app.target import TargetResolver

            match = TargetResolver().resolve(
                target, current_id, conversation_title, self.discovery
            )
            return match.ok, match.reason

        # Legacy behaviour: project / conversation substring match on title.
        if not conversation_title:
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
        current_id = ""
        if self.cfg.task_lock.enabled:
            self.controller.find_window()
            title = self.controller.conversation_title()
            current_id = self._current_conversation_id()

            # Unify on resolve_current_conversation(): combine the desktop tab
            # id with the UIA title and require the identity to be *verified*.
            # For a real send the current conversation must be desktop_active
            # with confidence >= 0.85 - a cached/guessed identity never
            # authorises typing.
            if not dry and self.discovery is not None:
                try:
                    resolved = self.discovery.resolve_current_conversation(title)
                except Exception:  # noqa: BLE001
                    resolved = None
                if resolved is not None:
                    current_id = resolved.id
                    title = resolved.title or title
                    if (
                        not resolved.is_verified
                        or resolved.source_kind != "desktop_active"
                        or resolved.confidence < 0.85
                    ):
                        result = ResumeResult(
                            False,
                            ErrorKind.TASK_LOCK_MISMATCH,
                            "当前对话身份未通过验证（无法确认就是目标对话），禁止发送",
                        )
                        self._finish(result)
                        return result
        ok, why = self.task_lock_ok(title, current_id)
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

        # ---- prompt resolution (preset bound to the target conversation) --
        # The preset for the target conversation wins over the static prompt;
        # safe variables are rendered right before the send. Resolution is
        # fail-closed: a broken binding/rendering aborts the send.
        binding_id = self.cfg.target.conversation_id or current_id
        try:
            prompt = self._resolve_prompt(prompt, binding_id, title, snapshot, now)
        except PromptResolutionError as exc:
            result = ResumeResult(False, ErrorKind.PROMPT_RESOLUTION_FAILED, str(exc))
            self._finish(result)
            return result

        # ---- send ---------------------------------------------------------
        if dry:
            log.info("DRY_RUN: Would send prompt (reset_id=%s)", fresh.reset_id)
            self.guard.commit_dry_run(fresh.reset_id)
            result = ResumeResult(False, ErrorKind.DRY_RUN, "dry run: nothing was typed")
            self._finish(result)
            return result

        # ---- durable two-phase transaction --------------------------------
        # An unfinished PREPARED transaction means a previous run may already
        # have typed into the conversation. Never resend in that situation.
        if self.store.state.has_prepared_send():
            self.store.state.mark_pending_uncertain()
            self.store.save()
            self._notify_uncertain(
                "上一次 Auto Resume 在发送过程中异常中断，无法确定提示词是否已经提交。"
                "本窗口不会自动再次发送，请人工检查。"
            )
            result = ResumeResult(False, ErrorKind.SEND_UNCERTAIN, "previous PREPARED send")
            self._finish(result)
            return result

        if self.store.state.has_uncertain_send():
            # UNCERTAIN is per-window: a *new* quota window is a fresh chance,
            # while the same window must never be retried.
            if self.store.state.pending_send_reset_id == fresh.reset_id:
                result = ResumeResult(
                    False,
                    ErrorKind.SEND_UNCERTAIN,
                    "a previous send for THIS window is UNCERTAIN; refusing to resend",
                )
                self._finish(result)
                return result
            # New window: the stale UNCERTAIN marker belongs to an old cycle.
            self.store.state.abort_pending_send()
            self.store.save()
            log.info("new quota window; cleared stale UNCERTAIN marker")

        # Step 1: persist PREPARED before any keystroke is produced.
        self.store.state.begin_pending_send(fresh.reset_id, now)
        self.store.save()

        result = self.controller.send_prompt(prompt, dry_run=False)
        if not result.success:
            # The keystroke sequence never completed, so nothing was sent.
            # Clearing the transaction lets the retry manager reschedule.
            self.store.state.abort_pending_send()
            self.store.save()
            log.warning("resume attempt failed: %s - %s", result.error, result.detail)
            self._finish(result)
            return result

        # Step 2: POST_SEND_VERIFY. "No exception" is not a confirmation.
        confirmed, reason = self.controller.verify_sent()
        if confirmed:
            self.store.state.confirm_pending_send(now)
            self.store.state.resumed_count += 1
            self.store.save()
            self.retry.reset()
            log.info("resume prompt sent and confirmed (%s)", reason)
            result = ResumeResult(True, None, f"confirmed: {reason}")
            self._finish(result)
            return result

        # Uncertain: the message may already be in the conversation. Do NOT
        # retry - resending is strictly worse than missing once.
        self.store.state.mark_pending_uncertain()
        self.store.save()
        self._notify_uncertain(
            "Auto Resume 发送后无法确认提示词是否被 ChatGPT 接收。"
            "本窗口不会自动再次发送，请人工检查。"
        )
        log.error("send uncertain, marked UNCERTAIN and not retrying: %s", reason)
        result = ResumeResult(False, ErrorKind.SEND_UNCERTAIN, f"send uncertain: {reason}")
        self._finish(result)
        return result

    def _current_conversation_id(self) -> str:
        """The id of the conversation currently open in ChatGPT Desktop."""
        if self.discovery is None:
            return ""
        try:
            self.discovery.refresh()
            current = self.discovery.get_current_conversation()
            return current.id if current else ""
        except Exception:  # noqa: BLE001
            return ""

    def _resolve_prompt(
        self,
        fallback: str,
        conversation_id: str,
        title: str,
        snapshot: UsageSnapshot,
        now: datetime,
    ) -> str:
        """Render the bound preset (or default) with safe variables.

        Fail-closed: an explicit binding whose preset is missing, or a
        rendering error, raises :class:`PromptResolutionError` - the caller
        must then refuse to send rather than silently fall back.
        """
        if self.presets is None:
            return fallback
        context = {
            "conversation_title": title,
            "project_name": self.cfg.target.project_name,
            "current_time": now.isoformat(),
            "quota_reset_time": (
                snapshot.five_hour_reset_at.isoformat() if snapshot.five_hour_reset_at else ""
            ),
            "last_resume_time": self.store.state.last_resume_time or "",
        }
        prompt, _preset_id = self.presets.resolve_prompt_strict(
            conversation_id, fallback=fallback, context=context
        )
        return prompt

    def _notify_uncertain(self, message: str) -> None:
        if self.notifier is not None:
            try:
                from app.notification.base import Event

                self.notifier.send(Event.SEND_UNCERTAIN, message)
            except Exception:  # noqa: BLE001
                pass

    def _finish(self, result: ResumeResult) -> None:
        self.last_result = result
