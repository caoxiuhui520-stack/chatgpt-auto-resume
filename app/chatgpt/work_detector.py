"""Work-in-progress detection - the most important safety gate.

Measured against the real ChatGPT Desktop (Electron, renderer accessibility
tree on ``Chrome_RenderWidgetHostHWND``):

* A generation in flight surfaces a ``Button`` named ``停止`` (Stop) and
  status text such as ``思考中`` / ``正在工作``.
* A quota-exhausted state surfaces the text ``你已达到使用上限``.
* The idle composer is a wide ``Edit`` named ``随心输入``, *enabled*, and the
  ``发送`` button exists (it is disabled while the composer is empty - that is
  normal and is *not* a busy signal).

Therefore the detector is deliberately positive-evidence driven:

* ``busy=True``  only when there is explicit busy evidence.
* ``busy=False`` only when there is explicit idle evidence AND no busy evidence.
* ``busy=None``  otherwise.

"Accessible tree is not empty" is *not* idle evidence, and neither is the mere
absence of a Stop button. The caller treats ``None`` as busy, because a false
positive only delays a resume while a false negative pastes a prompt into a
running task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.utils.logging_setup import get_logger

log = get_logger("chatgpt.busy")

#: Stop / cancel buttons - the strongest busy signal.
STOP_HINTS = (
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

#: Status text that means "the model is working right now".
GENERATION_TEXT_HINTS = (
    "thinking",
    "working",
    "running",
    "generating",
    "working on",
    "正在工作",
    "思考中",
    "运行中",
    "生成中",
    "处理中",
)

#: Quota-exhausted UI. Its presence means the client is not in a clean idle
#: state, so it is treated as "do not send" evidence.
QUOTA_TEXT_HINTS = (
    "你已达到使用上限",
    "达到使用上限",
    "使用上限",
    "usage limit",
    "hit your usage limit",
    "upgrade your plan",
    "or try again later",
)

#: Cap the accessibility scan; the conversation window can have 1000+ nodes.
MAX_SCAN = 300


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


def _collect_names(render_widget: Any, limit: int = MAX_SCAN) -> list[str]:
    """Visible Text/Button/StatusBar labels, lower-cased."""
    names: list[str] = []
    for control_type in ("Text", "Button", "StatusBar", "Hyperlink"):
        found = _safe(
            lambda ct=control_type: render_widget.descendants(control_type=ct), []
        ) or []
        for ctrl in found:
            if len(names) >= limit:
                break
            name = _safe(ctrl.window_text, "") or ""
            if not name:
                name = _safe(lambda c=ctrl: c.element_info.name, "") or ""
            name = str(name).strip().lower()
            if name:
                names.append(name)
    return names


def detect_busy(render_widget: Any, controls: Any | None = None) -> BusyVerdict:
    """Decide whether ChatGPT is currently generating a response."""
    names = _collect_names(render_widget)

    # --- positive busy evidence -------------------------------------------
    stop = getattr(controls, "stop_button", None) if controls is not None else None
    if stop is not None:
        return BusyVerdict(True, "stop button is present", ("stop-button",))

    for hint in STOP_HINTS:
        if any(hint == n or hint in n for n in names):
            return BusyVerdict(True, f"stop/cancel label {hint!r}", (f"stop:{hint}",))

    for hint in GENERATION_TEXT_HINTS:
        if any(hint in n for n in names):
            return BusyVerdict(True, f"generation text {hint!r}", (f"gen:{hint}",))

    for hint in QUOTA_TEXT_HINTS:
        if any(hint in n for n in names):
            return BusyVerdict(True, f"quota-exhausted text {hint!r}", (f"quota:{hint}",))

    # --- positive idle evidence -------------------------------------------
    box = getattr(controls, "input_box", None) if controls is not None else None
    send = getattr(controls, "send_button", None) if controls is not None else None
    input_enabled = getattr(controls, "input_enabled", None) if controls is not None else None

    if box is not None and input_enabled and send is not None:
        return BusyVerdict(
            False,
            "composer enabled, send button present, no busy evidence",
            ("composer-idle",),
        )

    # A disabled composer with no stop button is ambiguous (it can also mean
    # "no conversation open"), so it is *not* busy evidence on its own.
    if not names:
        return BusyVerdict(None, "no readable controls in the render tree", ("empty-tree",))

    return BusyVerdict(None, "no positive idle evidence", ())
