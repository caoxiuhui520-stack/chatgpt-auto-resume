"""Configuration loading and validation.

Plain dataclasses instead of a validation framework: fewer moving parts for a
long-running daemon, and it keeps the dependency surface small enough to audit.
Unknown keys in the YAML are collected and reported as warnings rather than
silently ignored, so typos in config.yaml do not look like working config.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from app.utils.logging_setup import get_logger

log = get_logger("config")

if getattr(sys, "frozen", False):  # PyInstaller exe: keep config/data next to the exe
    PROJECT_ROOT = Path(sys.executable).resolve().parent
    # Bundled read-only assets stay inside the _internal folder.
    _BUNDLED_DIR = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    _BUNDLED_DIR = PROJECT_ROOT
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = _BUNDLED_DIR / "config.example.yaml"


@dataclass
class ResumeConfig:
    enabled: bool = True
    prompt_file: str = "prompts/continue.txt"
    cooldown_minutes: int = 30
    max_retries: int = 5
    retry_backoff_seconds: list[int] = field(
        default_factory=lambda: [0, 30, 120, 300, 900]
    )


@dataclass
class ChatGptConfig:
    auto_start: bool = True
    exe_path: str = ""
    launch_wait_seconds: int = 30
    process_names: list[str] = field(default_factory=lambda: ["ChatGPT.exe"])


@dataclass
class UsageConfig:
    provider: str = "codex_app_server"
    fallback_providers: list[str] = field(
        default_factory=lambda: ["codex_http", "fake"]
    )
    request_timeout_seconds: int = 15
    codex_path: str = ""
    exhausted_threshold_percent: int = 100
    restored_threshold_percent: int = 99


@dataclass
class TaskLockConfig:
    enabled: bool = False
    project: str = ""
    conversation: str = ""
    # goal_hash is intentionally unsupported: the Electron client does not
    # expose the conversation body, so a hash of the *title* would pretend to
    # verify content it cannot see. The field is kept (ignored) so old configs
    # still parse; see README.
    goal_hash: str = ""


@dataclass
class TargetConfig:
    """The conversation the daemon must resume into.

    ``conversation_id`` is the primary identity (stable, unambiguous). The
    title fields are for display, diagnostics and fallback when no id is
    known. Both empty means "no target configured" - a real send is refused.
    """

    project_id: str = ""
    project_name: str = ""
    conversation_id: str = ""
    conversation_title: str = ""


@dataclass
class RealSendConfig:
    """Explicit arming for a real send. Independent of dry_run.

    A real send is only permitted when dry_run == false AND real_send.armed
    == true AND task_lock.enabled == true AND no fake provider is configured.
    Anything less degrades to monitor-only.
    """

    armed: bool = False


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class NotificationConfig:
    windows: bool = True
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    max_size_mb: int = 10
    backup_count: int = 5
    redact_keys: list[str] = field(
        default_factory=lambda: [
            "token",
            "secret",
            "authorization",
            "cookie",
            "api_key",
            "password",
            "credential",
        ]
    )


@dataclass
class HealthConfig:
    heartbeat_seconds: int = 300
    max_consecutive_failures: int = 5


@dataclass
class DaemonConfig:
    task_name: str = "ChatGPTAutoResume"


@dataclass
class AppConfig:
    poll_interval_seconds: int = 30
    dry_run: bool = True
    resume: ResumeConfig = field(default_factory=ResumeConfig)
    chatgpt: ChatGptConfig = field(default_factory=ChatGptConfig)
    usage: UsageConfig = field(default_factory=UsageConfig)
    task_lock: TaskLockConfig = field(default_factory=TaskLockConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    real_send: RealSendConfig = field(default_factory=RealSendConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)

    # -- paths -------------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def log_dir(self) -> Path:
        return PROJECT_ROOT / "logs"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    def prompt_path(self) -> Path:
        p = Path(self.resume.prompt_file)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def read_prompt(self) -> str:
        path = self.prompt_path()
        if not path.exists():
            log.warning("prompt file missing, using built-in fallback: %s", path)
            return "继续执行当前已经确定的目标和计划。"
        return path.read_text(encoding="utf-8").strip()

    def validate(self) -> list[str]:
        """Return a list of human-readable problems. Empty means OK."""
        problems: list[str] = []
        if self.poll_interval_seconds < 5:
            problems.append("poll_interval_seconds must be >= 5")
        if self.resume.cooldown_minutes < 0:
            problems.append("resume.cooldown_minutes must be >= 0")
        if self.resume.max_retries < 1:
            problems.append("resume.max_retries must be >= 1")
        if self.resume.retry_backoff_seconds and len(
            self.resume.retry_backoff_seconds
        ) < self.resume.max_retries:
            log.warning(
                "retry_backoff_seconds has %d entries but max_retries is %d; "
                "the last value will be reused",
                len(self.resume.retry_backoff_seconds),
                self.resume.max_retries,
            )
        if self.usage.restored_threshold_percent >= self.usage.exhausted_threshold_percent:
            problems.append(
                "usage.restored_threshold_percent must be lower than "
                "usage.exhausted_threshold_percent"
            )
        if self.notifications.telegram.enabled:
            if not self.notifications.telegram.bot_token:
                problems.append("telegram.enabled=true but telegram.bot_token is empty")
            if not self.notifications.telegram.chat_id:
                problems.append("telegram.enabled=true but telegram.chat_id is empty")
        if self.task_lock.enabled:
            has_target = bool(
                self.target.conversation_id
                or self.target.conversation_title
                or self.task_lock.project
                or self.task_lock.conversation
            )
            if not has_target:
                problems.append(
                    "task_lock.enabled=true but no target conversation is configured"
                )
        return problems


def _build(cls: type, data: Any) -> Any:
    """Recursively build a dataclass from a dict, ignoring unknown keys."""
    if not is_dataclass(cls):
        return data
    if data is None:
        return cls()
    if not isinstance(data, dict):
        log.warning("expected a mapping for %s, got %r", cls.__name__, type(data).__name__)
        return cls()

    kwargs: dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}
    for key, value in data.items():
        f = known.get(key)
        if f is None:
            log.warning("unknown config key ignored: %s.%s", cls.__name__, key)
            continue
        type_hint = f.type
        # Nested dataclass
        if isinstance(type_hint, str):
            nested = _resolve_type(type_hint)
        else:
            nested = type_hint
        if isinstance(nested, type) and is_dataclass(nested):
            kwargs[key] = _build(nested, value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def _resolve_type(name: str) -> Any:
    return globals().get(name)


def load_config(path: Path | str | None = None, *, create_if_missing: bool = True) -> AppConfig:
    """Load config.yaml, falling back to config.example.yaml, then defaults."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH

    if not cfg_path.exists():
        if create_if_missing and EXAMPLE_CONFIG_PATH.exists():
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(
                EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8"
            )
            log.info("created config.yaml from config.example.yaml: %s", cfg_path)
        else:
            log.warning("config file not found, using built-in defaults: %s", cfg_path)
            return AppConfig()

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.error("config.yaml is not valid YAML, using defaults: %s", exc)
        return AppConfig()

    if not isinstance(raw, dict):
        log.error("config.yaml root must be a mapping, using defaults")
        return AppConfig()

    # The brief's sample config puts `telegram` at the top level. Accept both
    # spellings so an existing config.yaml keeps working after the nested
    # notifications block was introduced.
    top_telegram = raw.pop("telegram", None)
    if isinstance(top_telegram, dict):
        notifications = raw.setdefault("notifications", {})
        if isinstance(notifications, dict) and "telegram" not in notifications:
            notifications["telegram"] = top_telegram
            log.debug("mapped top-level `telegram` into notifications.telegram")

    cfg = _build(AppConfig, raw)
    cfg._source_path = str(cfg_path)  # type: ignore[attr-defined]
    return cfg


def config_as_dict(cfg: AppConfig) -> dict[str, Any]:
    """Serialisable view (used by `--print-config`)."""
    def convert(obj: Any) -> Any:
        if hasattr(obj, "__dataclass_fields__"):
            return {f.name: convert(getattr(obj, f.name)) for f in fields(obj)}
        return obj

    return convert(copy.deepcopy(cfg))


def config_as_yaml(cfg: AppConfig) -> str:
    """Serialise the effective config back to YAML (used for atomic writes)."""
    return yaml.safe_dump(config_as_dict(cfg), allow_unicode=True, sort_keys=False)


def save_config(cfg: AppConfig, path: Path | str | None = None) -> Path:
    """Atomically write ``cfg`` to ``path`` (tmp + validate + os.replace).

    Guarantees the on-disk file is never half-written: a crash leaves either
    the old file or the complete new one.
    """
    import os
    import tempfile

    target = Path(path) if path else Path(getattr(cfg, "_source_path", DEFAULT_CONFIG_PATH))
    payload = config_as_yaml(cfg)
    # Validate it parses back before replacing the real file.
    try:
        yaml.safe_load(payload)
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise ValueError(f"refusing to write invalid YAML: {exc}") from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    log.info("config saved: %s", target)
    return target
