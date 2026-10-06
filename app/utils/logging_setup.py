"""Logging setup with rotation and mandatory secret redaction.

Security requirement (from the project brief): logs must never contain
ChatGPT credentials, full cookies, Telegram bot tokens or auth tokens. The
redaction filter below is the enforcement point, so every logger in the
project must be created through :func:`get_logger`.
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable

DEFAULT_REDACT_KEYS: tuple[str, ...] = (
    "token",
    "secret",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "password",
    "credential",
    "access_token",
    "refresh_token",
    "id_token",
    "bot_token",
)

# Patterns that look like secrets regardless of key name.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\b"),  # JWT
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram bot token
    re.compile(r'"?(access_token|refresh_token|id_token|bot_token)"?\s*[:=]\s*"?[^"\s,}]+'),
)

REDACTED = "<redacted>"

_configured = False


class RedactFilter(logging.Filter):
    """Rewrites the formatted message, replacing anything secret-looking."""

    def __init__(self, keys: Iterable[str] = DEFAULT_REDACT_KEYS) -> None:
        super().__init__()
        self.keys = tuple(k.lower() for k in keys)

    def _scrub_text(self, text: str) -> str:
        for pattern in _PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + REDACTED, text)
        for key in self.keys:
            # key=value / "key": "value" / key: value
            text = re.sub(
                rf'("{key}"\\s*:\\s*)"[^"]*"',
                rf'\1"{REDACTED}"',
                text,
                flags=re.IGNORECASE,
            )
            text = re.sub(
                rf"\b({key}\s*[=:]\s*)\S+",
                rf"\1{REDACTED}",
                text,
                flags=re.IGNORECASE,
            )
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self._scrub_text(str(record.msg))
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: (REDACTED if any(s in str(k).lower() for s in self.keys) else v)
                        for k, v in record.args.items()
                    }
                else:
                    record.args = tuple(
                        self._scrub_text(str(a)) if isinstance(a, str) else a for a in record.args
                    )
        except Exception:  # noqa: BLE001 - logging must never crash the daemon
            pass
        return True


def scrub(value: Any, keys: Iterable[str] = DEFAULT_REDACT_KEYS) -> Any:
    """Recursively redact a dict/list structure (used before logging payloads)."""
    lowered = tuple(k.lower() for k in keys)

    def walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                if any(s in str(k).lower() for s in lowered):
                    out[k] = REDACTED if v not in (None, "") else v
                else:
                    out[k] = walk(v)
            return out
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        if isinstance(obj, str) and len(obj) > 60:
            return obj[:20] + "…" + REDACTED
        return obj

    return walk(value)


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    max_size_mb: int = 10,
    backup_count: int = 5,
    console: bool = True,
    redact_keys: Iterable[str] = DEFAULT_REDACT_KEYS,
) -> logging.Logger:
    """Configure the root logger exactly once."""
    global _configured

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    root = logging.getLogger()
    if _configured:
        return root

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = RedactFilter(redact_keys)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        stream.addFilter(redactor)
        root.addHandler(stream)

    # Third-party noise control.
    logging.getLogger("pywinauto").setLevel(logging.WARNING)
    logging.getLogger("comtypes").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _configured = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
