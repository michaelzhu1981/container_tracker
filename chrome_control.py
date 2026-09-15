"""Process-scoped native Chrome bridge; no remote debugging connection."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SystemChromeError(RuntimeError):
    def __init__(self, message: str, code: str = "NAVIGATION") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ChromeTarget:
    pid: int
    window_id: int
    tab_id: int


def chrome_command(pid: int, action: str, *, target: ChromeTarget | None = None, **args) -> Any:
    request = {"pid": pid, "action": action, **args}
    if target is not None:
        request.update(window_id=target.window_id, tab_id=target.tab_id)
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", str(Path(__file__).with_name("chrome_bridge.js")),
             json.dumps(request)],
            check=False, capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemChromeError("Chrome did not respond within 15 seconds.", "TIMEOUT") from exc
    if result.returncode:
        raise SystemChromeError((result.stderr or result.stdout).strip() or "Chrome bridge failed.")
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise SystemChromeError("Chrome returned an invalid bridge response.") from exc
    if not response.get("ok"):
        raise SystemChromeError(response.get("error", "Chrome bridge failed."), response.get("code", "NAVIGATION"))
    return response.get("value")


def normal_chrome_pid() -> int | None:
    """Resolve regular Chrome, excluding Playwright and custom profile instances."""
    result = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True, check=False)
    if result.returncode:
        raise SystemChromeError("Cannot inspect Chrome processes for the tracking window.")
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(.+)", line)
        if not match:
            continue
        command = match[2]
        if not command.startswith(("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))):
            continue
        if not any(flag in command for flag in ("--type=", "--user-data-dir", "--remote-debugging", "--headless")):
            return int(match[1])
    return None


def launch_normal_chrome() -> None:
    executable = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not executable.is_file():
        executable = Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not executable.is_file():
        raise SystemChromeError("Google Chrome is not installed.")
    result = subprocess.run(
        ["open", "-na", str(executable.parents[2]), "--args", "--disable-popup-blocking", "--no-startup-window"],
        check=False, capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise SystemChromeError(result.stderr.strip() or "Could not start Google Chrome.")
