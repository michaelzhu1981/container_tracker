"""Process-scoped native Chrome bridge; no remote debugging connection."""

from __future__ import annotations

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
    window_id = target.window_id if target is not None else 0
    tab_id = target.tab_id if target is not None else 0
    payload = str(args.get("script", args.get("url", "")))
    try:
        result = subprocess.run(
            [
                "osascript", str(Path(__file__).with_name("chrome_bridge.applescript")),
                action, str(pid), str(window_id), str(tab_id), payload,
            ],
            check=False, capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemChromeError("Chrome did not respond within 15 seconds.", "TIMEOUT") from exc
    raw = (result.stdout or "").rstrip("\r\n")
    if result.returncode:
        message = (result.stderr or raw).strip() or "Chrome bridge failed."
        code = "BROWSER_PERMISSION" if re.search(r"-1743|-10004|not authorized|not permitted", message, re.I) else "NAVIGATION"
        raise SystemChromeError(message, code)
    if raw.startswith("ERROR\t"):
        _, code, message = raw.split("\t", 2)
        raise SystemChromeError(message, code)
    if raw == "OK":
        body = ""
    elif raw.startswith("OK\n"):
        body = raw[3:]
    else:
        raise SystemChromeError("Chrome returned an invalid bridge response.")

    def tab_record(line: str) -> dict[str, Any]:
        record_tab_id, _, url = line.partition("\t")
        return {"id": int(record_tab_id), "url": url}

    if action == "inventory":
        windows: dict[int, list[dict[str, Any]]] = {}
        for line in body.splitlines():
            if not line:
                continue
            record_window_id, record_tab_id, url = line.split("\t", 2)
            windows.setdefault(int(record_window_id), []).append(
                {"id": int(record_tab_id), "url": url}
            )
        return [{"id": key, "tabs": value} for key, value in windows.items()]
    if action == "new_window":
        record_window_id, record_tab_id, url = body.split("\t", 2)
        return {"id": int(record_window_id), "tab": {"id": int(record_tab_id), "url": url}}
    if action == "tabs":
        return [tab_record(line) for line in body.splitlines() if line]
    if action in {"tab", "new_tab", "navigate"}:
        return tab_record(body)
    if action == "bounds":
        return [int(value) for value in body.split(",")]
    if action in {"close_window", "close_tab", "activate"}:
        return body.lower() == "true"
    if action == "evaluate":
        return body
    return body


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


def launch_normal_chrome(url: str) -> None:
    """Launch a visible normal Chrome window at ``url``.

    The native bridge cannot create the first window when macOS has not yet
    granted Apple Events access.  Opening the target visibly also gives the
    user somewhere to grant/complete the carrier check while the bridge binds
    the exact process, window and tab afterwards.
    """
    executable = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not executable.is_file():
        executable = Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not executable.is_file():
        raise SystemChromeError("Google Chrome is not installed.")
    result = subprocess.run(
        [
            "open", "-na", str(executable.parents[2]), "--args",
            "--disable-popup-blocking", "--new-window", url,
        ],
        check=False, capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise SystemChromeError(result.stderr.strip() or "Could not start Google Chrome.")


def show_normal_chrome_url(url: str) -> None:
    """Show ``url`` in the existing normal Chrome without Apple Events."""
    executable = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not executable.is_file():
        executable = Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not executable.is_file():
        raise SystemChromeError("Google Chrome is not installed.")
    result = subprocess.run(
        ["open", "-a", str(executable.parents[2]), url],
        check=False, capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise SystemChromeError(result.stderr.strip() or "Could not show Google Chrome.")
