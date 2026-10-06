"""Diagnostic: enumerate top-level windows and match them against processes.

Run when the daemon reports ``chatgpt_running: true`` but no window is found.

    python tools/diagnose_windows.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chatgpt.process_detector import exe_name_for_pid  # noqa: E402


def main() -> int:
    from pywinauto import Desktop

    print("backend: uia")
    desktop = Desktop(backend="uia")
    try:
        windows = desktop.windows()
    except Exception as exc:  # noqa: BLE001
        print(f"enumeration failed: {type(exc).__name__}: {exc}")
        return 1

    print(f"total top-level windows: {len(windows)}\n")
    by_exe: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for wrapper in windows:
        try:
            title = wrapper.window_text() or ""
            pid = int(getattr(wrapper, "process_id", 0) or 0)
            handle = int(getattr(wrapper, "handle", 0) or 0)
            name = exe_name_for_pid(pid) or "?"
            by_exe[name].append((handle, title))
        except Exception:  # noqa: BLE001
            continue

    for exe in sorted(by_exe, key=str.lower):
        entries = by_exe[exe]
        print(f"{exe}  ({len(entries)} windows)")
        for handle, title in entries[:12]:
            shown = title if title.strip() else "<EMPTY TITLE>"
            print(f"    handle={handle:<12} title={shown!r}")
        if len(entries) > 12:
            print(f"    ... {len(entries) - 12} more")
        print()

    # Focused view: anything ChatGPT-like.
    print("=== ChatGPT-like windows ===")
    hits = 0
    for exe, entries in by_exe.items():
        if "chatgpt" not in exe.lower():
            continue
        for handle, title in entries:
            hits += 1
            print(f"  exe={exe} handle={handle} title={title!r}")
    if not hits:
        print("  none found by exe name")

    # Inspect the richest ChatGPT window's control tree so the composer and
    # send button can be identified.
    for wrapper in windows:
        try:
            pid = int(getattr(wrapper, "process_id", 0) or 0)
            name = (exe_name_for_pid(pid) or "").lower()
            if "chatgpt" not in name:
                continue
            title = wrapper.window_text() or ""
            print(f"\n=== control tree of handle={int(wrapper.handle)} title={title!r} ===")
            try:
                descendants = wrapper.descendants()
            except Exception as exc:  # noqa: BLE001
                print(f"  descendants failed: {exc}")
                continue
            print(f"  descendant count: {len(descendants)}")
            counts: dict[str, int] = defaultdict(int)
            for ctrl in descendants:
                ctype = getattr(ctrl.element_info, "control_type", "?")
                counts[ctype] += 1
            for ctype, count in sorted(counts.items()):
                print(f"  {ctype:<20} {count}")
            print("  -- named Edit / Document / Button controls --")
            shown = 0
            for ctrl in descendants:
                ctype = getattr(ctrl.element_info, "control_type", "?")
                if ctype not in ("Edit", "Document", "Button"):
                    continue
                name = ctrl.window_text() or ""
                auto = getattr(ctrl.element_info, "automation_id", "") or ""
                enabled = ""
                try:
                    enabled = "enabled" if ctrl.is_enabled() else "disabled"
                except Exception:  # noqa: BLE001
                    pass
                try:
                    rect = ctrl.rectangle()
                    size = f"{rect.width()}x{rect.height()}"
                except Exception:  # noqa: BLE001
                    size = "?"
                print(
                    f"    {ctype:<9} size={size:<12} {enabled:<9} "
                    f"name={name[:60]!r} auto_id={auto[:40]!r}"
                )
                shown += 1
                if shown >= 40:
                    print("    ... truncated")
                    break
            break
        except Exception:  # noqa: BLE001
            continue

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
