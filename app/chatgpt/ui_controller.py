"""Concrete controller: wires process detection, window control, busy
detection and prompt sending into the :class:`ChatGptController` interface.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from app.chatgpt import process_detector, prompt_sender, window_controller, work_detector
from app.chatgpt.base import ChatGptController, WindowInfo
from app.models import ErrorKind, ResumeResult
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig

log = get_logger("chatgpt.controller")

#: Where ChatGPT Desktop installs itself on Windows.
EXE_CANDIDATES = (
    r"%LOCALAPPDATA%\Programs\ChatGPT\ChatGPT.exe",
    r"%LOCALAPPDATA%\ChatGPT\ChatGPT.exe",
    r"%PROGRAMFILES%\ChatGPT\ChatGPT.exe",
)


def discover_chatgpt_exe(explicit: str = "") -> str | None:
    if explicit:
        p = Path(explicit)
        return str(p) if p.exists() else None
    for pattern in EXE_CANDIDATES:
        candidate = Path(os.path.expandvars(pattern))
        if candidate.exists():
            return str(candidate)
    # Last resort: ask the shell where it is.
    try:
        import shutil

        found = shutil.which("ChatGPT.exe")
        if found:
            return found
    except Exception:  # noqa: BLE001
        pass
    return None


class ChatGptUiaController(ChatGptController):
    name = "uia"

    def __init__(self, cfg: "AppConfig") -> None:
        self.cfg = cfg
        self.process_names = cfg.chatgpt.process_names
        self._window_info: WindowInfo | None = None
        self._wrapper = None
        self._controls = None
        self.explicit_exe = cfg.chatgpt.exe_path

    # -- discovery ---------------------------------------------------------

    def is_running(self) -> bool:
        return process_detector.is_process_running(self.process_names)

    def find_window(self) -> WindowInfo | None:
        windows = process_detector.find_chatgpt_windows(self.process_names)
        if not windows:
            self._window_info = None
            self._wrapper = None
            self._controls = None
            return None

        if self._window_info and self._window_info.handle == windows[0].handle and self._wrapper:
            wrapper = self._wrapper
            info = self._window_info
        else:
            info = windows[0]
            wrapper = window_controller.connect_window(info)
            if wrapper is None:
                return None
            self._window_info = info
            self._wrapper = wrapper
            self._controls = None

        window_controller.restore_window(wrapper, info)
        return info

    def _get_controls(self):
        if self._controls is None:
            if self._wrapper is None:
                return None
            self._controls = window_controller.locate(self._wrapper)
        return self._controls

    def invalidate(self) -> None:
        """Drop cached handles - call after any suspected UI change."""
        self._controls = None

    # -- state -------------------------------------------------------------

    def is_busy(self) -> bool | None:
        if self._wrapper is None:
            return None
        controls = self._get_controls()
        if controls is None:
            return None
        verdict = work_detector.detect_busy(self._wrapper, controls)
        if verdict.busy is None:
            log.debug("busy state unknown: %s (%s)", verdict.reason, verdict.evidence)
        else:
            log.debug("busy=%s (%s)", verdict.busy, verdict.reason)
        return verdict.busy

    def conversation_title(self) -> str:
        """Best-effort name of the conversation currently on screen.

        Measured behaviour: the window title is always just "ChatGPT", so the
        sidebar list item is the only useful signal. If nothing readable is
        found we return '' - and the task lock treats that as "cannot verify",
        which is the safe answer.
        """
        if self._wrapper is not None:
            try:
                items = self._wrapper.descendants(control_type="ListItem") or []
                for ctrl in items:
                    name = (ctrl.window_text() or "").strip()
                    if not name:
                        continue
                    selected = False
                    try:
                        selected = bool(ctrl.is_selected())
                    except Exception:  # noqa: BLE001
                        selected = False
                    if selected:
                        return name
                if items:
                    first = (items[0].window_text() or "").strip()
                    if first:
                        return first
            except Exception:  # noqa: BLE001
                pass
        return ""

    # -- activation --------------------------------------------------------

    def start(self) -> bool:
        exe = discover_chatgpt_exe(self.explicit_exe)
        if not exe:
            log.warning("ChatGPT Desktop executable not found; cannot auto-start")
            return False
        log.info("launching ChatGPT Desktop: %s", exe)
        try:
            subprocess.Popen([exe], close_fds=True)
        except OSError as exc:
            log.error("failed to launch ChatGPT Desktop: %s", exc)
            return False

        deadline = time.time() + max(5, self.cfg.chatgpt.launch_wait_seconds)
        while time.time() < deadline:
            if self.find_window() is not None:
                log.info("ChatGPT window appeared")
                return True
            time.sleep(1.0)
        log.warning("ChatGPT did not present a window within the timeout")
        return False

    # -- sending -----------------------------------------------------------

    def send_prompt(self, text: str, *, dry_run: bool = True) -> ResumeResult:
        if not self.is_running():
            return ResumeResult(False, ErrorKind.CHATGPT_NOT_RUNNING, "process missing")
        info = self.find_window()
        if info is None:
            return ResumeResult(False, ErrorKind.WINDOW_NOT_FOUND, "no main window")

        controls = self._get_controls()
        if controls is None:
            self.invalidate()
            return ResumeResult(False, ErrorKind.INPUT_NOT_FOUND, "window not attached")

        if dry_run:
            outcome = prompt_sender.send_prompt(controls, text, dry_run=True, hwnd=info.handle)
            return ResumeResult(False, ErrorKind.DRY_RUN, outcome.via or "dry_run")

        busy = self.is_busy()
        if busy is not False:
            return ResumeResult(
                False,
                ErrorKind.CHATGPT_BUSY,
                "busy state is true or unknown at send time",
            )

        outcome = prompt_sender.send_prompt(
            controls, text, dry_run=False, hwnd=info.handle
        )
        if outcome.sent:
            self.invalidate()
            return ResumeResult(True, None, f"sent via {outcome.via}")
        self.invalidate()
        return ResumeResult(False, outcome.kind or ErrorKind.SEND_FAILED, outcome.error or "unknown")

    def close(self) -> None:
        self._wrapper = None
        self._controls = None
        self._window_info = None

    def describe(self) -> str:
        return f"uia (processes={self.process_names})"


def _register() -> None:
    from app.chatgpt.base import register

    register("uia")(ChatGptUiaController)


_register()
