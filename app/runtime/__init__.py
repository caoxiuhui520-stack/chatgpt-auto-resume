"""Runtime concerns: autostart and health."""

from app.runtime.autostart import install_autostart, is_autostart_installed, uninstall_autostart
from app.runtime.health import HealthMonitor

__all__ = [
    "install_autostart",
    "uninstall_autostart",
    "is_autostart_installed",
    "HealthMonitor",
]
