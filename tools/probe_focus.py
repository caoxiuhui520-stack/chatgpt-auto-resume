"""Probe: can UI Automation see the *focused* element inside ChatGPT?

Two ideas are tested, in order of preference:

1. ``IUIAutomation::GetFocusedElement()`` - works even when the accessibility
   tree is otherwise incomplete, because Chromium exposes the focused
   renderer element through the UIA provider.
2. The accessibility tree of the focused element's ancestors, to see how much
   semantic context is available.

Run:
    python tools/probe_focus.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chatgpt import process_detector, window_controller  # noqa: E402

UIA_CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
}


def _ctype(value: int) -> str:
    return UIA_CONTROL_TYPES.get(value, f"?{value}")


def _describe(element, label: str = "") -> None:
    try:
        name = element.CurrentName
    except Exception:  # noqa: BLE001
        name = "<unavailable>"
    try:
        ctype = _ctype(element.CurrentControlType)
    except Exception:  # noqa: BLE001
        ctype = "?"
    try:
        auto = element.CurrentAutomationId
    except Exception:  # noqa: BLE001
        auto = "<unavailable>"
    try:
        cls = element.CurrentClassName
    except Exception:  # noqa: BLE001
        cls = "<unavailable>"
    try:
        rect = element.CurrentBoundingRectangle
        size = f"{rect.width}x{rect.height} @({rect.left},{rect.top})"
    except Exception:  # noqa: BLE001
        size = "?"
    try:
        enabled = element.CurrentIsEnabled
    except Exception:  # noqa: BLE001
        enabled = "?"
    try:
        kbd = element.CurrentIsKeyboardFocusable
    except Exception:  # noqa: BLE001
        kbd = "?"
    print(
        f"  {label:<14} type={ctype:<10} size={size:<26} "
        f"enabled={enabled!s:<6} kb_focusable={kbd!s:<6}\n"
        f"  {'':<14} name={name[:70]!r}\n"
        f"  {'':<14} auto_id={auto[:50]!r} class={cls[:50]!r}"
    )


def main() -> int:
    candidates = process_detector.find_chatgpt_windows()
    if not candidates:
        print("no ChatGPT window found")
        return 1
    info = candidates[0]
    print(f"target window: handle={info.handle} title={info.title!r}")

    # Bring it to the front first - Chromium only publishes the focused
    # element after the window itself is foregrounded.
    ok = window_controller.activate_window(info.handle)
    print(f"foreground activation: {'ok' if ok else 'FAILED'}")
    time.sleep(0.8)

    try:
        from pywinauto.uia_defines import IUIA
    except ImportError as exc:  # pragma: no cover
        print(f"pywinauto UIA interface unavailable: {exc}")
        return 1

    uia = IUIA().iuia
    try:
        focused = uia.GetFocusedElement()
    except Exception as exc:  # noqa: BLE001
        print(f"GetFocusedElement failed: {type(exc).__name__}: {exc}")
        return 1

    if focused is None:
        print("GetFocusedElement returned None")
        return 1

    print("\n=== focused element ===")
    _describe(focused, "focused")

    # Walk up a few ancestors to see what the focus lives inside.
    print("\n=== ancestors ===")
    walker = uia.ControlViewWalker
    element = focused
    for depth in range(6):
        try:
            parent = walker.GetParentElement(element)
        except Exception:  # noqa: BLE001
            parent = None
        if parent is None:
            break
        _describe(parent, f"parent+{depth + 1}")
        element = parent

    # Quick conclusion.
    print("\n=== verdict ===")
    try:
        ctype = focused.CurrentControlType
        cls = (focused.CurrentClassName or "").lower()
        name = (focused.CurrentName or "").lower()
        looks_like_composer = ctype in (50004, 50030, 50026) or any(
            hint in cls for hint in ("editor", "textbox", "richtext")
        ) or any(hint in name for hint in ("message", "prompt", "compose", "chat"))
        print(f"  control_type_id={ctype} class={cls!r}")
        print(f"  looks_like_composer={looks_like_composer}")
    except Exception:  # noqa: BLE001
        print("  could not classify")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
