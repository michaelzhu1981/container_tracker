"""Drive the user's normal Google Chrome via Apple Events, without CDP."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger("container_tracker")

_HAS_TEXT_RE = re.compile(
    r"""^(?P<tag>[\w.-]+)(?P<rest>[^:]*)?:has-text\((?P<q>['"])(?P<text>.*)(?P=q)\)$""",
    re.I,
)


class SystemChromeError(RuntimeError):
    pass


def _as_iife(script: str) -> str:
    text = script.strip()
    if text.startswith("() =>") or text.startswith("()=>"):
        return f"({text})()"
    return text


def _parse_js_result(raw: str) -> Any:
    text = (raw or "").strip()
    if text in {"", "missing value", "undefined"}:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _find_element_js(selector: str) -> str:
    match = _HAS_TEXT_RE.match(selector)
    if match:
        tag = json.dumps(match.group("tag"))
        rest = (match.group("rest") or "").strip()
        text = json.dumps(match.group("text"))
        css = json.dumps(f"{match.group('tag')}{rest}") if rest else tag
        return f"""(() => {{
            const needle = {text}.toLowerCase();
            const nodes = document.querySelectorAll({css});
            for (const el of nodes) {{
                const label = (el.innerText || el.textContent || "").toLowerCase();
                if (label.includes(needle)) return el;
            }}
            return null;
        }})()"""
    return f"document.querySelector({json.dumps(selector)})"


def run_osascript(source: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", source],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise SystemChromeError(err or "osascript failed")
    return (result.stdout or "").strip()


def open_chrome_window(url: str) -> None:
    subprocess.Popen(
        ["open", "-na", "Google Chrome", "--args", "--new-window", url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def chrome_js(script: str, *, host: str) -> Any:
    wrapped = (
        "JSON.stringify((function(){ try { const r = "
        + _as_iife(script)
        + "; return r === undefined ? true : r; } catch (e) { return null; } })())"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(wrapped)
        js_path = handle.name
    host_lit = json.dumps(host)
    posix = json.dumps(js_path)
    source = f"""
    set jsPath to {posix}
    set js to do shell script "cat " & quoted form of jsPath
    tell application "Google Chrome"
        repeat with w in windows
            repeat with t in tabs of w
                set tabURL to ""
                try
                    set tabURL to URL of t
                end try
                if tabURL contains {host_lit} then
                    return execute t javascript js
                end if
            end repeat
        end repeat
    end tell
    """
    try:
        raw = run_osascript(source)
    finally:
        Path(js_path).unlink(missing_ok=True)
    if raw in {"", "missing value"}:
        raise SystemChromeError(
            "Chrome JavaScript from Apple Events is off. "
            "In Google Chrome: View → Developer → Allow JavaScript from Apple Events."
        )
    return _parse_js_result(raw)


def chrome_tab_url(*, host: str) -> str:
    host_js = json.dumps(host)
    source = f"""
    tell application "Google Chrome"
        repeat with w in windows
            repeat with t in tabs of w
                try
                    set tabURL to URL of t
                    if tabURL contains {host_js} then
                        return tabURL
                    end if
                end try
            end repeat
        end repeat
    end tell
    """
    try:
        return run_osascript(source)
    except SystemChromeError:
        return ""


def set_chrome_tab_url(url: str, *, host: str) -> None:
    current = chrome_tab_url(host=host)
    if current:
        host_js = json.dumps(host)
        escaped = url.replace("\\", "\\\\").replace('"', '\\"')
        source = f"""
        tell application "Google Chrome"
            repeat with w in windows
                repeat with t in tabs of w
                    try
                        if URL of t contains {host_js} then
                            set URL of t to "{escaped}"
                            return
                        end if
                    end try
                end repeat
            end repeat
        end tell
        """
        run_osascript(source)
        return
    open_chrome_window(url)


class SystemLocator:
    def __init__(self, page: "SystemChromePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "SystemLocator":
        return self

    async def is_visible(self, timeout: float = 0) -> bool:
        deadline = time.monotonic() + max(timeout, 0) / 1000
        while True:
            found = await self.page.evaluate(
                f"""() => {{
                    const el = {_find_element_js(self.selector)};
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 8 && rect.height > 8;
                }}"""
            )
            if found:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def click(self, timeout: float = 0, force: bool = False) -> None:
        if not await self.is_visible(timeout=timeout):
            raise SystemChromeError(f"Not visible: {self.selector}")
        await self.page.evaluate(
            f"""() => {{
                const el = {_find_element_js(self.selector)};
                if (el) el.click();
            }}"""
        )

    async def fill(self, value: str, force: bool = False) -> None:
        await self.page.evaluate(
            f"""() => {{
                const el = {_find_element_js(self.selector)};
                if (!el) return;
                el.focus();
                const proto = window.HTMLInputElement && HTMLInputElement.prototype;
                const desc = proto && Object.getOwnPropertyDescriptor(proto, "value");
                if (desc && desc.set) desc.set.call(el, {json.dumps(value)});
                else el.value = {json.dumps(value)};
                el.dispatchEvent(new Event("input", {{ bubbles: true }}));
                el.dispatchEvent(new Event("change", {{ bubbles: true }}));
            }}"""
        )

    async def press(self, key: str) -> None:
        await self.page.evaluate(
            f"""() => {{
                const el = {_find_element_js(self.selector)} || document.activeElement;
                if (!el) return;
                el.dispatchEvent(new KeyboardEvent("keydown", {{ key: {json.dumps(key)}, bubbles: true }}));
                if ({json.dumps(key)} === "Enter" && el.form) el.form.requestSubmit();
            }}"""
        )

    async def wait_for(self, state: str = "visible", timeout: float = 0) -> None:
        if state == "hidden":
            deadline = time.monotonic() + max(timeout, 0) / 1000
            while time.monotonic() < deadline:
                if not await self.is_visible(timeout=0):
                    return
                await asyncio.sleep(0.2)
            return
        if not await self.is_visible(timeout=timeout):
            raise SystemChromeError(f"Timed out waiting for {self.selector}")

    async def screenshot(self, path: str | None = None) -> None:
        raise SystemChromeError("screenshot not supported")


class _Keyboard:
    def __init__(self, page: "SystemChromePage") -> None:
        self.page = page

    async def press(self, key: str) -> None:
        await self.page.evaluate(
            f"""() => document.dispatchEvent(new KeyboardEvent("keydown", {{
                key: {json.dumps(key)}, bubbles: true
            }}))"""
        )


class SystemChromePage:
    """Playwright-like page backed by the user's Google Chrome window."""

    is_system_chrome = True

    def __init__(
        self,
        url: str,
        *,
        host: str,
        should_abort: Callable[[], bool] | None = None,
        appear_s: float = 30.0,
    ) -> None:
        self._target_url = url
        self._host = host
        self.should_abort = should_abort
        self.appear_s = appear_s
        self.keyboard = _Keyboard(self)
        self._closed = False

    async def start(self) -> None:
        print()
        print("Opening a normal Google Chrome window for CMDU.")
        print("Complete DataDome in that window and keep it open.")
        print("If Chrome asks, enable View → Developer → Allow JavaScript from Apple Events.")
        print()
        if not chrome_tab_url(host=self._host):
            open_chrome_window(self._target_url)
        deadline = time.monotonic() + self.appear_s
        while time.monotonic() < deadline:
            if self.should_abort and self.should_abort():
                raise SystemChromeError("Stopped before Chrome opened the tracking page.")
            if chrome_tab_url(host=self._host):
                self._closed = False
                await self._wait_until_js_enabled()
                return
            await asyncio.sleep(0.5)
        raise SystemChromeError("Google Chrome did not open the CMA tracking page.")

    async def _wait_until_js_enabled(self, timeout_s: float = 600) -> None:
        print(
            "In that Chrome window: View → Developer → Allow JavaScript from Apple Events. "
            "Then complete DataDome and keep the window open."
        )
        deadline = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < deadline:
            if self.should_abort and self.should_abort():
                raise SystemChromeError("Stopped while waiting for Chrome JavaScript.")
            try:
                value = await asyncio.to_thread(chrome_js, "() => 1", host=self._host)
                if value in {1, "1", True}:
                    print("Chrome Apple Event JavaScript is on. Waiting for DataDome if needed.")
                    return
            except SystemChromeError as exc:
                last = str(exc)
            await asyncio.sleep(2)
        raise SystemChromeError(
            last
            or "Chrome still blocks Apple Event JavaScript. "
            "Enable View → Developer → Allow JavaScript from Apple Events."
        )

    @property
    def url(self) -> str:
        return chrome_tab_url(host=self._host)

    def is_closed(self) -> bool:
        return self._closed or not bool(chrome_tab_url(host=self._host))

    def locator(self, selector: str) -> SystemLocator:
        return SystemLocator(self, selector)

    def get_by_role(self, role: str) -> SystemLocator:
        return SystemLocator(self, "textarea, input, [contenteditable='true']")

    def on(self, event: str, handler: Callable) -> None:
        return None

    async def evaluate(self, script: str) -> Any:
        return await asyncio.to_thread(chrome_js, script, host=self._host)

    async def content(self) -> str:
        html = await self.evaluate("() => document.documentElement.outerHTML")
        return html or ""

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        await asyncio.to_thread(set_chrome_tab_url, url, host=self._host)
        await asyncio.sleep(1.0)

    async def wait_for_timeout(self, ms: int) -> None:
        await asyncio.sleep(max(ms, 0) / 1000)

    async def wait_for_function(
        self, script: str, timeout: float = 0, polling: float | None = None
    ) -> bool:
        deadline = time.monotonic() + max(timeout, 0) / 1000
        interval = 0.5 if polling is None else max(float(polling), 50) / 1000
        if polling is not None and polling <= 10:
            interval = float(polling)
        while time.monotonic() < deadline:
            if self.should_abort and self.should_abort():
                raise SystemChromeError("Stopped while waiting.")
            try:
                if await self.evaluate(script):
                    return True
            except SystemChromeError:
                pass
            await asyncio.sleep(interval)
        raise TimeoutError("still challenged")

    async def wait_for_load_state(self, state: str = "load", timeout: float = 0) -> None:
        await asyncio.sleep(0.2)

    async def screenshot(self, path: str | None = None, full_page: bool = False, clip=None) -> None:
        return None

    async def close(self) -> None:
        self._closed = True
