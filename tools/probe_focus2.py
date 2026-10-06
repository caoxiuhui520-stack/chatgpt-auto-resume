"""Probe 2: force Chromium renderer accessibility, then re-read focus.

Chromium enables its accessibility tree lazily. Sending ``WM_GETOBJECT`` with
``OBJID_CLIENT`` - which is exactly what ``AccessibleObjectFromWindow`` does -
flips that switch for the running process without restarting it.

Run:
    python tools/probe_focus2.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import POINTER, byref, c_long
from ctypes.wintypes import BOOL, DWORD, HWND
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chatgpt import process_detector, window_controller  # noqa: E402

OBJID_CLIENT = 0x00000000
WM_GETOBJECT = 0x003D


def _find_chrome_render_hwnds(hwnd: int) -> list[int]:
    """The accessibility provider lives on Chromium's render widget, a child
    of the top-level window - not on the top-level window itself."""
    import win32gui

    found: list[int] = []

    def callback(child: int, _param) -> bool:
        try:
            cls = win32gui.GetClassName(child)
            if "Chrome_RenderWidgetHost" in cls:
                found.append(child)
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, callback, None)
    except Exception:  # noqa: BLE001
        pass
    return found


def _trigger_accessibility(hwnd: int) -> tuple[bool, list[int]]:
    """Send WM_GETOBJECT/OBJID_CLIENT to wake Chromium accessibility.

    Tries the render widget first (the real provider), then the top-level
    window. The act of sending is what flips Chromium into accessibility
    mode; the LRESULT is not dereferenced.
    """
    targets = _find_chrome_render_hwnds(hwnd) + [hwnd]
    ok = False
    for target in targets:
        try:
            result = ctypes.windll.user32.SendMessageW(
                HWND(target), DWORD(WM_GETOBJECT), 0, ctypes.c_long(OBJID_CLIENT)
            )
            if result:
                ok = True
        except Exception:  # noqa: BLE001
            continue
    return ok, targets


def _rect_of(element) -> str:
    try:
        rect = element.CurrentBoundingRectangle
        left = getattr(rect, "left", None)
        top = getattr(rect, "top", None)
        right = getattr(rect, "right", None)
        bottom = getattr(rect, "bottom", None)
        if None in (left, top, right, bottom):
            return "?"
        return f"{right - left}x{bottom - top} @({left},{top})"
    except Exception:  # noqa: BLE001
        return "?"


def _describe(element, label: str) -> None:
    def get(attr: str):
        try:
            return getattr(element, attr)
        except Exception:  # noqa: BLE001
            return "<unavailable>"

    ctype = get("CurrentControlType")
    print(
        f"  {label:<12} type_id={ctype!s:<9} rect={_rect_of(element):<26} "
        f"enabled={get('CurrentIsEnabled')!s:<6} kb={get('CurrentIsKeyboardFocusable')!s:<6}\n"
        f"  {'':<12} name={str(get('CurrentName'))[:70]!r}\n"
        f"  {'':<12} auto={str(get('CurrentAutomationId'))[:40]!r} "
        f"class={str(get('CurrentClassName'))[:40]!r}"
    )


def main() -> int:
    candidates = process_detector.find_chatgpt_windows()
    if not candidates:
        print("no ChatGPT window found")
        return 1
    info = candidates[0]
    print(f"target window: handle={info.handle} title={info.title!r}")

    window_controller.activate_window(info.handle)
    time.sleep(0.4)

    triggered, targets = _trigger_accessibility(info.handle)
    print(f"accessibility trigger targets: {targets}")
    print(f"accessibility trigger (WM_GETOBJECT): {'ok' if triggered else 'FAILED'}")

    # Chromium takes a moment to build the tree after the first request.
    for attempt in (0.5, 1.0, 1.5):
        time.sleep(attempt)
        pass

    from pywinauto.uia_defines import IUIA

    uia = IUIA().iuia
    focused = uia.GetFocusedElement()
    if focused is None:
        print("GetFocusedElement returned None after the trigger")
        return 1

    print("\n=== focused element (post-trigger) ===")
    _describe(focused, "focused")

    print("\n=== ancestors (post-trigger) ===")
    walker = uia.ControlViewWalker
    element = focused
    for depth in range(8):
        try:
            parent = walker.GetParentElement(element)
        except Exception:  # noqa: BLE001
            break
        if parent is None:
            break
        _describe(parent, f"parent+{depth + 1}")
        element = parent

    # Also count how many descendants the window now exposes.
    try:
        from pywinauto import Desktop

        wrapper = Desktop(backend="uia").window(handle=info.handle)
        descendants = wrapper.descendants()
        print(f"\nwindow descendant count now: {len(descendants)}")
    except Exception as exc:  # noqa: BLE001
        print(f"\ndescendant count failed: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
