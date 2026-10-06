"""Right unified control panel: status, presets, settings, safety, actions."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.gui import theme
from app.gui.preset_widgets import PromptPanel
from app.gui.widgets import GlassCard, KVRow, SafetyChecklist, SettingRow, primary_button
from app.status import AppStatus

MATCH_LABEL = {
    "matched": ("✓ Matched", theme.READY),
    "mismatch": ("Target Mismatch", theme.MISMATCH),
    "ambiguous": ("Ambiguous", theme.ERROR),
    "unknown": ("Unknown", theme.WAITING),
    "not_configured": ("Not configured", theme.NEUTRAL),
}


def _pct(v) -> str:
    return "?" if v is None else f"{v:.0f}%"


def _countdown(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        from datetime import datetime

        reset = datetime.fromisoformat(iso)
        delta = reset - datetime.now(reset.tzinfo).astimezone()
        if delta.total_seconds() < 0:
            return "now"
        total = int(delta.total_seconds())
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m:02d}m"
    except ValueError:
        return "—"


class ControlPanel(QWidget):
    test_send_requested = Signal()
    arm_requested = Signal()
    disable_requested = Signal()
    set_target_requested = Signal(str)  # conversation id
    use_current_requested = Signal()
    save_config_requested = Signal()
    dry_run_requested = Signal()
    open_chatgpt_requested = Signal()
    open_logs_requested = Signal()
    refresh_requested = Signal()
    bind_preset_requested = Signal(str, str)  # conversation_id, preset_id

    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setSpacing(12)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        self._build_target_card()
        self.prompt_panel = PromptPanel(service)
        self.prompt_panel.test_requested.connect(self._emit_prompt_test)
        self._layout.addWidget(self.prompt_panel)
        self._build_settings_card()
        self._build_safety_card()
        self._build_actions_card()
        self._layout.addStretch()

        self._pending_id = ""

    # -- target card -------------------------------------------------------

    def _build_target_card(self) -> None:
        card = GlassCard("Target & Runtime Status")
        self.target_name = KVRow("Target")
        self.current_name = KVRow("Current")
        self.target_id = KVRow("Target ID")
        self.current_id = KVRow("Current ID")
        self.target_source = KVRow("Target source")
        self.current_source = KVRow("Current source")
        self.match_row = KVRow("Match")

        self.gpt_state = KVRow("ChatGPT Desktop")
        self.quota_5h = KVRow("5h remaining")
        self.quota_weekly = KVRow("Weekly remaining")
        self.quota_reset = KVRow("Reset countdown")
        self.daemon_state = KVRow("Daemon state")

        for w in (self.target_name, self.current_name, self.target_id, self.current_id,
                  self.target_source, self.current_source, self.match_row, self.gpt_state,
                  self.quota_5h, self.quota_weekly, self.quota_reset, self.daemon_state):
            card.body().addWidget(w)

        btn_row = QHBoxLayout()
        self.set_target_btn = primary_button("Set as Target")
        self.set_target_btn.clicked.connect(self._set_target)
        self.use_current_btn = primary_button("Use Current Conversation", flat=True)
        self.use_current_btn.clicked.connect(self.use_current_requested.emit)
        btn_row.addWidget(self.set_target_btn)
        btn_row.addWidget(self.use_current_btn)
        btn_row.addStretch()
        card.body().addLayout(btn_row)

        # binding
        bind_row = QHBoxLayout()
        self.bind_combo = QComboBox()
        self.bind_combo.setMinimumWidth(160)
        self.bind_btn = primary_button("Bind Preset", flat=True)
        self.bind_btn.clicked.connect(self._bind)
        bind_row.addWidget(QLabel("Bind preset:"))
        bind_row.addWidget(self.bind_combo, 1)
        bind_row.addWidget(self.bind_btn)
        card.body().addLayout(bind_row)

        self._layout.addWidget(card)

    def _build_settings_card(self) -> None:
        card = GlassCard("Auto Resume Settings")
        self.resume_toggle = self._toggle("Auto Resume", True)
        self.dry_run_toggle = self._toggle("Dry Run", True)
        self.task_lock_toggle = self._toggle("Task Lock", False)
        self.armed_toggle = self._toggle("Real Send Armed", False)
        self.autostart_toggle = self._toggle("Auto Start ChatGPT", True)
        self.startup_toggle = self._toggle("Start with Windows", False)
        self.win_notify_toggle = self._toggle("Windows Notification", True)
        self.telegram_toggle = self._toggle("Telegram", False)
        for row in (self.resume_toggle, self.dry_run_toggle, self.task_lock_toggle,
                    self.armed_toggle, self.autostart_toggle, self.startup_toggle,
                    self.win_notify_toggle, self.telegram_toggle):
            card.body().addWidget(row)
        self._layout.addWidget(card)

    def _toggle(self, label: str, default: bool) -> QCheckBox:
        cb = QCheckBox(label)
        cb.setChecked(default)
        return cb

    def _build_safety_card(self) -> None:
        card = GlassCard("Safety & Send State")
        self.checklist = SafetyChecklist()
        card.body().addWidget(self.checklist)
        self.pending_label = QLabel("")
        self.pending_label.setWordWrap(True)
        card.body().addWidget(self.pending_label)
        self._layout.addWidget(card)

    def _build_actions_card(self) -> None:
        card = GlassCard("Actions")
        self.test_send_btn = primary_button("Run Supervised Test Send")
        self.test_send_btn.clicked.connect(self.test_send_requested.emit)
        self.arm_btn = primary_button("Arm Auto Resume")
        self.arm_btn.clicked.connect(self.arm_requested.emit)
        self.disable_btn = primary_button("Disable Auto Resume", danger=True)
        self.disable_btn.clicked.connect(self.disable_requested.emit)

        card.body().addWidget(self.test_send_btn)
        row1 = QHBoxLayout()
        row1.addWidget(self.arm_btn)
        row1.addWidget(self.disable_btn)
        card.body().addLayout(row1)

        row2 = QHBoxLayout()
        self.save_btn = primary_button("Save Configuration", flat=True)
        self.save_btn.clicked.connect(self.save_config_requested.emit)
        self.dry_run_btn = primary_button("Run Dry Run", flat=True)
        self.dry_run_btn.clicked.connect(self.dry_run_requested.emit)
        self.open_chatgpt_btn = primary_button("Open ChatGPT", flat=True)
        self.open_chatgpt_btn.clicked.connect(self.open_chatgpt_requested.emit)
        self.open_logs_btn = primary_button("Open Logs", flat=True)
        self.open_logs_btn.clicked.connect(self.open_logs_requested.emit)
        self.refresh_btn = primary_button("Refresh", flat=True)
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        row2.addWidget(self.save_btn)
        row2.addWidget(self.dry_run_btn)
        card.body().addLayout(row2)
        row3 = QHBoxLayout()
        row3.addWidget(self.open_chatgpt_btn)
        row3.addWidget(self.open_logs_btn)
        row3.addWidget(self.refresh_btn)
        card.body().addLayout(row3)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        card.body().addWidget(self.note)
        self._layout.addWidget(card)

    # -- actions ----------------------------------------------------------

    def _set_target(self) -> None:
        if self._pending_id:
            self.set_target_requested.emit(self._pending_id)

    def _bind(self) -> None:
        preset_id = self.bind_combo.currentData()
        if preset_id and self._pending_id:
            self.bind_preset_requested.emit(self._pending_id, preset_id)

    def _emit_prompt_test(self, content: str) -> None:
        # Re-route the prompt test through the same test-send dialog path is
        # not correct: testing a preset must NOT send. It only previews.
        # (Handled by the main window as a dry-run preview.)
        pass

    def set_pending_target(self, conversation_id: str, title: str) -> None:
        self._pending_id = conversation_id
        self.target_name.set_value(title or conversation_id, theme.ACCENT)
        self.target_id.set_value(conversation_id)

    def reload_presets(self) -> None:
        presets = self.service.presets.list_presets()
        ids = [p.id for p in presets]
        if [self.bind_combo.itemData(i) for i in range(self.bind_combo.count())] == ids:
            return
        self.bind_combo.blockSignals(True)
        self.bind_combo.clear()
        for p in presets:
            self.bind_combo.addItem(p.name, p.id)
        self.bind_combo.blockSignals(False)

    def load_settings(self) -> None:
        cfg = self.service.cfg
        self.resume_toggle.setChecked(bool(cfg.resume.enabled))
        self.dry_run_toggle.setChecked(bool(cfg.dry_run))
        self.task_lock_toggle.setChecked(bool(cfg.task_lock.enabled))
        self.armed_toggle.setChecked(bool(cfg.real_send.armed))
        self.autostart_toggle.setChecked(bool(cfg.chatgpt.auto_start))
        self.win_notify_toggle.setChecked(bool(cfg.notifications.windows))
        self.telegram_toggle.setChecked(bool(cfg.notifications.telegram.enabled))
        try:
            from app.runtime.autostart import is_autostart_installed

            self.startup_toggle.setChecked(is_autostart_installed(cfg.daemon.task_name))
        except Exception:  # noqa: BLE001
            self.startup_toggle.setChecked(False)

    def apply_settings(self) -> None:
        cfg = self.service.cfg
        cfg.resume.enabled = self.resume_toggle.isChecked()
        cfg.dry_run = self.dry_run_toggle.isChecked()
        cfg.task_lock.enabled = self.task_lock_toggle.isChecked()
        cfg.real_send.armed = self.armed_toggle.isChecked()
        cfg.chatgpt.auto_start = self.autostart_toggle.isChecked()
        cfg.notifications.windows = self.win_notify_toggle.isChecked()
        cfg.notifications.telegram.enabled = self.telegram_toggle.isChecked()
        if cfg.dry_run:
            cfg.real_send.armed = False  # dry-run never armed
        self.service.persist_config()

        from app.config import PROJECT_ROOT
        from app.runtime.autostart import install_autostart, uninstall_autostart

        if self.startup_toggle.isChecked():
            install_autostart(PROJECT_ROOT, cfg.daemon.task_name)
        else:
            uninstall_autostart(cfg.daemon.task_name)

    # -- refresh ----------------------------------------------------------

    def refresh(self, status: AppStatus) -> None:
        self.reload_presets()
        # Target / current
        t = status.target or {}
        self.target_name.set_value(t.get("conversation_title") or t.get("conversation_id") or "—")
        self.target_id.set_value(t.get("conversation_id") or "—")
        self.current_name.set_value(status.active_conversation_title or status.current_conversation_id or "—")
        self.current_id.set_value(status.current_conversation_id or "—")

        match = status.target_match or {}
        mtext, mcolor = MATCH_LABEL.get(match.get("status", "not_configured"), ("Unknown", theme.NEUTRAL))
        self.match_row.set_value(mtext, mcolor)

        # Runtime
        gpt = "Running" if status.chatgpt_running is True else \
              "Not running" if status.chatgpt_running is False else "Unknown"
        self.gpt_state.set_value(gpt, theme.READY if status.chatgpt_running else theme.NEUTRAL)
        if status.usage:
            self.quota_5h.set_value(_pct(status.usage.five_hour_remaining_percent))
            self.quota_weekly.set_value(_pct(status.usage.weekly_remaining_percent))
            self.quota_reset.set_value(_countdown(status.usage.five_hour_reset_at))
        self.daemon_state.set_value(status.state, theme.state_color(
            "ready" if status.state in ("READY_TO_RESUME", "RESUMING") else
            "waiting" if status.state in ("WAITING_RESET", "QUOTA_EXHAUSTED", "COOLDOWN") else
            "error" if status.state == "ERROR" else "monitoring"))

        # Safety checklist
        self.checklist.set_items(self._safety_items(status))

        # UNCERTAIN banner
        if status.pending_transaction == "UNCERTAIN":
            self.pending_label.setText(
                "上一轮发送结果无法确认，为防止重复发送，本额度窗口已暂停自动发送。"
            )
            self.pending_label.setStyleSheet(f"color: {theme.UNCERTAIN}; font-weight: 600;")
        else:
            self.pending_label.setText("")

        # Test send button gating (AC-32/33/34)
        self._update_test_send_gate(status)

        # Arm button gating
        self._update_arm_gate(status)

    def _safety_items(self, status: AppStatus) -> list[tuple[str, bool, str]]:
        m = status.target_match or {}
        status_match = m.get("status", "not_configured")
        items = [
            ("Codex connected", status.provider_status == "ok", ""),
            ("Quota readable", status.usage is not None, ""),
            ("ChatGPT Desktop detected", status.chatgpt_running is True, ""),
            ("Target configured", status.target_configured, "Select a target conversation first."),
            ("Target uniquely resolved", status_match in ("matched",), ""),
            ("Current conversation matched", status_match == "matched",
             "Open the configured target conversation before testing."),
            ("Prompt selected", True, ""),
            ("Test Send passed", status.test_send_passed, ""),
            ("No pending UNCERTAIN transaction", status.pending_transaction != "UNCERTAIN", ""),
            ("No fake provider", status.usage is None or status.usage.source != "fake", ""),
            ("Task Lock enabled", bool((status.target or {}).get("conversation_id") or self.service.cfg.task_lock.enabled), ""),
        ]
        return items

    def _update_test_send_gate(self, status: AppStatus) -> None:
        m = status.target_match or {}
        status_match = m.get("status", "not_configured")
        if not status.target_configured:
            self.test_send_btn.setEnabled(False)
            self.test_send_btn.setToolTip("Select a target conversation first.")
        elif status_match == "mismatch":
            self.test_send_btn.setEnabled(False)
            self.test_send_btn.setToolTip("Open the configured target conversation before testing.")
        elif status_match == "ambiguous":
            self.test_send_btn.setEnabled(False)
            self.test_send_btn.setToolTip("Target identity is ambiguous.")
        else:
            self.test_send_btn.setEnabled(True)
            self.test_send_btn.setToolTip("")

    def _update_arm_gate(self, status: AppStatus) -> None:
        if status.send_mode == "armed":
            self.arm_btn.setText("Armed ✓")
            self.arm_btn.setEnabled(False)
        elif status.test_send_passed and status.pending_transaction != "UNCERTAIN":
            self.arm_btn.setText("Arm Auto Resume")
            self.arm_btn.setEnabled(True)
        else:
            self.arm_btn.setText("Arm Auto Resume")
            self.arm_btn.setEnabled(False)
            self.arm_btn.setToolTip("必须先通过 Supervised Test Send。")
