"""Scriptable fake ChatGPT controller.

Lets the full daemon - including the resume, duplicate-guard and retry paths -
run end to end with no real ChatGPT window. This is how the "never send twice"
guarantee is actually exercised.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.chatgpt.base import ChatGptController, WindowInfo, register
from app.models import ErrorKind, ResumeResult
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig

log = get_logger("chatgpt.fake")


class FakeChatGptController(ChatGptController):
    """A controller whose every answer is scriptable.

    Plain class (not a dataclass) so that scripted overrides and the recorded
    interaction log stay explicit and mutable.
    """

    name = "fake"

    def __init__(self, cfg: "AppConfig | None" = None, **kwargs: Any) -> None:
        self.cfg = cfg
        self.running: bool = True
        self.window_present: bool = True
        self.input_present: bool = True
        #: None = "cannot tell" - the caller must treat that as busy.
        self.busy: bool | None = False
        self.conversation: str = "huanyu-dev"
        self.launch_succeeds: bool = True
        #: When set, send_prompt fails with this ErrorKind instead of succeeding.
        self.fail_with: str | None = None

        # Recorded interactions.
        self.sent_prompts: list[str] = []
        self.send_attempts: int = 0
        self.start_calls: int = 0

        for key, value in kwargs.items():
            if not hasattr(self, key):
                raise AttributeError(f"unknown fake attribute {key!r}")
            setattr(self, key, value)

    # -- ChatGptController -------------------------------------------------

    def is_running(self) -> bool:
        return self.running

    def find_window(self) -> WindowInfo | None:
        if not self.window_present:
            return None
        return WindowInfo(handle=4242, title="ChatGPT", pid=1234, process_name="ChatGPT.exe")

    def is_busy(self) -> bool | None:
        return self.busy

    def conversation_title(self) -> str:
        return self.conversation

    def start(self) -> bool:
        self.start_calls += 1
        if self.launch_succeeds:
            self.running = True
            self.window_present = True
        return self.launch_succeeds

    def send_prompt(self, text: str, *, dry_run: bool = True) -> ResumeResult:
        self.send_attempts += 1

        if dry_run:
            log.info("DRY_RUN: Would send prompt (%d chars)", len(text))
            return ResumeResult(False, ErrorKind.DRY_RUN, "dry_run")

        if not self.running:
            return ResumeResult(False, ErrorKind.CHATGPT_NOT_RUNNING, "fake: not running")
        if not self.window_present:
            return ResumeResult(False, ErrorKind.WINDOW_NOT_FOUND, "fake: no window")
        if not self.input_present:
            return ResumeResult(False, ErrorKind.INPUT_NOT_FOUND, "fake: no composer")
        if self.busy is not False:
            return ResumeResult(False, ErrorKind.CHATGPT_BUSY, "fake: busy")
        if self.fail_with:
            return ResumeResult(False, self.fail_with, "fake: scripted failure")

        self.sent_prompts.append(text)
        return ResumeResult(True, None, "fake: sent")


@register("fake")
def _build(cfg: "AppConfig") -> ChatGptController:
    return FakeChatGptController(cfg)
