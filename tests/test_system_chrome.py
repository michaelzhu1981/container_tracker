import pytest

from system_chrome import (
    ChromeTarget,
    SystemChromeError,
    SystemChromePage,
    _as_iife,
    _bounds_to_rect,
    _clip_rect,
    _find_element_js,
    _parse_js_result,
    _parse_rect,
    _rect_inside,
    _screenshot_root_js,
    _tab_match_clause,
    capture_chrome_png,
    chrome_js,
    close_chrome_tabs,
    close_chrome_windows,
    list_chrome_tab_urls,
    open_chrome_tab,
    stitch_pngs_vertically,
    write_png_data_url,
    write_png_rgba,
)

TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_as_iife_invokes_arrow_functions():
    assert _as_iife("() => 1") == "(() => 1)()"
    assert _as_iife("document.title") == "document.title"


def test_parse_js_result_handles_json_and_missing_values():
    assert _parse_js_result('"CAPTCHA"') == "CAPTCHA"
    assert _parse_js_result("null") is None
    assert _parse_js_result("missing value") is None
    assert _parse_js_result("true") is True


def test_find_element_js_supports_has_text():
    js = _find_element_js("button:has-text('Search')")
    assert "Search" in js
    assert "querySelectorAll" in js
    assert _find_element_js("#Reference") == 'document.querySelector("#Reference")'
    aria = _find_element_js("a:has-text('Display Previous Moves')")
    assert "aria-label" in aria


def test_screenshot_root_prefers_tracking_section():
    assert "#trackingsearchsection" in _screenshot_root_js()
    assert ".hal-event-tracking" in _screenshot_root_js()
    assert "unitActivity" in _screenshot_root_js()
    assert "tracing-result-wrapper" in _screenshot_root_js()
    scoped = _screenshot_root_js("#gridTrackingDetails")
    assert "closest" in scoped
    assert "#gridTrackingDetails" in scoped
    hapag = _screenshot_root_js(".hal-event-tracking")
    assert ".hal-event-tracking" in hapag


def test_tab_helpers_read_and_filter_urls(monkeypatch):
    monkeypatch.setattr(
        "system_chrome.run_osascript",
        lambda src: "https://www.oocl.com/a\nhttps://www.cma-cgm.com/b\n",
    )
    assert list_chrome_tab_urls() == [
        "https://www.oocl.com/a",
        "https://www.cma-cgm.com/b",
    ]
    assert list_chrome_tab_urls(host="oocl.com") == ["https://www.oocl.com/a"]
    assert "tabURL is" in _tab_match_clause(
        host="oocl.com", tab_url="https://www.oocl.com/result"
    )
    assert "oocl.com" in _tab_match_clause(host="oocl.com")


def test_chrome_js_can_target_a_specific_tab(monkeypatch):
    seen: list[str] = []

    def fake(source: str) -> str:
        seen.append(source)
        return '"ok"'

    monkeypatch.setattr("system_chrome.run_osascript", fake)
    assert chrome_js("() => 1", host="oocl.com", tab_url="https://www.oocl.com/result") == "ok"
    assert "www.oocl.com/result" in seen[0]


def test_open_chrome_tab_uses_front_window(monkeypatch):
    seen: list[str] = []

    def fake(source: str) -> str:
        seen.append(source)
        return "true"

    monkeypatch.setattr("system_chrome.run_osascript", fake)
    open_chrome_tab("https://www.oocl.com/Pages/ExpressLink.aspx?n=1")
    assert "make new tab" in seen[0]
    assert "cargotracking.aspx" in seen[0]
    assert "www.oocl.com/Pages/ExpressLink.aspx" in seen[0]


def test_close_chrome_tabs_keeps_entry_url(monkeypatch):
    seen: list[str] = []

    def fake(source: str) -> str:
        seen.append(source)
        return "1"

    monkeypatch.setattr("system_chrome.run_osascript", fake)
    assert close_chrome_tabs(host="oocl.com", keep_contains="cargotracking.aspx") == 1
    assert "cargotracking.aspx" in seen[0]


def test_close_chrome_windows_targets_host_windows(monkeypatch):
    seen: list[str] = []

    def fake(source: str) -> str:
        seen.append(source)
        return "2"

    monkeypatch.setattr("system_chrome.run_osascript", fake)
    assert close_chrome_windows(host="cma-cgm.com") == 2
    assert "cma-cgm.com" in seen[0]
    assert "close (first window whose id is wid)" in seen[0]
    assert "quit" not in seen[0].lower()


def test_close_chrome_windows_returns_zero_when_chrome_is_gone(monkeypatch):
    def boom(_source: str) -> str:
        raise SystemChromeError("Google Chrome got an error: Application isn’t running.")

    monkeypatch.setattr("system_chrome.run_osascript", boom)
    assert close_chrome_windows(host="hapag-lloyd.com") == 0


def test_system_chrome_page_close_only_closes_bound_window(monkeypatch):
    import asyncio

    closed = []

    def fake(pid, action, *, target, **args) -> int:
        closed.append((pid, action, target))
        return 1

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("system_chrome.chrome_command", fake)
    monkeypatch.setattr("system_chrome.asyncio.sleep", no_sleep)
    page = SystemChromePage(
        "https://www.cma-cgm.com/ebusiness/tracking",
        host="cma-cgm.com",
        carrier="CMDU",
        challenge_name="DataDome",
    )
    page._target = ChromeTarget(123, 45, 67)
    asyncio.run(page.close())
    assert closed == [(123, "close_window", ChromeTarget(123, 45, 67))]
    assert page.is_closed() is True


def test_system_chrome_evaluate_passes_argument(monkeypatch):
    seen: list[str] = []

    def fake(pid, action, *, target, script):
        seen.append(script)
        return '"cont"'

    monkeypatch.setattr("system_chrome.chrome_command", fake)
    page = SystemChromePage(
        "https://www.oocl.com/track",
        host="oocl.com",
        carrier="OOLU",
        challenge_name="CAPTCHA",
    )
    page._target = ChromeTarget(123, 45, 67)

    async def run():
        return await page.evaluate("(value) => value", "cont")

    import asyncio

    assert asyncio.run(run()) == "cont"
    assert "cont" in seen[0]


@pytest.mark.asyncio
async def test_wait_for_function_retries_transient_navigation(monkeypatch):
    page = SystemChromePage(
        "https://www.cma-cgm.com/ebusiness/tracking",
        host="cma-cgm.com",
        carrier="CMDU",
        challenge_name="DataDome",
    )
    attempts = 0

    async def evaluate(_script, arg=None):
        nonlocal attempts
        assert arg == "current-document"
        attempts += 1
        if attempts == 1:
            raise SystemChromeError(
                "Chrome returned no JavaScript result.", "NAVIGATION"
            )
        return True

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(page, "evaluate", evaluate)
    monkeypatch.setattr("system_chrome.asyncio.sleep", no_sleep)
    assert await page.wait_for_function(
        "() => true", arg="current-document", timeout=1_000
    ) is True
    assert attempts == 2


def test_system_chrome_page_keeps_carrier_labels():
    page = SystemChromePage(
        "https://www.hapag-lloyd.com/track",
        host="hapag-lloyd.com",
        carrier="HLCU",
        challenge_name="Cloudflare",
    )
    assert page._host == "hapag-lloyd.com"
    assert page._carrier == "HLCU"
    assert page._challenge_name == "Cloudflare"


def test_write_png_data_url(tmp_path):
    dest = write_png_data_url(TINY_PNG, tmp_path / "shot.png")
    assert dest.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_write_png_data_url_rejects_junk(tmp_path):
    with pytest.raises(SystemChromeError):
        write_png_data_url(None, tmp_path / "shot.png")


def test_bounds_and_element_rects():
    assert _bounds_to_rect("48,30,1277,1613") == (48, 30, 1229, 1583)
    assert _bounds_to_rect("{48, 30, 1277, 1613}") == (48, 30, 1229, 1583)
    assert _bounds_to_rect("1,2") is None
    assert _parse_rect({"x": 10, "y": 20, "w": 400, "h": 300}) == (10, 20, 400, 300)
    assert _parse_rect({"x": 10, "y": 20, "w": 1, "h": 1}) is None
    window = (48, 30, 1229, 1583)
    assert _rect_inside((80, 120, 800, 600), window) is True
    assert _rect_inside((0, 0, 20, 20), window) is False
    assert _clip_rect((0, 0, 2000, 2000), window) == window


@pytest.mark.parametrize("host,selector", [
    ("hapag-lloyd.com", ".hal-event-tracking"),
    ("cma-cgm.com", "#trackingsearchsection"),
])
def test_capture_chrome_png_uses_window_screenshot(tmp_path, monkeypatch, host, selector):
    dest = tmp_path / "shot.png"
    seen: list[str] = []

    def fake_window(path, *, host, selector=None):
        seen.append(host)
        payload = b"\x89PNG\r\n\x1a\n" + b"window-shot" * 120
        dest.write_bytes(payload)
        return True

    monkeypatch.setattr("system_chrome._try_window_screenshot", fake_window)
    monkeypatch.setattr(
        "system_chrome.chrome_js",
        lambda script, host: (_ for _ in ()).throw(AssertionError("paint fallback should not run")),
    )
    wrote = capture_chrome_png(dest, host=host, selector=selector)
    assert seen == [host]
    assert wrote.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert b"window-shot" in wrote.read_bytes()


def test_stitch_pngs_vertically_keeps_both_slices(tmp_path):
    top = tmp_path / "top.png"
    bottom = tmp_path / "bottom.png"
    dest = tmp_path / "full.png"
    write_png_rgba(top, 80, 40, bytes([255, 0, 0, 255] * 80 * 40))
    write_png_rgba(bottom, 80, 48, bytes([0, 255, 0, 255] * 80 * 48))
    assert stitch_pngs_vertically([top, bottom], dest) is True
    raw = dest.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert dest.stat().st_size > top.stat().st_size


def test_capture_chrome_png_uses_dom_fallback_when_window_capture_fails(tmp_path, monkeypatch):
    dest = tmp_path / "shot.png"
    monkeypatch.setattr("system_chrome._try_window_screenshot", lambda *args, **kwargs: False)
    monkeypatch.setattr("system_chrome._png_looks_valid", lambda path: True)
    monkeypatch.setattr(
        "system_chrome.chrome_js",
        lambda script, host: TINY_PNG,
    )
    wrote = capture_chrome_png(dest, host="cma-cgm.com")
    assert wrote == dest
    assert wrote.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
