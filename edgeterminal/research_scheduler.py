"""Manage the local macOS launchd research refresh LaunchAgent."""
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
from pathlib import Path

from .config import ROOT

LABEL = "com.edgeterminal.research-refresh"
PYTHON = ROOT / ".venv/bin/python"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library/Logs/EdgeTerminal"
OUT_LOG = LOG_DIR / "research-refresh.out.log"
ERR_LOG = LOG_DIR / "research-refresh.err.log"


def specification() -> dict:
    return {"Label": LABEL, "ProgramArguments": [str(PYTHON), "-m",
            "edgeterminal.research_refresh", "run"],
            "WorkingDirectory": str(ROOT), "StartInterval": 300, "RunAtLoad": True,
            "StandardOutPath": str(OUT_LOG), "StandardErrorPath": str(ERR_LOG),
            "ProcessType": "Background"}


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, check=False)


def install() -> dict:
    if os.uname().sysname != "Darwin":
        raise RuntimeError("LaunchAgent installation requires macOS")
    if not PYTHON.is_file():
        raise FileNotFoundError(f"EdgeTerminal virtualenv Python missing: {PYTHON}")
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    raw = plistlib.dumps(specification(), fmt=plistlib.FMT_XML)
    if PLIST.exists() and PLIST.read_bytes() != raw:
        raise ValueError("installed LaunchAgent differs; uninstall before replacing it")
    if not PLIST.exists():
        with PLIST.open("xb") as file:
            file.write(raw)
    out = _launchctl("bootstrap", _domain(), str(PLIST))
    if out.returncode and "already loaded" not in out.stderr.lower():
        # A same-plist reinstall can report I/O error for an already loaded job.
        active = _launchctl("print", f"{_domain()}/{LABEL}")
        if active.returncode:
            raise RuntimeError(f"launchctl bootstrap failed: {out.stderr.strip()}")
    return status()


def uninstall() -> dict:
    if os.uname().sysname != "Darwin":
        raise RuntimeError("LaunchAgent removal requires macOS")
    if PLIST.exists():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
        PLIST.unlink()
    return status()


def status() -> dict:
    if os.uname().sysname != "Darwin":
        return {"supported": False, "installed": PLIST.exists(), "loaded": False}
    active = _launchctl("print", f"{_domain()}/{LABEL}")
    return {"supported": True, "installed": PLIST.exists(),
            "loaded": active.returncode == 0, "label": LABEL,
            "plist": str(PLIST), "interval_seconds": 300,
            "stdout_log": str(OUT_LOG), "stderr_log": str(ERR_LOG),
            "detail": "loaded" if active.returncode == 0 else active.stderr.strip()[:300]}


def logs(lines: int = 40) -> dict:
    if lines < 1 or lines > 500:
        raise ValueError("lines must be 1..500")
    return {"stdout": "\n".join(OUT_LOG.read_text().splitlines()[-lines:]) if OUT_LOG.exists() else "",
            "stderr": "\n".join(ERR_LOG.read_text().splitlines()[-lines:]) if ERR_LOG.exists() else ""}


def main() -> None:
    parser = argparse.ArgumentParser(description="Install or inspect the EdgeTerminal research LaunchAgent")
    parser.add_argument("action", choices=("install", "uninstall", "status", "logs"))
    parser.add_argument("--lines", type=int, default=40)
    args = parser.parse_args()
    result = {"install": install, "uninstall": uninstall, "status": status,
              "logs": lambda: logs(args.lines)}[args.action]()
    import json
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
