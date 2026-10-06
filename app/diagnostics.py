"""UI diagnostics and three-state sampling.

``diagnose-ui`` answers the questions the safety review raised, from live
measurement rather than from assumptions:

* which window is in the foreground, and what owns the keyboard focus
  (Win32 GUI-thread focus + UIA focused element, with control type /
  automation id / name),
* which ChatGPT window was selected as the main conversation window, and why,
* the composer / send button / stop button candidates that were located,
* the busy verdict with its full evidence trail,
* the focus proof the keyboard fallback would rely on (evaluated passively,
  without moving the focus).

The same collector backs the three-state sampling required before real sends
are considered: run ``diagnose-ui --label idle|busy|quota_exhausted --save``
in each real state and a redacted JSON snapshot lands in ``diagnostics/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.chatgpt import focus_verifier, process_detector, window_controller, work_detector
from app.config import PROJECT_ROOT
from app.models import utcnow
from app.utils.logging_setup import _mask_emails, get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig

log = get_logger("diagnostics")

VALID_LABELS = ("idle", "busy", "quota_exhausted")


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _describe_control(ctrl: Any) -> dict[str, Any]:
    rect = _safe(ctrl.rectangle, None)
    return {
        "control_type": _safe(lambda: ctrl.element_info.control_type, "?"),
        "name": _safe(lambda: ctrl.element_info.name, "") or "",
        "automation_id": _safe(lambda: ctrl.element_info.automation_id, "") or "",
        "rect": (
            [rect.left, rect.top, rect.width(), rect.height()] if rect is not None else None
        ),
        "enabled": _safe(ctrl.is_enabled, None),
        "keyboard_focusable": _safe(ctrl.is_keyboard_focusable, None),
        "has_value_pattern": window_controller._supports_value_pattern(ctrl),
        "current_value": _safe(lambda: window_controller.text_input_value(ctrl), "") or "",
    }


def collect_ui_diagnostics(cfg: "AppConfig") -> dict[str, Any]:
    """One passive pass over the live ChatGPT UI. Never types, never clicks."""
    from app.chatgpt.base import build_controller

    report: dict[str, Any] = {
        "captured_at": utcnow().isoformat(),
        "chatgpt_running": process_detector.is_process_running(cfg.chatgpt.process_names),
    }

    candidates = process_detector.find_chatgpt_windows(cfg.chatgpt.process_names)
    report["candidate_windows"] = [
        {
            "handle": c.handle,
            "title": c.title,
            "pid": c.pid,
            "process_name": c.process_name,
            "minimized": c.minimized,
        }
        for c in candidates
    ]

    controller = build_controller("uia", cfg)
    try:
        info = controller.find_window()
    except Exception as exc:  # noqa: BLE001
        report["main_window"] = None
        report["error"] = f"window selection failed: {exc}"
        return report

    if info is None:
        report["main_window"] = None
        report["focus"] = focus_verifier.describe_current_focus(0)
        return report

    report["main_window"] = {
        "handle": info.handle,
        "title": info.title,
        "pid": info.pid,
        "minimized": info.minimized,
        "backgrounded_renderer": bool(getattr(controller, "_backgrounded", False)),
    }
    report["focus"] = focus_verifier.describe_current_focus(info.handle)

    render_hwnd = controller._render_handle or window_controller.find_renderer_hwnd(info.handle)
    report["render_hwnd"] = render_hwnd
    render_widget = controller._render_wrapper
    if render_widget is None:
        report["controls"] = None
        return report

    report["render_tree_nodes"] = window_controller._tree_node_count(render_widget)
    controls = window_controller.locate(render_widget, render_hwnd)

    report["controls"] = {
        "composer": (
            _describe_control(controls.input_box) if controls.input_box is not None else None
        ),
        "send_button": (
            _describe_control(controls.send_button) if controls.send_button is not None else None
        ),
        "stop_button_present": controls.stop_button is not None,
    }

    verdict = work_detector.detect_busy(render_widget, controls)
    report["busy_verdict"] = {
        "busy": verdict.busy,
        "reason": verdict.reason,
        "evidence": list(verdict.evidence),
    }
    report["conversation_title"] = _safe(controller.conversation_title, "") or ""

    # The focus proof exactly as the keyboard fallback would evaluate it,
    # but passive (try_set_focus=False): we report what WOULD be provable.
    proof = focus_verifier.verify_composer_focus(
        info.handle,
        render_hwnd,
        composer=controls.input_box,
        render_widget=render_widget,
        try_set_focus=False,
    )
    report["focus_proof_passive"] = proof.to_dict()
    if not proof.proved and proof.reason.startswith("focus is on"):
        # Expected when nothing has focus on the composer; not a failure of
        # the diagnostic itself.
        report["focus_proof_note"] = (
            "passive check only; the real path calls set_focus() first"
        )
    return report


def save_sample(report: dict[str, Any], label: str, directory: Path | None = None) -> Path:
    """Persist a three-state sample. Email addresses are masked before the
    payload touches the disk."""
    if label not in VALID_LABELS:
        raise ValueError(f"label must be one of {VALID_LABELS}")
    directory = directory or (PROJECT_ROOT / "diagnostics")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{label}.json"
    payload = _mask_emails(json.dumps(report, indent=2, ensure_ascii=False))
    path.write_text(payload + "\n", encoding="utf-8")
    log.info("diagnostic sample saved: %s", path)
    return path


def render_report(report: dict[str, Any]) -> str:
    """The JSON that ``diagnose-ui`` prints. Masked, always."""
    return _mask_emails(json.dumps(report, indent=2, ensure_ascii=False))
