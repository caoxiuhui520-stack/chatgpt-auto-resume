# ChatGPT Auto Resume

Monitors the local Codex / ChatGPT usage quota and, **after the 5-hour window
has genuinely reset**, automatically sends a pre-configured "continue" prompt
into an already-running ChatGPT Desktop conversation so an interrupted Work
session resumes on its own.

> **This program never bypasses, extends or circumvents a quota limit.**
> It only ever acts *after* the provider itself reports fresh quota.

---

## What it does / does not do

**Does**

- Reads the real quota from the locally installed Codex CLI over stdio
  JSON-RPC (`account/rateLimits/read`) — no screen scraping, no cookies, no
  private web APIs, no OCR.
- Detects a genuine quota reset (previous exhausted → now available **and** the
  window moved forward), not just "the number went up".
- Checks that ChatGPT Desktop is running, that the app is idle, and (optionally)
  that the conversation on screen is the one you meant, before typing anything.
- Sends `prompts/continue.txt`, then records the fact so it can never send
  twice for the same window — across crashes and reboots.
- Notifies on Windows toast and/or Telegram.
- Registers itself as a Windows scheduled task at logon.

**Does not**

- Bypass quota limits, buy credits, or consume reset credits.
- Read or replay cookies / tokens for the web UI.
- Use fixed screen coordinates, OCR, or pixel matching.
- Use PyAutoGUI-style blind clicking.

---

## Architecture

```
config.yaml
     │
     ▼
  app/main.py ──────────────► Daemon (app/daemon.py)
                                  │
        ┌─────────────────────────┼──────────────────────────┐
        ▼                         ▼                          ▼
  UsageProvider            StateMachine               Notification
  (app/usage/)             (app/state_machine.py)     (app/notification/)
   ├─ codex_app_server      STARTING                   ├─ windows (toast)
   ├─ codex_http            WORKING                    └─ telegram
   └─ fake                  QUOTA_EXHAUSTED
                            WAITING_RESET            Storage
                            READY_TO_RESUME          (app/storage/state_store.py)
                            RESUMING                  data/state.json
                            COOLDOWN
                            ERROR  (never terminal)
                                  │
                                  ▼
                        ResumeManager (app/resume/)
                         ├─ DuplicateGuard   4-layer protection
                         ├─ RetryManager     error-classified backoff
                         └─ ChatGptController (app/chatgpt/)
                              ├─ process_detector  Win32 enumeration
                              ├─ window_controller UI Automation
                              ├─ work_detector     is_chatgpt_busy()
                              └─ prompt_sender     UIA ▸ clipboard+Ctrl+V
```

The whole decision surface is I/O-free and therefore unit-testable. The
provider and the ChatGPT controller are both replaceable behind an interface
(`UsageProvider`, `ChatGptController`), which is what lets 60 tests drive the
**real** daemon with no Codex and no ChatGPT window.

---

## Quota source (verified, not guessed)

Priority, exactly as specified:

1. **`codex app-server`** → `account/rateLimits/read` — *primary, verified on
   this machine*
2. `codex_http` → `GET https://chatgpt.com/backend-api/wham/usage` with the
   local Codex credentials — *fallback*
3. `fake` — offline scenarios

The app-server handshake, probed live against `codex-cli 0.147.0`:

```jsonc
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"chatgpt-auto-resume","version":"0.1.0"}}}
{"jsonrpc":"2.0","method":"initialized","params":{}}          // required by current builds
{"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read","params":{}}
```

Real response shape (identifiers removed):

```jsonc
{
  "rateLimits": {
    "limitId": "codex",
    "primary":   {"usedPercent": 100, "windowDurationMins": 300,   "resetsAt": 1791321160},
    "secondary": {"usedPercent": 33,  "windowDurationMins": 10080, "resetsAt": 1791597676},
    "planType": "plus",
    "rateLimitReachedType": "rate_limit_reached"
  },
  "rateLimitResetCredits": {"availableCount": 2, "credits": []}
}
```

Mapping rules baked into the parser:

| Field | Meaning |
|---|---|
| `primary` | the rolling **5h** window |
| `secondary` | the **weekly** window |
| `windowDurationMins` | classification (`> 12h` ⇒ weekly), with a positional fallback |
| `resetsAt` | Unix **seconds** (milliseconds tolerated), normalised to the minute |
| `rateLimitReachedType == "rate_limit_reached"` | hard "exhausted" signal |

Two traps handled explicitly:

- **Placeholder window.** Codex can report `usedPercent: 0` with
  `resetsAt == now + 5h` before the window has really started. That is flagged
  (`unactivated_window`) and never treated as a restoration.
- **Drift.** `resetsAt` wobbles by a second or two between polls of the *same*
  window; the window id is therefore truncated to the minute, and a new window
  requires the reset to move forward by **≥ 5 minutes**.

---

## Safety design

### Four layers of duplicate protection

| Layer | Mechanism | Survives |
|---|---|---|
| 1 | `reset_id` identity — `{source}:five_hour:{reset_unix}` fires once, ever | restarts |
| 2 | Cooldown (default 30 min) | restarts |
| 3 | Atomic `data/state.json` + bounded `triggered_reset_ids` history | crashes, reboots |
| 4 | Re-read the quota immediately before sending; abort if it changed | mid-flight changes |

### `DRY_RUN` is on by default

`config.yaml` ships with `dry_run: true`. In that mode the program will **only**
print:

```
Quota exhausted              → state QUOTA_EXHAUSTED
Quota restored               → state READY_TO_RESUME
ChatGPT window found
ChatGPT idle
Would send prompt
```

It never focuses the window, never touches the clipboard, never types. The
dry-run run does **not** mark the window as triggered, so the real reset is not
swallowed. Leave it running for one full quota cycle and confirm the log reads
correctly before setting `dry_run: false`.

### Never type into a busy app

`is_chatgpt_busy()` is three-valued. `None` ("cannot tell") is treated as
**busy**, because a false positive only costs a delayed resume while a false
negative pastes a prompt into a running task. Busy/not-running/window-missing
are *waitable*: they do not consume retry budget.

### Two transports, in the prescribed order

Measured on a real install: ChatGPT Desktop (Electron) publishes the outer
`RootWebArea` but **not** the contenteditable composer, so

1. UI Automation `ValuePattern` is used when a composer is addressable;
2. otherwise **clipboard + `Ctrl+V` + `Enter`** is used — guarded by a
   *foreground-window verification* (`GetForegroundWindow() == hwnd`). If the
   window cannot be proven to be in front, nothing is typed.

The send button is preferred over `Enter`; `Enter` is only the fallback. No
screen coordinates are used anywhere.

### Optional task lock

```yaml
task_lock:
  enabled: true
  project: "huanyu"
```

With the lock on, a resume is refused unless the on-screen conversation matches
the configured project. An unreadable conversation title counts as a mismatch —
it is that conservative on purpose.

---

## Install

```powershell
git clone https://github.com/<you>/chatgpt-auto-resume.git
cd chatgpt-auto-resume
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

The installer creates `.venv`, installs requirements, seeds `config.yaml` from
`config.example.yaml`, creates `logs/` and `data/`, and registers the
`ChatGPTAutoResume` scheduled task (logon trigger, 25 s delay,
restart-on-failure).

Requirements: Windows 10/11, Python 3.12+, ChatGPT Desktop, and the Codex CLI
logged in (`codex login`).

---

## Configuration

See `config.example.yaml`. The important keys:

```yaml
poll_interval_seconds: 30
dry_run: true                     # keep true until the logs look right

resume:
  prompt_file: prompts/continue.txt
  cooldown_minutes: 30
  max_retries: 5
  retry_backoff_seconds: [0, 30, 120, 300, 900]

chatgpt:
  auto_start: true                # launch the app if the quota is back but it is gone
  exe_path: ""                    # auto-discovered when empty

usage:
  provider: codex_app_server
  fallback_providers: ["codex_http", "fake"]
  exhausted_threshold_percent: 100
  restored_threshold_percent: 99

notifications:
  windows: true
telegram:                         # top-level, matching the brief's sample
  enabled: false
  bot_token: ""
  chat_id: ""
```

---

## CLI

```powershell
python -m app.main                     # run the daemon
python -m app.main once --ticks 4      # N ticks, printing every decision
python -m app.main usage               # current quota as JSON
python -m app.main doctor              # environment + live diagnostics
python -m app.main status              # persisted state
python -m app.main print-config
python -m app.main install-autostart
python -m app.main uninstall-autostart
```

Offline walkthrough of the whole pipeline (no Codex, no ChatGPT needed):

```powershell
$env:AUTO_RESUME_SCENARIO = "exhausted_then_restored"
python -m app.main once --provider fake --chatgpt fake --ticks 4
```

---

## Logs

`logs/app.log`, rotating (10 MB × 5). Every line carries timestamp, level,
module and state:

```
2026-10-07 03:12:00 INFO  usage          5h quota exhausted
2026-10-07 08:12:12 INFO  daemon         quota restored (new window)
2026-10-07 08:12:13 INFO  chatgpt.process ChatGPT window found
2026-10-07 08:12:13 INFO  daemon         ChatGPT idle
2026-10-07 08:12:14 INFO  chatgpt.sender prompt submitted via clipboard+ctrl-v+enter
```

Never written to logs: credentials, cookies, auth tokens, Telegram bot tokens.
Enforced by a `logging.Filter` (`app/utils/logging_setup.py`), not by
convention — plus regex scrubbing for bearer tokens, JWTs and Telegram token
shapes.

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q     # 60 tests
```

Coverage includes: every state-machine transition; ERROR being recoverable;
quota normal / exhausted / restored; **availability flapping inside one window
must not fire**; ChatGPT busy, idle, unknown, not running; send failure →
retry → give up; **a restart must not resend**; the same `reset_id` appearing
twice; task-lock mismatch; provider failure and recovery; JSON-RPC payload
parsing pinned to real captured data; state-file corruption and atomicity.

---

## Design decisions worth knowing

- **Win32 `EnumWindows` for discovery, UIA only for the picked window.**
  Measured: pywinauto's whole-desktop enumeration returned 104 windows and
  found zero ChatGPT windows, while `EnumWindows` returned the two real
  `ChatGPT.exe` windows immediately. COM is expensive and was unreliable here.
- **The app-server child is long-lived and restarted on failure**, so one crash
  does not kill the daemon, while a normal poll costs a single RPC round trip.
  `codex app-server` is launched through `cmd.exe /D /S /C` on Windows because
  it ships as a `.cmd` shim, with `CREATE_NO_WINDOW`.
- **`ERROR` is never terminal.** A single bad read cannot stop the daemon; the
  first failures are absorbed and only sustained failure raises the state.
- **Waitable errors do not consume retries.** Counting "ChatGPT is busy" would
  burn five retries in two minutes on a machine that was merely occupied.
- **Dataclasses, not a validation framework**, for configuration: a smaller
  dependency surface for something that runs unattended for weeks.
- **Unknown config keys are reported, not silently ignored**, so a typo does
  not look like working configuration.

---

## Status

Phases 1–10 of the build brief are implemented and tested. Phase 11 (tray UI)
is intentionally deferred — per the brief, it must not be allowed to delay the
core daemon.

| Phase | Scope | State |
|---|---|---|
| 1 | environment recon, app-server protocol verification | done |
| 2 | project skeleton, config, models, state store, state machine | done |
| 3 | real Codex usage provider | done |
| 4 | ChatGPT process / window detection | done |
| 5 | `is_chatgpt_busy()` | done |
| 6 | prompt sender with `DRY_RUN` | done |
| 7 | real send (requires `dry_run: false`) | implemented, gated |
| 8 | duplicate guard, retry, cooldown | done |
| 9 | Windows + Telegram notifications | done |
| 10 | Task Scheduler, install/uninstall scripts | done |
| 11 | tray UI | deferred |

## Reference projects

Studied for design ideas (not copied, not depended on) — clones live in
`references/` and are git-ignored:

- [codex-minibar](https://github.com/vertopolkaLF/codex-minibar) — app-server
  JSON-RPC client shape, window normalisation, activation state machine,
  atomically-written state.
- [codex-usage-monitor](https://github.com/upstream-ray/codex-usage-monitor) —
  quota window identity (`provider:window:reset_at`), notification
  deduplication, exponential backoff capped at the normal interval,
  fast-poll after the reset time passes.

Everything this project adds — the ChatGPT Desktop controller, Work busy
detection, the resume state machine, the prompt sender, the duplicate guard and
the retry manager — is original.

## License

MIT — see [LICENSE](LICENSE).
