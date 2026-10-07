"""V0.1 safety-hardening regression tests (TEST-01..15 from the brief)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import AppConfig, TargetConfig
from app.resume.test_send import SendTestJournal, run_test_send
from app.target import fingerprint_of, target_fingerprint


# --- TEST-09/10: test certification binds to the exact target -------------


def test_test_cert_is_bound_to_target(tmp_path):
    j = SendTestJournal(tmp_path / "test_send.json")
    j.begin("A", "Title A", "TEST")
    j.confirm("uia", "composer empty")

    # Same target -> valid
    assert j.is_valid_for_target(TargetConfig(conversation_id="A", conversation_title="Title A"))
    # Different id -> invalid
    assert not j.is_valid_for_target(TargetConfig(conversation_id="B", conversation_title="Title A"))
    # Same id, different title -> invalid (fingerprint differs)
    assert not j.is_valid_for_target(TargetConfig(conversation_id="A", conversation_title="Title X"))


def test_target_change_invalidates_cert(tmp_path):
    j = SendTestJournal(tmp_path / "test_send.json")
    j.begin("A", "Title A", "TEST")
    j.confirm("uia", "ok")
    assert j.passed

    # Switch to B: the prior cert must be invalidated.
    changed = j.invalidate_if_target_changed(TargetConfig(conversation_id="B", conversation_title="Title B"))
    assert changed is True
    assert j.record["status"] == "TEST_REQUIRED"
    assert not j.passed


def test_fingerprint_stable():
    assert target_fingerprint(TargetConfig(conversation_id="A", conversation_title="T")) == \
        fingerprint_of("A", "T")


# --- TEST-11: test PREPARED crash recovery --------------------------------


def test_test_send_prepared_crash_recovery(tmp_path):
    j = SendTestJournal(tmp_path / "test_send.json")
    j.begin("A", "Title A", "TEST")  # PREPARED on disk
    # Simulate a restart: load converts PREPARED -> UNCERTAIN.
    j2 = SendTestJournal(tmp_path / "test_send.json")
    assert j2.record["status"] == "UNCERTAIN"
    assert j2.uncertain


def test_uncertain_blocks_resend(tmp_path):
    from app.resume.test_send import SendTestJournal, run_test_send

    store = SendTestJournal(tmp_path / "test_send.json")
    store.begin("A", "Title A", "TEST")
    store.mark_uncertain("uia", "no confirmation")

    class _Cfg:
        target = TargetConfig(conversation_id="A", conversation_title="Title A")

    class _Ctrl:
        def is_running(self):
            return True

        def find_window(self):
            return object()

        def conversation_title(self):
            return "Title A"

        def is_busy(self):
            return False

        def send_prompt(self, text, *, dry_run=True):
            raise AssertionError("must not send while UNCERTAIN")

        def verify_sent(self, timeout=3.0):
            return (True, "ok")

    r = run_test_send(_Ctrl(), _Cfg(), None, store)
    assert r.ok is False
    assert r.status == "refused"
    assert "不确定" in r.reason


# --- TEST-12: preset fail-closed -----------------------------------------


def test_preset_missing_binding_fails_closed(tmp_path):
    from app.prompts.manager import PromptPresetManager, PromptResolutionError
    from app.prompts.models import ConversationBinding

    mgr = PromptPresetManager(data_dir=tmp_path)
    # Simulate a corrupted store: a binding points at a deleted/unknown preset.
    mgr._bindings["conv-A"] = ConversationBinding(
        conversation_id="conv-A", preset_id="nonexistent-preset"
    )
    mgr._persist_bindings()
    with pytest.raises(PromptResolutionError):
        mgr.resolve_prompt_strict("conv-A", fallback="FALLBACK")


def test_preset_unbound_uses_default(tmp_path):
    from app.prompts.manager import PromptPresetManager

    mgr = PromptPresetManager(data_dir=tmp_path)
    prompt, pid = mgr.resolve_prompt_strict("conv-Z", fallback="FALLBACK")
    assert pid == "continue-default"
    assert "继续执行" in prompt


# --- TEST-13: fake never in default config --------------------------------


def test_default_config_has_no_fake():
    cfg = AppConfig()
    assert "fake" not in cfg.usage.fallback_providers
    assert cfg.usage.provider != "fake"


# --- TEST-14/15: mtime-driven refresh ------------------------------------


def test_refresh_if_changed_detects_sidebar_mtime(tmp_path):
    from app.discovery import provider as provider_mod
    from app.discovery.provider import LocalChatGPTDiscoveryProvider

    # Use an isolated codex home + user data so we don't touch the real files.
    codex_home = tmp_path / "codex"
    user_data = tmp_path / "web"
    (codex_home).mkdir(parents=True)
    (user_data / "Default" / "Local Storage" / "leveldb").mkdir(parents=True)

    idx = codex_home / "session_index.jsonl"
    idx.write_text(json.dumps({"id": "A", "thread_name": "One", "updated_at": "2026-01-01T00:00:00Z"}) + "\n", encoding="utf-8")
    (codex_home / ".codex-global-state.json").write_text("{}", encoding="utf-8")
    (user_data / "browser-sidebar-page-states.json").write_text('{"pages":{}}', encoding="utf-8")

    p = LocalChatGPTDiscoveryProvider(user_data=user_data, codex_home=codex_home)
    assert [c.id for c in p.list_conversations()] == ["A"]

    # No change -> refresh_if_changed returns False, list unchanged.
    assert p.refresh_if_changed() is False

    # Touch the session index -> refresh_if_changed triggers a rescan.
    idx.write_text(
        json.dumps({"id": "B", "thread_name": "Two", "updated_at": "2026-01-02T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    assert p.refresh_if_changed() is True
    assert [c.id for c in p.list_conversations()] == ["B"]
