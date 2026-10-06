"""Concrete controller: wires process detection, main-window selection, busy
detection and prompt sending into the :class:`ChatGptController` interface.

Window model (measured): ChatGPT Desktop exposes several top-level windows.
The conversation lives in the *main* window's ``Chrome_RenderWidgetHostHWND``
render widget; an always-on overlay window exists too but contains no
composer. So this controller always resolves the window through
``select_main_window`` and drives the render widget, never the top-level
chrome.
"""

from __future__ import annotations

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
    import os

    for pattern in EXE_CANDIDATES:
        candidate = Path(os.path.expandvars(pattern))
        if candidate.exists():
            return str(candidate)
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
        self.explicit_exe = cfg.chatgpt.exe_path

        self._window_info: WindowInfo | None = None
        self._render_handle: int = 0
        self._render_wrapper = None
        self._controls = None
        #: True when the composer was found in a reparented (backgrounded)
        #: renderer: UIA patterns only, never the real mouse or keyboard.
        self._backgrounded = False

    # -- discovery ---------------------------------------------------------

    def is_running(self) -> bool:
        return process_detector.is_process_running(self.process_names)

    def _clear(self) -> None:
        self._window_info = None
        self._render_handle = 0
        self._render_wrapper = None
        self._controls = None
        self._backgrounded = False

    def _cached_main_window_alive(self) -> bool:
        """Cheap revalidation: is the composer still reachable?"""
        if self._render_wrapper is None:
            return False
        try:
            return window_controller.find_input_box(self._render_wrapper) is not None
        except Exception:  # noqa: BLE001
            return False

    def find_window(self) -> WindowInfo | None:
        # Prefer the already-verified main window if the composer is still
        # reachable - probing every poll would restore minimised windows and
        # disturb the desktop unnecessarily.
        if self._window_info is not None and self._cached_main_window_alive():
            return self._window_info
        self._clear()

        candidates = process_detector.find_chatgpt_windows(self.process_names)
        selected = (
            window_controller.select_main_window(candidates) if candidates else None
        )
        if selected is not None:
            info, render_handle = selected
            self._backgrounded = False
        else:
            # Last resort: Chromium may have reparented the conversation
            # renderer to a hidden helper window. The full tree (composer
            # included) stays alive there and UIA patterns still work.
            orphan = window_controller.select_orphan_renderer(self.process_names)
            if orphan is None:
                return None
            info, render_handle = orphan
            self._backgrounded = True

        self._window_info = info
        self._render_handle = render_handle
        self._render_wrapper = window_controller.connect_render_widget(render_handle)
        self._controls = None
        return info

    def _get_controls(self):
        if self._controls is None:
            if self._render_wrapper is None:
                return None
            self._controls = window_controller.locate(
                self._render_wrapper, self._render_handle
            )
            # A backgrounded renderer must never see the real mouse/keyboard.
            self._controls.mouse_safe = not self._backgrounded
        return self._controls

    def invalidate(self) -> None:
        """Drop the cached control tree - call after any suspected UI change."""
        self._controls = None

    # -- state -------------------------------------------------------------

    def is_busy(self) -> bool | None:
        if self._render_wrapper is None:
            return None
        controls = self._get_controls()
        if controls is None:
            return None
        verdict = work_detector.detect_busy(self._render_wrapper, controls)
        if verdict.busy is None:
            log.debug("busy state unknown: %s (%s)", verdict.reason, verdict.evidence)
        else:
            log.debug("busy=%s (%s)", verdict.busy, verdict.reason)
        return verdict.busy

    def conversation_title(self) -> str:
        """Best-effort name of the conversation currently on screen.

        The selected item in the sidebar conversation list is the only useful
        signal. Measured: the window title is always just "ChatGPT". If no
        selected item can be identified we return '' - and the task lock
        treats that as "cannot verify", which is the safe answer. There is
        deliberately no "first list item" fallback: the first conversation is
        not the current one.
        """
        wrapper = self._render_wrapper
        if wrapper is None:
            return ""
        try:
            items = wrapper.descendants(control_type="ListItem") or []
        except Exception:  # noqa: BLE001
            return ""
        for ctrl in items:
            try:
                name = (ctrl.window_text() or "").strip()
                if not name:
                    continue
                if ctrl.is_selected():
                    return name
            except Exception:  # noqa: BLE001
                continue
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
                log.info("ChatGPT main window appeared")
                return True
            time.sleep(1.0)
        log.warning("ChatGPT did not present a conversation window in time")
        return False

    # -- sending -----------------------------------------------------------

    def send_prompt(self, text: str, *, dry_run: bool = True) -> ResumeResult:
        if not self.is_running():
            return ResumeResult(False, ErrorKind.CHATGPT_NOT_RUNNING, "process missing")
        info = self.find_window()
        if info is None:
            return ResumeResult(False, ErrorKind.WINDOW_NOT_FOUND, "no conversation window")

        controls = self._get_controls()
        if controls is None:
            self.invalidate()
            return ResumeResult(False, ErrorKind.INPUT_NOT_FOUND, "render widget not attached")

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

        outcome = prompt_sender.send_prompt(controls, text, dry_run=False, hwnd=info.handle)
        if outcome.sent:
            self.invalidate()
            return ResumeResult(True, None, f"sent via {outcome.via}")
        self.invalidate()
        return ResumeResult(False, outcome.kind or ErrorKind.SEND_FAILED, outcome.error or "unknown")

    def verify_sent(self, timeout: float = 3.0) -> tuple[bool, str]:
        if self._render_wrapper is None:
            return False, "no render widget attached"
        try:
            return prompt_sender.verify_send_confirmed(self._render_wrapper, timeout)
        except Exception as exc:  # noqa: BLE001
            return False, f"verification failed: {exc}"

    def close(self) -> None:
        self._clear()

    def describe(self) -> str:
        return f"uia (processes={self.process_names})"


def _register() -> None:
    from app.chatgpt.base import register

    register("uia")(ChatGptUiaController)


_register()
