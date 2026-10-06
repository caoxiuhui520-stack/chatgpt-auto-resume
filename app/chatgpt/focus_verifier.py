"""Positive proof of composer focus before any keyboard input.

The keyboard channel (clipboard + Ctrl+V + Enter) types into whatever window
and control currently owns the focus. "We activated the window" is necessary
but NOT sufficient: the DOM focus inside the Chromium page might sit on a
link, a button or nowhere, and Ctrl+V would then paste into the wrong place
or nowhere.

So before a single keystroke this module demands positive proof:

1. the ChatGPT main window is the foreground window (Win32), **and**
2. the composer can be located, **and**
3. after an explicit ``set_focus`` on the composer, UIA reports the focused
   element *is* the composer (``CompareElements``), **and**
4. the Win32 GUI-thread focus sits inside the ChatGPT window tree.

If the composer cannot be located at all, proof is impossible by definition
and the caller must refuse to type (``FOCUS_UNVERIFIED``). Everything here
is read-only except the explicit ``set_focus`` call; failures degrade to
"not proved", never to an exception.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import asdict, dataclass
from typing import Any

from app.utils.logging_setup import get_logger

log = get_logger("chatgpt.focus")

FOCUS_SETTLE_SECONDS = 0.3


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("hwndActive", ctypes.c_void_p),
        ("hwndFocus", ctypes.c_void_p),
        ("hwndCapture", ctypes.c_void_p),
        ("hwndMenuOwner", ctypes.c_void_p),
        ("hwndMoveSize", ctypes.c_void_p),
        ("hwndCaret", ctypes.c_void_p),
        ("rcCaret", ctypes.c_long * 4),
    ]


@dataclass(slots=True)
class FocusProof:
    """The evidence trail of one focus verification. ``proved`` is the only
    field the send path may consult; the rest exists for diagnostics."""

    proved: bool = False
    reason: str = "not evaluated"
    foreground_hwnd: int = 0
    foreground_ok: bool = False
    focus_hwnd: int = 0
    focus_class: str = ""
    focus_in_window_tree: bool = False
    composer_located: bool = False
    composer_focused: bool = False
    uia_focused_control_type: str = ""
    uia_focused_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _gui_thread_focus(top_hwnd: int) -> tuple[int, str]:
    """(hwndFocus, class name) of the GUI thread that owns ``top_hwnd``."""
    try:
        import win32gui
        import win32process

        tid = win32process.GetWindowThreadProcessId(top_hwnd)[0]
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not ctypes.windll.user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return 0, ""
        hwnd = int(info.hwndFocus or 0)
        klass = _safe(lambda: win32gui.GetClassName(hwnd), "") if hwnd else ""
        return hwnd, klass or ""
    except Exception:  # noqa: BLE001
        return 0, ""


def _uia_focused_element() -> Any | None:
    """The raw IUIAutomationElement that currently has keyboard focus."""
    try:
        from pywinauto.uia_defines import IUIA

        return IUIA().get_focused_element()
    except Exception:  # noqa: BLE001
        return None


def _control_type_name(element: Any) -> str:
    try:
        from pywinauto.uia_defines import IUIA

        return IUIA().known_control_type_ids.get(element.CurrentControlType, "?")
    except Exception:  # noqa: BLE001
        return "?"


def _elements_equal(a: Any, b: Any) -> bool:
    try:
        from pywinauto.uia_defines import IUIA

        return bool(IUIA().iuia.CompareElements(a, b))
    except Exception:  # noqa: BLE001
        return False


def verify_composer_focus(
    top_hwnd: int,
    render_hwnd: int = 0,
    composer: Any | None = None,
    *,
    render_widget: Any | None = None,
    try_set_focus: bool = True,
) -> FocusProof:
    """Prove that typing right now would land in the ChatGPT composer.

    ``composer`` may be ``None`` (the keyboard fallback runs exactly when no
    composer was found); in that case one re-location attempt is made on
    ``render_widget`` when provided. Without a located composer the proof is
    impossible and the result is ``proved=False`` - by design.
    """
    import win32gui

    proof = FocusProof()

    # ---- 1. foreground ---------------------------------------------------
    proof.foreground_hwnd = _safe(win32gui.GetForegroundWindow, 0) or 0
    proof.foreground_ok = proof.foreground_hwnd == top_hwnd
    if not proof.foreground_ok:
        proof.reason = (
            f"foreground window is {proof.foreground_hwnd}, not the ChatGPT "
            f"window {top_hwnd}"
        )
        return proof

    # ---- 2. locate the composer -----------------------------------------
    if composer is None and render_widget is not None:
        from app.chatgpt.window_controller import find_input_box

        composer = _safe(lambda: find_input_box(render_widget), None)
    proof.composer_located = composer is not None
    if composer is None:
        proof.reason = "composer could not be located; focus cannot be proven"
        return proof

    # ---- 3. put the focus on the composer and read it back ---------------
    if try_set_focus:
        _safe(composer.set_focus)
        time.sleep(FOCUS_SETTLE_SECONDS)

    focused = _uia_focused_element()
    if focused is None:
        proof.reason = "UIA reported no focused element"
        return proof
    proof.uia_focused_control_type = _control_type_name(focused)
    proof.uia_focused_name = _safe(lambda: str(focused.CurrentName or ""), "") or ""

    composer_element = _safe(lambda: composer.element_info.element, None)
    proof.composer_focused = bool(
        composer_element is not None and _elements_equal(focused, composer_element)
    )
    if not proof.composer_focused:
        proof.reason = (
            f"focus is on {proof.uia_focused_control_type}"
            f" {proof.uia_focused_name!r}, not on the composer"
        )
        return proof

    # ---- 4. Win32 cross-check: focus belongs to this window's tree -------
    proof.focus_hwnd, proof.focus_class = _gui_thread_focus(top_hwnd)
    if proof.focus_hwnd:
        proof.focus_in_window_tree = bool(
            proof.focus_hwnd == top_hwnd
            or (render_hwnd and proof.focus_hwnd == render_hwnd)
            or _safe(lambda: win32gui.IsChild(top_hwnd, proof.focus_hwnd), False)
        )
        if not proof.focus_in_window_tree:
            proof.reason = (
                f"GUI thread focus {proof.focus_hwnd} ({proof.focus_class}) is "
                f"outside the ChatGPT window tree"
            )
            return proof
    # A zero hwndFocus is tolerated here: UIA has already positively proven
    # the composer holds focus, and Chromium sometimes reports no HWND-level
    # focus target.

    proof.proved = True
    proof.reason = (
        f"composer holds focus (uia={proof.uia_focused_control_type} "
        f"{proof.uia_focused_name!r}, focus_hwnd={proof.focus_hwnd} "
        f"{proof.focus_class})"
    )
    return proof


def describe_current_focus(top_hwnd: int) -> dict[str, Any]:
    """Passive snapshot of the focus state, for ``diagnose-ui``.

    Read-only: nothing is activated and no focus is moved.
    """
    import win32gui

    description: dict[str, Any] = {
        "foreground_hwnd": _safe(win32gui.GetForegroundWindow, 0) or 0,
        "foreground_title": "",
        "foreground_class": "",
        "foreground_is_target": False,
        "focus_hwnd": 0,
        "focus_class": "",
        "focus_in_window_tree": False,
        "uia_focused_control_type": "",
        "uia_focused_name": "",
        "uia_focused_automation_id": "",
    }
    fg = description["foreground_hwnd"]
    if fg:
        description["foreground_title"] = _safe(lambda: win32gui.GetWindowText(fg), "") or ""
        description["foreground_class"] = _safe(lambda: win32gui.GetClassName(fg), "") or ""
        description["foreground_is_target"] = fg == top_hwnd

    focus_hwnd, focus_class = _gui_thread_focus(top_hwnd)
    description["focus_hwnd"] = focus_hwnd
    description["focus_class"] = focus_class
    if focus_hwnd:
        description["focus_in_window_tree"] = bool(
            focus_hwnd == top_hwnd
            or _safe(lambda: win32gui.IsChild(top_hwnd, focus_hwnd), False)
        )

    focused = _uia_focused_element()
    if focused is not None:
        description["uia_focused_control_type"] = _control_type_name(focused)
        description["uia_focused_name"] = (
            _safe(lambda: str(focused.CurrentName or ""), "") or ""
        )
        description["uia_focused_automation_id"] = (
            _safe(lambda: str(focused.CurrentAutomationId or ""), "") or ""
        )
    return description
