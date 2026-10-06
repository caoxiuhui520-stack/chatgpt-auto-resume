"""Inspect the accessibility tree of the real ChatGPT window.

Verifies, before DRY_RUN is ever turned off, that the composer and the send
button can actually be located. Prints every Edit/Document/Button control with
its size, enabled state and accessible name.

    python tools/inspect_controls.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chatgpt import process_detector, window_controller, work_detector  # noqa: E402


def main() -> int:
    print("=== candidate windows (Win32 enumeration) ===")
    candidates = process_detector.find_chatgpt_windows()
    if not candidates:
        print("no ChatGPT window found")
        return 1
    for info in candidates:
        print(
            f"  handle={info.handle:<10} pid={info.pid:<8} "
            f"minimized={info.minimized!s:<6} title={info.title!r}"
        )

    info = candidates[0]
    print(f"\nattaching UIA to handle={info.handle} ...")
    wrapper = window_controller.connect_window(info)
    if wrapper is None:
        print("attach failed")
        return 1

    window_controller.restore_window(wrapper, info)

    try:
        descendants = wrapper.descendants()
    except Exception as exc:  # noqa: BLE001
        print(f"descendants() failed: {type(exc).__name__}: {exc}")
        return 1

    print(f"descendant count: {len(descendants)}")
    counts: Counter[str] = Counter()
    for ctrl in descendants:
        counts[getattr(ctrl.element_info, "control_type", "?")] += 1
    print("control type histogram:")
    for ctype, count in sorted(counts.items()):
        print(f"  {ctype:<20} {count}")

    print("\n-- Edit / Document / Button controls --")
    shown = 0
    for ctrl in descendants:
        ctype = getattr(ctrl.element_info, "control_type", "?")
        if ctype not in ("Edit", "Document", "Button"):
            continue
        name = ctrl.window_text() or ""
        auto = getattr(ctrl.element_info, "automation_id", "") or ""
        try:
            enabled = "enabled" if ctrl.is_enabled() else "DISABLED"
        except Exception:  # noqa: BLE001
            enabled = "?"
        try:
            rect = ctrl.rectangle()
            size = f"{rect.width()}x{rect.height()}"
        except Exception:  # noqa: BLE001
            size = "?"
        print(f"  {ctype:<9} {size:<12} {enabled:<9} name={name[:70]!r} auto_id={auto[:40]!r}")
        shown += 1
        if shown >= 60:
            print("  ... truncated")
            break

    print("\n=== resolver verdict ===")
    controls = window_controller.locate(wrapper)
    print(f"  input_box   : {'FOUND' if controls.input_box else 'NOT FOUND'}")
    if controls.input_box is not None:
        try:
            rect = controls.input_box.rectangle()
            print(f"                size={rect.width()}x{rect.height()}")
        except Exception:  # noqa: BLE001
            pass
        print(f"                enabled={controls.input_enabled}")
        print(f"                value_pattern={window_controller._supports_value_pattern(controls.input_box)}")
        current = window_controller.text_input_value(controls.input_box)
        print(f"                current text={current[:80]!r}")
    print(f"  send_button : {'FOUND' if controls.send_button else 'NOT FOUND'}")
    print(f"  stop_button : {'FOUND' if controls.stop_button else 'NOT FOUND'}")

    verdict = work_detector.detect_busy(wrapper, controls)
    print(f"  busy        : {verdict.busy} ({verdict.reason}; evidence={verdict.evidence})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
