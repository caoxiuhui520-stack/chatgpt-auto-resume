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


def test_all_four_conditions_arm_real_send(cfg):
    cfg = _real_cfg(cfg)
    cfg.real_send.armed = True
    cfg.task_lock.enabled = True
    mode, _reason = assess_send_mode(cfg)
    assert mode == "armed"
