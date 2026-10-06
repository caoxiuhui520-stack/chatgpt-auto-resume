"""Window-level UI Automation.

Everything here is defensive: a UI automation failure must degrade to
"not found" (which the caller treats as "do not send"), never to an exception
that kills the daemon. Controls are located by *semantic* properties
(control type, accessible name, enabled state) - never by screen coordinates,
which the project brief explicitly forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.chatgpt.base import WindowInfo
from app.utils.logging_setup import get_logger

log = get_logger("chatgpt.window")

INPUT_CONTROL_TYPES = ("Edit", "Document")
BUTTON_CONTROL_TYPE = "Button"

SEND_HINTS = ("send", "send message", "发送", "发送消息")
STOP_HINTS = ("stop", "stop generating", "cancel", "停止", "停止生成", "取消")

#: Ignore tiny controls: the search box, the sidebar rename field and other
#: incidental edit controls are all much narrower than the composer.
MIN_INPUT_WIDTH = 160
MIN_INPUT_HEIGHT = 18


@dataclass(slots=True)
class LocatedControls:
    window: Any
    input_box: Any | None = None
    send_button: Any | None = None
    stop_button: Any | None = None
    input_enabled: bool | None = None


def connect_window(info: WindowInfo) -> Any | None:
    """Attach UI Automation to an existing top-level window."""
    try:
        from pywinauto import Desktop

        return Desktop(backend="uia").window(handle=info.handle)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not attach to window %s: %s", info.handle, exc)
        return None


def restore_window(window: Any, info: WindowInfo | None = None) -> bool:
    """Bring the window back from minimised and focus it."""
    try:
        if info is not None and info.minimized:
            window.restore()
        else:
            try:
                if window.is_minimized():
                    window.restore()
            except Exception:  # noqa: BLE001
                pass
        try:
            window.set_focus()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("restore_window failed: %s", exc)
        return False


def activate_window(hwnd: int, timeout: float = 3.0) -> bool:
    """Force ``hwnd`` to the foreground and *verify* it got there.

    This is the safety valve for the keyboard fallback path: if we cannot
    prove the ChatGPT window is the foreground window, the caller must not
    type anything, because the keystrokes would land somewhere else.

    Windows refuses ``SetForegroundWindow`` from a background process, so a
    short AttachThreadInput dance is used when the naive call fails.
    """
    import time

    try:
        import win32con
        import win32gui
        import win32process
    except ImportError:  # pragma: no cover
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            # AttachThreadInput workaround for the foreground lock.
            try:
                fg = win32gui.GetForegroundWindow()
                target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
                fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
                import win32api

                cur_thread = win32api.GetCurrentThreadId()
                for thread in {fg_thread, cur_thread}:
                    if thread and thread != target_thread:
                        win32gui.AttachThreadInput(thread, target_thread, True)
                try:
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                finally:
                    for thread in {fg_thread, cur_thread}:
                        if thread and thread != target_thread:
                            win32gui.AttachThreadInput(thread, target_thread, False)
            except Exception as exc2:  # noqa: BLE001
                log.debug("foreground activation attempt failed: %s", exc2)
        time.sleep(0.2)

    ok = False
    try:
        ok = win32gui.GetForegroundWindow() == hwnd
    except Exception:  # noqa: BLE001
        pass
    if not ok:
        log.warning("could not bring window %s to the foreground", hwnd)
    return ok


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _size_ok(ctrl: Any) -> bool:
    rect = _safe(ctrl.rectangle)
    if rect is None:
        return True  # unknown geometry: do not exclude
    try:
        return rect.width() >= MIN_INPUT_WIDTH and rect.height() >= MIN_INPUT_HEIGHT
    except Exception:  # noqa: BLE001
        return True


def _visible(ctrl: Any) -> bool:
    return bool(_safe(ctrl.is_visible, False))


def find_input_box(window: Any) -> Any | None:
    """Locate the message composer.

    Preference order:
      1. a large Edit/Document that is enabled and exposes ValuePattern
      2. a large Edit/Document at all
    """
    candidates: list[tuple[int, Any]] = []
    for control_type in INPUT_CONTROL_TYPES:
        found = _safe(lambda ct=control_type: window.descendants(control_type=ct), []) or []
        for ctrl in found:
            if not _visible(ctrl) or not _size_ok(ctrl):
                continue
            score = 0
            if _safe(lambda c=ctrl: c.is_enabled(), False):
                score += 10
            if _safe(lambda c=ctrl: c.is_keyboard_focusable(), False):
                score += 5
            # ValuePattern support means we can set the text without the clipboard.
            if _supports_value_pattern(ctrl):
                score += 20
            try:
                rect = ctrl.rectangle()
                score += min(int(rect.height()), 300)
            except Exception:  # noqa: BLE001
                pass
            candidates.append((score, ctrl))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _supports_value_pattern(ctrl: Any) -> bool:
    try:
        from pywinauto.controls.uiawrapper import UIAWrapper  # noqa: F401

        patterns = getattr(ctrl, "get_supported_patterns", None)
        if callable(patterns):
            return "ValuePattern" in patterns()
    except Exception:  # noqa: BLE001
        pass
    try:
        iface = ctrl.iface_value  # noqa: B010
        return iface is not None
    except Exception:  # noqa: BLE001
        return False


def _find_button(window: Any, hints: Iterable[str]) -> Any | None:
    hints = tuple(h.lower() for h in hints)
    buttons = _safe(lambda: window.descendants(control_type=BUTTON_CONTROL_TYPE), []) or []
    for ctrl in buttons:
        name = (_safe(ctrl.window_text, "") or "").strip().lower()
        if not name:
            name = (_safe(lambda c=ctrl: c.element_info.name, "") or "").lower()
        if not name:
            continue
        if any(h in name for h in hints) and _visible(ctrl) and _safe(
            lambda c=ctrl: c.is_enabled(), False
        ):
            return ctrl
    return None


def find_send_button(window: Any) -> Any | None:
    return _find_button(window, SEND_HINTS)


def find_stop_button(window: Any) -> Any | None:
    return _find_button(window, STOP_HINTS)


def locate(window: Any) -> LocatedControls:
    """Resolve everything the resume flow needs, in one UIA pass."""
    controls = LocatedControls(window=window)
    controls.input_box = find_input_box(window)
    if controls.input_box is not None:
        controls.input_enabled = _safe(lambda: controls.input_box.is_enabled(), None)
    controls.send_button = find_send_button(window)
    controls.stop_button = find_stop_button(window)
    return controls


def text_input_value(ctrl: Any) -> str:
    """Current text in a composer, best effort. '' when unknown."""
    if ctrl is None:
        return ""
    try:
        iface = ctrl.iface_value
        if iface is not None:
            return str(iface.CurrentValue or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(ctrl.window_text() or "")
    except Exception:  # noqa: BLE001
        return ""
