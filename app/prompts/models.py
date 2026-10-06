"""Prompt preset system.

A preset is a named, reusable resume prompt. Auto Resume resolves which
preset to send for a given target conversation (bound preset → default
preset → the built-in "continue" preset) and renders a small set of safe
variables into it. Presets persist in ``data/prompt_presets.json`` and
conversation bindings in ``data/conversation_bindings.json`` - never in
``config.yaml``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from app.models import utcnow


@dataclass(slots=True)
class PromptPreset:
    id: str
    name: str
    description: str = ""
    content: str = ""
    created_at: str = ""
    updated_at: str = ""
    builtin: bool = False
    favorite: bool = False
    last_used_at: str | None = None
    bound_conversation_id: str = ""
    bound_project_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PromptPreset":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass(slots=True)
class ConversationBinding:
    conversation_id: str
    preset_id: str
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ConversationBinding":
        return cls(
            conversation_id=d.get("conversation_id", ""),
            preset_id=d.get("preset_id", ""),
            updated_at=d.get("updated_at", ""),
        )
