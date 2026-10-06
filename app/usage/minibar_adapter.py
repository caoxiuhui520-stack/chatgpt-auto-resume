"""HTTP fallback provider - the second-priority source.

The reference monitor implementations confirm this route works without
spawning a child process: read the local Codex credentials from
``~/.codex/auth.json`` and ask the account usage endpoint directly.

    GET https://chatgpt.com/backend-api/wham/usage
    Authorization: Bearer <tokens.access_token>
    ChatGPT-Account-Id: <tokens.account_id>
    User-Agent: codex-cli

Response fields are snake_case and differ from the app-server protocol::

    rate_limit.primary_window.used_percent / reset_at / limit_window_seconds
    rate_limit.secondary_window.*

This module exists so the daemon survives a change in the app-server protocol,
and so users without a working app-server still get quota data. It is *not*
the default. The credentials are read into memory only, are never logged, and
are never written anywhere.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

from app.models import (
    SHORT_WINDOW_MAX_MINUTES,
    UsageSnapshot,
    UsageWindow,
    from_unix,
    normalize_to_minute,
    utcnow,
)
from app.usage.base import ProviderError, UsageProvider, register
from app.utils.logging_setup import get_logger, scrub

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig

log = get_logger("usage.http")

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
AUTH_PATH = Path.home() / ".codex" / "auth.json"


def _load_credentials(auth_path: Path = AUTH_PATH) -> tuple[str, str | None]:
    """Return (access_token, account_id). Raises if unavailable."""
    if not auth_path.exists():
        raise ProviderError("no_credentials", f"credential file not found: {auth_path}")
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderError("no_credentials", f"credential file unreadable: {exc}") from exc

    tokens = data.get("tokens") or {}
    access = tokens.get("access_token") or data.get("access_token")
    if not access:
        raise ProviderError("no_credentials", "no access_token in the credential file")
    account_id = tokens.get("account_id") or data.get("account_id")
    return str(access), (str(account_id) if account_id else None)


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _window_from(node: Any, label: str) -> UsageWindow:
    if not isinstance(node, dict):
        return UsageWindow(label=label)
    seconds = _num(node.get("limit_window_seconds"))
    minutes = int(seconds // 60) if seconds else None
    return UsageWindow(
        used_percent=_num(node.get("used_percent")),
        window_minutes=minutes,
        resets_at=from_unix(node.get("reset_at")),
        label=label,
    )


def parse_usage_payload(
    payload: dict[str, Any],
    *,
    source: str = "codex_http",
    exhausted_threshold: float = 100.0,
    restored_threshold: float = 99.0,
    now: datetime | None = None,
) -> UsageSnapshot:
    now = now or utcnow()
    rate_limit = payload.get("rate_limit") or payload.get("rate_limits") or {}
    if not isinstance(rate_limit, dict):
        raise ProviderError("invalid_response", "no rate_limit object in the payload")

    primary = _window_from(rate_limit.get("primary_window"), "five_hour")
    secondary = _window_from(rate_limit.get("secondary_window"), "weekly")

    if secondary.used_percent is None and primary.used_percent is not None:
        if primary.window_minutes and primary.window_minutes > SHORT_WINDOW_MAX_MINUTES:
            secondary = UsageWindow(
                used_percent=primary.used_percent,
                window_minutes=primary.window_minutes,
                resets_at=primary.resets_at,
                label="weekly",
            )
            primary = UsageWindow(label="five_hour")

    five_remaining = primary.remaining_percent
    if five_remaining is None:
        available = False
    else:
        used = 100.0 - five_remaining
        available = used < exhausted_threshold and used <= restored_threshold

    snapshot = UsageSnapshot(
        available=available,
        five_hour_remaining_percent=five_remaining,
        weekly_remaining_percent=secondary.remaining_percent,
        five_hour_reset_at=normalize_to_minute(primary.resets_at),
        weekly_reset_at=normalize_to_minute(secondary.resets_at),
        source=source,
        timestamp=now,
        plan_type=payload.get("plan_type"),
    )
    snapshot.reset_id = snapshot.compute_reset_id()
    snapshot.raw = {"five_hour": primary.to_dict(), "weekly": secondary.to_dict()}
    return snapshot


@register("codex_http")
def _build(cfg: "AppConfig") -> UsageProvider:
    return CodexHttpProvider(cfg)


class CodexHttpProvider(UsageProvider):
    name = "codex_http"
    description = "HTTP account usage endpoint fed by local Codex credentials"

    def __init__(self, cfg: "AppConfig") -> None:
        self.cfg = cfg
        self.timeout = float(cfg.usage.request_timeout_seconds)

    def get_usage(self) -> UsageSnapshot:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("missing_dependency", "requests is required") from exc

        try:
            token, account_id = _load_credentials()
        except ProviderError as exc:
            log.warning("credential read failed: %s", exc)
            return UsageSnapshot(source=self.name, error=exc.kind, timestamp=utcnow())

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "codex-cli",
            "Accept": "application/json",
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id

        try:
            resp = requests.get(USAGE_URL, headers=headers, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            log.warning("usage endpoint unreachable: %s", exc)
            return UsageSnapshot(
                source=self.name, error=f"network: {exc}", timestamp=utcnow(), stale=True
            )

        status = resp.status_code
        if status in (401, 403):
            return UsageSnapshot(
                source=self.name, error="auth_required", timestamp=utcnow()
            )
        if status == 429:
            return UsageSnapshot(
                source=self.name, error="rate_limited", timestamp=utcnow(), stale=True
            )
        if status >= 500:
            return UsageSnapshot(
                source=self.name, error=f"server_error({status})", timestamp=utcnow(), stale=True
            )
        if status != 200:
            return UsageSnapshot(
                source=self.name, error=f"request_failed({status})", timestamp=utcnow()
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            return UsageSnapshot(
                source=self.name, error=f"invalid_response: {exc}", timestamp=utcnow()
            )

        try:
            return parse_usage_payload(
                payload,
                source=self.name,
                exhausted_threshold=float(self.cfg.usage.exhausted_threshold_percent),
                restored_threshold=float(self.cfg.usage.restored_threshold_percent),
            )
        except ProviderError as exc:
            log.debug("payload not usable: %s (%s)", exc, scrub(payload))
            return UsageSnapshot(
                source=self.name, error=f"{exc.kind}: {exc}", timestamp=utcnow()
            )
