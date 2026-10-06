"""Prompt preset system: seeding, CRUD, bindings, safe rendering."""

from __future__ import annotations

from app.prompts import builtins
from app.prompts.manager import PromptPresetManager
from app.prompts.renderer import render_prompt


def test_seeds_builtins(tmp_path):
    mgr = PromptPresetManager(data_dir=tmp_path)
    presets = mgr.list_presets()
    assert len(presets) == 5
    assert all(p.builtin for p in presets)
    assert mgr.default_preset_id == "continue-default"


def test_builtins_persist_across_reload(tmp_path):
    PromptPresetManager(data_dir=tmp_path)
    mgr2 = PromptPresetManager(data_dir=tmp_path)
    assert len(mgr2.list_presets()) == 5


def test_create_edit_delete(tmp_path):
    mgr = PromptPresetManager(data_dir=tmp_path)
    p = mgr.create("My Task", "do the thing", "desc")
    assert p.id.startswith("user-")
    assert mgr.get(p.id).content == "do the thing"

    mgr.update(p.id, name="Renamed", content="new content")
    assert mgr.get(p.id).name == "Renamed"

    assert mgr.delete(p.id) is True
    assert mgr.get(p.id) is None


def test_builtin_cannot_be_deleted_but_can_reset(tmp_path):
    mgr = PromptPresetManager(data_dir=tmp_path)
    assert mgr.delete("continue-default") is False
    mgr.update("continue-default", content="EDITED")
    assert mgr.get("continue-default").content == "EDITED"
    mgr.reset_builtin("continue-default")
    assert "继续执行当前已经确定的目标和计划" in mgr.get("continue-default").content


def test_duplicate_and_favorite(tmp_path):
    mgr = PromptPresetManager(data_dir=tmp_path)
    dup = mgr.duplicate("continue-default")
    assert dup.id != "continue-default"
    assert dup.content == mgr.get("continue-default").content
    assert mgr.toggle_favorite(dup.id) is True
    assert mgr.get(dup.id).favorite is True


def test_binding_resolution(tmp_path):
    mgr = PromptPresetManager(data_dir=tmp_path)
    mgr.set_binding("conv-A", "continue-fix")
    # bound preset wins
    preset = mgr.preset_for_conversation("conv-A")
    assert preset.id == "continue-fix"
    # unbound falls back to default
    assert mgr.preset_for_conversation("conv-Z").id == "continue-default"
    # clearing binding reverts to default
    mgr.clear_binding("conv-A")
    assert mgr.preset_for_conversation("conv-A").id == "continue-default"


def test_resolve_prompt_renders_variables(tmp_path):
    mgr = PromptPresetManager(data_dir=tmp_path)
    mgr.set_default("continue-default")
    prompt, preset_id = mgr.resolve_prompt(
        "conv-A",
        fallback="FALLBACK",
        context={"current_time": "2026-10-07 10:00", "conversation_title": "X"},
    )
    assert preset_id == "continue-default"
    assert "继续执行" in prompt


def test_render_whitelist_and_unknown_tokens():
    out = render_prompt("Hello {{conversation_title}} / {{evil}} / {{current_time}}",
                        {"conversation_title": "ABC", "evil": "IGNORED", "current_time": "now"})
    assert "ABC" in out
    assert "now" in out
    # unknown token left untouched, its value never substituted
    assert "{{evil}}" in out
    assert "IGNORED" not in out


def test_render_missing_becomes_placeholder():
    out = render_prompt("{{conversation_title}}", {})
    assert "unavailable" in out


def test_unknown_variable_not_in_whitelist_is_untouched():
    out = render_prompt("{{not_a_real_var}}", {"not_a_real_var": "x"})
    assert "{{not_a_real_var}}" in out
