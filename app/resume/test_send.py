"""Supervised test send - the P0 safety gate before real sends are armed.

A test send goes through the *exact* same transport as a real resume (task
lock, composer detection, ValuePattern, InvokePattern, PREPARED fsync before
input, POST_SEND_VERIFY) but uses a dedicated prompt and a dedicated
transaction record, so it can never consume a real quota ``reset_id`` nor
pollute the resume ``last_triggered_reset_id``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.models import utcnow
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.chatgpt.base import ChatGptController
    from app.config import AppConfig
    from app.discovery.provider import ConversationDiscoveryProvider

log = get_logger("test_send")

#: The test prompt is intentionally trivial and distinct from the real
#: continue prompt, per the brief.
TEST_PROMPT = "AUTO RESUME TEST\n请只回复：OK"

STATUS_NONE = "NONE"
STATUS_PREPARED = "PREPARED"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_UNCERTAIN = "UNCERTAIN"


@dataclass(slots=True)
class TestSendResult:
    ok: bool
    status: str  # "passed" | "failed" | "uncertain" | "refused"
    reason: str = ""
    conversation_id: str = ""
    channel: str = ""
    confirmation: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SendTestJournal:
    """Durable record of the last test send, isolated from the resume state."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.record: dict = {
            "transaction_type": "test",
            "status": STATUS_NONE,
            "conversation_id": "",
            "channel": "",
            "confirmation": "",
            "timestamp": None,
            "prompt": "",
        }
        self.load()

    def load(self) -> dict:
        if self.path.exists():
            try:
                self.record.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        return self.record

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".test_send-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.record, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def begin(self, conversation_id: str, prompt: str) -> None:
        self.record.update(
            {
                "status": STATUS_PREPARED,
                "conversation_id": conversation_id,
                "prompt": prompt,
                "timestamp": utcnow().isoformat(),
                "channel": "",
                "confirmation": "",
            }
        )
        self._write()

    def confirm(self, channel: str, confirmation: str) -> None:
        self.record.update(
            {"status": STATUS_CONFIRMED, "channel": channel, "confirmation": confirmation}
        )
        self._write()

    def mark_uncertain(self, channel: str, confirmation: str) -> None:
        self.record.update(
            {"status": STATUS_UNCERTAIN, "channel": channel, "confirmation": confirmation}
        )
        self._write()

    def abort(self) -> None:
        self.record.update({"status": STATUS_NONE, "channel": "", "confirmation": ""})
        self._write()

    @property
    def passed(self) -> bool:
        return self.record.get("status") == STATUS_CONFIRMED


def run_test_send(
    controller: "ChatGptController",
    cfg: "AppConfig",
    discovery: "ConversationDiscoveryProvider | None",
    store: SendTestJournal,
) -> TestSendResult:
    """Execute one supervised test send. Blocking; call from a worker thread."""
    from app.target import TargetResolver

    if not controller.is_running():
        return TestSendResult(False, "refused", reason="ChatGPT Desktop is not running")

    info = controller.find_window()
    if info is None:
        return TestSendResult(False, "refused", reason="no ChatGPT conversation window found")

    # -- task lock: the test may only go to the matched conversation --------
    title = controller.conversation_title()
    current_id = ""
    if discovery is not None:
        try:
            discovery.refresh()
            current = discovery.get_current_conversation()
            current_id = current.id if current else ""
        except Exception:  # noqa: BLE001
            pass

    target = cfg.target
    if not (target.conversation_id or target.conversation_title.strip()):
        return TestSendResult(False, "refused", reason="no target conversation configured")
    match = TargetResolver().resolve(target, current_id, title, discovery)
    if not match.ok:
        return TestSendResult(
            False, "refused", reason=f"target mismatch: {match.reason}"
        )

    if controller.is_busy() is not False:
        return TestSendResult(False, "refused", reason="ChatGPT is busy or state unknown")

    # -- durable PREPARED before any real input -----------------------------
    store.begin(match.target.id if match.target else target.conversation_id, TEST_PROMPT)

    result = controller.send_prompt(TEST_PROMPT, dry_run=False)
    if not result.success:
        store.abort()
        return TestSendResult(
            False, "failed", reason=f"send failed: {result.detail or result.error}",
            conversation_id=current_id,
        )

    confirmed, reason = controller.verify_sent()
    channel = getattr(result, "detail", "") or ""
    if confirmed:
        store.confirm(channel, reason)
        return TestSendResult(
            True, "passed", reason=reason, conversation_id=current_id,
            channel=channel, confirmation=reason, timestamp=utcnow().isoformat(),
        )

    store.mark_uncertain(channel, reason)
    return TestSendResult(
        False, "uncertain", reason=reason, conversation_id=current_id,
        channel=channel, confirmation=reason, timestamp=utcnow().isoformat(),
    )
