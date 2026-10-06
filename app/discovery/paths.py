"""Path resolution for the ChatGPT Desktop local data directory.

The ChatGPT Desktop app (the ``OpenAI.Codex`` MSIX bundle) stores its Electron
user-data under ``%APPDATA%\\Codex\\web\\Codex``. This module returns that
root, honouring an explicit override first, and never touches anything outside
the resolved directory.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Default location of the Electron user-data root for ChatGPT Desktop.
DEFAULT_USER_DATA = Path(os.environ.get("APPDATA", "")) / "Codex" / "web" / "Codex"

_ENV_OVERRIDE = "CHATGPT_AUTO_RESUME_USER_DATA"


def resolve_user_data_dir(explicit: str | Path | None = None) -> Path | None:
    """Return the ChatGPT user-data root, or None when it does not exist.

    Resolution order: explicit argument, the ``CHATGPT_AUTO_RESUME_USER_DATA``
    environment variable, then the default MSIX location.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get(_ENV_OVERRIDE)
    if env:
        candidates.append(Path(env))
    candidates.append(DEFAULT_USER_DATA)

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def local_storage_dir(user_data: Path) -> Path:
    return user_data / "Default" / "Local Storage" / "leveldb"


def sidebar_states_file(user_data: Path) -> Path:
    return user_data / "browser-sidebar-page-states.json"
