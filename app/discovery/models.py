"""Unified models for local conversation discovery.

These types are the only thing the GUI (or any other caller) is allowed to
know about local ChatGPT data. The discovery provider underneath may read
LevelDB, JSON side-car files, or anything else - callers never touch those
directly, so a future data-format change only means swapping the provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(slots=True)
class ProjectInfo:
    id: str
    name: str
    source: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "source": self.source}


@dataclass(slots=True)
class ConversationInfo:
    id: str
    title: str = ""
    project_id: str = ""
    updated_at: datetime | None = None
    source: str = "local"
    #: True when the id is a client-side placeholder ("client-new-thread:..."),
    #: i.e. an unsaved conversation that cannot be reliably re-targeted.
    is_ephemeral: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.updated_at, str):
            self.updated_at = _parse_dt(self.updated_at)
        if self.id.startswith("client-new-thread:"):
            self.is_ephemeral = True

    @property
    def display_title(self) -> str:
        return (self.title or "").strip() or "(untitled)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.display_title,
            "project_id": self.project_id,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "source": self.source,
            "is_ephemeral": self.is_ephemeral,
        }


@dataclass(slots=True)
class ConversationMatch:
    """Result of resolving a target identity against the local conversation."""

    status: str  # "matched" | "mismatch" | "ambiguous" | "unknown" | "not_configured"
    target: ConversationInfo | None = None
    current: ConversationInfo | None = None
    reason: str = ""
    matched_by: str = ""  # "conversation_id" | "conversation_title" | ""

    @property
    def ok(self) -> bool:
        return self.status == "matched"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": self.target.to_dict() if self.target else None,
            "current": self.current.to_dict() if self.current else None,
            "reason": self.reason,
            "matched_by": self.matched_by,
        }
