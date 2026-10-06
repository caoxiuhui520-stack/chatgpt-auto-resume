"""Phase 1 probe: talk to `codex app-server` over stdio JSON-RPC and dump
sanitized account / rate-limit data.

Usage:
    python tools/test_codex_usage.py

It spawns `codex app-server`, performs:
    1. initialize
    2. account/read
    3. account/rateLimits/read

and prints the sanitized JSON so we can build a real UsageSnapshot model
from the *actual* locally installed protocol version.

No credentials, tokens or cookies are printed - keys matching the
SECRET_HINTS list are redacted before output.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

SECRET_HINTS = ("token", "secret", "authorization", "cookie", "api_key",
                "apikey", "password", "credential", "access", "refresh", "jwt")

CLIENT_INFO = {"name": "chatgpt-auto-resume", "version": "0.1.0"}


def find_codex() -> str:
    """Locate the codex executable without relying on PATH inheritance quirks."""
    exe = shutil.which("codex")
    if exe:
        return exe
    candidates = [
        os.path.expandvars(r"%APPDATA%\npm\codex.cmd"),
        os.path.expandvars(r"%APPDATA%\npm\codex"),
        r"C:\Users\Administrator\AppData\Roaming\npm\codex.cmd",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError("codex executable not found")


def sanitize(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = k.lower()
            if any(h in lk for h in SECRET_HINTS):
                out[k] = "<redacted>" if v not in (None, "") else v
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


class JsonRpcStdio:
    """Minimal newline-delimited JSON-RPC client over a child process pipe."""

    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._stderr: list[str] = []
        self._next_id = 0
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._q.put(json.loads(line))
            except json.JSONDecodeError:
                self._q.put({"_unparsed": line})

    def _pump_stderr(self) -> None:
        assert self.proc.stderr
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())

    def send(self, method: str, params: dict | None = None) -> int:
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return self._next_id

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def read_until_id(self, want_id: int, timeout: float = 20.0) -> dict | None:
        deadline = time.time() + timeout
        leftover: list[dict] = []
        result = None
        while time.time() < deadline:
            try:
                msg = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if msg.get("id") == want_id:
                result = msg
                break
            leftover.append(msg)
        for m in leftover:
            self._q.put(m)
        return result

    def stderr_text(self) -> str:
        return "\n".join(self._stderr[-40:])

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def call(rpc: JsonRpcStdio, method: str, params: dict | None = None,
         timeout: float = 20.0) -> dict | None:
    rid = rpc.send(method, params)
    resp = rpc.read_until_id(rid, timeout=timeout)
    if resp is None:
        return {"_error": "timeout", "_method": method}
    return resp


def main() -> int:
    codex = find_codex()
    print(f"[i] codex executable : {codex}")
    argv = [codex, "app-server"]
    print(f"[i] spawning         : {' '.join(argv)}\n")

    rpc = JsonRpcStdio(argv)
    try:
        # 1. initialize
        init = call(rpc, "initialize", {"clientInfo": CLIENT_INFO}, timeout=30)
        print("=== initialize ===")
        print(json.dumps(sanitize(init), indent=2, ensure_ascii=False)[:2000])

        # Some builds require an "initialized" notification after the handshake.
        try:
            rpc.notify("initialized", {})
        except Exception:
            pass

        # 2. account/read
        acct = call(rpc, "account/read", {}, timeout=30)
        print("\n=== account/read ===")
        print(json.dumps(sanitize(acct), indent=2, ensure_ascii=False)[:2500])

        # 3. account/rateLimits/read
        limits = call(rpc, "account/rateLimits/read", {}, timeout=30)
        print("\n=== account/rateLimits/read ===")
        print(json.dumps(sanitize(limits), indent=2, ensure_ascii=False)[:6000])

        # Quick derived summary, when the shape matches the schema.
        try:
            r = (limits or {}).get("result", {})
            snap = r.get("rateLimits", {}) or {}
            primary = snap.get("primary") or {}
            secondary = snap.get("secondary") or {}
            print("\n=== derived (primary=5h, secondary=weekly, best-effort) ===")
            print(json.dumps({
                "plan_type": snap.get("planType"),
                "limit_id": snap.get("limitId"),
                "reached_type": snap.get("rateLimitReachedType"),
                "primary_used_percent": primary.get("usedPercent"),
                "primary_resets_at": primary.get("resetsAt"),
                "primary_window_mins": primary.get("windowDurationMins"),
                "secondary_used_percent": secondary.get("usedPercent"),
                "secondary_resets_at": secondary.get("resetsAt"),
                "secondary_window_mins": secondary.get("windowDurationMins"),
                "by_limit_id_keys": list((r.get("rateLimitsByLimitId") or {}).keys()),
            }, indent=2, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(f"\n[!] derived summary failed: {exc}")

        err = rpc.stderr_text()
        if err:
            print("\n=== child stderr (tail) ===")
            print(err)
        return 0
    finally:
        rpc.close()


if __name__ == "__main__":
    sys.exit(main())
