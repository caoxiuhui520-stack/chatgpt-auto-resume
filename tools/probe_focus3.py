"""Probe 3: deepest accessibility attempts on the Chromium render widget.

  A. Attach UIA directly to the Chrome_RenderWidgetHostHWND child window.
  B. Hold a UIA reference and poll the descendant count for several seconds -
     Chromium builds the renderer tree lazily and asynchronously.
  C. oleacc's AccessibleObjectFromWindow via comtypes (managed lifetime).

Run:
    python tools/probe_focus3.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chatgpt import process_detector, window_controller  # noqa: E402
from tools.probe_focus2 import _find_chrome_render_hwnds  # noqa: E402


def _descendant_count(handle: int) -> tuple[int, dict]:
    from collections import Counter

    from pywinauto import Desktop

    wrapper = Desktop(backend="uia").window(handle=handle)
    try:
        descendants = wrapper.descendants()
    except Exception as exc:  # noqa: BLE001
        return -1, {"error": f"{type(exc).__name__}: {exc}"}
    counts: Counter[str] = Counter()
    for ctrl in descendants:
        counts[getattr(ctrl.element_info, "control_type", "?")] += 1
    return len(descendants), dict(counts)


def main() -> int:
    candidates = process_detector.find_chatgpt_windows()
    if not candidates:
        print("no ChatGPT window found")
        return 1
    info = candidates[0]
    window_controller.activate_window(info.handle)

    renderers = _find_chrome_render_hwnds(info.handle)
    print(f"top window: {info.handle}; render widgets: {renderers}")

    # --- A: attach to the render widget directly ---------------------------
    for renderer in renderers:
        try:
            count, hist = _descendant_count(renderer)
        except Exception as exc:  # noqa: BLE001
            print(f"  renderer {renderer}: attach failed ({exc})")
            continue
        print(f"  renderer {renderer}: descendants={count} {hist}")

    # --- B: poll the descendant count while holding a reference ------------
    print("\npolling descendant count on the top window (10s)...")
    previous = -1
    for tick in range(10):
        try:
            count, _hist = _descendant_count(info.handle)
        except Exception as exc:  # noqa: BLE001
            print(f"  t={tick}s: {exc}")
            break
        marker = "" if count == previous else "  <-- changed"
        print(f"  t={tick}s: descendants={count}{marker}")
        previous = count
        time.sleep(1)

    # --- C: oleacc via comtypes ---------------------------------------------
    print("\noleacc AccessibleObjectFromWindow on the render widget:")
    for renderer in renderers or [info.handle]:
        try:
            import comtypes.client

            oleacc = comtypes.client.GetModule("oleacc.dll")
            pacc = comtypes.Pointer(oleacc.IAccessible)()
            hr = comtypes.oledll.oleacc.AccessibleObjectFromWindow(
                renderer, 0, comtypes.byref(oleacc.IAccessible._iid_), comtypes.byref(pacc)
            )
            print(f"  renderer {renderer}: hr={hr} got_object={bool(pacc)}")
            if pacc:
                try:
                    child_count = pacc.accChildCount
                except Exception:  # noqa: BLE001
                    child_count = "?"
                try:
                    role = pacc.accRole(0)
                except Exception:  # noqa: BLE001
                    role = "?"
                print(f"    accChildCount={child_count} role={role}")
        except Exception as exc:  # noqa: BLE001
            print(f"  renderer {renderer}: {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
