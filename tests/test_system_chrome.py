import pytest

from system_chrome import (
    SystemChromeError,
    SystemChromePage,
    _as_iife,
    _bounds_to_rect,
    _find_element_js,
    _parse_js_result,
    _parse_rect,
    _rect_inside,
    _screenshot_root_js,
    capture_chrome_png,
    write_png_data_url,
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
    scoped = _screenshot_root_js("#gridTrackingDetails")
    assert "closest" in scoped
    assert "#gridTrackingDetails" in scoped
    hapag = _screenshot_root_js(".hal-event-tracking")
    assert ".hal-event-tracking" in hapag


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


def test_capture_chrome_png_fails_when_window_capture_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("system_chrome._try_window_screenshot", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "system_chrome.chrome_js",
        lambda script, host: (_ for _ in ()).throw(AssertionError("paint fallback should not run")),
    )
    with pytest.raises(SystemChromeError, match="Chrome window"):
        capture_chrome_png(tmp_path / "shot.png", host="cma-cgm.com")