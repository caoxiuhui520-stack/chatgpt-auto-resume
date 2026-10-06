"""Codex app-server usage provider.

Talks to the locally installed ``codex`` CLI over stdio JSON-RPC, exactly the
way the protocol was verified on this machine (codex-cli 0.147.0)::

    {"id":1,"method":"initialize","params":{"clientInfo":{...}}}
    {"method":"initialized","params":{}}          <- required handshake notice
    {"id":2,"method":"account/rateLimits/read","params":{}}

Response shape (v2 protocol)::

    result.rateLimits.primary    -> RateLimitWindow {usedPercent, resetsAt, windowDurationMins}
    result.rateLimits.secondary  -> RateLimitWindow
    result.rateLimits.rateLimitReachedType -> "rate_limit_reached" | workspace_* | null

Design decisions taken from the reference implementations and recorded here
on purpose:

* The child process is long-lived and restarted on failure, so a single crash
  does not take down the daemon, but a normal poll costs one RPC round trip.
* ``resetsAt`` is Unix **seconds**; it is normalised to the minute before it is
  used to build a window id, because the field drifts by a second or two
  between polls of the *same* window.
* Window classification prefers ``windowDurationMins`` (> 12h => weekly) and
  only falls back to field position (primary/secondary) when the duration is
  missing.
* A quota window that has not been activated yet looks like a full window with
  ``reset == now + 5h``. It must not be mistaken for a fresh reset, so it is
  flagged on the snapshot.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from app.models import (
    FIVE_HOUR_WINDOW_MINUTES,
    SHORT_WINDOW_MAX_MINUTES,
    UsageSnapshot,
    UsageWindow,
    from_unix,
    normalize_to_minute,
    utcnow,
)
from app.usage.base import ProviderError, UsageProvider, register
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig

log = get_logger("usage.codex")

DEFAULT_TIMEOUT = 15.0
CREATE_NO_WINDOW = 0x08000000
UNACTIVATED_TOLERANCE = timedelta(minutes=5)

CLIENT_INFO = {"name": "chatgpt-auto-resume", "version": "0.1.0"}

_WINDOWS_EXEC_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1")


def discover_codex(explicit: str = "") -> str:
    """Find the codex executable. Never guesses blindly - raises if nothing."""
    if explicit:
        p = Path(explicit)
        if p.exists():
            return str(p)
        raise ProviderError("codex_not_found", f"configured codex_path does not exist: {explicit}")

    found = shutil.which("codex")
    if found:
        return found

    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        npm = Path(appdata) / "npm"
        candidates += [npm / "codex.cmd", npm / "codex.exe", npm / "codex.ps1"]
    if localappdata:
        candidates += [
            Path(localappdata) / "Programs" / "codex" / "codex.exe",
            Path(localappdata) / "Programs" / "OpenAI" / "Codex" / "codex.exe",
        ]
    candidates.append(Path.home() / ".codex" / "bin" / "codex.exe")
    candidates.append(Path("/usr/local/bin/codex"))

    for c in candidates:
        if c.exists():
            return str(c)

    raise ProviderError(
        "codex_not_found",
        "could not locate the codex CLI; set usage.codex_path in config.yaml",
    )


def _wrap_argv(exe: str, args: list[str]) -> list[str]:
    """Run .cmd/.bat/.ps1 wrappers through the right interpreter."""
    suffix = Path(exe).suffix.lower()
    if suffix in (".cmd", ".bat"):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/D", "/S", "/C", exe, *args]
    if suffix == ".ps1":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe, *args]
    return [exe, *args]


class CodexAppServerClient:
    """Minimal newline-delimited JSON-RPC client with auto-restart."""

    def __init__(
        self,
        executable: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        extra_args: list[str] | None = None,
    ) -> None:
        self.executable = executable
        self.timeout = timeout
        self.extra_args = extra_args or ["app-server"]
        self._proc: subprocess.Popen[str] | None = None
        self._q: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._stderr: list[str] = []
        self._lock = threading.Lock()
        self._next_id = 0
        self._started_at = 0.0
        self.restarts = 0

    # -- lifecycle ---------------------------------------------------------

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self._alive():
            return
        self.stop()
        argv = _wrap_argv(self.executable, self.extra_args)
        log.debug("starting codex app-server: %s", " ".join(argv))
        creation = CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation,
            )
        except OSError as exc:
            raise ProviderError("spawn_failed", f"could not start codex app-server: {exc}") from exc

        self._q = queue.Queue()
        self._stderr = []
        self._next_id = 0
        self._started_at = time.time()
        if self.restarts:
            log.info("codex app-server restarted (restart #%d)", self.restarts)
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

        # Handshake.  `initialize` then the `initialized` notification, which
        # newer builds require before serving any other request.
        init = self._request("initialize", {"clientInfo": CLIENT_INFO})
        if init is None:
            raise ProviderError("handshake_failed", "no response to initialize")
        if "error" in init:
            raise ProviderError("handshake_failed", f"initialize rejected: {init['error']}")
        self._notify("initialized", {})

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._q.put(json.loads(line))
            except json.JSONDecodeError:
                log.debug("non-JSON stdout from app-server: %s", line[:200])

    def _pump_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr.append(line.rstrip())
            if len(self._stderr) > 200:
                self._stderr = self._stderr[-100:]

    # -- JSON-RPC ----------------------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        self._next_id += 1
        rid = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        return self._await(rid)

    def _await(self, rid: int) -> dict[str, Any] | None:
        deadline = time.time() + self.timeout
        deferred: list[dict[str, Any]] = []
        try:
            while time.time() < deadline:
                if self._proc is not None and self._proc.poll() is not None:
                    raise ProviderError(
                        "app_server_exited",
                        f"codex app-server exited before responding (code={self._proc.returncode})",
                    )
                try:
                    msg = self._q.get(timeout=0.25)
                except queue.Empty:
                    continue
                if msg.get("id") == rid:
                    return msg
                # Server-initiated notifications / unrelated responses.
                deferred.append(msg)
        finally:
            for m in deferred:
                self._q.put(m)
        return None

    def stderr_tail(self, lines: int = 8) -> str:
        return "\n".join(self._stderr[-lines:])

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Thread-safe request with one automatic restart on transport failure."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    self.start()
                    resp = self._request(method, params)
                except ProviderError:
                    if attempt == 2:
                        raise
                    self.restarts += 1
                    log.warning("codex app-server transport error, restarting")
                    self.stop()
                    continue
                if resp is None:
                    if attempt == 2:
                        raise ProviderError(
                            "timeout",
                            f"{method} timed out after {self.timeout}s"
                            + (f"; stderr: {self.stderr_tail()}" if self._stderr else ""),
                        )
                    self.restarts += 1
                    log.warning("codex app-server gave no reply, restarting")
                    self.stop()
                    continue
                if "error" in resp:
                    raise ProviderError("rpc_error", f"{method} failed: {resp['error']}")
                return resp.get("result") or {}
        raise ProviderError("unreachable", "request loop fell through")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_window(value: Any, label: str) -> UsageWindow:
    if not isinstance(value, dict):
        return UsageWindow(label=label)
    used = value.get("usedPercent")
    try:
        used_f = float(used) if used is not None else None
    except (TypeError, ValueError):
        used_f = None
    minutes = value.get("windowDurationMins")
    try:
        minutes_i = int(minutes) if minutes is not None else None
    except (TypeError, ValueError):
        minutes_i = None
    return UsageWindow(
        used_percent=used_f,
        window_minutes=minutes_i,
        resets_at=from_unix(value.get("resetsAt")),
        label=label,
    )


def _looks_like_unactivated_five_hour(window: UsageWindow, now: datetime) -> bool:
    """A 5h window that has not really started yet.

    Codex returns ``usedPercent`` in 0..1 with ``resetsAt`` pinned to
    ``now + 5h`` before the first message of the window is sent. Treating that
    as "quota restored" would fire a resume that has nothing to resume.
    """
    if window.window_minutes != FIVE_HOUR_WINDOW_MINUTES:
        return False
    if window.used_percent is None or window.used_percent > 1:
        return False
    if window.resets_at is None:
        return False
    expected = now + timedelta(minutes=FIVE_HOUR_WINDOW_MINUTES)
    return abs(window.resets_at - expected) <= UNACTIVATED_TOLERANCE


def parse_rate_limits(
    result: dict[str, Any],
    *,
    source: str = "codex_app_server",
    exhausted_threshold: float = 100.0,
    restored_threshold: float = 99.0,
    now: datetime | None = None,
) -> UsageSnapshot:
    """Turn an ``account/rateLimits/read`` result into a UsageSnapshot."""
    now = now or utcnow()
    rate_limits = result.get("rateLimits")
    if not isinstance(rate_limits, dict):
        # Multi-bucket view: pick the "codex" bucket, else the first one.
        by_id = result.get("rateLimitsByLimitId")
        if isinstance(by_id, dict) and by_id:
            rate_limits = by_id.get("codex") or next(iter(by_id.values()))
        else:
            raise ProviderError("invalid_response", "rateLimits missing from the response")

    primary = _parse_window(rate_limits.get("primary"), "five_hour")
    secondary = _parse_window(rate_limits.get("secondary"), "weekly")

    # Normalisation: some plans report only `primary` and it is actually the
    # weekly window (duration > 12h). Move it rather than mislabelling it.
    if secondary.used_percent is None and primary.used_percent is not None:
        if primary.window_minutes and primary.window_minutes > SHORT_WINDOW_MAX_MINUTES:
            secondary = UsageWindow(
                used_percent=primary.used_percent,
                window_minutes=primary.window_minutes,
                resets_at=primary.resets_at,
                label="weekly",
            )
            primary = UsageWindow(label="five_hour")

    reached = rate_limits.get("rateLimitReachedType")
    five_remaining = primary.remaining_percent
    weekly_remaining = secondary.remaining_percent

    if five_remaining is None:
        # No 5h data at all: never claim "available", that would be a false
        # positive and could send a prompt into an exhausted account.
        available = False
    elif reached in ("rate_limit_reached",) or (
        five_remaining is not None and (100.0 - five_remaining) >= exhausted_threshold
    ):
        available = False
    else:
        available = (100.0 - five_remaining) <= restored_threshold

    unactivated = _looks_like_unactivated_five_hour(primary, now)

    snapshot = UsageSnapshot(
        available=available,
        five_hour_remaining_percent=five_remaining,
        weekly_remaining_percent=weekly_remaining,
        five_hour_reset_at=normalize_to_minute(primary.resets_at),
        weekly_reset_at=normalize_to_minute(secondary.resets_at),
        source=source,
        timestamp=now,
        plan_type=rate_limits.get("planType"),
        reached_type=reached,
    )
    snapshot.reset_id = snapshot.compute_reset_id()
    credits = result.get("rateLimitResetCredits") or {}
    snapshot.raw = {
        "five_hour": primary.to_dict(),
        "weekly": secondary.to_dict(),
        "unactivated_window": unactivated,
        "reset_credits_available": credits.get("availableCount"),
        "limit_id": rate_limits.get("limitId"),
        "spend_control_reached": rate_limits.get("spendControlReached"),
    }
    return snapshot


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


@register("codex_app_server")
def _build(cfg: "AppConfig") -> UsageProvider:
    return CodexAppServerProvider(cfg)


class CodexAppServerProvider(UsageProvider):
    name = "codex_app_server"
    description = "local `codex app-server` stdio JSON-RPC (account/rateLimits/read)"

    def __init__(self, cfg: "AppConfig") -> None:
        self.cfg = cfg
        self.executable = discover_codex(cfg.usage.codex_path)
        self.client = CodexAppServerClient(
            self.executable, timeout=float(cfg.usage.request_timeout_seconds)
        )
        self._failures = 0

    def get_usage(self) -> UsageSnapshot:
        try:
            result = self.client.call("account/rateLimits/read", {})
            self._failures = 0
            return parse_rate_limits(
                result,
                source=self.name,
                exhausted_threshold=float(self.cfg.usage.exhausted_threshold_percent),
                restored_threshold=float(self.cfg.usage.restored_threshold_percent),
            )
        except ProviderError as exc:
            self._failures += 1
            log.warning("usage read failed (%s): %s", exc.kind, exc)
            snap = UsageSnapshot(
                available=False,
                source=self.name,
                error=f"{exc.kind}: {exc}",
                timestamp=utcnow(),
                stale=True,
            )
            return snap
        except Exception as exc:  # noqa: BLE001 - a provider must never crash the daemon
            self._failures += 1
            log.exception("unexpected usage read failure")
            return UsageSnapshot(
                available=False,
                source=self.name,
                error=f"unexpected: {exc}",
                timestamp=utcnow(),
                stale=True,
            )

    def close(self) -> None:
        self.client.stop()

    def describe(self) -> str:
        return f"{self.name} (exe={self.executable}, restarts={self.client.restarts})"
