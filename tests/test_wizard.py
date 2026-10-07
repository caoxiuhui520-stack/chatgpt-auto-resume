"""TEST-01/02: the wizard's preselected current conversation must persist.

The current conversation is added to the combo even when it is NOT in the
ordinary list_conversations() result; Finish must still save it as the
target.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from app.config import AppConfig  # noqa: E402
from app.discovery.models import (  # noqa: E402
    ConversationInfo,
    SOURCE_CODEX_WORK,
    SOURCE_DESKTOP_ACTIVE,
)
from app.status import AppStatus  # noqa: E402


class _Discovery:
    def __init__(self, current, others):
        self._current = current
        self._others = others

    def refresh(self):
        pass

    def list_conversations(self, project_id=None):
        return list(self._others)

    def get_current_conversation(self):
        return self._current

    def get_by_id(self, cid):
        for c in [self._current, *self._others]:
            if c.id == cid:
                return c
        return None


class _Presets:
    def __init__(self):
        self.default = ""

    def list_presets(self):
        return []

    @property
    def default_preset_id(self):
        return ""

    def set_default(self, pid):
        self.default = pid


class _TestStore:
    passed = False


class _Service:
    def __init__(self, current, others):
        self.discovery = _Discovery(current, others)
        self.presets = _Presets()
        self.test_store = _TestStore()
        self.cfg = AppConfig()

    def snapshot(self):
        return AppStatus()

    def set_target(self, **kw):
        self.cfg.target.conversation_id = kw.get("conversation_id", "")
        self.cfg.target.conversation_title = kw.get("conversation_title", "")
        self.cfg.task_lock.enabled = True


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_wizard_persists_preselected_current(app, tmp_path):
    from app.gui.main_window import WizardDialog

    # TEST-02: current "A" is NOT in list_conversations, yet get_current_conversation returns it.
    current = ConversationInfo(id="A", title="当前工作", source_kind=SOURCE_DESKTOP_ACTIVE,
                               is_current=True, is_verified=True)
    others = [ConversationInfo(id="B", title="其他", source_kind=SOURCE_CODEX_WORK)]
    svc = _Service(current, others)

    wizard = WizardDialog(svc)
    wizard._timer.stop()
    conv = wizard._selected_conversation()
    assert conv is not None and conv.id == "A", "preselected current must resolve via by_id"

    wizard._finish()
    assert svc.cfg.target.conversation_id == "A"
    assert svc.cfg.target.conversation_title == "当前工作"
    assert svc.cfg.task_lock.enabled is True

    # The wizard completion is persisted atomically.
    from pathlib import Path
    import json

    wf = Path(svc.cfg.data_dir) / "wizard.json"
    assert wf.exists()
    data = json.loads(wf.read_text(encoding="utf-8"))
    assert data["completed"] is True
    wizard.deleteLater()


def test_wizard_reject_is_skip_not_complete(app, tmp_path):
    from app.gui.main_window import WizardDialog

    current = ConversationInfo(id="A", title="X", source_kind=SOURCE_DESKTOP_ACTIVE)
    svc = _Service(current, [])
    wizard = WizardDialog(svc)
    wizard._timer.stop()
    wizard.reject()

    from pathlib import Path
    import json

    wf = Path(svc.cfg.data_dir) / "wizard.json"
    data = json.loads(wf.read_text(encoding="utf-8"))
    assert data["completed"] is False
    assert data["skipped"] is True
    wizard.deleteLater()
