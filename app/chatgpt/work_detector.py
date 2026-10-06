"""Work-in-progress detection - the most important safety gate.

The brief calls this out explicitly: a restored quota is *not* permission to
send. If ChatGPT is still generating (an interrupted Work may keep streaming
after the quota message, or the user may have started something manually), the
prompt must not be injected.

The detector answers three-valued: True (busy), False (idle), None (cannot
tell). The caller must treat ``None`` as busy, because a false positive is far
cheaper than a false negative.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.utils.logging_setup import get_logger

log = get_logger("chatgpt.busy")

#: Strings that indicate generation is in flight. Kept broad on purpose -
#: an unrecognised "busy" label is exactly the case we must not miss.
BUSY_NAME_HINTS = (
    "stop",
    "stop generating",
    "stop streaming",
    "cancel",
    "停止",
    "停止生成",
    "停止响应",
    "取消",
    "abort",
)

#: Status text hints (used when no stop button is exposed).
BUSY_TEXT_HINTS = (
    "thinking",
    "working",
    "running",
    "generating",
    "正在工作",
    "思考中",
    "运行中",
    "生成中",
    "处理中",
)

#: Buttons that mean "there is something to stop" - treated as strong evidence.
MIN_BUSY_CONFIDENCE = 1


@dataclass(slots=True)
class BusyVerdict:
    busy: bool | None
    reason: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def confident(self) -> bool:
        return self.busy is not None


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _collect_names(window: Any, control_types: tuple[str, ...], limit: int = 400) -> list[str]:
    names: list[str] = []
    for ctype in control_types:
        found = _safe(lambda ct=ctype: window.descendants(control_type=ct), []) or []
        for idx, ctrl in enumerate(found):
            if idx > limit:
                break
            name = _safe(ctrl.window_text, "") or ""
            if not name:
                name = _safe(lambda c=ctrl: c.element_info.name, "") or ""
            if name:
                names.append(str(name).strip().lower())
    return names


def detect_busy(window: Any, controls: Any | None = None) -> BusyVerdict:
    """Decide whether ChatGPT is currently generating a response."""
    evidence: list[str] = []

    # 1. Stop button. The strongest and most stable signal.
    stop = getattr(controls, "stop_button", None) if controls is not None else None
    if stop is not None:
        evidence.append("stop-button-present")
        return BusyVerdict(True, "stop button is present", tuple(evidence))

    # 2. Scan button names for stop/cancel labels (covers builds where the
    #    button is a generic control rather than a Button).
    names = _collect_names(window, ("Button", "Text", "Hyperlink"))
    for hint in BUSY_NAME_HINTS:
        if any(hint in n for n in names):
            evidence.append(f"label:{hint}")
            return BusyVerdict(True, f"control labelled {hint!r}", tuple(evidence))

    # 3. Status text.
    for hint in BUSY_TEXT_HINTS:
        if any(hint in n for n in names):
            evidence.append(f"status:{hint}")
            return BusyVerdict(True, f"status text {hint!r}", tuple(evidence))

    # 4. Composer disabled while there is no stop button is ambiguous: it can
    #    also mean "no conversation open". Report unknown rather than idle.
    enabled = getattr(controls, "input_enabled", None) if controls is not None else None
    if enabled is False:
        evidence.append("input-disabled")
        return BusyVerdict(None, "composer is disabled but no stop button found", tuple(evidence))

    if not names:
        return BusyVerdict(None, "no accessible controls could be read", ("empty-tree",))

    return BusyVerdict(False, "no busy indicator found", tuple(evidence))
