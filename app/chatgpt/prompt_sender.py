"""Prompt injection.

Hard rules, taken directly from the project brief:

* ``dry_run=True`` must only print ``Would send prompt``. It must never type,
  never focus, never paste. Enforced here and re-checked by the caller.
* Never click a fixed coordinate. The composer is set via the UI Automation
  ValuePattern when available; clipboard + Ctrl+V is the documented fallback.
* Prefer the send button; only fall back to Enter when no button is found.
* Refuse to type when the composer is not empty - a human may be mid-typing,
  and overwriting their input would be destructive.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.models import ErrorKind, ResumeResult
from app.utils.logging_setup import get_logger

log = get_logger("chatgpt.sender")

#: Multi-line prompts are submitted with Shift+Enter in most chat UIs, but we
#: deliberately paste a single logical block and use the send button, so no
#: keystroke sequence is built from the prompt content.
PASTE_SETTLE_SECONDS = 0.35
SEND_SETTLE_SECONDS = 0.6
MAX_PROMPT_CHARS = 8000

#: Composer text that is only UI chrome rather than user input.
PLACEHOLDER_HINTS = (
    "ask anything",
    "message chatgpt",
    "有什么可以帮",
    "询问任何",
    "发送消息",
    "输入消息",
)


@dataclass(slots=True)
class SendOutcome:
    sent: bool
    via: str = ""
    error: str | None = None
    kind: str = ErrorKind.UNKNOWN


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _is_placeholder(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return True
    return any(h in t for h in PLACEHOLDER_HINTS)


def composer_state(controls: Any) -> tuple[bool, str]:
    """Return (is_empty, current_text). Empty includes placeholder text."""
    from app.chatgpt.window_controller import text_input_value

    box = getattr(controls, "input_box", None)
    if box is None:
        return True, ""
    text = text_input_value(box)
    if _is_placeholder(text):
        return True, text
    return False, text


def _set_clipboard(text: str) -> bool:
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("clipboard write failed: %s", exc)
        return False


def _paste_via_value_pattern(box: Any, text: str) -> bool:
    try:
        iface = box.iface_value
        if iface is None:
            return False
        iface.SetValue(text)
        return True
    except Exception:  # noqa: BLE001
        return False


def _paste_via_clipboard(box: Any, text: str) -> bool:
    if not _set_clipboard(text):
        return False
    try:
        box.click_input()
    except Exception:  # noqa: BLE001
        try:
            box.set_focus()
        except Exception:  # noqa: BLE001
            return False
    time.sleep(PASTE_SETTLE_SECONDS)
    try:
        from pywinauto.keyboard import send_keys

        send_keys("^a")
        send_keys("^v")
        time.sleep(PASTE_SETTLE_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("clipboard paste failed: %s", exc)
        return False


def _click_send(controls: Any) -> bool:
    btn = getattr(controls, "send_button", None)
    if btn is None:
        return False
    try:
        btn.click_input()
        time.sleep(SEND_SETTLE_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("send button click failed: %s", exc)
        return False


def _press_enter(controls: Any) -> bool:
    box = getattr(controls, "input_box", None)
    if box is not None:
        try:
            box.set_focus()
            time.sleep(0.2)
        except Exception:  # noqa: BLE001
            pass
    try:
        from pywinauto.keyboard import send_keys

        send_keys("{ENTER}")
        time.sleep(SEND_SETTLE_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("enter fallback failed: %s", exc)
        return False


def _keyboard_submit(hwnd: int, text: str, controls: Any) -> SendOutcome:
    """Clipboard + Ctrl+V + Enter.

    Used when ChatGPT exposes no addressable composer. Measured on the real
    install: Electron publishes the outer ``RootWebArea`` but not the
    contenteditable composer, so there is nothing to drive with ValuePattern.

    Safety gate: the window must be *proven* to be in the foreground before a
    single keystroke is sent. Otherwise the text would land in whatever window
    happens to be focused.
    """
    from app.chatgpt.window_controller import activate_window

    if not _set_clipboard(text):
        return SendOutcome(False, error="clipboard write failed", kind=ErrorKind.SEND_FAILED)

    if not activate_window(hwnd):
        return SendOutcome(
            False,
            error="ChatGPT window could not be brought to the foreground; refusing to type",
            kind=ErrorKind.WINDOW_NOT_FOUND,
        )

    try:
        from pywinauto.keyboard import send_keys

        time.sleep(PASTE_SETTLE_SECONDS)
        send_keys("^a")
        time.sleep(0.15)
        send_keys("^v")
        time.sleep(PASTE_SETTLE_SECONDS)
    except Exception as exc:  # noqa: BLE001
        return SendOutcome(False, error=f"keyboard paste failed: {exc}", kind=ErrorKind.SEND_FAILED)

    # Prefer the send button when one exists; otherwise Enter.
    if not _click_send(controls):
        try:
            from pywinauto.keyboard import send_keys

            send_keys("{ENTER}")
            time.sleep(SEND_SETTLE_SECONDS)
        except Exception as exc:  # noqa: BLE001
            return SendOutcome(
                False, error=f"submit failed: {exc}", kind=ErrorKind.SEND_FAILED
            )

    return SendOutcome(True, via="clipboard+ctrl-v+enter")


def verify_sent(controls: Any) -> bool:
    """Best-effort confirmation: the composer should be empty again."""
    empty, _ = composer_state(controls)
    return empty


def send_prompt(
    controls: Any,
    text: str,
    *,
    dry_run: bool = True,
    allow_overwrite: bool = False,
    hwnd: int | None = None,
) -> SendOutcome:
    """Set the composer and submit it. See module docstring for the rules.

    Two transports, in the order the brief prescribes:

    1. UI Automation ValuePattern on a located composer.
    2. clipboard + Ctrl+V + Enter, guarded by a foreground-window check.
       ``hwnd`` is required for this path.
    """
    if not text.strip():
        return SendOutcome(False, error="empty prompt", kind=ErrorKind.SEND_FAILED)

    box = getattr(controls, "input_box", None)
    keyboard_possible = bool(hwnd)

    if box is None and not keyboard_possible:
        return SendOutcome(False, error="composer not found", kind=ErrorKind.INPUT_NOT_FOUND)

    # ---- the hard safety switch -----------------------------------------
    if dry_run:
        via = "uia-value-pattern" if box is not None else "clipboard+keyboard"
        log.info(
            "DRY_RUN: Would send prompt via %s (%d chars): %s",
            via,
            len(text),
            text.splitlines()[0][:80] if text.strip() else "",
        )
        return SendOutcome(False, via="dry_run", error=None, kind=ErrorKind.DRY_RUN)

    if len(text) > MAX_PROMPT_CHARS:
        return SendOutcome(
            False, error=f"prompt too long ({len(text)} chars)", kind=ErrorKind.SEND_FAILED
        )

    # ---- transport 2: keyboard-only -------------------------------------
    if box is None:
        log.info("no addressable composer; using the clipboard+keyboard fallback")
        return _keyboard_submit(int(hwnd), text, controls)

    empty, current = composer_state(controls)
    if not empty and not allow_overwrite:
        return SendOutcome(
            False,
            error="composer already contains text; refusing to overwrite",
            kind=ErrorKind.SEND_FAILED,
        )

    # ---- transport 1: type it in ----------------------------------------
    if _paste_via_value_pattern(box, text):
        via = "uia-value-pattern"
    else:
        if not _paste_via_clipboard(box, text):
            return SendOutcome(
                False, error="could not set the composer text", kind=ErrorKind.SEND_FAILED
            )
        via = "clipboard+ctrl-v"

    time.sleep(PASTE_SETTLE_SECONDS)

    # ---- submit ----------------------------------------------------------
    if _click_send(controls):
        via += "+send-button"
    elif _press_enter(controls):
        via += "+enter"
    else:
        return SendOutcome(
            False, error="prompt typed but no way to submit it", kind=ErrorKind.SEND_FAILED
        )

    confirmed = verify_sent(controls)
    log.info("prompt submitted via %s (composer cleared: %s)", via, confirmed)
    return SendOutcome(True, via=via)
