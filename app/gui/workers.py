"""Qt worker threads for the GUI.

The Qt main thread never runs the Codex poll, UI Automation or the daemon
loop. Each long-running operation lives in its own QThread and reports back
through signals.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QThread, Signal

from app.status import AppStatus


class PollWorker(QThread):
    """Drives the daemon one tick at a time and emits a fresh AppStatus."""

    status_changed = Signal(object)  # AppStatus

    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D401 - QThread entry point
        while not self._stop:
            started = time.monotonic()
            try:
                status = self.service.poll_once()
            except Exception:  # noqa: BLE001 - keep the GUI alive
                from app.utils.logging_setup import get_logger

                get_logger("gui.worker").exception("poll failed")
                status = self.service.snapshot()
            self.status_changed.emit(status)

            interval = self.service.next_interval()
            elapsed = time.monotonic() - started
            wait = max(0.5, interval - elapsed)
            # Busy-wait in small slices so request_stop() is responsive.
            deadline = time.monotonic() + wait
            while not self._stop and time.monotonic() < deadline:
                time.sleep(0.1)


class TestSendWorker(QThread):
    """Runs one supervised test send and reports the result."""

    finished_ok = Signal(object)  # TestSendResult

    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service

    def run(self) -> None:
        try:
            result = self.service.run_test_send()
        except Exception as exc:  # noqa: BLE001
            from app.resume.test_send import TestSendResult

            result = TestSendResult(False, "failed", reason=f"test send crashed: {exc}")
        self.finished_ok.emit(result)
