"""GUI pages (Dashboard, Target, Prompt, Notifications, Logs, Settings).

Each page renders an :class:`AppStatus` and talks back to the service through
the main window. Pages never touch the daemon, providers or the state store
directly.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.status import AppStatus

# -- colour language (always colour + text, never colour alone) -------------
GREEN = "#1a7f37"
ORANGE = "#b35900"
RED = "#c62828"
BLUE = "#1f6fb2"
GREY = "#666666"

STATE_DISPLAY = {
    "STARTING": ("Starting", BLUE),
    "WORKING": ("Monitoring", BLUE),
    "QUOTA_EXHAUSTED": ("Quota exhausted", ORANGE),
    "WAITING_RESET": ("Waiting for reset", ORANGE),
    "READY_TO_RESUME": ("Ready to resume", GREEN),
    "RESUMING": ("Sending", GREEN),
    "COOLDOWN": ("Cooldown", BLUE),
    "ERROR": ("Error", RED),
}

MATCH_DISPLAY = {
    "matched": ("Matched", GREEN),
    "mismatch": ("Mismatch", RED),
    "ambiguous": ("Ambiguous", RED),
    "unknown": ("Unknown", ORANGE),
    "not_configured": ("Not configured", GREY),
}


def _badge(text: str, color: str, *, bold: bool = True) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(
        f"color: white; background: {color}; padding: 3px 10px; border-radius: 4px;"
        f"font-weight: {'700' if bold else '400'};"
    )
    return label


def _section(title: str) -> QLabel:
    label = QLabel(title)
    label.setStyleSheet("font-weight: 700; font-size: 13px; color: #333; margin-top: 6px;")
    return label


def _kv(parent_form: QFormLayout, key: str, value_widget: QWidget) -> None:
    parent_form.addRow(key + ":", value_widget)


def _pct(value) -> str:
    return "?" if value is None else f"{value:.0f}%"


def _reset_countdown(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        reset = datetime.fromisoformat(iso)
        delta = reset - datetime.now(reset.tzinfo).astimezone()
        if delta.total_seconds() < 0:
            return "now"
        total = int(delta.total_seconds())
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m:02d}m {s:02d}s"
    except ValueError:
        return "—"


class DashboardPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.state_badge = _badge("Starting", BLUE)
        self.mode_badge = _badge("Dry Run", ORANGE)
        header = QHBoxLayout()
        header.addWidget(self.state_badge)
        header.addWidget(self.mode_badge)
        header.addStretch()
        layout.addLayout(header)

        # Quota block
        layout.addWidget(_section("Quota"))
        self.quota_form = QFormLayout()
        self.five_hour = QLabel("?")
        self.weekly = QLabel("?")
        self.reset_time = QLabel("—")
        self.countdown = QLabel("—")
        self.provider = QLabel("—")
        _kv(self.quota_form, "5h remaining", self.five_hour)
        _kv(self.quota_form, "Weekly remaining", self.weekly)
        _kv(self.quota_form, "5h reset at", self.reset_time)
        _kv(self.quota_form, "Countdown", self.countdown)
        _kv(self.quota_form, "Usage provider", self.provider)
        layout.addLayout(self.quota_form)

        # ChatGPT + target block
        layout.addWidget(_section("ChatGPT"))
        self.gpt_form = QFormLayout()
        self.chatgpt_running = QLabel("—")
        self.target_conv = QLabel("—")
        self.active_conv = QLabel("—")
        self.lock_status = QLabel("—")
        _kv(self.gpt_form, "ChatGPT Desktop", self.chatgpt_running)
        _kv(self.gpt_form, "Target conversation", self.target_conv)
        _kv(self.gpt_form, "Open conversation", self.active_conv)
        _kv(self.gpt_form, "Task Lock", self.lock_status)
        layout.addLayout(self.gpt_form)

        # Resume history
        layout.addWidget(_section("Resume"))
        self.resume_form = QFormLayout()
        self.last_resume_time = QLabel("—")
        self.last_resume_result = QLabel("—")
        self.resumed_count = QLabel("0")
        self.pending = QLabel("NONE")
        self.test_send = QLabel("Not run")
        self.uptime = QLabel("—")
        _kv(self.resume_form, "Last resume", self.last_resume_time)
        _kv(self.resume_form, "Last result", self.last_resume_result)
        _kv(self.resume_form, "Resume count", self.resumed_count)
        _kv(self.resume_form, "Transaction", self.pending)
        _kv(self.resume_form, "Test send", self.test_send)
        _kv(self.resume_form, "Uptime", self.uptime)
        layout.addLayout(self.resume_form)

        # UNCERTAIN warning banner
        self.uncertain_banner = QLabel("")
        self.uncertain_banner.setWordWrap(True)
        self.uncertain_banner.setStyleSheet(
            f"color: white; background: {RED}; padding: 10px; border-radius: 4px;"
        )
        self.uncertain_banner.hide()
        layout.addWidget(self.uncertain_banner)

        layout.addStretch()

    def refresh(self, status: AppStatus) -> None:
        state_text, state_color = STATE_DISPLAY.get(status.state, (status.state, GREY))
        if status.pending_transaction == "UNCERTAIN":
            state_text, state_color = "Uncertain", RED
        self.state_badge.setText(state_text)
        self._set_badge_color(self.state_badge, state_color)

        if status.send_mode == "armed":
            mode_text, mode_color = "Real Send Armed", GREEN
        elif not status.dry_run:
            mode_text, mode_color = "Dry Run (off)", ORANGE
        else:
            mode_text, mode_color = "DRY RUN", ORANGE
        self.mode_badge.setText(mode_text)
        self._set_badge_color(self.mode_badge, mode_color)

        if status.usage:
            self.five_hour.setText(_pct(status.usage.five_hour_remaining_percent))
            self.weekly.setText(_pct(status.usage.weekly_remaining_percent))
            self.reset_time.setText(
                status.usage.five_hour_reset_at[:19].replace("T", " ")
                if status.usage.five_hour_reset_at else "—"
            )
            self.countdown.setText(_reset_countdown(status.usage.five_hour_reset_at))
            self.provider.setText(status.usage.source or "—")
        else:
            self.provider.setText("no data yet")

        if status.chatgpt_running is True:
            self.chatgpt_running.setText("Running")
            self.chatgpt_running.setStyleSheet(f"color: {GREEN};")
        elif status.chatgpt_running is False:
            self.chatgpt_running.setText("Not running")
            self.chatgpt_running.setStyleSheet(f"color: {RED};")
        else:
            self.chatgpt_running.setText("Unknown")
            self.chatgpt_running.setStyleSheet(f"color: {ORANGE};")

        self.target_conv.setText(
            (status.target.get("conversation_title") or status.target.get("conversation_id") or "—")
        )
        self.active_conv.setText(status.active_conversation_title or status.current_conversation_id or "—")

        match = status.target_match or {}
        mtext, mcolor = MATCH_DISPLAY.get(match.get("status", "not_configured"), ("Unknown", GREY))
        self.lock_status.setText(mtext)
        self.lock_status.setStyleSheet(f"color: {mcolor}; font-weight: 600;")

        self.last_resume_time.setText(status.last_resume_time or "—")
        self.last_resume_result.setText(status.last_resume_result or "—")
        self.resumed_count.setText(str(status.resumed_count))
        self.pending.setText(status.pending_transaction)

        ts = status.test_send_last or {}
        if status.test_send_passed:
            self.test_send.setText("Passed")
            self.test_send.setStyleSheet(f"color: {GREEN}; font-weight: 600;")
        elif ts.get("status") == "UNCERTAIN":
            self.test_send.setText("Uncertain")
            self.test_send.setStyleSheet(f"color: {ORANGE};")
        elif ts.get("status") == "CONFIRMED":
            self.test_send.setText("Passed")
            self.test_send.setStyleSheet(f"color: {GREEN};")
        else:
            self.test_send.setText("Not run")
            self.test_send.setStyleSheet(f"color: {GREY};")

        if status.uptime_seconds:
            s = int(status.uptime_seconds)
            h, rem = divmod(s, 3600)
            m, _ = divmod(rem, 60)
            self.uptime.setText(f"{h}h {m}m")
        else:
            self.uptime.setText("—")

        if status.pending_transaction == "UNCERTAIN":
            self.uncertain_banner.setText(
                "上一轮发送结果无法确认，为防止重复发送，本额度窗口已暂停自动发送。"
            )
            self.uncertain_banner.show()
        else:
            self.uncertain_banner.hide()

    @staticmethod
    def _set_badge_color(badge: QLabel, color: str) -> None:
        badge.setStyleSheet(
            f"color: white; background: {color}; padding: 3px 10px; border-radius: 4px;"
            f"font-weight: 700;"
        )


class TargetPage(QWidget):
    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        layout = QVBoxLayout(self)

        layout.addWidget(_section("Target conversation"))

        form = QFormLayout()
        self.project_combo = QComboBox()
        self.project_combo.setEnabled(False)  # no local project names in V0.1
        self.conversation_combo = QComboBox()
        form.addRow("Project:", self.project_combo)
        form.addRow("Conversation:", self.conversation_combo)
        layout.addLayout(form)

        info = QFormLayout()
        self.conv_title = QLabel("—")
        self.conv_id = QLabel("—")
        self.project_id = QLabel("—")
        self.current_conv = QLabel("—")
        self.match_status = QLabel("—")
        _kv(info, "Conversation title", self.conv_title)
        _kv(info, "Conversation ID", self.conv_id)
        _kv(info, "Project ID", self.project_id)
        _kv(info, "Current active", self.current_conv)
        _kv(info, "Match status", self.match_status)
        layout.addLayout(info)

        buttons = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.use_current_btn = QPushButton("Use Current Conversation")
        self.save_btn = QPushButton("Save Target")
        self.clear_btn = QPushButton("Clear Target")
        buttons.addWidget(self.refresh_btn)
        buttons.addWidget(self.use_current_btn)
        buttons.addStretch()
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.clear_btn)
        layout.addLayout(buttons)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        layout.addStretch()

        # populated on refresh
        self._conversations: list = []

    def reload_conversations(self) -> None:
        self.service.discovery.refresh()
        self._conversations = self.service.discovery.list_conversations()
        self.conversation_combo.clear()
        for c in self._conversations:
            self.conversation_combo.addItem(f"{c.display_title}", c.id)
        # reselect current target if present
        tid = self.service.cfg.target.conversation_id
        if tid:
            idx = self.conversation_combo.findData(tid)
            if idx >= 0:
                self.conversation_combo.setCurrentIndex(idx)

    def _selected_conversation(self):
        idx = self.conversation_combo.currentIndex()
        if idx < 0 or idx >= len(self._conversations):
            return None
        return self._conversations[idx]

    def refresh_status(self, status: AppStatus) -> None:
        conv = self._selected_conversation()
        if conv is not None:
            self.conv_title.setText(conv.display_title)
            self.conv_id.setText(conv.id)
            self.project_id.setText(conv.project_id or "—")
        self.current_conv.setText(
            status.active_conversation_title or status.current_conversation_id or "—"
        )
        match = status.target_match or {}
        mtext, mcolor = MATCH_DISPLAY.get(match.get("status", "not_configured"), ("Unknown", GREY))
        self.match_status.setText(mtext)
        self.match_status.setStyleSheet(f"color: {mcolor}; font-weight: 600;")

    def save(self) -> None:
        conv = self._selected_conversation()
        if conv is None:
            self.note.setText("请选择一个对话。")
            self.note.setStyleSheet(f"color: {RED};")
            return
        self.service.set_target(
            conversation_id=conv.id,
            conversation_title=conv.display_title,
            project_id=conv.project_id,
            project_name="",
        )
        self.note.setText("已保存目标对话。")
        self.note.setStyleSheet(f"color: {GREEN};")

    def use_current(self, status: AppStatus) -> None:
        cid = status.current_conversation_id
        title = status.active_conversation_title
        if not cid and not title:
            self.note.setText("无法确定当前打开的对话。")
            self.note.setStyleSheet(f"color: {ORANGE};")
            return
        self.service.set_target(conversation_id=cid, conversation_title=title)
        self.reload_conversations()
        self.note.setText("已把当前对话设为目标。")
        self.note.setStyleSheet(f"color: {GREEN};")


class PromptPage(QWidget):
    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        layout = QVBoxLayout(self)
        layout.addWidget(_section("Continue prompt"))
        self.editor = QPlainTextEdit()
        self.editor.setMinimumHeight(220)
        layout.addWidget(self.editor)

        self.count_label = QLabel("0 chars")
        layout.addWidget(self.count_label)

        buttons = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.reset_btn = QPushButton("Reset to default")
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.reset_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.status = QLabel("")
        layout.addWidget(self.status)
        layout.addStretch()

        self.editor.textChanged.connect(self._update_count)

    def load_prompt(self) -> None:
        self.editor.setPlainText(self.service.prompt_text())
        self._update_count()

    def _update_count(self) -> None:
        self.count_label.setText(f"{len(self.editor.toPlainText())} chars")

    def save(self) -> None:
        text = self.editor.toPlainText()
        if not text.strip():
            self.status.setText("Prompt 不能为空。")
            self.status.setStyleSheet(f"color: {RED};")
            return
        self.service.set_prompt(text)
        self.status.setText("已保存。")
        self.status.setStyleSheet(f"color: {GREEN};")

    def reset(self) -> None:
        self.editor.setPlainText("继续执行当前已经确定的目标和计划。")
        self.status.setText("已重置为默认。")
        self.status.setStyleSheet(f"color: {BLUE};")


class NotificationsPage(QWidget):
    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        layout = QVBoxLayout(self)
        layout.addWidget(_section("Notifications"))

        self.windows_check = QCheckBox("Windows toast notifications")
        layout.addWidget(self.windows_check)

        self.tg_check = QCheckBox("Telegram notifications")
        layout.addWidget(self.tg_check)

        tg_form = QFormLayout()
        self.bot_token = QLineEdit()
        self.bot_token.setEchoMode(QLineEdit.Password)
        self.chat_id = QLineEdit()
        tg_form.addRow("Bot token:", self.bot_token)
        tg_form.addRow("Chat ID:", self.chat_id)
        layout.addLayout(tg_form)

        buttons = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.test_btn = QPushButton("Test Notification")
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.test_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.status = QLabel("")
        layout.addWidget(self.status)
        layout.addStretch()
        self._load()

    def _load(self) -> None:
        n = self.service.cfg.notifications
        self.windows_check.setChecked(bool(n.windows))
        self.tg_check.setChecked(bool(n.telegram.enabled))
        self.bot_token.setText(n.telegram.bot_token or "")
        self.chat_id.setText(n.telegram.chat_id or "")

    def save(self) -> None:
        self.service.apply(
            **{
                "notifications.windows": self.windows_check.isChecked(),
                "notifications.telegram.enabled": self.tg_check.isChecked(),
                "notifications.telegram.bot_token": self.bot_token.text().strip(),
                "notifications.telegram.chat_id": self.chat_id.text().strip(),
            }
        )
        self.status.setText("已保存（Token 不会写入日志）。")
        self.status.setStyleSheet(f"color: {GREEN};")

    def test(self) -> None:
        from app.notification.base import Event, build_notifier

        # Save first so the test uses current values.
        self.save()
        try:
            notifier = build_notifier(self.service.cfg)
            ok = notifier.send(Event.RESUME_PREPARING, "Auto Resume 测试通知。")
            notifier.close()
            self.status.setText("测试通知已发送。" if ok else "已尝试发送（可能未配置）。")
            self.status.setStyleSheet(f"color: {GREEN if ok else ORANGE};")
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"发送失败：{exc}")
            self.status.setStyleSheet(f"color: {RED};")


class LogsPage(QWidget):
    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        layout = QVBoxLayout(self)
        layout.addWidget(_section("Logs"))
        self.list = QListWidget()
        self.list.setStyleSheet("font-family: Consolas; font-size: 11px;")
        layout.addWidget(self.list)

        buttons = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.open_btn = QPushButton("Open Log Folder")
        buttons.addWidget(self.refresh_btn)
        buttons.addWidget(self.open_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

    def reload(self) -> None:
        log_file = Path(self.service.cfg.log_dir) / "app.log"
        self.list.clear()
        if not log_file.exists():
            self.list.addItem("(no log file yet)")
            return
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
        except OSError:
            lines = []
        for line in lines:
            self.list.addItem(line.rstrip())
        self.list.scrollToBottom()

    def open_folder(self) -> None:
        import os

        os.startfile(str(self.service.cfg.log_dir))  # type: ignore[attr-defined]


class SettingsPage(QWidget):
    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        layout = QVBoxLayout(self)
        layout.addWidget(_section("Auto Resume"))

        form = QFormLayout()
        self.dry_run_check = QCheckBox("Dry run (never type)")
        self.armed_check = QCheckBox("Arm real send")
        self.task_lock_check = QCheckBox("Task lock enabled")
        form.addRow(self.dry_run_check)
        form.addRow(self.armed_check)
        form.addRow(self.task_lock_check)
        layout.addLayout(form)

        layout.addWidget(_section("Startup"))
        self.startup_check = QCheckBox("Start with Windows")
        layout.addWidget(self.startup_check)

        buttons = QHBoxLayout()
        self.save_btn = QPushButton("Apply")
        buttons.addWidget(self.save_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.arming_note = QLabel("")
        self.arming_note.setWordWrap(True)
        layout.addWidget(self.arming_note)
        layout.addStretch()
        self._load()

    def _load(self) -> None:
        from app.runtime.autostart import is_autostart_installed

        self.dry_run_check.setChecked(bool(self.service.cfg.dry_run))
        self.armed_check.setChecked(bool(self.service.cfg.real_send.armed))
        self.task_lock_check.setChecked(bool(self.service.cfg.task_lock.enabled))
        try:
            self.startup_check.setChecked(is_autostart_installed(self.service.cfg.daemon.task_name))
        except Exception:  # noqa: BLE001
            self.startup_check.setChecked(False)

    def refresh_status(self, status: AppStatus) -> None:
        self.arming_note.setText(f"Arming: {status.armed_reason}")

    def apply(self) -> None:
        self.service.set_dry_run(self.dry_run_check.isChecked())
        self.service.cfg.task_lock.enabled = self.task_lock_check.isChecked()
        self.service.apply()
        # arm separately (set_dry_run already reset armed if dry_run on)
        if self.armed_check.isChecked() and not self.dry_run_check.isChecked():
            self.service.arm(True)
        elif not self.armed_check.isChecked():
            self.service.arm(False)

        from app.runtime.autostart import install_autostart, uninstall_autostart

        from app.config import PROJECT_ROOT

        if self.startup_check.isChecked():
            install_autostart(PROJECT_ROOT, self.service.cfg.daemon.task_name)
        else:
            uninstall_autostart(self.service.cfg.daemon.task_name)

        self.status.setText("已应用。")
        self.status.setStyleSheet(f"color: {GREEN};")
        self._load()
