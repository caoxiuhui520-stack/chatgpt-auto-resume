"""Windows autostart via Task Scheduler.

The brief is explicit that a Startup-folder shortcut is not acceptable. A
scheduled task gives us a logon trigger, a start delay (so the task does not
race the desktop session), and restart-on-failure.

Implemented through PowerShell's ScheduledTasks module because the XML settings
that matter here (``RestartCount`` / ``RestartInterval`` / ``ExecutionTimeLimit``)
are awkward to express with plain ``schtasks``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.utils.logging_setup import get_logger

log = get_logger("runtime.autostart")

DEFAULT_TASK_NAME = "ChatGPTAutoResume"


def _run_ps(script: str, timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 1, "powershell.exe not found"
    except subprocess.TimeoutExpired:
        return 1, "powershell command timed out"


def _python_launcher(project_root: Path) -> tuple[str, str]:
    """Return (executable, argument string) for the daemon."""
    venv_python = project_root / ".venv" / "Scripts" / "python.exe"
    exe = str(venv_python if venv_python.exists() else Path(sys.executable))
    args = f'-m app.main --config "{project_root / "config.yaml"}"'
    return exe, args


def is_autostart_installed(task_name: str = DEFAULT_TASK_NAME) -> bool:
    code, out = _run_ps(
        f"if (Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue) "
        f"{{ 'YES' }} else {{ 'NO' }}"
    )
    return code == 0 and "YES" in out


def install_autostart(
    project_root: Path,
    task_name: str = DEFAULT_TASK_NAME,
    delay_seconds: int = 25,
) -> tuple[bool, str]:
    exe, args = _python_launcher(project_root)
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    script = f"""
$ErrorActionPreference = 'Stop'
$action  = New-ScheduledTaskAction -Execute '{exe}' -Argument '{args}' -WorkingDirectory '{project_root}'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT{int(delay_seconds)}S'
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName '{task_name}' -Action $action -Trigger $trigger `
    -Settings $settings -Description 'ChatGPT Auto Resume daemon' -Force | Out-Null
'INSTALLED'
"""
    code, out = _run_ps(script)
    ok = code == 0 and "INSTALLED" in out
    if ok:
        log.info("scheduled task installed: %s -> %s %s", task_name, exe, args)
    else:
        log.error("failed to install scheduled task: %s", out.strip()[:500])
    return ok, out.strip()


def uninstall_autostart(
    task_name: str = DEFAULT_TASK_NAME, *, remove_user_data: bool = False
) -> tuple[bool, str]:
    """Remove the task. User config and logs are preserved by default."""
    script = f"""
$ErrorActionPreference = 'SilentlyContinue'
Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false
'REMOVED'
"""
    code, out = _run_ps(script)
    ok = code == 0 and "REMOVED" in out
    log.info("scheduled task removed: %s (user data preserved=%s)", task_name, not remove_user_data)
    return ok, out.strip()


def run_now(task_name: str = DEFAULT_TASK_NAME) -> tuple[bool, str]:
    code, out = _run_ps(f"Start-ScheduledTask -TaskName '{task_name}'; 'STARTED'")
    return code == 0 and "STARTED" in out, out.strip()
