"""Conversation discovery over local ChatGPT Desktop / Codex data.

Read-only. Three local sources, each clearly classified:

* ``~/.codex/session_index.jsonl`` -> **codex_work_session**: the desktop
  Work/Agent threads (what the desktop app actually drives - user-confirmed).
* the desktop app's embedded chatgpt.com browser cache -> **web_cache**: its
  conversation list mirrors the chatgpt.com *web* sidebar, not the Work
  threads. Listed for completeness, always badged Web Cache, never trusted as
  "the open conversation".
* ``browser-sidebar-page-states.json`` -> which conversation id the desktop
  app currently has open (**desktop_active**, execution truth).

Projects come from ``~/.codex/.codex-global-state.json`` (``local-projects``),
which carries real project names.

Nothing here is ever written; no private web APIs; no cookies; no uploads.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.discovery import paths
from app.discovery.leveldb_reader import LevelDB
from app.discovery.models import (
    ConversationInfo,
    ProjectInfo,
    SOURCE_CODEX_WORK,
    SOURCE_DESKTOP_ACTIVE,
    SOURCE_WEB_CACHE,
)
from app.utils.logging_setup import get_logger

log = get_logger("discovery")

CONVERSATIONS_KEY = b"codex.chatgpt-conversations"
PINNED_KEY = b"codex.chatgpt-pinned-conversations"
DEVICE_ID_KEY = b"codex.chatgpt-conversations.device-id"


def _decode_local_storage_value(value: bytes) -> str:
    """LocalStorage values are stored as a 1-byte type tag + UTF-16LE string."""
    if not value:
        return ""
    body = value[1:] if value[0] in (0x00, 0x01) else value
    try:
        text = body.decode("utf-16-le")
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
    """The real implementation, backed by the local user-data directories."""

    name = "local"

    def __init__(
        self,
        user_data: str | Path | None = None,
        codex_home: str | Path | None = None,
    ) -> None:
        self._explicit_user_data = user_data
        self._explicit_codex_home = codex_home
        self._user_data: Path | None = None
        self._codex_home: Path | None = None
        self._work_sessions: list[ConversationInfo] = []
        self._web_conversations: list[ConversationInfo] = []
        self._projects: list[ProjectInfo] = []
        self._current_id: str = ""
        self._available = False
        self.refresh()

    # -- availability ------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    # -- refresh -----------------------------------------------------------

    def refresh(self) -> None:
        self._user_data = paths.resolve_user_data_dir(self._explicit_user_data)
        self._codex_home = paths.resolve_codex_home(self._explicit_codex_home)

        self._work_sessions = self._read_work_sessions()
        self._web_conversations = self._read_web_conversations()
        self._projects = self._read_projects()
        self._current_id = self._read_current_id()
        self._available = bool(self._work_sessions or self._web_conversations)
        log.info(
            "discovery: %d work sessions, %d web conversations, %d projects, current=%s",
            len(self._work_sessions),
            len(self._web_conversations),
            len(self._projects),
            self._current_id or "(none)",
        )

    # -- source 1: Codex Work threads (the desktop app's real list) --------

    def _read_work_sessions(self) -> list[ConversationInfo]:
        if self._codex_home is None:
            return []
        index_file = paths.session_index_file(self._codex_home)
        if not index_file.is_file():
            log.info("no codex session index at %s", index_file)
            return []
        rows: dict[str, dict] = {}
        try:
            with index_file.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cid = entry.get("id")
                    if not cid:
                        continue
                    prev = rows.get(cid)
                    if prev is None or (entry.get("updated_at") or "") > (
                        prev.get("updated_at") or ""
                    ):
                        rows[cid] = entry
        except OSError as exc:
            log.warning("could not read session index: %s", exc)
            return []

        sessions = [
            ConversationInfo(
                id=cid,
                title=entry.get("thread_name", "") or "",
                updated_at=entry.get("updated_at"),
                source=SOURCE_CODEX_WORK,
                source_kind=SOURCE_CODEX_WORK,
                is_cached=True,
                confidence=0.8,
            )
            for cid, entry in rows.items()
        ]
        sessions.sort(key=lambda c: c.updated_at or c.title, reverse=True)
        return sessions

    # -- source 2: embedded chatgpt.com cache (web sidebar mirror) ---------

    def _read_web_conversations(self) -> list[ConversationInfo]:
        if self._user_data is None:
            return []
        leveldb_dir = paths.local_storage_dir(self._user_data)
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
                if existing is None or (row.get("update_time") or "") > (
                    existing.get("update_time") or ""
                ):
                    merged[cid] = row

        convos = [
            ConversationInfo(
                id=row.get("id", ""),
                title=row.get("title", "") or "",
                project_id=row.get("workspace_id") or "",
                updated_at=row.get("update_time"),
                source=SOURCE_WEB_CACHE,
                source_kind=SOURCE_WEB_CACHE,
                is_cached=True,
                confidence=0.3,
            )
            for row in merged.values()
        ]
        convos.sort(key=lambda c: c.updated_at or c.title, reverse=True)
        return convos

    # -- projects ----------------------------------------------------------

    def _read_projects(self) -> list[ProjectInfo]:
        """Real project names from the Codex global state (local-projects)."""
        if self._codex_home is None:
            return []
        state_file = paths.global_state_file(self._codex_home)
        if not state_file.is_file():
            return []
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        projects: list[ProjectInfo] = []
        for entry in (data.get("local-projects") or {}).values():
            if not isinstance(entry, dict):
                continue
            pid = entry.get("id")
            name = entry.get("name")
            if pid and name:
                projects.append(ProjectInfo(id=pid, name=name, source="codex_local"))
        return projects

    # -- current conversation ---------------------------------------------

    def _read_current_id(self) -> str:
        if self._user_data is None:
            return ""
        sidebar = paths.sidebar_states_file(self._user_data)
        if not sidebar.is_file():
            return ""
        try:
            data = json.loads(sidebar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""

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
        """Work sessions first (the desktop app's real threads), then the web
        cache. ``project_id`` filters (matches either source's project id)."""
        if project_id is None:
            return [*self._work_sessions, *self._web_conversations]
        return [
            c
            for c in (*self._work_sessions, *self._web_conversations)
            if c.project_id == project_id
        ]

    def get_current_conversation(self) -> ConversationInfo | None:
        """The conversation currently open in the desktop UI (execution truth).

        The id comes from the desktop app's own tab state; the title is
        resolved from the Work thread index first, then the web cache.
        """
        if not self._current_id:
            return None
        for source in (self._work_sessions, self._web_conversations):
            for c in source:
                if c.id == self._current_id:
                    return ConversationInfo(
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
        for source in (self._work_sessions, self._web_conversations):
            for c in source:
                if c.id == conversation_id:
                    return c
        return None

    def get_conversation_by_id(self, conversation_id: str) -> ConversationInfo | None:
        return self.get_by_id(conversation_id)

    def find_by_title(self, title: str) -> list[ConversationInfo]:
        """All conversations whose title matches exactly.

        More than one result means the title is ambiguous - callers must NOT
        guess. Work sessions take precedence in the result order.
        """
        needle = (title or "").strip()
        if not needle:
            return []
        hits = [c for c in self._work_sessions if (c.title or "").strip() == needle]
        hits += [c for c in self._web_conversations if (c.title or "").strip() == needle]
        return hits

    def resolve_current_conversation(self, uia_title: str = "") -> ConversationInfo | None:
        """Best-effort identity of the open conversation, combining the desktop
        tab id with the UIA selected title."""
        current = self.get_current_conversation()
        if current is None:
            return None

        title = (uia_title or "").strip()
        if not title:
            current.confidence = min(current.confidence, 0.5)
            current.is_verified = False
            return current

        if current.title and current.title.strip() == title:
            current.confidence = 0.95
            current.is_verified = True
            return current

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

        current.confidence = 0.3
        current.is_verified = False
        return current

    def describe(self) -> str:
        if not self._available:
            return "local (unavailable)"
        return (
            f"local ({len(self._work_sessions)} work sessions, "
            f"{len(self._web_conversations)} web cached)"
        )
