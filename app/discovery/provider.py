"""Conversation discovery over local ChatGPT Desktop data.

Read-only. The provider reads the app's local ``Local Storage`` LevelDB
(``codex.chatgpt-conversations``) for the conversation list and the
``browser-sidebar-page-states.json`` side-car for the currently open
conversation id. It never writes, never calls a private web API, never reads
cookies, and never uploads anything.

Measured data shape (ChatGPT Desktop, ``OpenAI.Codex`` MSIX bundle):

* Local Storage key ``codex.chatgpt-conversations`` → JSON
  ``{"pageParams":[...],"pages":[{"items":[{"id","title","create_time",
  "update_time","workspace_id",...}]}],"version":...}``
* ``browser-sidebar-page-states.json`` → ``{"pages": { "<key>": {
  "conversationId", "page": {"updatedAt", "title", ...} }}, "version":...}``
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from app.discovery import paths
from app.discovery.leveldb_reader import LevelDB
from app.discovery.models import (
    ConversationInfo,
    ProjectInfo,
    SOURCE_CODEX_LOCAL_STORAGE,
    SOURCE_DESKTOP_ACTIVE,
    SOURCE_UNKNOWN,
)
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    pass

log = get_logger("discovery")

CONVERSATIONS_KEY = b"codex.chatgpt-conversations"
PINNED_KEY = b"codex.chatgpt-pinned-conversations"
DEVICE_ID_KEY = b"codex.chatgpt-conversations.device-id"


def _decode_local_storage_value(value: bytes) -> str:
    """LocalStorage values are stored as a 1-byte type tag + UTF-16LE string.

    ``0x00``/``0x01`` are both seen as type tags in the wild; a UTF-8 fallback
    covers values written by other code paths.
    """
    if not value:
        return ""
    body = value[1:] if value[0] in (0x00, 0x01) else value
    try:
        text = body.decode("utf-16-le")
        # A correctly decoded JSON string never starts with a control char.
        if text.lstrip("\ufeff").lstrip()[:1] in ("{", "[") or "title" in text:
            return text
    except UnicodeDecodeError:
        pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _conversation_rows(payload: str) -> list[dict]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []
    rows: list[dict] = []
    for page in data.get("pages", []):
        for item in page.get("items", []):
            if isinstance(item, dict) and item.get("id"):
                rows.append(item)
    return rows


class ConversationDiscoveryProvider:
    """Interface every discovery source implements."""

    name: str = "base"

    def list_projects(self) -> list[ProjectInfo]:
        raise NotImplementedError

    def list_conversations(self, project_id: str | None = None) -> list[ConversationInfo]:
        raise NotImplementedError

    def get_current_conversation(self) -> ConversationInfo | None:
        raise NotImplementedError

    def get_conversation_by_id(self, conversation_id: str) -> ConversationInfo | None:
        raise NotImplementedError

    def resolve_current_conversation(self, uia_title: str = "") -> ConversationInfo | None:
        raise NotImplementedError

    def refresh(self) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class LocalChatGPTDiscoveryProvider(ConversationDiscoveryProvider):
    """The real implementation, backed by the local user-data directory."""

    name = "local"

    def __init__(self, user_data: str | Path | None = None) -> None:
        self._user_data: Path | None = None
        self._explicit = user_data
        self._conversations: list[ConversationInfo] = []
        self._projects: list[ProjectInfo] = []
        self._current_id: str = ""
        self._available = False
        self.refresh()

    # -- availability ------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    def _resolve(self) -> Path | None:
        self._user_data = paths.resolve_user_data_dir(self._explicit)
        return self._user_data

    # -- refresh -----------------------------------------------------------

    def refresh(self) -> None:
        root = self._resolve()
        if root is None:
            log.info("ChatGPT user-data directory not found; discovery disabled")
            self._available = False
            self._conversations = []
            self._projects = []
            self._current_id = ""
            return

        self._conversations = self._read_conversations(root)
        self._projects = self._read_projects()
        self._current_id = self._read_current_id(root)
        self._available = True
        log.info(
            "discovery: %d conversations, %d projects, current=%s",
            len(self._conversations),
            len(self._projects),
            self._current_id or "(none)",
        )

    def _read_conversations(self, root: Path) -> list[ConversationInfo]:
        leveldb_dir = paths.local_storage_dir(root)
        if not leveldb_dir.is_dir():
            return []
        try:
            items = LevelDB(leveldb_dir).items()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read local storage: %s", exc)
            return []

        merged: dict[str, dict] = {}
        for key, value in items.items():
            if CONVERSATIONS_KEY not in key or DEVICE_ID_KEY in key or PINNED_KEY in key:
                continue
            payload = _decode_local_storage_value(value)
            for row in _conversation_rows(payload):
                cid = row.get("id")
                if not cid:
                    continue
                existing = merged.get(cid)
                # Keep the newest update_time across the multiple versions the
                # app keeps around.
                if existing is None or (row.get("update_time") or "") > (
                    existing.get("update_time") or ""
                ):
                    merged[cid] = row

        conversations = [self._to_info(row) for row in merged.values()]
        conversations.sort(key=lambda c: c.updated_at or c.title, reverse=True)
        return conversations

    def _to_info(self, row: dict) -> ConversationInfo:
        return ConversationInfo(
            id=row.get("id", ""),
            title=row.get("title", "") or "",
            project_id=row.get("workspace_id") or "",
            updated_at=row.get("update_time"),
            source=SOURCE_CODEX_LOCAL_STORAGE,
            source_kind=SOURCE_CODEX_LOCAL_STORAGE,
            is_cached=True,
        )

    def _read_projects(self) -> list[ProjectInfo]:
        # ChatGPT's local cache carries workspace_id per conversation but no
        # workspace *name*. When workspaces are in use, surface them by id;
        # when absent, expose a single default bucket so the UI is not empty.
        seen: dict[str, str] = {}
        for c in self._conversations:
            if c.project_id and c.project_id not in seen:
                seen[c.project_id] = c.project_id
        return [ProjectInfo(id=pid, name=pid, source=self.name) for pid in seen]

    def _read_current_id(self, root: Path) -> str:
        sidebar = paths.sidebar_states_file(root)
        if not sidebar.is_file():
            return ""
        try:
            data = json.loads(sidebar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""

        # The most recently updated page is the best proxy for "the tab the
        # user is looking at". Skip pages that failed to load.
        best_id = ""
        best_updated = -1
        for entry in data.get("pages", {}).values():
            if not isinstance(entry, dict):
                continue
            cid = entry.get("conversationId") or ""
            if not cid:
                continue
            page = entry.get("page") or {}
            updated = page.get("updatedAt") or 0
            title = page.get("title") or ""
            if "无法访问" in title:
                continue
            if updated > best_updated:
                best_updated = updated
                best_id = cid
        return best_id

    # -- queries -----------------------------------------------------------

    def list_projects(self) -> list[ProjectInfo]:
        return list(self._projects)

    def list_conversations(self, project_id: str | None = None) -> list[ConversationInfo]:
        if project_id is None:
            return list(self._conversations)
        return [c for c in self._conversations if c.project_id == project_id]

    def get_current_conversation(self) -> ConversationInfo | None:
        """The conversation currently open in the desktop UI (execution truth).

        Sourced from the app's own sidebar tab state, cross-checked against the
        local conversation cache to attach a title when available.
        """
        if not self._current_id:
            return None
        for c in self._conversations:
            if c.id == self._current_id:
                info = ConversationInfo(
                    id=c.id,
                    title=c.title,
                    project_id=c.project_id,
                    updated_at=c.updated_at,
                    source=SOURCE_DESKTOP_ACTIVE,
                    source_kind=SOURCE_DESKTOP_ACTIVE,
                    is_current=True,
                    is_verified=True,
                    confidence=0.95,
                )
                return info
        # The open tab is an unsaved / not-yet-listed thread.
        return ConversationInfo(
            id=self._current_id,
            title="",
            source=SOURCE_DESKTOP_ACTIVE,
            source_kind=SOURCE_DESKTOP_ACTIVE,
            is_current=True,
            is_verified=False,
            confidence=0.6,
        )

    def get_by_id(self, conversation_id: str) -> ConversationInfo | None:
        for c in self._conversations:
            if c.id == conversation_id:
                return c
        return None

    def get_conversation_by_id(self, conversation_id: str) -> ConversationInfo | None:
        return self.get_by_id(conversation_id)

    def resolve_current_conversation(self, uia_title: str = "") -> ConversationInfo | None:
        """Best-effort identity of the open conversation, combining the desktop
        sidebar id with the UIA selected title.

        Confidence drops (and verification is withdrawn) when the two signals
        disagree or when the title is ambiguous - the caller must then refuse
        to send rather than guess.
        """
        current = self.get_current_conversation()
        if current is None:
            return None

        title = (uia_title or "").strip()
        if not title:
            # No UIA title: trust the sidebar id alone, with reduced confidence.
            current.confidence = min(current.confidence, 0.5)
            current.is_verified = False
            return current

        # Sidebar id maps to a cached conversation whose title matches UIA.
        if current.title and current.title.strip() == title:
            current.confidence = 0.95
            current.is_verified = True
            return current

        # Sidebar id not in cache, but the UIA title uniquely maps to one
        # cached conversation: that conversation is what is on screen.
        matches = self.find_by_title(title)
        if len(matches) == 1:
            resolved = matches[0]
            return ConversationInfo(
                id=resolved.id,
                title=resolved.title,
                project_id=resolved.project_id,
                updated_at=resolved.updated_at,
                source=SOURCE_DESKTOP_ACTIVE,
                source_kind=SOURCE_DESKTOP_ACTIVE,
                is_current=True,
                is_verified=True,
                confidence=0.85,
            )

        # Ambiguous or unresolved title: do not guess.
        current.confidence = 0.3
        current.is_verified = False
        return current

    def find_by_title(self, title: str) -> list[ConversationInfo]:
        """All conversations whose title matches exactly (whitespace-insensitive).

        More than one result means the title is ambiguous - callers must NOT
        guess.
        """
        needle = (title or "").strip()
        if not needle:
            return []
        return [c for c in self._conversations if (c.title or "").strip() == needle]

    def describe(self) -> str:
        if not self._available:
            return "local (unavailable)"
        return f"local ({len(self._conversations)} conversations)"
