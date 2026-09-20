"""Drive the user's normal Google Chrome via Apple Events, without CDP."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import struct
import subprocess
import tempfile
import time
import zlib
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

from chrome_control import (
    ChromeTarget, SystemChromeError, chrome_command, normal_chrome_pid,
    launch_normal_chrome, show_normal_chrome_url,
)

LOGGER = logging.getLogger("container_tracker")

_HAS_TEXT_RE = re.compile(
    r"""^(?P<tag>[\w.-]+)(?P<rest>[^:]*)?:has-text\((?P<q>['"])(?P<text>.*)(?P=q)\)$""",
    re.I,
)


_CAPTURE_TARGET: ContextVar[ChromeTarget | None] = ContextVar("chrome_capture_target", default=None)


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
    target = _CAPTURE_TARGET.get()
    if target is not None:
        raw = chrome_command(target.pid, "evaluate", target=target, script=wrapped)
        return _parse_js_result(raw)
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
            "Could not find the requested Chrome tab or read its JavaScript result.",
            "TAB_NOT_FOUND",
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


def close_chrome_windows(*, host: str) -> int:
    """Close every Chrome window that has a tab for this host. Do not quit Chrome."""
    host_lit = json.dumps(host)
    source = f"""
    tell application "Google Chrome"
        set idsToClose to {{}}
        repeat with w in windows
            repeat with t in tabs of w
                try
                    if URL of t contains {host_lit} then
                        set end of idsToClose to id of w
                        exit repeat
                    end if
                end try
            end repeat
        end repeat
        set closedCount to 0
        repeat with wid in idsToClose
            try
                close (first window whose id is wid)
                set closedCount to closedCount + 1
            end try
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
    || document.querySelector(".tracing-result-wrapper")
    || document.querySelector(".activity-card-holder")
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

_SCROLL_ROOT_START_JS = r"""() => {
    const root = __ROOT__;
    if (!root) return false;
    try { root.scrollIntoView({ block: "start", inline: "nearest" }); } catch (err) {}
    return true;
}"""

_VISIBLE_SLICE_JS = r"""() => {
    const root = __ROOT__;
    if (!root) return null;
    const r = root.getBoundingClientRect();
    const top = Math.max(0, Math.min(window.innerHeight, r.top));
    const bottom = Math.max(0, Math.min(window.innerHeight, r.bottom));
    const left = Math.max(0, Math.min(window.innerWidth, r.left));
    const right = Math.max(0, Math.min(window.innerWidth, r.right));
    const h = Math.round(bottom - top);
    const w = Math.round(right - left);
    if (w < 20 || h < 8) return null;
    const chromeX = Math.max(0, window.outerWidth - window.innerWidth);
    const chromeY = Math.max(0, window.outerHeight - window.innerHeight);
    return {
        x: Math.round(window.screenX + chromeX / 2 + left),
        y: Math.round(window.screenY + chromeY + top),
        w: w,
        h: h,
        remaining: Math.round(Math.max(0, r.bottom - window.innerHeight))
    };
}"""

_SCROLL_SLICE_JS = r"""() => {
    const root = __ROOT__;
    const amount = __DY__;
    if (!amount) return 0;
    const canScroll = (el) => el && ((el.scrollHeight || 0) - (el.clientHeight || 0) > 4);
    let node = root;
    while (node && node !== document.documentElement && node !== document.body) {
        if (canScroll(node)) {
            const before = node.scrollTop;
            node.scrollTop = before + amount;
            return Math.round(node.scrollTop - before);
        }
        node = node.parentElement;
    }
    const before = window.scrollY;
    window.scrollBy(0, amount);
    return Math.round(window.scrollY - before);
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
        return el.closest("#trackingsearchsection, .tracking-details, .hal-event-tracking, .tracing-result-wrapper, .activity-card-holder") || el;
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
    target = _CAPTURE_TARGET.get()
    if target is not None:
        return bool(chrome_command(target.pid, "activate", target=target))
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
    target = _CAPTURE_TARGET.get()
    if target is not None:
        bounds = chrome_command(target.pid, "bounds", target=target)
        if isinstance(bounds, dict):
            return (int(bounds["x"]), int(bounds["y"]), int(bounds["width"]), int(bounds["height"]))
        return None
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


def _clip_rect(
    inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    x1, y1 = max(ix, ox), max(iy, oy)
    x2, y2 = min(ix + iw, ox + ow), min(iy + ih, oy + oh)
    if x2 - x1 < 20 or y2 - y1 < 8:
        return None
    return x1, y1, x2 - x1, y2 - y1


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png_rgba(path: Path) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGB/RGBA PNG into tightly packed RGBA pixels."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    width = height = 0
    bit_depth = color_type = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break
    if bit_depth != 8 or color_type not in {2, 6} or width < 1 or height < 1:
        raise ValueError("unsupported PNG")
    raw = zlib.decompress(bytes(idat))
    bpp = 4 if color_type == 6 else 3
    stride = width * bpp
    rows: list[bytes] = []
    prev = bytearray(stride)
    i = 0
    for _ in range(height):
        ftype = raw[i]
        i += 1
        row = bytearray(raw[i : i + stride])
        i += stride
        if ftype == 1:
            for x in range(stride):
                row[x] = (row[x] + (row[x - bpp] if x >= bpp else 0)) & 255
        elif ftype == 2:
            for x in range(stride):
                row[x] = (row[x] + prev[x]) & 255
        elif ftype == 3:
            for x in range(stride):
                left = row[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + ((left + prev[x]) // 2)) & 255
        elif ftype == 4:
            for x in range(stride):
                left = row[x - bpp] if x >= bpp else 0
                up_left = prev[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + _paeth(left, prev[x], up_left)) & 255
        elif ftype != 0:
            raise ValueError(f"unsupported PNG filter {ftype}")
        rows.append(bytes(row))
        prev = row
    if bpp == 4:
        return width, height, b"".join(rows)
    rgba = bytearray(width * height * 4)
    src = b"".join(rows)
    for px in range(width * height):
        rgba[px * 4 : px * 4 + 3] = src[px * 3 : px * 3 + 3]
        rgba[px * 4 + 3] = 255
    return width, height, bytes(rgba)


def write_png_rgba(path: Path, width: int, height: int, pixels: bytes) -> Path:
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(pixels[y * stride : (y + 1) * stride])

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    return path


def stitch_pngs_vertically(paths: list[Path], dest: Path) -> bool:
    decoded: list[tuple[int, int, bytes]] = []
    for path in paths:
        try:
            decoded.append(read_png_rgba(path))
        except (OSError, ValueError, zlib.error):
            return False
    if not decoded:
        return False
    width = min(item[0] for item in decoded)
    height = sum(item[1] for item in decoded)
    if width < 20 or height < 8:
        return False
    out = bytearray(width * height * 4)
    y = 0
    for src_w, src_h, pixels in decoded:
        for row in range(src_h):
            src = row * src_w * 4
            dst = (y + row) * width * 4
            out[dst : dst + width * 4] = pixels[src : src + width * 4]
        y += src_h
    write_png_rgba(dest, width, height, bytes(out))
    return dest.is_file() and dest.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


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


def _chrome_root_js(script: str, *, host: str, selector: str | None) -> Any:
    return chrome_js(
        script.replace("__ROOT__", _screenshot_root_js(selector), 1),
        host=host,
    )


def _try_scrolled_element_screenshot(
    dest: Path, *, host: str, selector: str | None, window: tuple[int, int, int, int]
) -> bool:
    try:
        _chrome_root_js(_SCROLL_ROOT_START_JS, host=host, selector=selector)
    except SystemChromeError:
        return False
    time.sleep(0.2)
    work = dest.parent / f".{dest.stem}_slices"
    work.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    try:
        for idx in range(16):
            try:
                info = _chrome_root_js(_VISIBLE_SLICE_JS, host=host, selector=selector)
            except SystemChromeError:
                break
            rect = _parse_rect(info)
            if rect is None:
                break
            if not _rect_inside(rect, window):
                rect = _clip_rect(rect, window)
            if rect is None:
                break
            part = work / f"{idx:02d}.png"
            if not _screencapture_rect(rect, part):
                break
            parts.append(part)
            remaining = int(info.get("remaining") or 0) if isinstance(info, dict) else 0
            if remaining <= 4:
                break
            try:
                moved = _chrome_root_js(
                    _SCROLL_SLICE_JS.replace("__DY__", str(max(rect[3], 1)), 1),
                    host=host,
                    selector=selector,
                )
            except SystemChromeError:
                break
            if not moved:
                break
            time.sleep(0.2)
        if not parts:
            return False
        if len(parts) == 1:
            dest.write_bytes(parts[0].read_bytes())
            return _png_looks_valid(dest)
        return stitch_pngs_vertically(parts, dest)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        dest.unlink(missing_ok=True)
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
    if _try_scrolled_element_screenshot(dest, host=host, selector=selector, window=window):
        return True
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
    # Screen Recording can be unavailable to a background service even when
    # Chrome automation is allowed. Paint the bound result DOM as a reliable
    # fallback so a successful query never loses its evidence image.
    try:
        data = _chrome_root_js(_DOM_SCREENSHOT_JS, host=host, selector=selector)
        write_png_data_url(data, dest)
        if _png_looks_valid(dest):
            LOGGER.info("Used DOM screenshot fallback for %s", host)
            return dest
    except (OSError, SystemChromeError, ValueError):
        dest.unlink(missing_ok=True)
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
        wait_for_permission: bool = True,
        on_permission_wait: Callable[[dict], None] | None = None,
    ) -> None:
        self._target_url = url
        self._host = host
        self._carrier = carrier
        self._challenge_name = challenge_name
        self.wait_for_permission = wait_for_permission
        self.on_permission_wait = on_permission_wait
        self.should_abort = should_abort
        self.appear_s = appear_s
        self.keyboard = _Keyboard(self)
        self._closed = False
        self._target: ChromeTarget | None = None
        self._entry_target: ChromeTarget | None = None
        self._tab_url: str | None = None
        self._known_urls: dict[str, ChromeTarget] = {}

    def _bound_target(self) -> ChromeTarget:
        if self._target is None:
            raise SystemChromeError("No Chrome tracking tab is bound.", "TAB_NOT_FOUND")
        return self._target

    def _command(self, action: str, **args) -> Any:
        target = self._bound_target()
        return chrome_command(target.pid, action, target=target, **args)

    def _remember_tab(self, tab: dict) -> ChromeTarget:
        current = self._bound_target()
        target = ChromeTarget(current.pid, current.window_id, int(tab["id"]))
        if tab.get("url"):
            self._known_urls[tab["url"]] = target
        return target

    def _launched_target(self, pid: int, inventory: Any) -> ChromeTarget | None:
        """Find the visible target tab created before Apple Events binding."""
        if not isinstance(inventory, list):
            return None
        host = self._host.lower()
        for window in inventory:
            if not isinstance(window, dict):
                continue
            for tab in window.get("tabs") or []:
                url = str(tab.get("url") or "")
                if host and host in url.lower():
                    return ChromeTarget(pid, int(window["id"]), int(tab["id"]))
        return None

    @property
    def tab_url(self) -> str | None:
        return self._tab_url

    @tab_url.setter
    def tab_url(self, value: str | None) -> None:
        # OOCL resets this handle when returning to its pinned entry tab.
        if value is None and self._entry_target is not None:
            self._target = self._entry_target
        elif value in self._known_urls:
            self._target = self._known_urls[value]
        self._tab_url = value

    async def start(self) -> None:
        print()
        print(f"Opening a normal Google Chrome window for {self._carrier}.")
        print(f"Complete {self._challenge_name} in that window and keep it open.")
        print("If Chrome asks, enable View → Developer → Allow JavaScript from Apple Events.")
        print()
        # Keep the user's existing verification cookies, but exclude every
        # Playwright/custom-profile instance when resolving the normal process.
        pid = await asyncio.to_thread(normal_chrome_pid)
        launched_here = pid is None
        if pid is None:
            await asyncio.to_thread(launch_normal_chrome, self._target_url)
        deadline = time.monotonic() + self.appear_s
        last_error = None
        permission_waiting = False
        while time.monotonic() < deadline:
            if self.should_abort and self.should_abort():
                raise SystemChromeError("Stopped before Chrome opened the tracking page.", "CANCELLED")
            pid = await asyncio.to_thread(normal_chrome_pid)
            if pid is not None:
                try:
                    inventory = await asyncio.to_thread(chrome_command, pid, "inventory")
                except SystemChromeError as exc:
                    if exc.code == "BROWSER_PERMISSION":
                        if not self.wait_for_permission:
                            raise
                        if not permission_waiting:
                            # LaunchServices can show the target without Apple
                            # Events.  Keep the carrier alive while the user
                            # grants Automation access, then bind this tab.
                            if not launched_here:
                                await asyncio.to_thread(show_normal_chrome_url, self._target_url)
                            launched_here = True
                            permission_waiting = True
                            deadline = max(deadline, time.monotonic() + 600)
                            LOGGER.warning(
                                "%s Chrome automation permission required: %s",
                                self._carrier, exc,
                            )
                            if self.on_permission_wait:
                                self.on_permission_wait({
                                    "code": exc.code,
                                    "mode": "browser_permission",
                                    "timeout_seconds": 600,
                                })
                        last_error = exc
                        await asyncio.sleep(2)
                        continue
                    last_error = exc
                else:
                    if launched_here:
                        target = self._launched_target(pid, inventory)
                        if target is None:
                            await asyncio.sleep(0.5)
                            continue
                        self._target = target
                        self._entry_target = target
                        tab = await asyncio.to_thread(
                            chrome_command, pid, "tab", target=target
                        )
                        self._remember_tab(tab)
                        self._closed = False
                        LOGGER.info(
                            "Bound %s Chrome pid=%s window=%s tab=%s",
                            self._carrier, pid, target.window_id, target.tab_id,
                        )
                        if permission_waiting and self.on_permission_wait:
                            self.on_permission_wait({"code": None})
                        await self._wait_until_js_enabled()
                        return
                    # Never retry a window-creation mutation: it may have
                    # succeeded even if its response was lost.
                    window = await asyncio.to_thread(chrome_command, pid, "new_window", url=self._target_url)
                    tab = window["tab"]
                    self._target = ChromeTarget(pid, window["id"], tab["id"])
                    self._entry_target = self._target
                    self._remember_tab(tab)
                    self._closed = False
                    LOGGER.info("Bound %s Chrome pid=%s window=%s tab=%s", self._carrier, pid, window["id"], tab["id"])
                    await self._wait_until_js_enabled()
                    return
            await asyncio.sleep(0.5)
        raise SystemChromeError(
            f"Google Chrome did not open the {self._carrier} tracking page."
            + (f" {last_error}" if last_error else ""), "BROWSER_CLOSED",
        )

    async def _wait_until_js_enabled(self, timeout_s: float = 600) -> None:
        print(
            "In that Chrome window: View → Developer → Allow JavaScript from Apple Events. "
            f"Then complete {self._challenge_name} and keep the window open."
        )
        deadline = time.monotonic() + timeout_s
        last: SystemChromeError | None = None
        while time.monotonic() < deadline:
            if self.should_abort and self.should_abort():
                raise SystemChromeError("Stopped while waiting for Chrome JavaScript.", "CANCELLED")
            try:
                value = await self.evaluate("() => 1")
                if value in {1, "1", True}:
                    if last is not None and self.on_permission_wait:
                        self.on_permission_wait({"code": None})
                    print(
                        "Chrome Apple Event JavaScript is on. "
                        f"Waiting for {self._challenge_name} if needed."
                    )
                    return
            except SystemChromeError as exc:
                if exc.code != "BROWSER_PERMISSION":
                    raise
                if not self.wait_for_permission:
                    raise
                if last is None:
                    LOGGER.warning("%s Chrome permission required: %s", self._carrier, exc)
                    if self.on_permission_wait:
                        self.on_permission_wait({
                            "code": exc.code, "mode": "browser_permission",
                            "timeout_seconds": int(timeout_s),
                        })
                last = exc
            await asyncio.sleep(2)
        raise SystemChromeError(
            str(last) if last else "Chrome did not return a valid JavaScript probe.",
            last.code if last else "NAVIGATION",
        )

    @property
    def url(self) -> str:
        tab = self._command("tab")
        self._remember_tab(tab)
        return tab["url"]

    def is_closed(self) -> bool:
        if self._closed or self._target is None:
            return True
        try:
            self._command("tab")
            return False
        except SystemChromeError as exc:
            if exc.code in {"BROWSER_CLOSED", "TAB_NOT_FOUND"}:
                return True
            raise

    def locator(self, selector: str) -> SystemLocator:
        return SystemLocator(self, selector)

    def get_by_role(self, role: str) -> SystemLocator:
        return SystemLocator(self, "textarea, input, [contenteditable='true']")

    def on(self, event: str, handler: Callable) -> None:
        return None

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if arg is not None:
            script = f"((fn) => fn({json.dumps(arg)}))({script.strip()})"
        wrapped = "JSON.stringify((function(){ const r = " + _as_iife(script) + "; return r === undefined ? true : r; })())"
        raw = await asyncio.to_thread(self._command, "evaluate", script=wrapped)
        if raw is None or raw == "":
            raise SystemChromeError("Chrome returned no JavaScript result.", "NAVIGATION")
        return _parse_js_result(raw)

    async def list_tab_urls(self) -> list[str]:
        tabs = await asyncio.to_thread(self._command, "tabs")
        for tab in tabs:
            self._remember_tab(tab)
        return [tab["url"] for tab in tabs]

    async def open_tab(self, url: str) -> None:
        tab = await asyncio.to_thread(self._command, "new_tab", url=url)
        self._target = self._remember_tab(tab)
        # Preserve the requested URL as an alias even if it already redirected.
        self._known_urls[url] = self._target
        self._tab_url = tab["url"]
        await asyncio.to_thread(self._command, "activate")

    async def focus_tab(self, url: str) -> None:
        if url not in self._known_urls:
            await self.list_tab_urls()
        target = self._known_urls.get(url)
        if target is None:
            raise SystemChromeError("Could not locate the requested tracking tab.", "TAB_NOT_FOUND")
        self._target = target
        self._tab_url = url
        await asyncio.to_thread(self._command, "activate")

    async def close_tab(self, url: str) -> None:
        target = self._known_urls.get(url)
        if target is None:
            await self.list_tab_urls()
            target = self._known_urls.get(url)
        if target is None or target == self._entry_target:
            return
        try:
            await asyncio.to_thread(chrome_command, target.pid, "close_tab", target=target)
        except SystemChromeError as exc:
            if exc.code not in {"TAB_NOT_FOUND", "BROWSER_CLOSED"}:
                raise
        self._known_urls = {key: value for key, value in self._known_urls.items() if value != target}
        if self._target == target:
            self.tab_url = None

    async def close_other_host_tabs(self, keep_contains: str) -> None:
        # Only this session's window; never sweep other carriers or user windows.
        for url in await self.list_tab_urls():
            if keep_contains.lower() not in url.lower():
                await self.close_tab(url)

    async def content(self) -> str:
        html = await self.evaluate("() => document.documentElement.outerHTML")
        return html or ""

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        tab = await asyncio.to_thread(self._command, "navigate", url=url)
        self._remember_tab(tab)
        await asyncio.sleep(1.0)

    async def wait_for_timeout(self, ms: int) -> None:
        await asyncio.sleep(max(ms, 0) / 1000)

    async def wait_for_function(
        self,
        script: str,
        arg: Any = None,
        timeout: float = 0,
        polling: float | None = None,
    ) -> bool:
        deadline = time.monotonic() + max(timeout, 0) / 1000
        interval = 0.5 if polling is None else max(float(polling), 50) / 1000
        if polling is not None and polling <= 10:
            interval = float(polling)
        while time.monotonic() < deadline:
            if self.should_abort and self.should_abort():
                raise SystemChromeError("Stopped while waiting.")
            try:
                if await self.evaluate(script, arg):
                    return True
            except SystemChromeError as exc:
                # A POST navigation temporarily makes Chrome's Apple Event
                # JavaScript result empty. Playwright retries this condition;
                # keep polling here as well instead of letting callers parse
                # the page while the new document is only partly loaded.
                if exc.code not in {"NAVIGATION", "TIMEOUT"}:
                    raise
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
        token = _CAPTURE_TARGET.set(self._bound_target())
        try:
            await asyncio.to_thread(capture_chrome_png, path, host=self._host, selector=selector)
        finally:
            _CAPTURE_TARGET.reset(token)

    async def close(self) -> None:
        self._closed = True
        if self._target is None:
            return
        try:
            closed = await asyncio.to_thread(self._command, "close_window")
        except Exception:  # noqa: BLE001
            LOGGER.info("Could not close the Chrome window for %s.", self._carrier)
            return
        if closed:
            LOGGER.info("Closed the Chrome window for %s.", self._carrier)
        await asyncio.sleep(0.4)
