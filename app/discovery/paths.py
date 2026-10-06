"""Path resolution for local ChatGPT/Codex data directories.

Two distinct local stores exist (user-confirmed 2026-10-07):

* **Codex core home** (``~/.codex``) - holds the desktop Work/Agent thread
  index (``session_index.jsonl``), the global state with real project names,
  and the thread history databases. This is what the desktop app drives.
* **Desktop app web view** (``%APPDATA%\\Codex\\web\\Codex``) - the embedded
  chatgpt.com browser inside the desktop app. Its Local Storage conversation
  list mirrors the *web* sidebar, not the desktop Work threads.

Neither is ever written to.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Desktop app's embedded-web user-data (chatgpt.com cache).
DEFAULT_USER_DATA = Path(os.environ.get("APPDATA", "")) / "Codex" / "web" / "Codex"

#: Codex core home (Work threads, projects, thread history).
DEFAULT_CODEX_HOME = Path.home() / ".codex"

_ENV_USER_DATA = "CHATGPT_AUTO_RESUME_USER_DATA"
_ENV_CODEX_HOME = "CODEX_HOME"


def resolve_user_data_dir(explicit: str | Path | None = None) -> Path | None:
    """Return the desktop app web-view user-data root, or None."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get(_ENV_USER_DATA)
    if env:
        candidates.append(Path(env))
    candidates.append(DEFAULT_USER_DATA)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def resolve_codex_home(explicit: str | Path | None = None) -> Path | None:
    """Return the Codex core home (``~/.codex``), or None."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get(_ENV_CODEX_HOME)
    if env:
        candidates.append(Path(env))
    candidates.append(DEFAULT_CODEX_HOME)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def local_storage_dir(user_data: Path) -> Path:
    return user_data / "Default" / "Local Storage" / "leveldb"


def sidebar_states_file(user_data: Path) -> Path:
    return user_data / "browser-sidebar-page-states.json"


def session_index_file(codex_home: Path) -> Path:
    return codex_home / "session_index.jsonl"


def global_state_file(codex_home: Path) -> Path:
    return codex_home / ".codex-global-state.json"
