"""Start email-watch automatically when you log in to your Mac (macOS launchd).

    uv run email-autostart on              # start now, and at every login
    uv run email-autostart on --keep-awake # ...and keep the Mac from idle-sleeping
    uv run email-autostart status          # is it running? the last lines it printed
    uv run email-autostart off             # stop it, and don't start at login

`on` writes one small file, ~/Library/LaunchAgents/local.email-agent.watch.plist,
which tells macOS to run this project's email-watch for you (and to restart it if it
ever crashes). `off` removes that file again. What it prints goes to
data/logs/email-watch.log in this project.
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from email_agent.core.config import PROJECT_ROOT, load_config
from email_agent.core.watch_lock import WatchLock

LABEL = "local.email-agent.watch"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def plist_path(agents_dir: Path = AGENTS_DIR) -> Path:
    return agents_dir / f"{LABEL}.plist"


def log_path() -> Path:
    return load_config().data_dir / "logs" / "email-watch.log"


def build_plist(keep_awake: bool = False, root: Path = PROJECT_ROOT) -> dict:
    """What macOS needs to know: which program, where, and when to (re)start it."""
    program = [str(root / ".venv" / "bin" / "email-watch")]
    if keep_awake:  # caffeinate -i: no idle sleep while it runs (a closed lid still sleeps)
        program = ["/usr/bin/caffeinate", "-i", *program]
    log = str(log_path())
    return {
        "Label": LABEL,
        "ProgramArguments": program,
        "WorkingDirectory": str(root),
        "RunAtLoad": True,                       # start at login (and right now)
        "KeepAlive": {"SuccessfulExit": False},  # restart it if it crashes
        "ThrottleInterval": 60,                  # ...but at most once a minute
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},  # log lines appear at once
        "ProcessType": "Background",
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def is_loaded(run=subprocess.run) -> bool:
    return run(["launchctl", "print", f"{_domain()}/{LABEL}"],
               capture_output=True, text=True).returncode == 0


def turn_on(keep_awake: bool = False, run=subprocess.run, agents_dir: Path = AGENTS_DIR,
            root: Path = PROJECT_ROOT) -> Path:
    program = root / ".venv" / "bin" / "email-watch"
    if not program.exists():
        raise RuntimeError(f"{program} is missing: run  uv sync  first")
    if load_config().mailbox != "imap":
        raise RuntimeError("email-watch watches your real mailbox: set MAILBOX=imap in .env first")
    if is_loaded(run):  # already on: replace it with the new settings
        run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    path = plist_path(agents_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        plistlib.dump(build_plist(keep_awake, root), handle)
    result = run(["launchctl", "bootstrap", _domain(), str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"launchctl could not start it: {result.stderr.strip()}")
    return path


def turn_off(run=subprocess.run, agents_dir: Path = AGENTS_DIR) -> bool:
    """Stop it and remove the login item. Returns False if it wasn't on."""
    was_on = is_loaded(run) or plist_path(agents_dir).exists()
    run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    plist_path(agents_dir).unlink(missing_ok=True)
    return was_on


def status_text(run=subprocess.run, agents_dir: Path = AGENTS_DIR, log_lines: int = 8) -> str:
    lines = []
    if not plist_path(agents_dir).exists():
        lines.append("Auto-start is OFF.  Turn it on with:  uv run email-autostart on")
    else:
        info = run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
        running = info.returncode == 0 and "state = running" in info.stdout
        lines.append("Auto-start is ON (starts at every login). "
                     + ("email-watch is running now." if running else
                        "email-watch is NOT running right now (see the log below)."))
    holder = WatchLock().holder()
    if holder:
        lines.append(f"{load_config().provider_name} is being watched by process {holder}.")
    log = log_path()
    if log.exists():
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-log_lines:]
        lines.append(f"\nLast lines of {log.relative_to(PROJECT_ROOT) if log.is_relative_to(PROJECT_ROOT) else log}:")
        lines += [f"  {line}" for line in tail]
    return "\n".join(lines)


def main() -> None:
    args = sys.argv[1:]
    try:
        if args[:1] == ["on"]:
            path = turn_on(keep_awake="--keep-awake" in args)
            print(f"✅ Auto-start is on. email-watch is starting now, and will start at every "
                  f"login.\n   (login item: {path})\n"
                  "   If you also run email-watch yourself in a terminal, stop that one "
                  "(Ctrl+C): only one watches at a time.\n"
                  "   Check on it with:  uv run email-autostart status")
        elif args[:1] == ["off"]:
            print("Auto-start is off; email-watch stopped." if turn_off()
                  else "Auto-start was already off.")
        elif args[:1] == ["status"] or not args:
            print(status_text())
        else:
            sys.exit("Usage: uv run email-autostart [on [--keep-awake] | off | status]")
    except RuntimeError as problem:
        sys.exit(f"❌ {problem}")


if __name__ == "__main__":
    main()
