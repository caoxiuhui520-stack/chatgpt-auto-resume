"""Locate the ChatGPT desktop process and its main window.

Discovery strategy (revised after measuring on a real install):

* Enumerating the whole desktop through UI Automation proved unreliable here -
  it returned 104 windows, none of them ChatGPT, while Win32 ``EnumWindows``
  returned the two real ``ChatGPT.exe`` top-level windows immediately.
* So the *discovery* step uses Win32 (cheap, exact, no COM), and pywinauto is
  only attached afterwards to the one window we picked, for the accessibility
  tree.

Never hard-codes a PID and never hard-codes a window handle: both change on
every restart. Candidates are ranked by executable-name match, then by window
area (the real app window is much larger than any helper window).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TYPE_CHECKING

from app.chatgpt.base import WindowInfo
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig

log = get_logger("chatgpt.process")

# Titles ChatGPT Desktop has used. Only consulted when the executable name
# could not be resolved.
TITLE_HINTS = ("chatgpt",)

# Windows we must never touch.
TITLE_DENYLIST = (
    "program manager",
    "default ime",
    "windows input experience",
    "msctfime ui",
    "gdi+ window",
    "hidden window",
)


def exe_name_for_pid(pid: int) -> str:
    """Resolve a PID to its executable file name. '' when unavailable."""
    if pid <= 0:
        return ""
    try:
        import psutil  # type: ignore

        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        pass
    try:
        import win32api
        import win32con
        import win32process

        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid
        )
        try:
            path = win32process.GetModuleFileNameEx(handle, 0)
            return path.rsplit("\\", 1)[-1]
        finally:
            win32api.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return ""


@dataclass(slots=True)
class _RawWindow:
    handle: int
    title: str
    pid: int
    process_name: str
    minimized: bool
    area: int


def _enumerate_win32() -> list[_RawWindow]:
    """Top-level visible windows via EnumWindows. No COM, no pywinauto."""
    import win32gui
    import win32process

    found: list[_RawWindow] = []

    def callback(hwnd: int, _param) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if not pid:
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = max(0, right - left), max(0, bottom - top)
            if width <= 1 or height <= 1:
                return True
            found.append(
                _RawWindow(
                    handle=hwnd,
                    title=title.strip(),
                    pid=pid,
                    process_name=exe_name_for_pid(pid),
                    minimized=bool(win32gui.IsIconic(hwnd)),
                    area=width * height,
                )
            )
        except Exception:  # noqa: BLE001 - one bad window must not abort the scan
            pass
        return True

    win32gui.EnumWindows(callback, None)
    return found


def find_chatgpt_windows(
    process_names: Iterable[str] = ("ChatGPT.exe",),
    title_hints: Iterable[str] = TITLE_HINTS,
) -> list[WindowInfo]:
    """Return candidate ChatGPT main windows, best candidate first."""
    wanted = {n.lower() for n in process_names if n}
    hints = tuple(h.lower() for h in title_hints)
    results: list[tuple[int, WindowInfo]] = []

    try:
        raw_windows = _enumerate_win32()
    except Exception as exc:  # noqa: BLE001
        log.warning("win32 window enumeration failed: %s", exc)
        return []

    for raw in raw_windows:
        if raw.title.lower() in TITLE_DENYLIST:
            continue

        name_known = bool(raw.process_name)
        matched_by_process = name_known and raw.process_name.lower() in wanted
        if matched_by_process:
            pass
        elif not name_known and any(h in raw.title.lower() for h in hints):
            # Executable name unreadable: fall back to the title, which is a
            # weaker signal but still far better than nothing.
            log.debug("matching window %r by title (exe name unknown)", raw.title)
        else:
            continue

        info = WindowInfo(
            handle=raw.handle,
            title=raw.title or "ChatGPT",
            pid=raw.pid,
            process_name=raw.process_name,
            minimized=raw.minimized,
        )
        # Rank: exact "chatgpt" title first, then non-minimised, then area.
        score = 0
        if raw.title.strip().lower() == "chatgpt":
            score += 1000
        if not raw.minimized:
            score += 500
        score += min(raw.area // 1000, 499)
        results.append((score, info))

    results.sort(key=lambda item: item[0], reverse=True)
    return [info for _score, info in results]


def find_chatgpt_pids(process_names: Iterable[str] = ("ChatGPT.exe",)) -> set[int]:
    """All live PIDs whose executable name matches. ``set()`` on failure."""
    wanted = {n.lower() for n in process_names if n}
    pids: set[int] = set()
    try:
        import psutil  # type: ignore

        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info.get("name") or "").lower() in wanted:
                    pids.add(proc.pid)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return pids


def is_process_running(process_names: Iterable[str] = ("ChatGPT.exe",)) -> bool:
    """Cheap process check that does not need any window enumeration."""
    wanted = {n.lower() for n in process_names if n}
    try:
        import psutil  # type: ignore

        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info.get("name") or "").lower() in wanted:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(find_chatgpt_windows(process_names))
    except Exception:  # noqa: BLE001
        return False


def describe_candidates(process_names: Iterable[str] = ("ChatGPT.exe",)) -> list[dict]:
    """Diagnostic helper used by `doctor` and the diagnose tool."""
    out: list[dict] = []
    for info in find_chatgpt_windows(process_names):
        out.append(
            {
                "handle": info.handle,
                "title": info.title,
                "pid": info.pid,
                "process": info.process_name,
                "minimized": info.minimized,
            }
        )
    return out
