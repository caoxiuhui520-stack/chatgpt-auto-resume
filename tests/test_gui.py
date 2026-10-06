"""GUI smoke tests (offscreen). The UI shell is exercised against a stub
service so no real daemon, Codex child or UIA is spawned."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from app.config import AppConfig  # noqa: E402
from app.status import AppStatus, UsageView  # noqa: E402


class _StubDiscovery:
    available = True

    def list_conversations(self, project_id=None):
        return []

    def get_current_conversation(self):
        return None

    def find_by_title(self, title):
        return []

    def refresh(self):
        return None


class _StubTestStore:
    passed = False
    record = {"status": "NONE"}


class StubService:
    def __init__(self):
        self.cfg = AppConfig()
        self.discovery = _StubDiscovery()
        self.test_store = _StubTestStore()
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def poll_once(self):
        return self.snapshot()

    def next_interval(self):
        return 30.0

    def snapshot(self):
        s = AppStatus()
        s.state = "WORKING"
        s.send_mode = "monitor"
        s.dry_run = True
        s.usage = UsageView(
            five_hour_remaining_percent=42.0,
            weekly_remaining_percent=80.0,
            five_hour_reset_at="2026-10-07T10:00:00+00:00",
            source="fake",
        )
        s.chatgpt_running = False
        s.discovery_available = True
        s.target_configured = False
        s.pending_transaction = "NONE"
        s.test_send_passed = False
        return s

    def set_target(self, **kw):
        self.cfg.target.conversation_id = kw.get("conversation_id", "")
        self.cfg.target.conversation_title = kw.get("conversation_title", "")

    def clear_target(self):
        pass

    def set_prompt(self, text):
        pass

    def prompt_text(self):
        return "继续执行。"

    def apply(self, **kw):
        pass

    def arm(self, armed):
        self.cfg.real_send.armed = bool(armed)

    def set_dry_run(self, dry_run):
        self.cfg.dry_run = bool(dry_run)

    def run_test_send(self):
        from app.resume.test_send import TestSendResult

        return TestSendResult(False, "refused", reason="stub")


@pytest.fixture
def service(tmp_path):
    svc = StubService()
    (tmp_path / "continue.txt").write_text("继续执行。", encoding="utf-8")
    svc.cfg.resume.prompt_file = str(tmp_path / "continue.txt")
    return svc


def test_main_window_constructs(service):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from app.gui.main_window import MainWindow

    window = MainWindow(service)
    assert window.stack.count() == 6
    window._on_status(service.snapshot())
    window._shutdown()
    window.deleteLater()


def test_pages_refresh(service):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from app.gui.pages import DashboardPage, TargetPage, PromptPage

    d = DashboardPage()
    d.refresh(service.snapshot())
    t = TargetPage(service)
    t.reload_conversations()
    t.refresh_status(service.snapshot())
    p = PromptPage(service)
    p.load_prompt()
    assert p.editor.toPlainText() == "继续执行。"
    d.deleteLater()
    t.deleteLater()
    p.deleteLater()


def test_target_resolver_is_id_primary():
    from app.config import TargetConfig
    from app.target import TargetResolver

    m = TargetResolver().resolve(TargetConfig(conversation_id="abc"), "abc", "X", None)
    assert m.status == "matched"
    assert m.matched_by == "conversation_id"
