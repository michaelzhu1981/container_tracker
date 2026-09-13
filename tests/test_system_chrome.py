from system_chrome import _as_iife, _find_element_js, _parse_js_result


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