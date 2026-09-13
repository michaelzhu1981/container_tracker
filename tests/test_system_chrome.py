import pytest

from system_chrome import (
    SystemChromeError,
    _as_iife,
    _find_element_js,
    _parse_js_result,
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
    scoped = _screenshot_root_js("#gridTrackingDetails")
    assert "closest" in scoped
    assert "#gridTrackingDetails" in scoped


def test_write_png_data_url(tmp_path):
    dest = write_png_data_url(TINY_PNG, tmp_path / "shot.png")
    assert dest.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_write_png_data_url_rejects_junk(tmp_path):
    with pytest.raises(SystemChromeError):
        write_png_data_url(None, tmp_path / "shot.png")


def test_capture_chrome_png_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr("system_chrome.chrome_js", lambda script, host: TINY_PNG)
    dest = capture_chrome_png(tmp_path / "shot.png", host="cma-cgm.com")
    assert dest.stat().st_size > 32