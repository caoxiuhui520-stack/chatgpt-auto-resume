"""Notification interface.

Events are enumerated so the daemon cannot invent ad-hoc strings, and the
message templates live here so that no call site can accidentally interpolate
a token into a notification body.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Callable, TYPE_CHECKING

from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig

log = get_logger("notify")


class Event(str, Enum):
    QUOTA_EXHAUSTED = "quota_exhausted"
    RESET_ESTIMATED = "reset_estimated"
    QUOTA_RESTORED = "quota_restored"
    RESUME_PREPARING = "resume_preparing"
    RESUME_SENT = "resume_sent"
    RESUME_FAILED = "resume_failed"
    SEND_UNCERTAIN = "send_uncertain"
    MAX_RETRIES = "max_retries"
    EXCEPTION = "exception"
    RESTARTED = "restarted"
    HEALTH_WARNING = "health_warning"


TITLES: dict[Event, str] = {
    Event.QUOTA_EXHAUSTED: "ChatGPT Auto Resume - quota exhausted",
    Event.RESET_ESTIMATED: "ChatGPT Auto Resume - reset estimated",
    Event.QUOTA_RESTORED: "ChatGPT Auto Resume - quota restored",
    Event.RESUME_PREPARING: "ChatGPT Auto Resume - preparing to resume",
    Event.RESUME_SENT: "ChatGPT Auto Resume - resumed",
    Event.RESUME_FAILED: "ChatGPT Auto Resume - resume failed",
    Event.SEND_UNCERTAIN: "ChatGPT Auto Resume - send uncertain",
    Event.MAX_RETRIES: "ChatGPT Auto Resume - giving up",
    Event.EXCEPTION: "ChatGPT Auto Resume - error",
    Event.RESTARTED: "ChatGPT Auto Resume - restarted",
    Event.HEALTH_WARNING: "ChatGPT Auto Resume - health warning",
}


class Notifier(ABC):
    name: str = "base"

    @abstractmethod
    def send(self, event: Event, message: str, *, title: str | None = None) -> bool:
        ...

    def close(self) -> None:  # pragma: no cover
        ...


class NullNotifier(Notifier):
    name = "null"
    sent: list[tuple[Event, str]]

    def __init__(self) -> None:
        self.sent = []

    def send(self, event: Event, message: str, *, title: str | None = None) -> bool:
        self.sent.append((event, message))
        log.debug("notification suppressed (%s): %s", event.value, message)
        return False


class MultiNotifier(Notifier):
    """Fan out to every enabled channel. One failing channel never blocks
    another, and a notification failure never propagates."""

    name = "multi"

    def __init__(self, channels: list[Notifier]) -> None:
        self.channels = channels

    def send(self, event: Event, message: str, *, title: str | None = None) -> bool:
        any_ok = False
        for channel in self.channels:
            try:
                any_ok |= bool(channel.send(event, message, title=title))
            except Exception as exc:  # noqa: BLE001
                log.warning("notification channel %s failed: %s", channel.name, exc)
        return any_ok

    def close(self) -> None:
        for channel in self.channels:
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass


def build_notifier(cfg: "AppConfig") -> Notifier:
    from app.notification.telegram import TelegramNotifier
    from app.notification.windows import WindowsNotifier

    channels: list[Notifier] = []
    if cfg.notifications.windows:
        channels.append(WindowsNotifier())
    if cfg.notifications.telegram.enabled:
        channels.append(
            TelegramNotifier(
                bot_token=cfg.notifications.telegram.bot_token,
                chat_id=cfg.notifications.telegram.chat_id,
            )
        )
    if not channels:
        log.info("no notification channels enabled")
        return NullNotifier()
    log.info("notification channels: %s", ", ".join(c.name for c in channels))
    return MultiNotifier(channels)


def notifier_factory(cfg: "AppConfig") -> Callable[[], Notifier]:
    return lambda: build_notifier(cfg)
