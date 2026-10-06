"""Windows toast notifications.

Uses ``winotify`` when available (toast with an app identity), and falls back
to a PowerShell balloon via the WinRT toast API, and finally to a message box
free approach: log-only. Notification failure must never affect the daemon, so
every path swallows its own errors.
"""

from __future__ import annotations

from app.notification.base import Event, Notifier, TITLES
from app.utils.logging_setup import get_logger

log = get_logger("notify.windows")

APP_ID = "ChatGPT Auto Resume"


class WindowsNotifier(Notifier):
    name = "windows"

    def __init__(self) -> None:
        self._backend = self._detect_backend()
        log.debug("windows notification backend: %s", self._backend)

    @staticmethod
    def _detect_backend() -> str:
        try:
            import winotify  # noqa: F401

            return "winotify"
        except Exception:  # noqa: BLE001
            pass
        try:
            import win32api  # noqa: F401

            return "powershell"
        except Exception:  # noqa: BLE001
            return "log"

    def send(self, event: Event, message: str, *, title: str | None = None) -> bool:
        title = title or TITLES.get(event, APP_ID)
        if self._backend == "winotify":
            return self._send_winotify(title, message)
        if self._backend == "powershell":
            return self._send_powershell(title, message)
        log.info("[notification] %s - %s", title, message)
        return False

    def _send_winotify(self, title: str, message: str) -> bool:
        try:
            from winotify import Notification

            toast = Notification(app_id=APP_ID, title=title, msg=message, duration="short")
            toast.show()
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("winotify failed: %s", exc)
            return self._send_powershell(title, message)

    def _send_powershell(self, title: str, message: str) -> bool:
        import subprocess

        safe_title = title.replace("'", "''")
        safe_msg = message.replace("'", "''")
        script = (
            "[reflection.assembly]::loadwithpartialname('System.Windows.Forms')|Out-Null;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.Visible=$true;"
            f"$n.ShowBalloonTip(5000,'{safe_title}','{safe_msg}',"
            "[System.Windows.Forms.ToolTipIcon]::Info);"
            "Start-Sleep -Seconds 6;$n.Dispose()"
        )
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                close_fds=True,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("powershell notification failed: %s", exc)
            log.info("[notification] %s - %s", title, message)
            return False
