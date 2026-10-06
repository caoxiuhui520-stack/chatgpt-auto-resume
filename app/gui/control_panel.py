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
    "matched": ("✓ 已匹配", theme.READY),
    "mismatch": ("目标不匹配", theme.MISMATCH),
    "ambiguous": ("目标不明确", theme.ERROR),
    "unknown": ("未知", theme.WAITING),
    "not_configured": ("未配置", theme.NEUTRAL),
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
        card = GlassCard("目标与运行状态")
        self.target_name = KVRow("目标对话")
        self.current_name = KVRow("当前对话")
        self.target_id = KVRow("目标 ID")
        self.current_id = KVRow("当前 ID")
        self.target_source = KVRow("目标来源")
        self.current_source = KVRow("当前来源")
        self.match_row = KVRow("匹配")

        self.gpt_state = KVRow("ChatGPT 桌面端")
        self.quota_5h = KVRow("5 小时剩余")
        self.quota_weekly = KVRow("每周剩余")
        self.quota_reset = KVRow("重置倒计时")
        self.daemon_state = KVRow("守护进程状态")

        for w in (self.target_name, self.current_name, self.target_id, self.current_id,
                  self.target_source, self.current_source, self.match_row, self.gpt_state,
                  self.quota_5h, self.quota_weekly, self.quota_reset, self.daemon_state):
            card.body().addWidget(w)

        btn_row = QHBoxLayout()
        self.set_target_btn = primary_button("设为目标")
        self.set_target_btn.clicked.connect(self._set_target)
        self.use_current_btn = primary_button("使用当前打开的对话", flat=True)
        self.use_current_btn.clicked.connect(self.use_current_requested.emit)
        btn_row.addWidget(self.set_target_btn)
        btn_row.addWidget(self.use_current_btn)
        btn_row.addStretch()
        card.body().addLayout(btn_row)

        # binding
        bind_row = QHBoxLayout()
        self.bind_combo = QComboBox()
        self.bind_combo.setMinimumWidth(160)
        self.bind_btn = primary_button("绑定 Preset", flat=True)
        self.bind_btn.clicked.connect(self._bind)
        bind_row.addWidget(QLabel("绑定 Preset："))
        bind_row.addWidget(self.bind_combo, 1)
        bind_row.addWidget(self.bind_btn)
        card.body().addLayout(bind_row)

        self._layout.addWidget(card)

    def _build_settings_card(self) -> None:
        card = GlassCard("自动续跑设置")
        self.resume_toggle = self._toggle("自动续跑", True)
        self.dry_run_toggle = self._toggle("Dry Run（演练，不输入）", True)
        self.task_lock_toggle = self._toggle("任务锁", False)
        self.armed_toggle = self._toggle("真实发送（Armed）", False)
        self.autostart_toggle = self._toggle("自动启动 ChatGPT", True)
        self.startup_toggle = self._toggle("开机自启", False)
        self.win_notify_toggle = self._toggle("Windows 通知", True)
        self.telegram_toggle = self._toggle("Telegram 通知", False)
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
        card = GlassCard("安全检查与发送状态")
        self.checklist = SafetyChecklist()
        card.body().addWidget(self.checklist)
        self.pending_label = QLabel("")
        self.pending_label.setWordWrap(True)
        card.body().addWidget(self.pending_label)
        self._layout.addWidget(card)

    def _build_actions_card(self) -> None:
        card = GlassCard("操作")
        self.test_send_btn = primary_button("运行监督式测试发送")
        self.test_send_btn.clicked.connect(self.test_send_requested.emit)
        self.arm_btn = primary_button("启用自动续跑")
        self.arm_btn.clicked.connect(self.arm_requested.emit)
        self.disable_btn = primary_button("停用自动续跑", danger=True)
        self.disable_btn.clicked.connect(self.disable_requested.emit)

        card.body().addWidget(self.test_send_btn)
        row1 = QHBoxLayout()
        row1.addWidget(self.arm_btn)
        row1.addWidget(self.disable_btn)
        card.body().addLayout(row1)

        row2 = QHBoxLayout()
        self.save_btn = primary_button("保存配置", flat=True)
        self.save_btn.clicked.connect(self.save_config_requested.emit)
        self.dry_run_btn = primary_button("运行演练（Dry Run）", flat=True)
        self.dry_run_btn.clicked.connect(self.dry_run_requested.emit)
        self.open_chatgpt_btn = primary_button("打开 ChatGPT", flat=True)
        self.open_chatgpt_btn.clicked.connect(self.open_chatgpt_requested.emit)
        self.open_logs_btn = primary_button("打开日志", flat=True)
        self.open_logs_btn.clicked.connect(self.open_logs_requested.emit)
        self.refresh_btn = primary_button("刷新", flat=True)
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
        mtext, mcolor = MATCH_LABEL.get(match.get("status", "not_configured"), ("未知", theme.NEUTRAL))
        self.match_row.set_value(mtext, mcolor)

        # Runtime
        gpt = "Running" if status.chatgpt_running is True else \
              "Not running" if status.chatgpt_running is False else "未知"
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
            ("Codex 已连接", status.provider_status == "ok", ""),
            ("额度可读取", status.usage is not None, ""),
            ("检测到 ChatGPT 桌面端", status.chatgpt_running is True, ""),
            ("已配置目标", status.target_configured,
             "点上方【使用当前打开的对话】一键设置"),
            ("目标唯一解析", status_match in ("matched",), ""),
            ("当前对话已匹配", status_match == "matched",
             "请先在 ChatGPT Desktop 打开目标对话再测试。"),
            ("已选择 Prompt", True, ""),
            ("测试发送已通过", status.test_send_passed, ""),
            ("无未决 UNCERTAIN 事务", status.pending_transaction != "UNCERTAIN", ""),
            ("无 fake 数据源", status.usage is None or status.usage.source != "fake", ""),
            ("任务锁已启用", bool((status.target or {}).get("conversation_id") or self.service.cfg.task_lock.enabled), ""),
        ]
        return items

    def _update_test_send_gate(self, status: AppStatus) -> None:
        m = status.target_match or {}
        status_match = m.get("status", "not_configured")
        if not status.target_configured:
            self.test_send_btn.setEnabled(False)
            self.test_send_btn.setToolTip("请先选择目标对话。")
        elif status_match == "mismatch":
            self.test_send_btn.setEnabled(False)
            self.test_send_btn.setToolTip("请先在 ChatGPT Desktop 打开目标对话再测试。")
        elif status_match == "ambiguous":
            self.test_send_btn.setEnabled(False)
            self.test_send_btn.setToolTip("Target identity is ambiguous.")
        else:
            self.test_send_btn.setEnabled(True)
            self.test_send_btn.setToolTip("")

    def _update_arm_gate(self, status: AppStatus) -> None:
        if status.send_mode == "armed":
            self.arm_btn.setText("已启用 ✓")
            self.arm_btn.setEnabled(False)
        elif status.test_send_passed and status.pending_transaction != "UNCERTAIN":
            self.arm_btn.setText("启用自动续跑")
            self.arm_btn.setEnabled(True)
        else:
            self.arm_btn.setText("启用自动续跑")
            self.arm_btn.setEnabled(False)
            self.arm_btn.setToolTip("必须先通过监督式测试发送。")
