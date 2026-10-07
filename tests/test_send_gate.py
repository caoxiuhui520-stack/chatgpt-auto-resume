"""Supervised test send: transaction isolation + resolver gating.

The test send must never touch the resume transaction (``last_triggered_reset_id``
or the resume ``pending_send_*`` fields) - it has its own durable record and
its own transaction type.
"""

from __future__ import annotations

from app.resume.test_send import SendTestJournal, run_test_send
from tests.test_gui import StubService  # noqa: F401  (reuse the stub shape)


class _FakeController:
    def __init__(self, running=True, window=True, busy=False, title="Target", send_ok=True,
                 confirm=(True, "composer empty")):
        self.running = running
        self.window = window
        self.busy = busy
        self.title = title
        self.send_ok = send_ok
        self.confirm = confirm
        self.sent = []

    def is_running(self):
        return self.running

    def find_window(self):
        return object() if self.window else None

    def conversation_title(self):
        return self.title

    def is_busy(self):
        return self.busy

    def send_prompt(self, text, *, dry_run=True):
        assert dry_run is False, "test send must never take the dry-run branch silently"
        self.sent.append(text)
        if not self.send_ok:
            from app.models import ResumeResult, ErrorKind
            return ResumeResult(False, ErrorKind.SEND_FAILED, "boom")
        from app.models import ResumeResult
        return ResumeResult(True, None, "uia-value-pattern+send-button")

    def verify_sent(self, timeout=3.0):
        return self.confirm


class _FakeCfg:
    def __init__(self, cid="abc", title="Target"):
        from app.config import TargetConfig

        self.target = TargetConfig(conversation_id=cid, conversation_title=title)


class _FakeDiscovery:
    def __init__(self, current_id="abc"):
        self.current_id = current_id

    def refresh(self):
        pass

    def get_current_conversation(self):
        from app.discovery.models import ConversationInfo

        return ConversationInfo(id=self.current_id, title="Target")

    def get_by_id(self, cid):
        from app.discovery.models import ConversationInfo, SOURCE_CODEX_WORK

        if cid == "abc":
            return ConversationInfo(id="abc", title="Target", source_kind=SOURCE_CODEX_WORK)
        return None


def _store(tmp_path):
    return SendTestJournal(tmp_path / "test_send.json")


def test_refused_without_target(tmp_path):
    cfg = _FakeCfg(cid="", title="")
    r = run_test_send(_FakeController(), cfg, None, _store(tmp_path))
    assert r.ok is False and r.status == "refused"


def test_refused_on_target_mismatch(tmp_path):
    cfg = _FakeCfg(cid="abc", title="Target")
    # current id "def" differs from target "abc" → mismatch
    r = run_test_send(_FakeController(title="Other"), cfg, _FakeDiscovery(current_id="def"),
                      _store(tmp_path))
    assert r.ok is False and r.status == "refused"
    assert "mismatch" in r.reason or "不匹配" in r.reason or "不是目标" in r.reason


def test_passed_confirms_transaction(tmp_path):
    cfg = _FakeCfg()
    store = _store(tmp_path)
    ctrl = _FakeController()
    r = run_test_send(ctrl, cfg, _FakeDiscovery(), store)
    assert r.ok is True and r.status == "passed"
    assert store.record["status"] == "CONFIRMED"
    assert store.record["transaction_type"] == "test"
    assert ctrl.sent == ["AUTO RESUME TEST\n请只回复：OK"]
    # Reload from disk: durable.
    reloaded = SendTestJournal(tmp_path / "test_send.json")
    assert reloaded.passed


def test_uncertain_marks_transaction(tmp_path):
    cfg = _FakeCfg()
    store = _store(tmp_path)
    ctrl = _FakeController(confirm=(False, "no positive confirmation"))
    r = run_test_send(ctrl, cfg, _FakeDiscovery(), store)
    assert r.ok is False and r.status == "uncertain"
    assert store.record["status"] == "UNCERTAIN"


def test_failed_send_aborts(tmp_path):
    cfg = _FakeCfg()
    store = _store(tmp_path)
    ctrl = _FakeController(send_ok=False)
    r = run_test_send(ctrl, cfg, _FakeDiscovery(), store)
    assert r.ok is False and r.status == "failed"
    assert store.record["status"] == "NONE"


def test_busy_refuses(tmp_path):
    cfg = _FakeCfg()
    ctrl = _FakeController(busy=True)
    r = run_test_send(ctrl, cfg, None, _store(tmp_path))
    assert r.ok is False and r.status == "refused"
    assert ctrl.sent == []


def test_test_transaction_never_touches_resume_state(tmp_path):
    """The store writes to its own file only - no resume reset_id involved."""
    cfg = _FakeCfg()
    store = _store(tmp_path)
    run_test_send(_FakeController(), cfg, _FakeDiscovery(), store)
    raw = (tmp_path / "test_send.json").read_text(encoding="utf-8")
    assert '"transaction_type": "test"' in raw
    assert "last_triggered_reset_id" not in raw
