"""Main window, tray, first-run wizard and the GUI entry point."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QSystemTrayIcon,
    QMenu,
)

from app.config import PROJECT_ROOT
from app.gui.pages import (
    DashboardPage,
    LogsPage,
    NotificationsPage,
    PromptPage,
    SettingsPage,
    TargetPage,
)
from app.gui.workers import PollWorker, TestSendWorker
from app.service import AppService
from app.status import AppStatus
from app.utils.logging_setup import get_logger

log = get_logger("gui")


def _app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#1f6fb2"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white"))
    p.setBrush(QColor("white"))
    p.drawEllipse(24, 24, 16, 16)
    p.end()
    return QIcon(pm)


class WizardDialog(QDialog):
    """First-run safety wizard. Blocks real send until the test send passes."""

    def __init__(self, service: AppService, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("ChatGPT Auto Resume — 首次配置")
        self.resize(520, 460)

        layout = QVBoxLayout(self)
        title = QLabel("首次运行向导")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        layout.addWidget(title)

        self.steps = QLabel("")
        self.steps.setStyleSheet("font-family: Consolas; font-size: 12px; white-space: pre;")
        layout.addWidget(self.steps)

        self.test_btn = QPushButton("Run Supervised Test Send")
        self.test_btn.clicked.connect(self._run_test)
        layout.addWidget(self.test_btn)

        self.test_result = QLabel("Test send not run.")
        self.test_result.setWordWrap(True)
        layout.addWidget(self.test_result)

        self.finish_btn = QPushButton("Finish")
        self.finish_btn.clicked.connect(self._finish)
        self.finish_btn.setEnabled(False)
        layout.addWidget(self.finish_btn)

        self._refresh_steps()
        # Keep the checklist current while the first polls land in the
        # background (quota / ChatGPT state arrive asynchronously).
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_steps)
        self._timer.start(2000)

    def _refresh_steps(self) -> None:
        status = self.service.snapshot()
        gpt = (
            "running" if status.chatgpt_running is True
            else "not running" if status.chatgpt_running is False
            else "unknown"
        )
        lines = [
            f"1. Environment: Python OK | Codex {status.provider_status} | ChatGPT {gpt}",
            f"2. Quota: 5h {_pct(status.usage.five_hour_remaining_percent if status.usage else None)} | weekly {_pct(status.usage.weekly_remaining_percent if status.usage else None)}",
            f"3. Target: {'configured' if status.target_configured else 'NOT configured'}",
            f"4. Prompt: {len(self.service.prompt_text())} chars",
            f"5. Dry run: {'on' if status.dry_run else 'off'}",
            f"6. Test send: {'PASSED' if status.test_send_passed else 'not passed'}",
            f"7. Arm: {'ALLOWED' if status.test_send_passed else 'blocked until test passes'}",
        ]
        self.steps.setText("\n".join(lines))
        self.finish_btn.setEnabled(True)

    def _run_test(self) -> None:
        self.test_btn.setEnabled(False)
        self.test_result.setText("Running test send…")
        self.test_result.setStyleSheet("color: #b35900;")
        self.worker = TestSendWorker(self.service)
        self.worker.finished_ok.connect(self._on_test_done)
        self.worker.start()

    def _on_test_done(self, result) -> None:
        self.test_btn.setEnabled(True)
        if result.ok:
            self.test_result.setText("Test Send Passed ✓  " + (result.confirmation or ""))
            self.test_result.setStyleSheet("color: #1a7f37; font-weight: 700;")
        elif result.status == "uncertain":
            self.test_result.setText("Test Send Uncertain — 无法确认，请人工检查后重试。")
            self.test_result.setStyleSheet("color: #b35900;")
        else:
            self.test_result.setText(f"Test Send failed: {result.reason}")
            self.test_result.setStyleSheet("color: #c62828;")
        self._refresh_steps()

    def _finish(self) -> None:
        wizard_file = Path(self.service.cfg.data_dir) / "wizard.json"
        wizard_file.parent.mkdir(parents=True, exist_ok=True)
        wizard_file.write_text(
            json.dumps({"completed": True, "test_send_passed": self.service.test_store.passed}),
            encoding="utf-8",
        )
        self.accept()


def _pct(value) -> str:
    return "?" if value is None else f"{value:.0f}%"


class MainWindow(QMainWindow):
    def __init__(self, service: AppService) -> None:
        super().__init__()
        self.service = service
        self.setWindowTitle("ChatGPT Auto Resume")
        self.setWindowIcon(_app_icon())
        self.resize(980, 680)

        central = QWidget()
        root = QHBoxLayout(central)
        self.setCentralWidget(central)

        # Sidebar
        self.nav = QListWidget()
        self.nav.setFixedWidth(160)
        for label in ("Overview", "Target", "Prompt", "Notifications", "Logs", "Settings"):
            QListWidgetItem(label, self.nav)
        root.addWidget(self.nav)

        # Stacked pages
        self.stack = QStackedWidget()
        self.dashboard = DashboardPage()
        self.target_page = TargetPage(service)
        self.prompt_page = PromptPage(service)
        self.notify_page = NotificationsPage(service)
        self.logs_page = LogsPage(service)
        self.settings_page = SettingsPage(service)
        for page in (
            self.dashboard,
            self.target_page,
            self.prompt_page,
            self.notify_page,
            self.logs_page,
            self.settings_page,
        ):
            self.stack.addWidget(page)
        root.addWidget(self.stack, 1)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)

        # Wire page buttons
        self.target_page.refresh_btn.clicked.connect(self.target_page.reload_conversations)
        self.target_page.use_current_btn.clicked.connect(
            lambda: self.target_page.use_current(self._last_status)
        )
        self.target_page.save_btn.clicked.connect(self.target_page.save)
        self.target_page.clear_btn.clicked.connect(self.service.clear_target)
        self.prompt_page.save_btn.clicked.connect(self.prompt_page.save)
        self.prompt_page.reset_btn.clicked.connect(self.prompt_page.reset)
        self.notify_page.save_btn.clicked.connect(self.notify_page.save)
        self.notify_page.test_btn.clicked.connect(self.notify_page.test)
        self.logs_page.refresh_btn.clicked.connect(self.logs_page.reload)
        self.logs_page.open_btn.clicked.connect(self.logs_page.open_folder)
        self.settings_page.save_btn.clicked.connect(self.settings_page.apply)

        # Persistent action bar
        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        self.test_send_btn = QPushButton("Test Real Send")
        self.test_send_btn.clicked.connect(self._run_test_send)
        self.arm_btn = QPushButton("Arm Real Send")
        self.arm_btn.clicked.connect(self._arm)
        self.test_result_label = QLabel("Test send: not run")
        bar_layout.addWidget(self.test_send_btn)
        bar_layout.addWidget(self.arm_btn)
        bar_layout.addWidget(self.test_result_label, 1)
        root.addWidget(bar)

        self._last_status: AppStatus | None = None

        # Tray
        self.tray = None
        self._setup_tray()

        # Start polling in the background
        self.poll_worker = PollWorker(service)
        self.poll_worker.status_changed.connect(self._on_status)
        self.poll_worker.start()

        # Initial page data
        self.prompt_page.load_prompt()
        self.logs_page.reload()
        self.target_page.reload_conversations()

    # -- status ------------------------------------------------------------

    def _on_status(self, status: AppStatus) -> None:
        self._last_status = status
        self.dashboard.refresh(status)
        self.target_page.refresh_status(status)
        self.settings_page.refresh_status(status)
        self._update_arm_button(status)
        if self.tray is not None:
            self.tray.setToolTip(
                f"ChatGPT Auto Resume — {status.state} ({status.send_mode})"
            )

    def _update_arm_button(self, status: AppStatus) -> None:
        if status.send_mode == "armed":
            self.arm_btn.setText("Disarm")
            self.arm_btn.setEnabled(True)
        elif status.test_send_passed:
            self.arm_btn.setText("Arm Real Send")
            self.arm_btn.setEnabled(True)
        else:
            self.arm_btn.setText("Arm Real Send")
            self.arm_btn.setEnabled(False)
            self.arm_btn.setToolTip("必须先通过 Supervised Test Send。")

    def _arm(self) -> None:
        if self._last_status and self._last_status.send_mode == "armed":
            self.service.arm(False)
            self.service.set_dry_run(True)
        else:
            # Require a passed test send.
            if not self.service.test_store.passed:
                QMessageBox.warning(self, "Arm", "必须先通过 Supervised Test Send 才能 Arm。")
                return
            if not self.service.cfg.task_lock.enabled:
                QMessageBox.warning(self, "Arm", "必须先启用 Task Lock。")
                return
            if not self.service.cfg.target.conversation_id and not self.service.cfg.target.conversation_title:
                QMessageBox.warning(self, "Arm", "必须先配置目标对话。")
                return
            self.service.arm(True)

    # -- test send ---------------------------------------------------------

    def _run_test_send(self) -> None:
        self.test_send_btn.setEnabled(False)
        self.test_result_label.setText("Test send running…")
        self.test_result_label.setStyleSheet("color: #b35900;")
        self.test_worker = TestSendWorker(self.service)
        self.test_worker.finished_ok.connect(self._on_test_result)
        self.test_worker.start()

    def _on_test_result(self, result) -> None:
        self.test_send_btn.setEnabled(True)
        if result.ok:
            self.test_result_label.setText("Test Send Passed ✓")
            self.test_result_label.setStyleSheet("color: #1a7f37; font-weight: 700;")
        elif result.status == "uncertain":
            self.test_result_label.setText("Test Send Uncertain — 请人工检查")
            self.test_result_label.setStyleSheet("color: #b35900;")
        else:
            self.test_result_label.setText(f"Test Send failed: {result.reason}")
            self.test_result_label.setStyleSheet("color: #c62828;")
        # Refresh status so the arm button updates.
        self._on_status(self.service.snapshot())

    # -- tray --------------------------------------------------------------

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(_app_icon(), self)
        menu = QMenu()
        open_action = QAction("Open", self)
        open_action.triggered.connect(self._show_window)
        menu.addAction(open_action)

        toggle_action = QAction("Enable/Disable Auto Resume", self)
        toggle_action.triggered.connect(self._toggle_resume)
        menu.addAction(toggle_action)

        status_action = QAction("Status", self)
        status_action.triggered.connect(self._show_status)
        menu.addAction(status_action)

        menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self._quit)
        menu.addAction(exit_action)

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
        enabled = not self.service.cfg.resume.enabled
        self.service.apply(**{"resume.enabled": enabled})

    def _show_status(self) -> None:
        if self._last_status is None:
            return
        QMessageBox.information(
            self,
            "Status",
            f"State: {self._last_status.state}\nMode: {self._last_status.send_mode}",
        )

    def _quit(self) -> None:
        self.close()

    # -- close-to-tray -----------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.tray is not None:
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "ChatGPT Auto Resume",
                "仍在后台运行。右键托盘图标可退出。",
                QSystemTrayIcon.Information,
                3000,
            )
            return
        self._shutdown()
        event.accept()

    def _shutdown(self) -> None:
        self.poll_worker.request_stop()
        self.poll_worker.wait(3000)
        self.service.stop()


def run_gui() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ChatGPT Auto Resume")
    app.setStyle("Fusion")

    service = AppService()
    window = MainWindow(service)

    wizard_file = Path(service.cfg.data_dir) / "wizard.json"
    if not wizard_file.exists():
        wizard = WizardDialog(service, window)
        wizard.exec()

    window.show()
    exit_code = app.exec()
    service.stop()
    return exit_code
