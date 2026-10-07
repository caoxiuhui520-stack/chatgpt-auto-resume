"""Real-send arming: the four conditions and the fake-provider hard stop.

A real send may only happen when ALL of these hold simultaneously:

* ``dry_run = false``
* ``real_send.armed = true``
* ``task_lock.enabled = true``
* no fake provider anywhere (primary or fallback)

A fake provider with ``dry_run=false`` is a configuration error and must
abort startup (SystemExit), never degrade silently.
"""

from __future__ import annotations

import pytest

from app.main import assess_send_mode, build_daemon


def _real_cfg(cfg):
    """Fixture cfg with a non-fake provider and no fake fallbacks."""
    cfg.dry_run = False
    cfg.usage.provider = "codex_app_server"
    cfg.usage.fallback_providers = []
    return cfg


def test_fake_primary_provider_with_real_send_is_refused(cfg):
    cfg.dry_run = False
    cfg.usage.provider = "fake"
    with pytest.raises(SystemExit):
        assess_send_mode(cfg)


def test_fake_fallback_provider_with_real_send_is_refused(cfg):
    cfg = _real_cfg(cfg)
    cfg.usage.fallback_providers = ["fake"]
    with pytest.raises(SystemExit):
        assess_send_mode(cfg)


def test_build_daemon_refuses_fake_with_real_send(cfg):
    cfg.dry_run = False
    cfg.usage.provider = "fake"
    with pytest.raises(SystemExit):
        build_daemon(cfg, controller_name="fake")


def test_fake_provider_is_fine_when_dry_run(cfg):
    cfg.dry_run = True
    cfg.usage.provider = "fake"
    mode, reason = assess_send_mode(cfg)
    assert mode == "monitor"
    assert "dry_run" in reason


def test_not_armed_means_monitor(cfg):
    cfg = _real_cfg(cfg)
    cfg.real_send.armed = False
    cfg.task_lock.enabled = True
    mode, reason = assess_send_mode(cfg)
    assert mode == "monitor"
    assert "armed" in reason


def test_armed_without_task_lock_means_monitor(cfg):
    cfg = _real_cfg(cfg)
    cfg.real_send.armed = True
    cfg.task_lock.enabled = False
    mode, reason = assess_send_mode(cfg)
    assert mode == "monitor"
    assert "task_lock" in reason


def test_all_conditions_arm_real_send(cfg):
    """Full core gate: dry_run=false + armed + task_lock + trusted target +
    valid test certification."""
    from app.discovery.models import ConversationInfo, SOURCE_CODEX_WORK
    from app.target import fingerprint_of

    cfg = _real_cfg(cfg)
    cfg.real_send.armed = True
    cfg.task_lock.enabled = True
    cfg.target.conversation_id = "abc"
    cfg.target.conversation_title = "Project X"

    class _Discovery:
        def get_by_id(self, cid):
            return ConversationInfo(id=cid, title="Project X", source_kind=SOURCE_CODEX_WORK)

    class _TestStore:
        uncertain = False

        def is_valid_for_target(self, target):
            return target.conversation_id == "abc"

    class _State:
        pending_send_status = "NONE"

    mode, reason = assess_send_mode(cfg, _Discovery(), _TestStore(), _State())
    assert mode == "armed", reason


def test_arm_blocked_without_valid_test_cert(cfg):
    from app.discovery.models import ConversationInfo, SOURCE_CODEX_WORK

    cfg = _real_cfg(cfg)
    cfg.real_send.armed = True
    cfg.task_lock.enabled = True
    cfg.target.conversation_id = "abc"
    cfg.target.conversation_title = "Project X"

    class _Discovery:
        def get_by_id(self, cid):
            return ConversationInfo(id=cid, title="Project X", source_kind=SOURCE_CODEX_WORK)

    class _TestStore:
        uncertain = False

        def is_valid_for_target(self, target):
            return False  # no valid certification

    class _State:
        pending_send_status = "NONE"

    mode, reason = assess_send_mode(cfg, _Discovery(), _TestStore(), _State())
    assert mode == "monitor"
    assert "certification" in reason


def test_arm_blocked_with_untrusted_target(cfg):
    from app.discovery.models import ConversationInfo, SOURCE_WEB_CACHE

    cfg = _real_cfg(cfg)
    cfg.real_send.armed = True
    cfg.task_lock.enabled = True
    cfg.target.conversation_id = "w1"
    cfg.target.conversation_title = "Web"

    class _Discovery:
        def get_by_id(self, cid):
            return ConversationInfo(id=cid, title="Web", source_kind=SOURCE_WEB_CACHE)

    class _State:
        pending_send_status = "NONE"

    mode, reason = assess_send_mode(cfg, _Discovery(), None, _State())
    assert mode == "monitor"
    assert "untrusted" in reason
