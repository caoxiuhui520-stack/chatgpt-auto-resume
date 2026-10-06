# Known Issues (non-blocking)

These do not prevent daily use of Auto Resume. P0 problems are fixed before
release; this file lists accepted V0.1 limitations.

## Conversation discovery

1. **Project names are not available locally.** ChatGPT Desktop's local cache
   stores `workspace_id` per conversation but no workspace *name*, and this
   account uses no workspaces. The Target picker is therefore
   conversation-first; `ProjectInfo` exists in the model but the project
   dropdown stays disabled until a local source for project names exists.
2. **The local conversation list is a cache.** A conversation created moments
   ago may not appear in `codex.chatgpt-conversations` until the app refreshes
   it. "Use Current Conversation" still works: it stores the id from the
   sidebar state file even when the title is not cached yet.
3. **The current conversation id comes from `browser-sidebar-page-states.json`
   (most recently updated non-error tab).** If the user opens two tabs and
   switches rapidly, the id can briefly lag one tab behind. The title cross
   check (UIA selected list item) catches most of these, and on any
   inconsistency the resolver answers `unknown`/`mismatch` - refusing to send.

## Test send

4. **After an UNCERTAIN test send the GUI cannot verify the ChatGPT side.**
   The transaction is parked as UNCERTAIN and the Arm button stays blocked
   until a later test send confirms. There is deliberately no "retry" button.

## Packaging

5. **The packaged exe starts slower the first time** (Windows Defender scans
   the extracted PySide6 tree). Subsequent starts are quick. The venv +
   `start-gui.ps1` path starts fastest and remains the recommended install.

## Daemon

6. **Chromium renderer reparenting** (backgrounded conversation window) is
   handled by the orphan-renderer fallback, but in that state the keyboard
   transport is unavailable (focus cannot be proven) - only UIA
   ValuePattern/InvokePattern sends work. This is by design.
