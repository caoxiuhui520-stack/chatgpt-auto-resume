"""Main window: left conversation sidebar + right unified control panel."""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from app.gui import theme
from app.gui.control_panel import ControlPanel
from app.gui.conversation_sidebar import ConversationSidebar
from app.gui.workers import PollWorker, TestSendWorker
from app.gui.widgets import StatusBadge
from app.service import AppService
from app.status import AppStatus
from app.utils.logging_setup import get_logger

log = get_logger("gui")


def _app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#3b82f6"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white"))
    p.setBrush(QColor("white"))
    p.drawEllipse(24, 24, 16, 16)
    p.end()
    return QIcon(pm)


class WizardDialog(QDialog):
    """First-run wizard: environment → quota → conversation → preset → review
    → dry run → test send → arm. The main window is already visible behind it.
    """

    def __init__(self, service: AppService, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("首次配置向导 — 完成后进入主控台")
        self.resize(640, 620)
        self.setStyleSheet(theme.STYLESHEET)

        layout = QVBoxLayout(self)
        title = QLabel("首次配置向导")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        layout.addWidget(title)
        hint = QLabel("主控台已在向导后面运行。完成以下步骤后点 Finish 进入。")
        hint.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.steps = QLabel("")
        self.steps.setStyleSheet("white-space: pre; font-size: 12.5px;")
        layout.addWidget(self.steps)

        layout.addWidget(QLabel("选择目标对话（来自桌面端本地数据）："))
        self.conv_combo = QComboBox()
        self.conv_combo.setMinimumWidth(420)
        layout.addWidget(self.conv_combo)

        self.use_current_btn = QPushButton("使用桌面端当前打开的对话")
        self.use_current_btn.setProperty("flat", True)
        self.use_current_btn.clicked.connect(self._select_current)
        layout.addWidget(self.use_current_btn)

        layout.addWidget(QLabel("选择续跑 Prompt："))
        self.preset_combo = QComboBox()
        layout.addWidget(self.preset_combo)

        self.test_btn = QPushButton("运行监督式测试发送")
        self.test_btn.clicked.connect(self._run_test)
        layout.addWidget(self.test_btn)
        self.test_result = QLabel("测试发送尚未运行。")
        self.test_result.setWordWrap(True)
        layout.addWidget(self.test_result)

        self.finish_btn = QPushButton("完成 — 进入主控台")
        self.finish_btn.clicked.connect(self._finish)
        layout.addWidget(self.finish_btn)

        self._load_choices()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_steps)
        self._timer.start(2000)
        self._refresh_steps()

    def _load_choices(self) -> None:
        self.service.discovery.refresh()
        self.conv_combo.clear()
        self._conversations = self.service.discovery.list_conversations()
        # The conversation actually open in the desktop UI goes first and is
        # preselected - a first test send then matches immediately.
        current = None
        try:
            current = self.service.discovery.get_current_conversation()
        except Exception:  # noqa: BLE001
            current = None
        if current is not None:
            label = f"[当前打开] {current.display_title or current.short_id}  ({current.short_id})"
            self.conv_combo.addItem(label, current.id)
            self._conversations = [c for c in self._conversations if c.id != current.id]
        for c in self._conversations:
            self.conv_combo.addItem(f"{c.display_title}  ({c.short_id})", c.id)
        self.preset_combo.clear()
        for p in self.service.presets.list_presets():
            self.preset_combo.addItem(p.name, p.id)
        didx = self.preset_combo.findData(self.service.presets.default_preset_id)
        if didx >= 0:
            self.preset_combo.setCurrentIndex(didx)

    def _select_current(self) -> None:
        current = None
        try:
            self.service.discovery.refresh()
            current = self.service.discovery.get_current_conversation()
        except Exception:  # noqa: BLE001
            pass
        if current is None:
            self.test_result.setText("无法确定桌面端当前打开的对话。")
            self.test_result.setStyleSheet(f"color: {theme.WAITING};")
            return
        idx = self.conv_combo.findData(current.id)
        if idx < 0:
            self._load_choices()
            idx = self.conv_combo.findData(current.id)
        if idx >= 0:
            self.conv_combo.setCurrentIndex(idx)

    def _selected_conversation(self):
        cid = self.conv_combo.currentData()
        for c in self._conversations:
            if c.id == cid:
                return c
        return None

    def _refresh_steps(self) -> None:
        status = self.service.snapshot()
        gpt = ("运行中" if status.chatgpt_running is True
               else "未运行" if status.chatgpt_running is False else "未知")
        lines = [
            f"1. 环境：Python 正常 | Codex {status.provider_status} | ChatGPT {gpt}",
            f"2. 额度：5 小时 {self._pct(status.usage.five_hour_remaining_percent if status.usage else None)}",
            f"3. 对话：{'已选择' if self.conv_combo.currentData() else '未选择'}",
            f"4. Prompt：{self.preset_combo.currentText() or '未选择'}",
            f"5. Dry Run：{'开启' if status.dry_run else '关闭'}",
            f"6. 测试发送：{'已通过' if status.test_send_passed else '未通过'}",
            f"7. 启用：{'允许' if status.test_send_passed else '测试通过前禁止'}",
        ]
        self.steps.setText("\n".join(lines))
        self.test_btn.setEnabled(bool(self.conv_combo.currentData()))

    @staticmethod
    def _pct(v) -> str:
        return "?" if v is None else f"{v:.0f}%"

    def _run_test(self) -> None:
        conv = self._selected_conversation()
        if conv is None:
            self.test_result.setText("请先选择目标对话。")
            return
        self.service.set_target(conversation_id=conv.id, conversation_title=conv.display_title)
        preset_id = self.preset_combo.currentData()
        if preset_id:
            self.service.presets.set_default(preset_id)
        self.test_btn.setEnabled(False)
        self.test_result.setText("正在运行测试发送…")
        self.test_result.setStyleSheet(f"color: {theme.WAITING};")
        self.worker = TestSendWorker(self.service)
        self.worker.finished_ok.connect(self._on_test_done)
        self.worker.start()

    def _on_test_done(self, result) -> None:
        self.test_btn.setEnabled(bool(self.conv_combo.currentData()))
        if result.ok:
            self.test_result.setText("测试发送已通过 ✓  " + (result.confirmation or ""))
            self.test_result.setStyleSheet(f"color: {theme.READY}; font-weight: 700;")
        elif result.status == "uncertain":
            self.test_result.setText("测试发送结果不确定 — 无法确认是否送达，请到 ChatGPT 里人工检查后重试。")
            self.test_result.setStyleSheet(f"color: {theme.WAITING};")
        elif result.status == "refused" and "mismatch" in (result.reason or ""):
            self.test_result.setText(
                "ChatGPT 桌面端当前打开的不是目标对话。\n"
                "请先在 ChatGPT Desktop 里打开上面选中的对话，再点 Test Send。\n"
                f"（{result.reason}）"
            )
            self.test_result.setStyleSheet(f"color: {theme.MISMATCH};")
        else:
            self.test_result.setText(f"测试发送失败：{result.reason}")
            self.test_result.setStyleSheet(f"color: {theme.ERROR};")
        self._refresh_steps()

    def _finish(self) -> None:
        wizard_file = Path(self.service.cfg.data_dir) / "wizard.json"
        wizard_file.parent.mkdir(parents=True, exist_ok=True)
        wizard_file.write_text(
            json.dumps({"completed": True, "test_send_passed": self.service.test_store.passed}),
            encoding="utf-8",
        )
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, service: AppService) -> None:
        super().__init__()
        self.service = service
        self.setWindowTitle("ChatGPT Auto Resume")
        self.setWindowIcon(_app_icon())
        self.setStyleSheet(theme.STYLESHEET)
        self.resize(1080, 720)
        self.setMinimumSize(880, 560)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        header = QWidget()
        header.setStyleSheet(f"background: {theme.SURFACE_SOLID}; border-bottom: 1px solid {theme.CARD_BORDER};")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 10, 16, 10)
        title = QLabel("ChatGPT Auto Resume")
        title.setStyleSheet("font-weight: 700; font-size: 15px;")
        hl.addWidget(title)
        self.current_badge = QLabel("当前：—")
        self.current_badge.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        hl.addWidget(self.current_badge, 1)
        self.mode_badge = StatusBadge("演练 DRY RUN", "dry_run")
        hl.addWidget(self.mode_badge)
        root.addWidget(header)

        splitter = QSplitter(Qt.Horizontal)
        self.sidebar = ConversationSidebar()
        self.sidebar.setMinimumWidth(260)
        self.sidebar.setMaximumWidth(380)
        self.sidebar.setStyleSheet(f"background: {theme.SIDEBAR};")
        self.panel = ControlPanel(service)
        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        self._wire()

        self.tray = None
        self._setup_tray()

        self._last_status: AppStatus | None = None

        self.poll_worker = PollWorker(service)
        self.poll_worker.status_changed.connect(self._on_status)
        self.poll_worker.start()

        self._reload_conversations()
        self.panel.load_settings()
        self.panel.reload_presets()

    def _wire(self) -> None:
        self.sidebar.selection_changed.connect(self._on_conversation_selected)
        self.sidebar.refresh_requested.connect(self._reload_conversations)
        self.sidebar.use_current_requested.connect(self._use_current)
        self.panel.set_target_requested.connect(self._set_target)
        self.panel.use_current_requested.connect(self._use_current)
        self.panel.bind_preset_requested.connect(self._bind_preset)
        self.panel.test_send_requested.connect(self._run_test_send)
        self.panel.arm_requested.connect(self._arm)
        self.panel.disable_requested.connect(self._disable)
        self.panel.save_config_requested.connect(self._save_config)
        self.panel.dry_run_requested.connect(self._dry_run_once)
        self.panel.open_chatgpt_requested.connect(self._open_chatgpt)
        self.panel.open_logs_requested.connect(self._open_logs)
        self.panel.refresh_requested.connect(self._refresh_all)

    def _on_status(self, status: AppStatus) -> None:
        self._last_status = status
        self.panel.refresh(status)
        self._refresh_sidebar(status)

        current_label = status.active_conversation_title or status.current_conversation_id or "—"
        self.current_badge.setText(f"当前：{current_label}")
        if status.send_mode == "armed":
            self.mode_badge.set_kind("ready")
            self.mode_badge.setText("已启用 ARMED")
        elif not status.dry_run:
            self.mode_badge.set_kind("dry_run")
            self.mode_badge.setText("真实发送（未启用）")
        else:
            self.mode_badge.set_kind("dry_run")
            self.mode_badge.setText("演练 DRY RUN")

    def _refresh_sidebar(self, status: AppStatus) -> None:
        convos = self.service.discovery.list_conversations()
        cur_id = status.current_conversation_id
        target_id = (status.target or {}).get("conversation_id", "")
        matched = (status.target_match or {}).get("status") == "matched"
        if cur_id and not any(c.id == cur_id for c in convos):
            from app.discovery.models import ConversationInfo, SOURCE_DESKTOP_ACTIVE

            convos.insert(0, ConversationInfo(
                id=cur_id, title=status.active_conversation_title,
                source_kind=SOURCE_DESKTOP_ACTIVE, is_current=True, is_verified=False,
            ))
        self.sidebar.set_data(convos, cur_id, target_id, matched)

    def _on_conversation_selected(self, conversation_id: str) -> None:
        conv = self.service.discovery.get_by_id(conversation_id)
        title = conv.display_title if conv else conversation_id
        self.panel.set_pending_target(conversation_id, title)

    def _set_target(self, conversation_id: str) -> None:
        conv = self.service.discovery.get_by_id(conversation_id)
        title = conv.display_title if conv else ""
        self.service.set_target(conversation_id=conversation_id, conversation_title=title)
        self.panel.note.setText("已保存目标对话。")
        self.panel.note.setStyleSheet(f"color: {theme.READY};")
        self._refresh_all()

    def _use_current(self) -> None:
        status = self._last_status or self.service.snapshot()
        cid = status.current_conversation_id
        title = status.active_conversation_title
        if not cid and not title:
            self.panel.note.setText("无法确定当前打开的对话。")
            self.panel.note.setStyleSheet(f"color: {theme.WAITING};")
            return
        self.service.set_target(conversation_id=cid, conversation_title=title)
        self.panel.note.setText("已把当前对话设为目标。")
        self.panel.note.setStyleSheet(f"color: {theme.READY};")
        self._refresh_all()

    def _bind_preset(self, conversation_id: str, preset_id: str) -> None:
        self.service.presets.set_binding(conversation_id, preset_id)
        self.panel.note.setText("已绑定 Preset 到该对话。")
        self.panel.note.setStyleSheet(f"color: {theme.READY};")

    def _save_config(self) -> None:
        self.panel.apply_settings()
        self.panel.note.setText("已保存配置。")
        self.panel.note.setStyleSheet(f"color: {theme.READY};")

    def _dry_run_once(self) -> None:
        status = self.service.poll_once()
        self._on_status(status)
        self.panel.note.setText(f"Dry run tick done → state {status.state}.")
        self.panel.note.setStyleSheet(f"color: {theme.MONITORING};")

    def _open_chatgpt(self) -> None:
        try:
            from app.chatgpt.ui_controller import discover_chatgpt_exe

            exe = discover_chatgpt_exe(self.service.cfg.chatgpt.exe_path)
            if exe:
                os.startfile(exe)  # type: ignore[attr-defined]
            else:
                QMessageBox.information(self, "ChatGPT", "未找到 ChatGPT Desktop 可执行文件。")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "ChatGPT", f"无法打开：{exc}")

    def _open_logs(self) -> None:
        try:
            os.startfile(str(self.service.cfg.log_dir))  # type: ignore[attr-defined]
        except OSError:
            pass

    def _refresh_all(self) -> None:
        self._reload_conversations()
        self._on_status(self.service.snapshot())

    def _reload_conversations(self) -> None:
        try:
            self.service.discovery.refresh()
        except Exception as exc:  # noqa: BLE001
            log.warning("conversation refresh failed: %s", exc)

    def _run_test_send(self) -> None:
        self.panel.test_send_btn.setEnabled(False)
        self.panel.note.setText("正在运行测试发送…")
        self.panel.note.setStyleSheet(f"color: {theme.WAITING};")
        self.test_worker = TestSendWorker(self.service)
        self.test_worker.finished_ok.connect(self._on_test_result)
        self.test_worker.start()

    def _on_test_result(self, result) -> None:
        if result.ok:
            self.panel.note.setText("测试发送已通过 ✓")
            self.panel.note.setStyleSheet(f"color: {theme.READY}; font-weight: 700;")
        elif result.status == "refused" and "mismatch" in (result.reason or ""):
            self.panel.note.setText(
                "ChatGPT 桌面端当前打开的不是目标对话——请先在 ChatGPT Desktop 打开目标对话再测试。"
            )
            self.panel.note.setStyleSheet(f"color: {theme.MISMATCH};")
        elif result.status == "uncertain":
            self.panel.note.setText("测试发送结果不确定 — 请人工检查")
            self.panel.note.setStyleSheet(f"color: {theme.WAITING};")
        else:
            self.panel.note.setText(f"测试发送失败：{result.reason}")
            self.panel.note.setStyleSheet(f"color: {theme.ERROR};")
        self._refresh_all()

    def _arm(self) -> None:
        if not self.service.test_store.passed:
            QMessageBox.warning(self, "启用", "必须先通过监督式测试发送才能启用。")
            return
        if not self.service.cfg.task_lock.enabled:
            QMessageBox.warning(self, "启用", "必须先启用任务锁。")
            return
        if not (self.service.cfg.target.conversation_id or self.service.cfg.target.conversation_title):
            QMessageBox.warning(self, "启用", "必须先配置目标对话。")
            return
        box = QMessageBox.question(
            self, "启用自动续跑",
            "确认启用真实自动续跑？启用后额度恢复时程序会向目标对话真实发送 Prompt。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if box == QMessageBox.Yes:
            self.service.arm(True)
            self._refresh_all()

    def _disable(self) -> None:
        self.service.arm(False)
        self.service.set_dry_run(True)
        self.panel.note.setText("已停用自动续跑（回到演练模式）。")
        self.panel.note.setStyleSheet(f"color: {theme.MONITORING};")
        self._refresh_all()

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(_app_icon(), self)
        menu = QMenu()
        act_open = QAction("打开", self)
        act_open.triggered.connect(self._show_window)
        menu.addAction(act_open)
        act_toggle = QAction("启用/停用自动续跑", self)
        act_toggle.triggered.connect(self._toggle_resume)
        menu.addAction(act_toggle)
        act_status = QAction("状态", self)
        act_status.triggered.connect(self._show_status)
        menu.addAction(act_status)
        menu.addSeparator()
        act_exit = QAction("退出", self)
        act_exit.triggered.connect(self.close)
        menu.addAction(act_exit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self._show_window()

    def _show_window(self) -> None:
        self.showNormal()
        self.activateWindow()

    def _toggle_resume(self) -> None:
        self.service.cfg.resume.enabled = not self.service.cfg.resume.enabled
        self.service.persist_config()

    def _show_status(self) -> None:
        if self._last_status is None:
            return
        QMessageBox.information(
            self, "Status",
            f"状态：{self._last_status.state}\n模式：{self._last_status.send_mode}",
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.tray is not None:
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "ChatGPT Auto Resume",
                "仍在后台运行。右键托盘图标可退出。",
                QSystemTrayIcon.Information, 3000,
            )
            return
        self._shutdown()
        event.accept()

    def _shutdown(self) -> None:
        self.poll_worker.request_stop()
        self.poll_worker.wait(3000)
        self.service.stop()


def run_gui() -> int:
    app = QApplication([])
    app.setApplicationName("ChatGPT Auto Resume")
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)

    service = AppService()
    window = MainWindow(service)
    # The main console must be visible immediately - the wizard (if any) is a
    # modal layer on top of it, not a replacement for it.
    window.show()

    wizard_file = Path(service.cfg.data_dir) / "wizard.json"
    if not wizard_file.exists():
        wizard = WizardDialog(service, window)
        wizard.exec()

    code = app.exec()
    service.stop()
    return code
