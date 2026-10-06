"""Unified application status snapshot for the GUI.

The GUI must never reach into StateStore / Provider / controller internals.
It renders exactly what :class:`AppStatus` carries; :class:`~app.service.AppService`
is the only thing allowed to build this object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class UsageView:
    five_hour_remaining_percent: float | None = None
    weekly_remaining_percent: float | None = None
    five_hour_reset_at: str | None = None
    weekly_reset_at: str | None = None
    available: bool = False
    source: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "five_hour_remaining_percent": self.five_hour_remaining_percent,
            "weekly_remaining_percent": self.weekly_remaining_percent,
            "five_hour_reset_at": self.five_hour_reset_at,
            "weekly_reset_at": self.weekly_reset_at,
            "available": self.available,
            "source": self.source,
            "error": self.error,
        }


@dataclass(slots=True)
class AppStatus:
    """Everything the Dashboard needs, computed from the service layer."""

    daemon_running: bool = False
    state: str = "STARTING"
    dry_run: bool = True
    real_send_armed: bool = False
    armed_reason: str = ""
    send_mode: str = "monitor"  # "monitor" | "dry_run" | "armed"

    usage: UsageView | None = None
    provider_status: str = "unknown"

    chatgpt_running: bool | None = None
    active_conversation_title: str = ""
    current_conversation_id: str = ""
    discovery_available: bool = False
    conversation_count: int = 0

    target_configured: bool = False
    target: dict[str, str] = field(default_factory=dict)
    target_match: dict[str, Any] | None = None

    pending_transaction: str = "NONE"
    last_resume_time: str | None = None
    last_resume_result: str = ""
    resumed_count: int = 0
    test_send_passed: bool = False
    test_send_last: dict[str, Any] | None = None

    uptime_seconds: float = 0.0
    started_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "daemon_running": self.daemon_running,
            "state": self.state,
            "dry_run": self.dry_run,
            "real_send_armed": self.real_send_armed,
            "armed_reason": self.armed_reason,
            "send_mode": self.send_mode,
            "usage": self.usage.to_dict() if self.usage else None,
            "provider_status": self.provider_status,
            "chatgpt_running": self.chatgpt_running,
            "active_conversation_title": self.active_conversation_title,
            "current_conversation_id": self.current_conversation_id,
            "discovery_available": self.discovery_available,
            "conversation_count": self.conversation_count,
            "target_configured": self.target_configured,
            "target": self.target,
            "target_match": self.target_match,
            "pending_transaction": self.pending_transaction,
            "last_resume_time": self.last_resume_time,
            "last_resume_result": self.last_resume_result,
            "resumed_count": self.resumed_count,
            "test_send_passed": self.test_send_passed,
            "test_send_last": self.test_send_last,
            "uptime_seconds": self.uptime_seconds,
            "started_at": self.started_at,
        }
