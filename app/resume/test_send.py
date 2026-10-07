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
    from app.config import AppConfig, TargetConfig
    from app.discovery.provider import ConversationDiscoveryProvider

log = get_logger("test_send")

#: The test prompt is intentionally trivial and distinct from the real
#: continue prompt, per the brief.
TEST_PROMPT = "AUTO RESUME TEST\n请只回复：OK"

STATUS_NONE = "NONE"
STATUS_PREPARED = "PREPARED"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_UNCERTAIN = "UNCERTAIN"
STATUS_TEST_REQUIRED = "TEST_REQUIRED"


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
    """Durable record of the last test send, isolated from the resume state.

    A confirmed test send is a *certification for one specific target*: the
    record carries the target fingerprint (sha256 of id+title). If the target
    changes, the certification no longer applies and a new test is required.

    Crash recovery: a ``PREPARED`` record on load means the previous process
    may already have sent the test message; it is converted to ``UNCERTAIN``
    (never auto-resend), cleared only by an explicit user action.
    """

    TRANSPORT_VERSION = "1"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.record: dict = {
            "transaction_type": "test",
            "status": STATUS_NONE,
            "conversation_id": "",
            "conversation_title": "",
            "target_fingerprint": "",
            "channel": "",
            "confirmation": "",
            "timestamp": None,
            "prompt": "",
            "transport_version": self.TRANSPORT_VERSION,
        }
        self.load()

    def load(self) -> dict:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            self.record.update(loaded)
            # Crash recovery: PREPARED on disk = a previous send may have been
            # delivered before the crash. Never auto-resend.
            if self.record.get("status") == STATUS_PREPARED:
                self.record["status"] = STATUS_UNCERTAIN
                self.record.setdefault("confirmation", "previous test send interrupted")
                self._write()
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

    def begin(self, conversation_id: str, conversation_title: str, prompt: str) -> None:
        from app.target import fingerprint_of

        # Bind the certification to this exact target.
        self.record.update(
            {
                "status": STATUS_PREPARED,
                "conversation_id": conversation_id or "",
                "conversation_title": conversation_title or "",
                "target_fingerprint": fingerprint_of(conversation_id, conversation_title),
                "prompt": prompt,
                "timestamp": utcnow().isoformat(),
                "channel": "",
                "confirmation": "",
                "transport_version": self.TRANSPORT_VERSION,
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
        self.record.update(
            {"status": STATUS_NONE, "channel": "", "confirmation": "", "target_fingerprint": ""}
        )
        self._write()

    def clear(self) -> None:
        """Explicit user action to reset a UNCERTAIN/old test state."""
        self.record.update(
            {
                "status": STATUS_NONE,
                "conversation_id": "",
                "conversation_title": "",
                "target_fingerprint": "",
                "channel": "",
                "confirmation": "",
                "prompt": "",
            }
        )
        self._write()

    @property
    def passed(self) -> bool:
        return self.record.get("status") == STATUS_CONFIRMED

    @property
    def uncertain(self) -> bool:
        return self.record.get("status") == STATUS_UNCERTAIN

    def is_valid_for_target(self, target: "TargetConfig") -> bool:
        """True only when the last confirmed test send certifies THIS target."""
        from app.target import target_fingerprint

        if self.record.get("status") != STATUS_CONFIRMED:
            return False
        if (self.record.get("conversation_id") or "") != (target.conversation_id or "").strip():
            return False
        if (self.record.get("target_fingerprint") or "") != target_fingerprint(target):
            return False
        return True

    def invalidate_if_target_changed(self, target: "TargetConfig") -> bool:
        """If a test was CONFIRMED for a *different* target, mark TEST_REQUIRED.

        Returns True when the certification was invalidated. The historical
        record (which target was last verified) is kept for display.
        """
        if self.record.get("status") == STATUS_CONFIRMED and not self.is_valid_for_target(target):
            self.record["status"] = STATUS_TEST_REQUIRED
            self._write()
            return True
        return False


def run_test_send(
    controller: "ChatGptController",
    cfg: "AppConfig",
    discovery: "ConversationDiscoveryProvider | None",
    store: SendTestJournal,
) -> TestSendResult:
    """Execute one supervised test send. Blocking; call from a worker thread."""
    from app.target import TargetResolver

    # P0-6: a previous test send ended UNCERTAIN (or was interrupted and
    # recovered to UNCERTAIN). It may already have been delivered - refuse a
    # new send until the user explicitly clears the test state.
    if store.uncertain:
        return TestSendResult(
            False, "refused",
            reason="上一次测试发送结果不确定（可能已送达），请先在 GUI 清除测试状态后再重试",
        )

    if not controller.is_running():
        return TestSendResult(False, "refused", reason="ChatGPT 桌面端未运行")

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
        return TestSendResult(False, "refused", reason="未配置目标对话")
    match = TargetResolver().resolve(target, current_id, title, discovery)
    if not match.ok:
        return TestSendResult(False, "refused", reason=f"目标未通过验证: {match.reason}")

    if controller.is_busy() is not False:
        return TestSendResult(False, "refused", reason="ChatGPT 正在生成或状态未知")

    # -- durable PREPARED before any real input -----------------------------
    store.begin(
        target.conversation_id or current_id,
        target.conversation_title or title,
        TEST_PROMPT,
    )

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
