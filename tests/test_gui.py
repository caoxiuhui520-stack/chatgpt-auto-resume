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

    def get_by_id(self, cid):
        return None

    def get_conversation_by_id(self, cid):
        return None

    def resolve_current_conversation(self, uia_title=""):
        return None

    def find_by_title(self, title):
        return []

    def refresh(self):
        return None


class _StubTestStore:
    passed = False
    record = {"status": "NONE"}


class _StubPresets:
    def list_presets(self):
        return []

    @property
    def default_preset_id(self):
        return "continue-default"

    def get(self, pid):
        return None

    def set_binding(self, cid, pid):
        pass


class StubService:
    def __init__(self):
        self.cfg = AppConfig()
        self.discovery = _StubDiscovery()
        self.test_store = _StubTestStore()
        self.presets = _StubPresets()
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
            source="codex_app_server",
        )
        s.provider_status = "ok"
        s.chatgpt_running = False
        s.discovery_available = True
        s.target_configured = False
        s.pending_transaction = "NONE"
        s.test_send_passed = False
        return s

    def set_target(self, **kw):
        pass

    def clear_target(self):
        pass

    def set_prompt(self, text):
        pass

    def prompt_text(self):
        return "继续执行。"

    def apply(self, **kw):
        pass

    def persist_config(self):
        pass

    def arm(self, armed):
        self.cfg.real_send.armed = bool(armed)

    def set_dry_run(self, dry_run):
        self.cfg.dry_run = bool(dry_run)


@pytest.fixture
def service():
    return StubService()


def test_main_window_constructs(service):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from app.gui.main_window import MainWindow

    window = MainWindow(service)
    assert window.sidebar is not None
    assert window.panel is not None
    window._on_status(service.snapshot())
    window._shutdown()
    window.deleteLater()


def test_sidebar_renders_sources(service):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from app.discovery.models import ConversationInfo, SOURCE_DESKTOP_ACTIVE, SOURCE_CODEX_LOCAL_STORAGE
    from app.gui.conversation_sidebar import ConversationSidebar

    sb = ConversationSidebar()
    convos = [
        ConversationInfo(id="a", title="Current chat", source_kind=SOURCE_DESKTOP_ACTIVE),
        ConversationInfo(id="b", title="Cached chat", source_kind=SOURCE_CODEX_LOCAL_STORAGE),
    ]
    sb.set_data(convos, current_id="a", target_id="a", matched=True)
    assert sb.list.count() == 2
    sb.deleteLater()


def test_control_panel_test_send_gate(service):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from app.gui.control_panel import ControlPanel

    panel = ControlPanel(service)
    status = service.snapshot()
    status.target_configured = False
    panel.refresh(status)
    assert panel.test_send_btn.isEnabled() is False, "no target → Test Send disabled"

    status.target_configured = True
    status.target_match = {"status": "mismatch"}
    panel.refresh(status)
    assert panel.test_send_btn.isEnabled() is False, "mismatch → Test Send disabled"

    status.target_match = {"status": "matched"}
    panel.refresh(status)
    assert panel.test_send_btn.isEnabled() is True, "matched → Test Send enabled"
    panel.deleteLater()


def test_target_resolver_is_id_primary():
    from app.config import TargetConfig
    from app.target import TargetResolver

    m = TargetResolver().resolve(TargetConfig(conversation_id="abc"), "abc", "X", None)
    assert m.status == "matched"
    assert m.matched_by == "conversation_id"
