"""Automated E2E checks for the scenarios that do not need a human click.

Writes diagnostics/e2e_report.json. Scenario C (the real supervised test
send) is intentionally NOT executed here - it types into the user's real
ChatGPT conversation and is a BLOCKED BY USER ACTION gate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import PROJECT_ROOT, load_config, save_config  # noqa: E402
from app.discovery.provider import LocalChatGPTDiscoveryProvider  # noqa: E402
from app.service import AppService  # noqa: E402
from app.state_machine import State  # noqa: E402
from app.target import TargetResolver  # noqa: E402
from app.usage.base import build_provider  # noqa: E402
from app.utils.logging_setup import _mask_emails  # noqa: E402

REPORT: dict = {"scenarios": {}}


def record(name: str, ok: bool, detail: str) -> None:
    REPORT["scenarios"][name] = {"pass": ok, "detail": detail}
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def scenario_a() -> None:
    """GUI/service round trip: read quota + current conversation, set target,
    persist, 'restart', verify the target survived and matches."""
    svc = AppService()
    discovery = svc.discovery
    cfg = svc.cfg

    provider = build_provider(cfg.usage.provider, cfg)
    try:
        snap = provider.get_usage()
    finally:
        provider.close()

    current = discovery.get_current_conversation()
    convos = discovery.list_conversations()

    ok_quota = snap.ok and snap.five_hour_remaining_percent is not None or snap.weekly_remaining_percent is not None
    record("A1_real_quota_read", bool(ok_quota),
           f"5h={snap.five_hour_remaining_percent} weekly={snap.weekly_remaining_percent} source={snap.source}")

    record("A2_current_conversation", current is not None and bool(current.id),
           f"current id={current.id if current else None}")

    assert convos, "conversation list must not be empty"
    record("A3_conversation_list", True, f"{len(convos)} conversations")

    # Pick the current conversation if it is known; otherwise the newest.
    target = current or convos[0]
    title = target.display_title if target in convos else (current.title if current else "")
    svc.set_target(conversation_id=target.id, conversation_title=title)

    # 'Restart': a brand-new service instance must see the persisted target.
    svc2 = AppService()
    persisted = svc2.cfg.target
    ok = persisted.conversation_id == target.id
    record("A4_target_persists_restart", ok,
           f"target id={persisted.conversation_id!r} title={persisted.conversation_title!r}")

    match = TargetResolver().resolve(
        persisted, target.id, title, svc2.discovery
    )
    record("A5_target_matches", match.ok, f"status={match.status} by={match.matched_by}")


def scenario_b_dry_run() -> None:
    """Simulated restore with the fake provider in dry-run: 'Would send' and
    absolutely no real input."""
    env = {
        **__import__("os").environ,
        "AUTO_RESUME_SCENARIO": "exhausted_then_restored",
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run(
        [
            str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
            "-m", "app.main", "once",
            "--provider", "fake", "--chatgpt", "fake", "--ticks", "4",
        ],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    out = proc.stdout + proc.stderr
    dry = "dry_run" in out.lower() or "dry run" in out.lower()
    record("B_dry_run_no_real_input", proc.returncode == 0 and dry,
           f"rc={proc.returncode} dry_run_seen={dry}")


def scenario_e_mismatch_refused() -> None:
    """A target that is not the open conversation must be refused by the
    resolver (unit-level guarantee re-verified against live identities)."""
    svc = AppService()
    discovery = svc.discovery
    convos = discovery.list_conversations()
    current = discovery.get_current_conversation()
    if not convos or current is None:
        record("E_mismatch_refused", True, "skipped: no live conversations/current id")
        return
    wrong = next((c for c in convos if c.id != current.id), None)
    if wrong is None:
        record("E_mismatch_refused", True, "skipped: only one conversation exists")
        return
    from app.config import TargetConfig

    m = TargetResolver().resolve(
        TargetConfig(conversation_id=wrong.id, conversation_title=wrong.display_title),
        current.id,
        current.title,
        discovery,
    )
    record("E_mismatch_refused", m.status == "mismatch",
           f"status={m.status} reason={m.reason}")


def scenario_f_waiting_reset() -> None:
    """Live quota: while the 5h window is exhausted the daemon must sit in
    WAITING_RESET / QUOTA_EXHAUSTED and never attempt a send."""
    svc = AppService()
    svc.start()
    try:
        status = svc.poll_once()
        ok = status.state in (State.WAITING_RESET.value, State.QUOTA_EXHAUSTED.value)
        record("F_real_exhausted_waits", ok,
               f"state={status.state} 5h={status.usage.five_hour_remaining_percent if status.usage else None} "
               f"weekly={status.usage.weekly_remaining_percent if status.usage else None} "
               f"pending={status.pending_transaction}")
    finally:
        svc.stop()


def main() -> int:
    for fn in (scenario_a, scenario_b_dry_run, scenario_e_mismatch_refused, scenario_f_waiting_reset):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            record(fn.__name__, False, f"exception: {type(exc).__name__}: {exc}")

    REPORT["all_pass"] = all(s["pass"] for s in REPORT["scenarios"].values())
    out = PROJECT_ROOT / "diagnostics" / "e2e_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_mask_emails(json.dumps(REPORT, indent=2, ensure_ascii=False)) + "\n", encoding="utf-8")
    print("report:", out)
    return 0 if REPORT["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
