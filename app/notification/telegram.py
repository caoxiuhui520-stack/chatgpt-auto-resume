"""Telegram notifications.

The bot token is held in memory only, is never logged, and is never included
in a message body. A failed send is a debug-level event: the daemon must not
be affected by a chat service being unreachable.
"""

from __future__ import annotations

from app.notification.base import Event, Notifier, TITLES
from app.utils.logging_setup import get_logger

log = get_logger("notify.telegram")

API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3900


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self._disabled = not (bot_token and chat_id)
        if self._disabled:
            log.warning("telegram notifier created without credentials; disabled")

    def send(self, event: Event, message: str, *, title: str | None = None) -> bool:
        if self._disabled:
            return False
        try:
            import requests
        except ImportError:  # pragma: no cover
            log.debug("requests unavailable; telegram disabled")
            return False

        title = title or TITLES.get(event, "ChatGPT Auto Resume")
        body = f"{title}\n{message}"[:MAX_LEN]
        try:
            resp = requests.post(
                API_TEMPLATE.format(token=self.bot_token),
                json={"chat_id": self.chat_id, "text": body, "disable_notification": False},
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            # Exception text can include the URL (and therefore the token):
            # log the type only.
            log.debug("telegram send failed: %s", type(exc).__name__)
            return False

        if resp.status_code == 200:
            return True
        log.debug("telegram rejected the message with status %s", resp.status_code)
        return False
