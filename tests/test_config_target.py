"""Config atomic write + target round-trip."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.config import AppConfig, load_config, save_config


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig()
    cfg.target.conversation_id = "abc-123"
    cfg.target.conversation_title = "Project X"
    cfg.target.project_id = "ws-1"
    cfg.task_lock.enabled = True
    save_config(cfg, path)

    reloaded = load_config(path, create_if_missing=False)
    assert reloaded.target.conversation_id == "abc-123"
    assert reloaded.target.conversation_title == "Project X"
    assert reloaded.target.project_id == "ws-1"
    assert reloaded.task_lock.enabled is True


def test_atomic_write_leaves_no_tmp(tmp_path):
    path = tmp_path / "config.yaml"
    save_config(AppConfig(), path)
    leftovers = list(tmp_path.glob(".config-*"))
    assert leftovers == []
    # and the file parses
    assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)


def test_set_target_enables_task_lock(tmp_path):
    from app.service import AppService

    path = tmp_path / "config.yaml"
    save_config(AppConfig(), path)
    svc = AppService(config_path=path)
    svc.set_target(conversation_id="cid", conversation_title="CT")
    reloaded = load_config(path, create_if_missing=False)
    assert reloaded.target.conversation_id == "cid"
    assert reloaded.target.conversation_title == "CT"
    assert reloaded.task_lock.enabled is True


def test_set_prompt_writes_atomically(tmp_path):
    from app.service import AppService

    path = tmp_path / "config.yaml"
    save_config(AppConfig(), path)
    svc = AppService(config_path=path)
    svc.set_prompt("HELLO PROMPT")
    assert svc.prompt_text() == "HELLO PROMPT"
