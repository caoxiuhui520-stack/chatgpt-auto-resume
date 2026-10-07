# Known Issues (non-blocking)

These do not prevent daily use of Auto Resume. P0/P1 safety gates are fixed
before release; this file lists accepted V0.1 limitations.

## Conversation discovery

1. **Project names come from `~/.codex/.codex-global-state.json`
   (`local-projects`).** If a project was never opened locally it will not
   appear; there is no remote project list (no private web APIs).
2. **The Work thread index (`~/.codex/session_index.jsonl`) is a cache.** A
   brand-new thread may not appear until the desktop app writes it. "Use
   Current Conversation" still works: it stores the id from the desktop tab
   state even when the title is not cached yet.
3. **The current conversation id comes from `browser-sidebar-page-states.json`
   (most recently updated non-error tab).** If two tabs switch rapidly the id
   can lag. This is why the send path is **dual-factor** (id AND UIA title):
   an id/title conflict is IDENTITY_CONFLICT and refuses to send.
4. **Embedded web-cache conversations (chatgpt.com inside the desktop app)
   are read but badged "Web Cache".** They can never become a production
   target: the resolver returns `web_cache_target`, the service `set_target`
   raises, and the GUI disables "Set as Target" for them.

## Prompt presets

5. **Preset variables are render-time only.** The editor preview does not
   substitute `{{conversation_title}}` etc.; they are filled at send time.
6. **Bindings are keyed by conversation id.** If ChatGPT re-issues a new id
   for the same thread, the binding must be re-made.
7. **A broken preset binding is fail-closed.** If a conversation's bound
   preset is missing or fails to render, the send is refused with
   PROMPT_RESOLUTION_FAILED (never a silent fallback to a different prompt).

## Test send / certification

8. **A Test Send certifies exactly one target.** Switching the target
   invalidates the certification (TEST_REQUIRED) and the Arm button stays
   disabled until a new test passes for the new target.
9. **After an UNCERTAIN test send the GUI cannot verify the ChatGPT side.**
   The transaction is parked UNCERTAIN and no new test can run until the user
   clicks "清除测试状态". There is deliberately no auto-retry.

## Packaging

10. **The packaged exe starts slower the first time** (Windows Defender scans
    the extracted PySide6 tree). The venv + `start-gui.ps1` path starts
    fastest and remains the recommended install.

## Daemon

11. **Chromium renderer reparenting** (backgrounded conversation window) is
    handled by the orphan-renderer fallback, but in that state the keyboard
    transport is unavailable (focus cannot be proven) - only UIA
    ValuePattern/InvokePattern sends work. This is by design.
