"""Window-level UI Automation.

Measured reality on this machine (worth reading before touching this file):

* ChatGPT Desktop owns **several** top-level windows. One is a thin always-on
  overlay (766x1440, ~18 accessibility nodes, *no composer*); the real
  conversation lives in a **large** window (2146x1055, >1100 nodes). That main
  window may be **minimised** - when minimised its Chromium render widget is
  destroyed and UIA sees nothing.
* The accessibility tree lives on the ``Chrome_RenderWidgetHostHWND`` **child**
  window, not on the top-level window. Attaching to the top-level window only
  shows the outer chrome.
* **Renderer reparenting (measured 2026-10-07):** when the conversation window
  is backgrounded for a while, Chromium can *reparent* the render widget to a
  hidden ``Chrome_WidgetWin_0`` helper window. The visible top-level window
  then becomes a dead shell (no renderer child) while the full conversation
  tree - composer included - stays alive under the hidden owner. So the last
  resort is a *global* renderer scan across all ChatGPT PIDs, keyed by the
  renderer's root ancestor. In that state only the UIA transport may be used:
  the mouse and the keyboard must stay untouched (``mouse_safe=False``).
* The composer *is* addressable: a large ``Edit`` (713x44) with a ValuePattern
  and the placeholder text ``随心输入``; the send button is a ``Button``
  named ``发送`` that is **disabled while the composer is empty**; a quota
  exhausted state surfaces as the text ``你已达到使用上限``.

So: select the *main* window by looking for a composer inside its render
widget, restoring it from minimised first if needed. Everything is defensive:
a UI automation failure degrades to "not found" (which the caller treats as
"do not send"), never to an exception that kills the daemon. Controls are
located by semantic properties - never by screen coordinates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from app.chatgpt.base import WindowInfo
from app.utils.logging_setup import get_logger

log = get_logger("chatgpt.window")

INPUT_CONTROL_TYPES = ("Edit", "Document")
BUTTON_CONTROL_TYPE = "Button"

SEND_HINTS = ("send", "send message", "发送", "发送消息")
STOP_HINTS = ("stop", "stop generating", "cancel", "停止", "停止生成", "取消")
PLACEHOLDER_HINTS = (
    "随心输入",
    "ask anything",
    "message chatgpt",
    "send a message",
    "type a message",
    "输入",
    "有什么可以帮",
    "发送消息",
)

#: The real composer is wide; the search box and the sidebar rename field are
#: all much narrower. This is geometry on the *control*, not a fixed screen
#: coordinate, which is what the brief forbids.
MIN_COMPOSER_WIDTH = 300
MIN_INPUT_WIDTH = 160
MIN_INPUT_HEIGHT = 18

#: A render widget with fewer nodes than this is the overlay, not a
#: conversation window.
MIN_MAIN_TREE_NODES = 50


@dataclass(slots=True)
class LocatedControls:
    window: Any
    input_box: Any | None = None
    send_button: Any | None = None
    stop_button: Any | None = None
    input_enabled: bool | None = None
    render_handle: int = 0
    #: False when the composer lives in a backgrounded (reparented) renderer:
    #: the real mouse and the keyboard must not be touched, only UIA patterns.
    mouse_safe: bool = True


def find_renderer_hwnd(top_handle: int) -> int:
    """The accessibility provider lives on Chromium's render widget, a child
    of the top-level window."""
    import win32gui

    found: list[int] = []

    def callback(child: int, _param) -> bool:
        try:
            if "Chrome_RenderWidgetHost" in win32gui.GetClassName(child):
                found.append(child)
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        win32gui.EnumChildWindows(top_handle, callback, None)
    except Exception:  # noqa: BLE001
        pass
    return found[0] if found else 0


def connect_window(info: WindowInfo) -> Any | None:
    """Attach UI Automation to an existing top-level window."""
    try:
        from pywinauto import Desktop

        return Desktop(backend="uia").window(handle=info.handle)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not attach to window %s: %s", info.handle, exc)
        return None


def connect_render_widget(handle: int) -> Any | None:
    """Attach UI Automation to the Chromium render widget child window."""
    if not handle:
        return None
    try:
        from pywinauto import Desktop

        return Desktop(backend="uia").window(handle=handle)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not attach to render widget %s: %s", handle, exc)
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


def find_input_box(render_widget: Any) -> Any | None:
    """Locate the message composer inside the Chromium render widget.

    Measured shape: a wide ``Edit`` (713x44) that is enabled, keyboard
    focusable, exposes a ValuePattern and carries placeholder text such as
    ``随心输入``. The search box (73x20) and the sidebar rename field are far
    narrower, so a width floor separates them cleanly.
    """
    candidates: list[tuple[int, Any]] = []
    for control_type in INPUT_CONTROL_TYPES:
        found = _safe(
            lambda ct=control_type: render_widget.descendants(control_type=ct), []
        ) or []
        for ctrl in found:
            if not _visible(ctrl):
                continue
            rect = _safe(ctrl.rectangle)
            width = 0
            height = 0
            if rect is not None:
                try:
                    width, height = rect.width(), rect.height()
                except Exception:  # noqa: BLE001
                    pass
            if width and width < MIN_COMPOSER_WIDTH:
                continue
            if height and height < MIN_INPUT_HEIGHT:
                continue

            name = (_safe(ctrl.window_text, "") or "").strip().lower()
            score = 0
            if _safe(lambda c=ctrl: c.is_enabled(), False):
                score += 10
            if _safe(lambda c=ctrl: c.is_keyboard_focusable(), False):
                score += 5
            if _supports_value_pattern(ctrl):
                score += 25
            if any(h in name for h in PLACEHOLDER_HINTS):
                score += 15
            score += min(width // 10, 40)
            candidates.append((score, ctrl))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _supports_value_pattern(ctrl: Any) -> bool:
    try:
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


def _find_button(render_widget: Any, hints: Iterable[str]) -> Any | None:
    hints = tuple(h.lower() for h in hints)
    buttons = _safe(
        lambda: render_widget.descendants(control_type=BUTTON_CONTROL_TYPE), []
    ) or []
    for ctrl in buttons:
        name = (_safe(ctrl.window_text, "") or "").strip().lower()
        if not name:
            name = (_safe(lambda c=ctrl: c.element_info.name, "") or "").lower()
        if not name:
            continue
        if any(h in name for h in hints) and _visible(ctrl):
            return ctrl
    return None


def find_send_button(render_widget: Any) -> Any | None:
    return _find_button(render_widget, SEND_HINTS)


def find_stop_button(render_widget: Any) -> Any | None:
    return _find_button(render_widget, STOP_HINTS)


def _tree_node_count(render_widget: Any) -> int:
    found = _safe(lambda: render_widget.descendants(), None)
    return len(found) if found else 0


def select_main_window(
    candidates: Iterable[WindowInfo], restore_minimised: bool = True
) -> tuple[WindowInfo, int] | None:
    """Pick the window that actually hosts a conversation.

    Rationale: ChatGPT Desktop keeps an always-on overlay window that *never*
    contains a composer, while the real conversation window may be minimised.
    "Non-minimised first" therefore picks the overlay every time. The only
    reliable discriminator is the presence of a composer inside the render
    widget, so each candidate is restored (if needed) and probed.
    """
    for info in candidates:
        render_handle = find_renderer_hwnd(info.handle)
        if not render_handle and restore_minimised and info.minimized:
            if _restore_minimised_handle(info.handle):
                time.sleep(0.8)
                render_handle = find_renderer_hwnd(info.handle)
        if not render_handle:
            continue

        render_widget = connect_render_widget(render_handle)
        if render_widget is None:
            continue
        if _tree_node_count(render_widget) < MIN_MAIN_TREE_NODES:
            log.debug(
                "window %s: render tree too small (%s nodes) - likely the overlay",
                info.handle,
                _tree_node_count(render_widget),
            )
            continue
        composer = find_input_box(render_widget)
        if composer is None:
            log.debug("window %s: no composer found in render tree", info.handle)
            continue
        log.info(
            "main ChatGPT window selected: handle=%s render=%s title=%r",
            info.handle,
            render_handle,
            info.title,
        )
        return info, render_handle

    log.warning("no ChatGPT window exposes a composer")
    return None


def _restore_minimised_handle(hwnd: int) -> bool:
    try:
        import win32con
        import win32gui

        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("could not restore %s: %s", hwnd, exc)
        return False


def find_renderers_for_pids(pids: Iterable[int]) -> list[tuple[int, int]]:
    """Every ``Chrome_RenderWidgetHostHWND`` owned by these processes.

    Returns ``[(renderer_hwnd, root_owner_hwnd)]``. A renderer is NOT always a
    child of the visible conversation window: Chromium reparents backgrounded
    web contents to a hidden helper window, so the scan covers every top-level
    window of the process, visible or not.
    """
    import win32gui
    import win32process

    wanted = {int(p) for p in pids if p}
    renderers: list[int] = []

    def child_cb(child: int, _param) -> bool:
        try:
            if "Chrome_RenderWidgetHost" in win32gui.GetClassName(child):
                renderers.append(child)
        except Exception:  # noqa: BLE001
            pass
        return True

    def top_cb(hwnd: int, _param) -> bool:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid in wanted:
                win32gui.EnumChildWindows(hwnd, child_cb, None)
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        win32gui.EnumWindows(top_cb, None)
    except Exception:  # noqa: BLE001
        pass

    out: list[tuple[int, int]] = []
    for renderer in renderers:
        owner = _safe(lambda r=renderer: win32gui.GetAncestor(r, win32gui.GA_ROOT), 0) or 0
        out.append((renderer, owner))
    return out


def select_orphan_renderer(
    process_names: Iterable[str] = ("ChatGPT.exe",),
) -> tuple[WindowInfo, int] | None:
    """Last resort: the conversation renderer reparented to a hidden window.

    Only used when no *visible* window exposes a composer. The returned
    ``WindowInfo.handle`` is the renderer's root ancestor (usually a hidden
    helper window): fine for UIA patterns, useless for mouse/keyboard - the
    caller must mark the controls ``mouse_safe=False``.
    """
    from app.chatgpt import process_detector

    import win32gui
    import win32process

    pids = process_detector.find_chatgpt_pids(process_names)
    if not pids:
        return None
    for renderer, owner in find_renderers_for_pids(pids):
        widget = connect_render_widget(renderer)
        if widget is None:
            continue
        nodes = _tree_node_count(widget)
        if nodes < MIN_MAIN_TREE_NODES:
            continue
        if find_input_box(widget) is None:
            continue
        _, pid = _safe(lambda: win32process.GetWindowThreadProcessId(owner), (0, 0))
        title = _safe(lambda: win32gui.GetWindowText(owner), "") or "ChatGPT (backgrounded)"
        log.info(
            "conversation renderer found reparented: render=%s owner=%s nodes=%s",
            renderer,
            owner,
            nodes,
        )
        info = WindowInfo(
            handle=owner,
            title=title or "ChatGPT (backgrounded)",
            pid=int(pid or 0),
            process_name="ChatGPT.exe",
            minimized=True,
        )
        return info, renderer
    return None


def locate(render_widget: Any, render_handle: int = 0) -> LocatedControls:
    """Resolve everything the resume flow needs, in one UIA pass."""
    controls = LocatedControls(window=render_widget, render_handle=render_handle)
    controls.input_box = find_input_box(render_widget)
    if controls.input_box is not None:
        controls.input_enabled = _safe(lambda: controls.input_box.is_enabled(), None)
    controls.send_button = find_send_button(render_widget)
    controls.stop_button = find_stop_button(render_widget)
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
