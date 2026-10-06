"""Durable JSON stores for presets and conversation bindings.

Both use the same atomic write discipline as the rest of the app: temp file
in the same directory, fsync, os.replace. A crash leaves either the old file
or the complete new one - never a half-written JSON.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.utils.logging_setup import get_logger

log = get_logger("prompts.store")


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class JsonFile:
    """A small, typed read-modify-write store over a JSON file."""

    def __init__(self, path: Path, default: Any) -> None:
        self.path = Path(path)
        self._default = default
        self.data: Any = default

    def load(self) -> Any:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("unreadable %s (%s); using defaults", self.path, exc)
                self.data = self._default
        else:
            self.data = self._default
        return self.data

    def save(self) -> None:
        _atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=2))
