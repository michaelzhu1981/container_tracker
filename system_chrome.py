"""Drive the user's normal Google Chrome via Apple Events, without CDP."""

from __future__ import annotations

import asyncio
import base64
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
                const label = (
                    (el.innerText || el.textContent || "")
                    + " "
                    + (el.getAttribute("aria-label") || "")
                ).toLowerCase();
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
        ["open", "-na", "Google Chrome", "--args", "--disable-popup-blocking", "--new-window", url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_chrome_tab(url: str) -> None:
    target = json.dumps(url)
    source = f"""
    tell application "Google Chrome"
        activate
        if (count of windows) is 0 then
            make new window
        end if
        set dest to missing value
        repeat with w in windows
            repeat with t in tabs of w
                try
                    if URL of t contains "cargotracking.aspx" then
                        set dest to w
                        exit repeat
                    end if
                end try
            end repeat
            if dest is not missing value then exit repeat
        end repeat
        if dest is missing value then set dest to front window
        tell dest
            make new tab with properties {{URL:{target}}}
        end tell
    end tell
    """
    run_osascript(source)


def _tab_match_clause(*, host: str, tab_url: str | None = None) -> str:
    if tab_url:
        target = json.dumps(tab_url)
        return f"tabURL is {target} or tabURL contains {target}"
    return f"tabURL contains {json.dumps(host)}"


def chrome_js(script: str, *, host: str, tab_url: str | None = None) -> Any:
    wrapped = (
        "JSON.stringify((function(){ try { const r = "
        + _as_iife(script)
        + "; return r === undefined ? true : r; } catch (e) { return null; } })())"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(wrapped)
        js_path = handle.name
    match = _tab_match_clause(host=host, tab_url=tab_url)
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
                if {match} then
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


def list_chrome_tab_urls(*, host: str | None = None) -> list[str]:
    source = """
    tell application "Google Chrome"
        set out to ""
        repeat with w in windows
            repeat with t in tabs of w
                try
                    set tabURL to URL of t
                    if out is "" then
                        set out to tabURL
                    else
                        set out to out & linefeed & tabURL
                    end if
                end try
            end repeat
        end repeat
        return out
    end tell
    """
    try:
        raw = run_osascript(source)
    except SystemChromeError:
        return []
    urls = [line.strip() for line in raw.splitlines() if line.strip()]
    if not host:
        return urls
    needle = host.lower()
    return [url for url in urls if needle in url.lower()]


def activate_chrome_tab(url: str) -> bool:
    target = json.dumps(url)
    source = f"""
    tell application "Google Chrome"
        repeat with w in windows
            set tabIndex to 0
            repeat with t in tabs of w
                set tabIndex to tabIndex + 1
                try
                    set tabURL to URL of t
                    if tabURL is {target} or tabURL contains {target} then
                        set index of w to 1
                        set active tab index of w to tabIndex
                        activate
                        return true
                    end if
                end try
            end repeat
        end repeat
    end tell
    return false
    """
    try:
        return run_osascript(source).lower() == "true"
    except SystemChromeError:
        return False


def close_chrome_tab(url: str) -> bool:
    target = json.dumps(url)
    source = f"""
    tell application "Google Chrome"
        repeat with w in windows
            set tabList to tabs of w
            repeat with i from (count of tabList) to 1 by -1
                try
                    set tabURL to URL of tab i of w
                    if tabURL is {target} or tabURL contains {target} then
                        close tab i of w
                        return true
                    end if
                end try
            end repeat
        end repeat
    end tell
    return false
    """
    try:
        return run_osascript(source).lower() == "true"
    except SystemChromeError:
        return False


def close_chrome_tabs(*, host: str, keep_contains: str | None = None) -> int:
    host_lit = json.dumps(host)
    keep_lit = json.dumps(keep_contains or "")
    skip_keep = "false" if keep_contains else "true"
    source = f"""
    tell application "Google Chrome"
        set closedCount to 0
        repeat with w in windows
            set tabList to tabs of w
            repeat with i from (count of tabList) to 1 by -1
                try
                    set tabURL to URL of tab i of w
                    if tabURL contains {host_lit} then
                        if {skip_keep} or tabURL does not contain {keep_lit} then
                            close tab i of w
                            set closedCount to closedCount + 1
                        end if
                    end if
                end try
            end repeat
        end repeat
        return closedCount
    end tell
    """
    try:
        raw = run_osascript(source)
    except SystemChromeError:
        return 0
    try:
        return int(raw or 0)
    except ValueError:
        return 0


_DEFAULT_SHOT_ROOT_JS = """(document.querySelector("#trackingsearchsection")
    || document.querySelector("#gridTrackingDetails")
    || document.querySelector(".tracking-details")
    || document.querySelector(".hal-event-tracking")
    || document.querySelector("[class*='unitActivity']")
    || document.body)"""

_ELEMENT_SCREEN_RECT_JS = r"""() => {
    const root = __ROOT__;
    if (!root) return null;
    try { root.scrollIntoView({ block: "nearest", inline: "nearest" }); } catch (err) {}
    const r = root.getBoundingClientRect();
    if (r.width < 20 || r.height < 20) return null;
    const chromeX = Math.max(0, window.outerWidth - window.innerWidth);
    const chromeY = Math.max(0, window.outerHeight - window.innerHeight);
    return {
        x: Math.round(window.screenX + chromeX / 2 + r.left),
        y: Math.round(window.screenY + chromeY + r.top),
        w: Math.round(r.width),
        h: Math.round(r.height)
    };
}"""

# Fallback when macOS window capture is unavailable. This paints boxes and
# text, so custom webfonts can look wrong.
_DOM_SCREENSHOT_JS = r"""() => {
    const root = __ROOT__;
    if (!root) return null;
    const rootRect = root.getBoundingClientRect();
    const width = Math.max(root.scrollWidth || 0, rootRect.width, 1);
    const height = Math.max(root.scrollHeight || 0, rootRect.height, 1);
    const scale = Math.min(2, window.devicePixelRatio || 1);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.ceil(width * scale));
    canvas.height = Math.max(1, Math.ceil(height * scale));
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.scale(scale, scale);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, width, height);
    if (ctx.letterSpacing !== undefined) ctx.letterSpacing = "0px";
    const skip = new Set(["SCRIPT", "STYLE", "LINK", "NOSCRIPT", "META", "HEAD", "SVG", "PATH", "CANVAS"]);
    const transparent = (bg) => !bg || bg === "transparent" || /,\s*0\)$/.test(bg);
    const els = [root, ...root.querySelectorAll("*")];
    for (const el of els) {
        if (skip.has(el.tagName)) continue;
        const cs = getComputedStyle(el);
        if (cs.display === "none" || cs.visibility === "hidden" || Number(cs.opacity) === 0) continue;
        const r = el.getBoundingClientRect();
        const x = r.left - rootRect.left + (root.scrollLeft || 0);
        const y = r.top - rootRect.top + (root.scrollTop || 0);
        if (r.width < 1 || r.height < 1) continue;
        if (x > width || y > height || x + r.width < 0 || y + r.height < 0) continue;
        const bg = cs.backgroundColor;
        if (!transparent(bg)) {
            const radius = Math.min(
                parseFloat(cs.borderTopLeftRadius) || 0,
                r.height / 2,
                r.width / 2
            );
            ctx.fillStyle = bg;
            if (radius > 0 && ctx.roundRect) {
                ctx.beginPath();
                ctx.roundRect(x, y, r.width, r.height, radius);
                ctx.fill();
            } else {
                ctx.fillRect(x, y, r.width, r.height);
            }
        }
        const sides = [
            ["Top", x, y, x + r.width, y],
            ["Right", x + r.width, y, x + r.width, y + r.height],
            ["Bottom", x, y + r.height, x + r.width, y + r.height],
            ["Left", x, y, x, y + r.height],
        ];
        for (const [side, x1, y1, x2, y2] of sides) {
            const bw = parseFloat(cs["border" + side + "Width"]) || 0;
            if (bw <= 0 || cs["border" + side + "Style"] === "none") continue;
            ctx.strokeStyle = cs["border" + side + "Color"];
            ctx.lineWidth = bw;
            ctx.beginPath();
            ctx.moveTo(x1, y1);
            ctx.lineTo(x2, y2);
            ctx.stroke();
        }
        let text = "";
        for (const node of el.childNodes) {
            if (node.nodeType === 3) text += node.nodeValue || "";
        }
        text = text.replace(/\s+/g, " ").trim();
        if (!text) continue;
        const family = String(cs.fontFamily || "");
        if (/musticon|fontawesome|glyph|icomoon|icon/i.test(family) && text.length <= 2) {
            continue;
        }
        const size = parseFloat(cs.fontSize) || 14;
        if (size < 6) continue;
        ctx.font = (cs.fontWeight || "400") + " " + size + "px Arial, Helvetica, sans-serif";
        ctx.fillStyle = cs.color || "#111111";
        ctx.textBaseline = "middle";
        ctx.textAlign = (cs.textAlign === "center" || cs.justifyContent === "center")
            ? "center"
            : "left";
        ctx.save();
        ctx.beginPath();
        ctx.rect(x, y, r.width, r.height);
        ctx.clip();
        const padL = parseFloat(cs.paddingLeft) || 0;
        const padR = parseFloat(cs.paddingRight) || 0;
        const maxW = Math.max(8, r.width - padL - padR);
        const lineH = parseFloat(cs.lineHeight) || size * 1.3;
        const words = text.split(" ");
        const lines = [];
        let line = "";
        for (const word of words) {
            const test = line ? line + " " + word : word;
            if (ctx.measureText(test).width > maxW && line) {
                lines.push(line);
                line = word;
            } else {
                line = test;
            }
        }
        if (line) lines.push(line);
        const tx = ctx.textAlign === "center" ? x + r.width / 2 : x + padL;
        let ty = y + r.height / 2 - (lines.length * lineH) / 2 + lineH / 2;
        for (const part of lines) {
            ctx.fillText(part, tx, ty, maxW);
            ty += lineH;
        }
        ctx.restore();
    }
    return canvas.toDataURL("image/png");
}"""


def _screenshot_root_js(selector: str | None = None) -> str:
    if not selector:
        return _DEFAULT_SHOT_ROOT_JS
    found = _find_element_js(selector)
    return f"""(() => {{
        const el = {found};
        if (!el) return null;
        return el.closest("#trackingsearchsection, .tracking-details, .hal-event-tracking") || el;
    }})()"""


def _parse_rect(data: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(data, dict):
        return None
    try:
        x, y = int(data["x"]), int(data["y"])
        width, height = int(data["w"]), int(data["h"])
    except (KeyError, TypeError, ValueError):
        return None
    if width < 20 or height < 20:
        return None
    return x, y, width, height


def _bounds_to_rect(raw: str) -> tuple[int, int, int, int] | None:
    parts = [part.strip() for part in raw.replace("{", "").replace("}", "").split(",") if part.strip()]
    if len(parts) != 4:
        return None
    try:
        left, top, right, bottom = (int(float(part)) for part in parts)
    except ValueError:
        return None
    width, height = right - left, bottom - top
    if width < 20 or height < 20:
        return None
    return left, top, width, height


def _png_looks_valid(path: Path) -> bool:
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    return len(raw) > 1000 and raw[:8] == b"\x89PNG\r\n\x1a\n"


def activate_chrome_host(host: str) -> bool:
    host_lit = json.dumps(host)
    source = f"""
    tell application "Google Chrome"
        activate
        repeat with w in windows
            repeat with t in tabs of w
                try
                    if URL of t contains {host_lit} then
                        set active tab index of w to (index of t)
                        set index of w to 1
                        return true
                    end if
                end try
            end repeat
        end repeat
    end tell
    return false
    """
    try:
        return run_osascript(source).lower() == "true"
    except SystemChromeError:
        return False


def chrome_window_rect(*, host: str) -> tuple[int, int, int, int] | None:
    host_lit = json.dumps(host)
    source = f"""
    tell application "Google Chrome"
        repeat with w in windows
            repeat with t in tabs of w
                try
                    if URL of t contains {host_lit} then
                        set b to bounds of w
                        return (item 1 of b as string) & "," & (item 2 of b as string) & "," & (item 3 of b as string) & "," & (item 4 of b as string)
                    end if
                end try
            end repeat
        end repeat
    end tell
    """
    try:
        return _bounds_to_rect(run_osascript(source))
    except SystemChromeError:
        return None


def _rect_inside(
    inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]
) -> bool:
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return ix >= ox - 8 and iy >= oy - 8 and ix + iw <= ox + ow + 8 and iy + ih <= oy + oh + 8


def _screencapture_rect(rect: tuple[int, int, int, int], dest: Path) -> bool:
    x, y, width, height = rect
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    result = subprocess.run(
        ["screencapture", "-x", "-R", f"{x},{y},{width},{height}", str(dest)],
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )
    return result.returncode == 0 and _png_looks_valid(dest)


def _try_window_screenshot(
    path: str | Path, *, host: str, selector: str | None = None
) -> bool:
    dest = Path(path)
    activate_chrome_host(host)
    time.sleep(0.35)
    window = chrome_window_rect(host=host)
    if window is None:
        LOGGER.info("No on-screen Chrome window found for %s", host)
        return False
    rect = window
    try:
        script = _ELEMENT_SCREEN_RECT_JS.replace("__ROOT__", _screenshot_root_js(selector), 1)
        element = _parse_rect(chrome_js(script, host=host))
    except SystemChromeError:
        element = None
    if element is not None and _rect_inside(element, window):
        rect = element
    try:
        return _screencapture_rect(rect, dest)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        dest.unlink(missing_ok=True)
        return False


def write_png_data_url(data: Any, path: str | Path) -> Path:
    dest = Path(path)
    if not isinstance(data, str) or "base64," not in data:
        raise SystemChromeError("Chrome did not return a screenshot.")
    raw = base64.b64decode(data.split("base64,", 1)[1], validate=False)
    if len(raw) < 32:
        raise SystemChromeError("Chrome screenshot was empty.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return dest


def capture_chrome_png(path: str | Path, *, host: str, selector: str | None = None) -> Path:
    dest = Path(path)
    if _try_window_screenshot(dest, host=host, selector=selector):
        return dest
    raise SystemChromeError(
        f"Could not capture the Chrome window for {host}. "
        "Keep that window visible and try again."
    )


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
                if (!el) return;
                el.scrollIntoView({{ block: "center", inline: "nearest" }});
                el.focus();
                for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) {{
                    el.dispatchEvent(new MouseEvent(type, {{ bubbles: true, cancelable: true, view: window }}));
                }}
                if (typeof el.click === "function") el.click();
            }}"""
        )

    async def inner_text(self) -> str:
        text = await self.page.evaluate(
            f"""() => {{
                const el = {_find_element_js(self.selector)};
                return el ? (el.innerText || el.textContent || "") : "";
            }}"""
        )
        return str(text or "")

    async def select_option(self, label: str | None = None, value: str | None = None, **kwargs) -> None:
        wanted = value if value is not None else label
        await self.page.evaluate(
            f"""() => {{
                const el = {_find_element_js(self.selector)};
                if (!el || !el.options) return;
                const wanted = {json.dumps(str(wanted or ""))}.toLowerCase();
                for (const opt of el.options) {{
                    const text = (opt.text || opt.label || "").toLowerCase();
                    if ((opt.value || "").toLowerCase() === wanted || text.includes(wanted)) {{
                        el.value = opt.value;
                        el.dispatchEvent(new Event("change", {{ bubbles: true }}));
                        return;
                    }}
                }}
            }}"""
        )

    async def scroll_into_view_if_needed(self) -> None:
        await self.page.evaluate(
            f"""() => {{
                const el = {_find_element_js(self.selector)};
                if (el) el.scrollIntoView({{ block: "center", inline: "nearest" }});
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
        await self.page.screenshot(path=path, selector=self.selector)


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
        carrier: str = "CMDU",
        challenge_name: str = "DataDome",
    ) -> None:
        self._target_url = url
        self._host = host
        self._carrier = carrier
        self._challenge_name = challenge_name
        self.should_abort = should_abort
        self.appear_s = appear_s
        self.keyboard = _Keyboard(self)
        self._closed = False
        self.tab_url: str | None = None

    async def start(self) -> None:
        print()
        print(f"Opening a normal Google Chrome window for {self._carrier}.")
        print(f"Complete {self._challenge_name} in that window and keep it open.")
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
        raise SystemChromeError(
            f"Google Chrome did not open the {self._carrier} tracking page."
        )

    async def _wait_until_js_enabled(self, timeout_s: float = 600) -> None:
        print(
            "In that Chrome window: View → Developer → Allow JavaScript from Apple Events. "
            f"Then complete {self._challenge_name} and keep the window open."
        )
        deadline = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < deadline:
            if self.should_abort and self.should_abort():
                raise SystemChromeError("Stopped while waiting for Chrome JavaScript.")
            try:
                value = await asyncio.to_thread(chrome_js, "() => 1", host=self._host)
                if value in {1, "1", True}:
                    print(
                        "Chrome Apple Event JavaScript is on. "
                        f"Waiting for {self._challenge_name} if needed."
                    )
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
        if self.tab_url:
            return self.tab_url
        return chrome_tab_url(host=self._host)

    def is_closed(self) -> bool:
        return self._closed or not bool(chrome_tab_url(host=self._host))

    def locator(self, selector: str) -> SystemLocator:
        return SystemLocator(self, selector)

    def get_by_role(self, role: str) -> SystemLocator:
        return SystemLocator(self, "textarea, input, [contenteditable='true']")

    def on(self, event: str, handler: Callable) -> None:
        return None

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if arg is not None:
            script = f"((fn) => fn({json.dumps(arg)}))({script.strip()})"
        return await asyncio.to_thread(
            chrome_js, script, host=self._host, tab_url=self.tab_url
        )

    async def list_tab_urls(self) -> list[str]:
        return await asyncio.to_thread(list_chrome_tab_urls)

    async def open_tab(self, url: str) -> None:
        await asyncio.to_thread(open_chrome_tab, url)
        self.tab_url = url
        await asyncio.to_thread(activate_chrome_tab, url)

    async def focus_tab(self, url: str) -> None:
        self.tab_url = url
        await asyncio.to_thread(activate_chrome_tab, url)

    async def close_tab(self, url: str) -> None:
        await asyncio.to_thread(close_chrome_tab, url)
        if self.tab_url == url:
            self.tab_url = None

    async def close_other_host_tabs(self, keep_contains: str) -> None:
        await asyncio.to_thread(
            close_chrome_tabs, host=self._host, keep_contains=keep_contains
        )
        if self.tab_url and keep_contains.lower() not in self.tab_url.lower():
            self.tab_url = None

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

    async def screenshot(
        self,
        path: str | None = None,
        full_page: bool = False,
        clip=None,
        selector: str | None = None,
    ) -> None:
        if not path:
            return
        if self.tab_url:
            await asyncio.to_thread(activate_chrome_tab, self.tab_url)
        await asyncio.to_thread(
            capture_chrome_png, path, host=self._host, selector=selector
        )

    async def close(self) -> None:
        self._closed = True
