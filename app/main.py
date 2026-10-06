"""Command line entry point.

    python -m app.main                     # run the daemon (DRY_RUN per config)
    python -m app.main once                # a single tick, prints what it decided
    python -m app.main usage               # print the current quota only
    python -m app.main doctor              # environment + configuration report
    python -m app.main install-autostart   # register the scheduled task
    python -m app.main uninstall-autostart # remove it (keeps config and logs)
    python -m app.main print-config        # dump the effective configuration

Safety: ``--no-dry-run`` must be passed explicitly to enable real typing. The
config default is ``dry_run: true`` and this CLI never overrides it silently.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app import __version__
from app.chatgpt.base import build_controller
from app.config import PROJECT_ROOT, config_as_dict, load_config
from app.daemon import Daemon
from app.models import utcnow
from app.notification.base import build_notifier
from app.resume.duplicate_guard import DuplicateGuard
from app.resume.resume_manager import ResumeManager
from app.resume.retry_manager import RetryManager
from app.runtime.autostart import install_autostart, is_autostart_installed, uninstall_autostart
from app.runtime.health import HealthMonitor
from app.state_machine import State, StateMachine
from app.storage.state_store import StateStore
from app.usage.base import build_provider
from app.utils.logging_setup import get_logger, setup_logging

log = get_logger("main")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chatgpt-auto-resume",
        description="Monitor the local Codex quota and resume an interrupted ChatGPT Work session.",
    )
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "once", "usage", "doctor", "print-config",
                                 "install-autostart", "uninstall-autostart", "status"])
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    parser.add_argument("--provider", default=None,
                        help="override usage.provider (codex_app_server | codex_http | fake)")
    parser.add_argument("--chatgpt", default=None,
                        help="override the ChatGPT controller (uia | fake)")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                        help="force DRY_RUN on")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="allow real typing (config must also allow it)")
    parser.add_argument("--ticks", type=int, default=0,
                        help="for `once`: run N ticks instead of 1")
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def build_daemon(cfg, *, controller_name: str | None = None, provider_name: str | None = None,
                 store: StateStore | None = None) -> Daemon:
    """Wire everything together. Exposed for tests."""
    provider_name = provider_name or cfg.usage.provider
    controller_name = controller_name or "uia"

    provider = build_provider(provider_name, cfg)
    fallbacks = []
    for name in cfg.usage.fallback_providers:
        if name == provider_name:
            continue
        try:
            fallbacks.append(build_provider(name, cfg))
        except Exception as exc:  # noqa: BLE001
            log.debug("fallback provider %s unavailable: %s", name, exc)

    controller = build_controller(controller_name, cfg)
    store = store or StateStore(cfg.state_path)
    store.load()

    notifier = build_notifier(cfg)
    sm = StateMachine(State(store.state.program_state)) if _valid_state(store.state.program_state) \
        else StateMachine(State.STARTING)
    guard = DuplicateGuard(store, cfg.resume.cooldown_minutes)
    retry = RetryManager(store, cfg.resume.max_retries, cfg.resume.retry_backoff_seconds)
    resume_manager = ResumeManager(cfg, store, controller, guard, retry, notifier, provider=provider)
    health = HealthMonitor(cfg.health.heartbeat_seconds, cfg.health.max_consecutive_failures)

    return Daemon(
        cfg=cfg,
        provider=provider,
        controller=controller,
        store=store,
        notifier=notifier,
        state_machine=sm,
        guard=guard,
        retry=retry,
        resume_manager=resume_manager,
        health=health,
        fallback_providers=fallbacks,
    )


def _valid_state(name: str) -> bool:
    try:
        State(name)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_usage(cfg) -> int:
    provider = build_provider(cfg.usage.provider, cfg)
    try:
        snap = provider.get_usage()
    finally:
        provider.close()
    print(json.dumps(snap.to_dict(), indent=2, ensure_ascii=False))
    return 0 if snap.ok else 2


def cmd_once(cfg, args) -> int:
    daemon = build_daemon(cfg, controller_name=args.chatgpt)
    ticks = max(1, args.ticks)
    try:
        for index in range(ticks):
            state = daemon.run_once()
            snap = daemon.store.state.last_usage_snapshot or {}
            print(
                f"tick {index + 1}/{ticks}  state={state.value:<16} "
                f"5h_left={snap.get('five_hour_remaining_percent')}% "
                f"available={snap.get('available')} "
                f"reset_id={snap.get('reset_id') or '(none)'}"
            )
    finally:
        daemon.close()
    return 0


def cmd_run(cfg, args) -> int:
    daemon = build_daemon(cfg, controller_name=args.chatgpt)
    state = daemon.store.state
    state.restart_count += 1
    state.started_at = utcnow().isoformat()
    daemon.store.save()
    try:
        daemon.run_forever()
    except KeyboardInterrupt:  # pragma: no cover
        log.info("interrupted by the user")
        daemon.request_stop()
        daemon.close()
    return 0


def cmd_status(cfg) -> int:
    store = StateStore(cfg.state_path)
    state = store.load()
    print(json.dumps({
        "program_state": state.program_state,
        "last_seen_reset_id": state.last_seen_reset_id,
        "last_triggered_reset_id": state.last_triggered_reset_id,
        "last_resume_time": state.last_resume_time,
        "retry_count": state.retry_count,
        "resumed_count": state.resumed_count,
        "restart_count": state.restart_count,
        "last_usage": state.last_usage_snapshot,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(cfg) -> int:
    report: dict[str, object] = {
        "version": __version__,
        "project_root": str(PROJECT_ROOT),
        "python": sys.version.split()[0],
        "dry_run": cfg.dry_run,
        "config_source": getattr(cfg, "_source_path", "(defaults)"),
        "problems": cfg.validate(),
    }

    # Python interpreter / venv check.
    report["in_venv"] = sys.prefix != getattr(sys, "base_prefix", sys.prefix)

    # Codex CLI.
    try:
        from app.usage.codex_app_server import discover_codex

        report["codex_exe"] = discover_codex(cfg.usage.codex_path)
    except Exception as exc:  # noqa: BLE001
        report["codex_exe"] = f"NOT FOUND ({exc})"

    # Quota read.
    try:
        snap = build_provider(cfg.usage.provider, cfg).get_usage()
        report["usage_read"] = "ok" if snap.ok else f"failed ({snap.error})"
        report["five_hour_remaining_percent"] = snap.five_hour_remaining_percent
        report["weekly_remaining_percent"] = snap.weekly_remaining_percent
        report["five_hour_reset_at"] = (
            snap.five_hour_reset_at.isoformat() if snap.five_hour_reset_at else None
        )
        report["plan_type"] = snap.plan_type
    except Exception as exc:  # noqa: BLE001
        report["usage_read"] = f"error: {exc}"

    # ChatGPT controller.
    try:
        controller = build_controller("uia", cfg)
        report["chatgpt_running"] = controller.is_running()
        info = controller.find_window()
        report["chatgpt_window"] = info.title if info else None
        report["chatgpt_busy"] = controller.is_busy() if info else None
        report["chatgpt_conversation"] = controller.conversation_title() if info else None
        controller.close()
    except Exception as exc:  # noqa: BLE001
        report["chatgpt_running"] = f"error: {exc}"

    # Autostart.
    try:
        report["autostart_installed"] = is_autostart_installed(cfg.daemon.task_name)
    except Exception as exc:  # noqa: BLE001
        report["autostart_installed"] = f"unknown ({exc})"

    # Prompt file.
    prompt_path = cfg.prompt_path()
    report["prompt_file"] = str(prompt_path)
    report["prompt_file_exists"] = prompt_path.exists()

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not report.get("problems") else 1


def cmd_print_config(cfg) -> int:
    print(json.dumps(config_as_dict(cfg), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)

    if args.provider:
        cfg.usage.provider = args.provider
    if args.dry_run is not None:
        cfg.dry_run = args.dry_run
    if args.log_level:
        cfg.logging.level = args.log_level
    if args.command == "once":
        cfg.logging.level = args.log_level or "DEBUG"

    setup_logging(
        cfg.log_dir,
        level=cfg.logging.level,
        max_size_mb=cfg.logging.max_size_mb,
        backup_count=cfg.logging.backup_count,
        redact_keys=cfg.logging.redact_keys,
    )

    problems = cfg.validate()
    for problem in problems:
        log.warning("config problem: %s", problem)

    log.info("chatgpt-auto-resume %s (dry_run=%s, command=%s)", __version__, cfg.dry_run, args.command)

    if args.command == "usage":
        return cmd_usage(cfg)
    if args.command == "once":
        return cmd_once(cfg, args)
    if args.command == "doctor":
        return cmd_doctor(cfg)
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "print-config":
        return cmd_print_config(cfg)
    if args.command == "install-autostart":
        ok, out = install_autostart(PROJECT_ROOT, cfg.daemon.task_name)
        print(out)
        return 0 if ok else 1
    if args.command == "uninstall-autostart":
        ok, out = uninstall_autostart(cfg.daemon.task_name)
        print(out)
        return 0 if ok else 1

    if not cfg.resume.enabled:
        log.warning("resume.enabled is false: the daemon will monitor only, never send")
    if not cfg.dry_run:
        log.warning(
            "DRY_RUN IS OFF. The daemon may type into ChatGPT Desktop. "
            "Make sure you have watched at least one full quota cycle in dry-run mode."
        )
    return cmd_run(cfg, args)


def cli_entry() -> None:  # pragma: no cover - console_script shim
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
