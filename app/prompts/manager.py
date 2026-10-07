"""Prompt preset management: CRUD, default, favorite, binding, resolution."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from app.models import utcnow
from app.prompts import builtins
from app.prompts.models import ConversationBinding, PromptPreset
from app.prompts.renderer import render_prompt
from app.prompts.store import JsonFile
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig

log = get_logger("prompts.manager")

RESERVED_IDS = {"__default__"}


class PromptPresetManager:
    """Owns presets and conversation bindings. Thread-safe enough for the GUI
    (single writer) while remaining unit-testable without Qt."""

    def __init__(self, cfg: "AppConfig | None" = None, data_dir: Path | None = None) -> None:
        if data_dir is None and cfg is not None:
            data_dir = Path(cfg.data_dir)
        self.data_dir = data_dir or Path("data")
        self.presets_file = JsonFile(self.data_dir / "prompt_presets.json", {})
        self.bindings_file = JsonFile(self.data_dir / "conversation_bindings.json", {})
        self._presets: dict[str, PromptPreset] = {}
        self._bindings: dict[str, ConversationBinding] = {}
        self._default_preset_id = builtins.DEFAULT_PRESET_ID
        self._seed()

    # -- loading / seeding ------------------------------------------------

    def _seed(self) -> None:
        raw = self.presets_file.load()
        if not isinstance(raw, dict) or not raw.get("presets"):
            # First run: install builtins and persist.
            self._presets = {p.id: p for p in builtins.builtin_presets()}
            self._default_preset_id = builtins.DEFAULT_PRESET_ID
            self._persist_presets()
        else:
            self._presets = {
                pid: PromptPreset.from_dict(d) for pid, d in raw["presets"].items()
            }
            self._default_preset_id = raw.get("default_preset_id", builtins.DEFAULT_PRESET_ID)
            # Re-seed any missing builtins (e.g. after an upgrade).
            changed = False
            for p in builtins.builtin_presets():
                if p.id not in self._presets:
                    self._presets[p.id] = p
                    changed = True
            if self._default_preset_id not in self._presets:
                self._default_preset_id = builtins.DEFAULT_PRESET_ID
                changed = True
            if changed:
                self._persist_presets()

        raw_bindings = self.bindings_file.load()
        if isinstance(raw_bindings, dict):
            self._bindings = {
                cid: ConversationBinding.from_dict(b) for cid, b in raw_bindings.items()
            }
        else:
            self._bindings = {}

    def _persist_presets(self) -> None:
        self.presets_file.data = {
            "presets": {pid: p.to_dict() for pid, p in self._presets.items()},
            "default_preset_id": self._default_preset_id,
        }
        self.presets_file.save()

    def _persist_bindings(self) -> None:
        self.bindings_file.data = {
            cid: b.to_dict() for cid, b in self._bindings.items()
        }
        self.bindings_file.save()

    # -- presets ----------------------------------------------------------

    def list_presets(self) -> list[PromptPreset]:
        presets = list(self._presets.values())
        presets.sort(key=lambda p: (not p.favorite, p.name))
        return presets

    def get(self, preset_id: str) -> PromptPreset | None:
        return self._presets.get(preset_id)

    @property
    def default_preset_id(self) -> str:
        return self._default_preset_id

    def default_preset(self) -> PromptPreset | None:
        return self._presets.get(self._default_preset_id)

    def create(self, name: str, content: str, description: str = "") -> PromptPreset:
        now = utcnow().isoformat()
        preset = PromptPreset(
            id=f"user-{uuid.uuid4().hex[:8]}",
            name=(name or "").strip() or "Untitled",
            description=description.strip(),
            content=content,
            created_at=now,
            updated_at=now,
        )
        self._presets[preset.id] = preset
        self._persist_presets()
        return preset

    def update(self, preset_id: str, **fields: object) -> PromptPreset | None:
        preset = self._presets.get(preset_id)
        if preset is None:
            return None
        for key, value in fields.items():
            if key in ("id", "builtin", "created_at"):
                continue
            setattr(preset, key, value)
        preset.updated_at = utcnow().isoformat()
        self._persist_presets()
        return preset

    def duplicate(self, preset_id: str) -> PromptPreset | None:
        src = self._presets.get(preset_id)
        if src is None:
            return None
        return self.create(f"{src.name} (copy)", src.content, src.description)

    def delete(self, preset_id: str) -> bool:
        preset = self._presets.get(preset_id)
        if preset is None or preset.builtin:
            return False
        del self._presets[preset_id]
        if self._default_preset_id == preset_id:
            self._default_preset_id = builtins.DEFAULT_PRESET_ID
        # Drop bindings pointing at the deleted preset.
        self._bindings = {
            cid: b for cid, b in self._bindings.items() if b.preset_id != preset_id
        }
        self._persist_presets()
        self._persist_bindings()
        return True

    def set_default(self, preset_id: str) -> bool:
        if preset_id not in self._presets:
            return False
        self._default_preset_id = preset_id
        self._persist_presets()
        return True

    def toggle_favorite(self, preset_id: str) -> bool | None:
        preset = self._presets.get(preset_id)
        if preset is None:
            return None
        preset.favorite = not preset.favorite
        self._persist_presets()
        return preset.favorite

    def reset_builtin(self, preset_id: str) -> PromptPreset | None:
        preset = self._presets.get(preset_id)
        if preset is None or not preset.builtin:
            return None
        for builtin in builtins.builtin_presets():
            if builtin.id == preset_id:
                preset.name = builtin.name
                preset.description = builtin.description
                preset.content = builtin.content
                preset.updated_at = utcnow().isoformat()
                self._persist_presets()
                return preset
        return None

    # -- bindings ---------------------------------------------------------

    def set_binding(self, conversation_id: str, preset_id: str) -> None:
        if not conversation_id or preset_id not in self._presets:
            return
        self._bindings[conversation_id] = ConversationBinding(
            conversation_id=conversation_id,
            preset_id=preset_id,
            updated_at=utcnow().isoformat(),
        )
        self._persist_bindings()

    def clear_binding(self, conversation_id: str) -> None:
        if conversation_id in self._bindings:
            del self._bindings[conversation_id]
            self._persist_bindings()

    def get_binding(self, conversation_id: str) -> ConversationBinding | None:
        return self._bindings.get(conversation_id)

    def preset_for_conversation(self, conversation_id: str) -> PromptPreset | None:
        binding = self._bindings.get(conversation_id)
        if binding is not None:
            preset = self._presets.get(binding.preset_id)
            if preset is not None:
                return preset
        return self.default_preset()

    # -- resolution -------------------------------------------------------

    def resolve_prompt(
        self,
        conversation_id: str,
        *,
        fallback: str = "",
        context: Mapping[str, str] | None = None,
    ) -> tuple[str, str]:
        """Return (rendered_prompt, preset_id). ``conversation_id`` selects the
        bound preset (or the default); ``fallback`` is used only when nothing
        else is available.

        This is the *lenient* variant (used for previews/diagnostics). The
        send path must use :meth:`resolve_prompt_strict`, which fails closed.
        """
        preset = self.preset_for_conversation(conversation_id)
        if preset is None:
            return fallback, ""
        return self._render(preset, context)

    def resolve_prompt_strict(
        self,
        conversation_id: str,
        *,
        fallback: str = "",
        context: Mapping[str, str] | None = None,
    ) -> tuple[str, str]:
        """Fail-closed resolution for real/test sends.

        * An explicit binding whose preset is missing/undecodable, or a
          rendering failure, raises :class:`PromptResolutionError` - never a
          silent fallback to a different prompt.
        * Only when there is *no* explicit binding AND no default preset is
          the legacy ``fallback`` (continue.txt) used, and it is logged.
        """
        binding = self.get_binding(conversation_id)
        if binding is not None:
            preset = self._presets.get(binding.preset_id)
            if preset is None:
                raise PromptResolutionError(
                    f"对话 {conversation_id} 绑定的 preset 不存在或已损坏: {binding.preset_id}"
                )
            return self._render(preset, context)

        preset = self.default_preset()
        if preset is None:
            log.warning("legacy fallback used: no default preset available")
            return fallback, ""
        return self._render(preset, context)

    def _render(self, preset: PromptPreset, context: Mapping[str, str] | None) -> tuple[str, str]:
        try:
            rendered = render_prompt(preset.content, context or {})
        except Exception as exc:  # noqa: BLE001
            raise PromptResolutionError(f"preset {preset.id} 渲染失败: {exc}") from exc
        # P1-4: persist last_used_at, but only on real resolution (not preview).
        preset.last_used_at = utcnow().isoformat()
        preset.updated_at = preset.updated_at or preset.created_at
        self._persist_presets()
        return rendered, preset.id


class PromptResolutionError(Exception):
    """A preset could not be resolved safely. The caller must NOT send."""
