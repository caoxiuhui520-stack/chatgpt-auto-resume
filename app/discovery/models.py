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


#: Coarse classification of where a conversation came from. Local cache is a
#: *candidate* source; the desktop UI is the execution truth.
#:
#: Measured reality (2026-10-07, user-confirmed):
#: - ``codex_work_session``  -> the ChatGPT Desktop (Codex) Work/Agent threads
#:   listed in ``~/.codex/session_index.jsonl``. This is what the desktop app
#:   actually drives.
#: - ``web_cache``           -> the embedded chatgpt.com browser cache inside
#:   the desktop app (``%APPDATA%\\Codex\\web\\Codex``). Its conversation list
#:   mirrors the chatgpt.com *web* sidebar, NOT the desktop Work threads.
#: - ``desktop_active``      -> the conversation currently open in the desktop
#:   UI (execution truth).
SOURCE_DESKTOP_ACTIVE = "desktop_active"
SOURCE_DESKTOP_CACHE = "desktop_cache"
SOURCE_CODEX_WORK = "codex_work_session"
SOURCE_CODEX_LOCAL_STORAGE = "codex_local_storage"
SOURCE_WEB_CACHE = "web_cache"
SOURCE_UNKNOWN = "unknown"

KNOWN_SOURCE_KINDS = (
    SOURCE_DESKTOP_ACTIVE,
    SOURCE_DESKTOP_CACHE,
    SOURCE_CODEX_WORK,
    SOURCE_CODEX_LOCAL_STORAGE,
    SOURCE_WEB_CACHE,
    SOURCE_UNKNOWN,
)


@dataclass(slots=True)
class ConversationInfo:
    id: str
    title: str = ""
    project_id: str = ""
    updated_at: datetime | None = None
    source: str = "local"
    #: Coarse classification - desktop_active is execution truth, the rest are
    #: candidates and must never be treated as "the open conversation".
    source_kind: str = SOURCE_UNKNOWN
    #: True when the id is a client-side placeholder ("client-new-thread:..."),
    #: i.e. an unsaved conversation that cannot be reliably re-targeted.
    is_ephemeral: bool = False
    #: This conversation is currently open in the desktop UI (execution truth).
    is_current: bool = False
    #: This conversation is the configured resume target.
    is_target: bool = False
    #: This entry comes from a local cache, not a live read of the open app.
    is_cached: bool = False
    #: The id/title were cross-checked against the live desktop UI.
    is_verified: bool = False
    #: 0.0..1.0 - how much to trust this as "the current desktop conversation".
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if isinstance(self.updated_at, str):
            self.updated_at = _parse_dt(self.updated_at)
        if self.id.startswith("client-new-thread:"):
            self.is_ephemeral = True
        if self.source_kind not in KNOWN_SOURCE_KINDS:
            self.source_kind = SOURCE_UNKNOWN

    @property
    def display_title(self) -> str:
        return (self.title or "").strip() or "(untitled)"

    @property
    def short_id(self) -> str:
        return self.id[:8] if self.id else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.display_title,
            "project_id": self.project_id,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "source": self.source,
            "source_kind": self.source_kind,
            "is_ephemeral": self.is_ephemeral,
            "is_current": self.is_current,
            "is_target": self.is_target,
            "is_cached": self.is_cached,
            "is_verified": self.is_verified,
            "confidence": self.confidence,
        }


@dataclass(slots=True)
class ConversationMatch:
    """Result of resolving a target identity against the live conversation.

    ``status`` is one of the identity states below. ``ok`` is true only for a
    fully authorised ``matched`` (dual-factor: id AND title). ``authorized`` is
    the flag the send path may consult: title-only matches are reported for
    diagnostics but are never authorised for a real send.
    """

    status: str  # see MATCH_STATUSES below
    target: ConversationInfo | None = None
    current: ConversationInfo | None = None
    reason: str = ""
    matched_by: str = ""  # "conversation_id" | "conversation_title" | ""
    authorized: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "matched" and self.authorized

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": self.target.to_dict() if self.target else None,
            "current": self.current.to_dict() if self.current else None,
            "reason": self.reason,
            "matched_by": self.matched_by,
            "authorized": self.authorized,
        }


#: Identity-verification states. Only ``matched`` (authorized) permits a real
#: send; every other state refuses.
MATCH_STATUSES = (
    "not_configured",
    "matched",
    "mismatch",
    "identity_conflict",
    "ambiguous",
    "unknown",
    "web_cache_target",
    "ephemeral_target",
)
