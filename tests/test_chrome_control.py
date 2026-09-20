import asyncio
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from chrome_control import (
    ChromeTarget,
    SystemChromeError,
    chrome_command,
    launch_normal_chrome,
    normal_chrome_pid,
    show_normal_chrome_url,
)
from system_chrome import SystemChromePage


ENTRY = "https://www.oocl.com/eng/ourservices/eservices/cargotracking/Pages/cargotracking.aspx"
POPUP = "https://www.oocl.com/Pages/ExpressLink.aspx?eltype=ct&businessNumber=TGBU5255226"
RESULT = "https://www.cargosmart.com/result/123"


@pytest.fixture(autouse=True)
def native_bridge_path(monkeypatch):
    monkeypatch.setattr("chrome_control._bridge_executable", lambda: Path("/tmp/chrome_bridge_native"))


def test_resolve_normal_process_ignores_parallel_chrome_instances(monkeypatch):
    exe = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    listing = f"""101 {exe} --user-data-dir=/profiles/oney --remote-debugging-pipe
102 {exe} --headless --user-data-dir=/profiles/ymja
103 {exe} --user-data-dir=/profiles/cosu
104 {exe} --disable-popup-blocking --new-window {ENTRY}
"""
    monkeypatch.setattr("chrome_control.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=listing))
    assert normal_chrome_pid() == 104


def test_launch_normal_chrome_opens_visible_target_window(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("chrome_control.subprocess.run", run)
    launch_normal_chrome(ENTRY)
    assert calls == [[
        "open", "-na", "/Applications/Google Chrome.app", "--args",
        "--disable-popup-blocking", "--new-window", ENTRY,
    ]]
    assert "--no-startup-window" not in calls[0]


def test_show_normal_chrome_url_reuses_existing_app(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("chrome_control.subprocess.run", run)
    show_normal_chrome_url(ENTRY)
    assert calls == [[
        "open", "-a", "/Applications/Google Chrome.app", ENTRY,
    ]]


@pytest.mark.parametrize("code", ["BROWSER_PERMISSION", "TAB_NOT_FOUND", "BROWSER_CLOSED"])
def test_bridge_preserves_error_codes_and_target(monkeypatch, code):
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout=f"ERROR\t{code}\ttest failure\n")

    monkeypatch.setattr("chrome_control.subprocess.run", run)
    with pytest.raises(SystemChromeError) as error:
        chrome_command(104, "evaluate", target=ChromeTarget(104, 20, 30), script="1")
    assert error.value.code == code
    assert commands[0][0] == "/tmp/chrome_bridge_native"
    assert commands[0][1:] == ["evaluate", "104", "20", "30", "1"]


def test_native_bridge_parses_inventory_and_evaluate(monkeypatch):
    outputs = iter([
        "OK\n20\t30\thttps://www.oocl.com/entry\n99\t98\thttps://example.com/\n",
        "OK\n{\"ready\":true}\n",
    ])
    monkeypatch.setattr(
        "chrome_control.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=next(outputs), stderr=""),
    )
    assert chrome_command(104, "inventory") == [
        {"id": 20, "tabs": [{"id": 30, "url": "https://www.oocl.com/entry"}]},
        {"id": 99, "tabs": [{"id": 98, "url": "https://example.com/"}]},
    ]
    assert chrome_command(
        104, "evaluate", target=ChromeTarget(104, 20, 30), script="probe"
    ) == '{"ready":true}'


def test_unresponsive_bridge_is_bounded(monkeypatch):
    def run(*args, **kwargs):
        assert kwargs["timeout"] == 15
        raise subprocess.TimeoutExpired(args[0], 15)

    monkeypatch.setattr("chrome_control.subprocess.run", run)
    with pytest.raises(SystemChromeError) as error:
        chrome_command(104, "inventory")
    assert error.value.code == "TIMEOUT"


class ChromeModel:
    """One owned window; a duplicate result in a foreign window must be untouched."""

    def __init__(self):
        self.windows = {20: {30: ENTRY}, 99: {98: RESULT}}
        self.calls = []

    def command(self, pid, action, *, target=None, **args):
        self.calls.append((pid, action, target))
        assert pid == 104
        if action == "inventory":
            return []
        if action == "new_window":
            return {"id": 20, "tab": {"id": 30, "url": ENTRY}}
        assert target is not None and target.window_id == 20
        if 20 not in self.windows:
            raise SystemChromeError("closed window", "BROWSER_CLOSED")
        tabs = self.windows[20]
        if action == "tabs":
            return [{"id": key, "url": value} for key, value in tabs.items()]
        if action == "new_tab":
            tabs[31] = args["url"]
            return {"id": 31, "url": tabs[31]}
        if action == "close_window":
            del self.windows[20]
            return True
        if target.tab_id not in tabs:
            raise SystemChromeError("closed tab", "TAB_NOT_FOUND")
        if action == "evaluate":
            return '"event table"' if target.tab_id == 31 else "1"
        if action == "tab":
            return {"id": target.tab_id, "url": tabs[target.tab_id]}
        if action == "close_tab":
            del tabs[target.tab_id]
        return True


@pytest.fixture
def bound_chrome(monkeypatch):
    model = ChromeModel()
    monkeypatch.setattr("system_chrome.chrome_command", model.command)
    monkeypatch.setattr("system_chrome.normal_chrome_pid", lambda: 104)
    page = SystemChromePage(ENTRY, host="oocl.com", carrier="OOLU")
    return model, page


@pytest.mark.asyncio
async def test_start_binds_normal_process_and_queries_immediately(bound_chrome):
    model, page = bound_chrome
    await asyncio.wait_for(page.start(), 1)
    assert page._target == ChromeTarget(104, 20, 30)
    assert await page.evaluate("() => 1") == 1
    assert [action for _, action, _ in model.calls] == ["inventory", "new_window", "evaluate", "evaluate"]


@pytest.mark.asyncio
async def test_start_binds_the_visible_window_it_launched(monkeypatch):
    launched = []
    calls = []
    pids = iter([None, 104])

    monkeypatch.setattr("system_chrome.normal_chrome_pid", lambda: next(pids, 104))
    monkeypatch.setattr("system_chrome.launch_normal_chrome", launched.append)

    def command(pid, action, *, target=None, **kwargs):
        calls.append((action, target))
        if action == "inventory":
            return [{"id": 20, "tabs": [{"id": 30, "url": ENTRY}]}]
        if action == "tab":
            return {"id": 30, "url": ENTRY}
        if action == "evaluate":
            return "1"
        raise AssertionError(action)

    monkeypatch.setattr("system_chrome.chrome_command", command)
    page = SystemChromePage(ENTRY, host="oocl.com", carrier="OOLU")
    await asyncio.wait_for(page.start(), 1)
    assert launched == [ENTRY]
    assert page._target == ChromeTarget(104, 20, 30)
    assert [action for action, _ in calls] == ["inventory", "tab", "evaluate"]


@pytest.mark.asyncio
async def test_inventory_permission_shows_chrome_and_waits_instead_of_failing(monkeypatch):
    attempts = 0
    shown = []
    events = []

    monkeypatch.setattr("system_chrome.normal_chrome_pid", lambda: 104)
    monkeypatch.setattr("system_chrome.show_normal_chrome_url", shown.append)

    def command(pid, action, *, target=None, **kwargs):
        nonlocal attempts
        if action == "inventory":
            attempts += 1
            if attempts == 1:
                raise SystemChromeError("inventory denied (-1743)", "BROWSER_PERMISSION")
            return [{"id": 20, "tabs": [{"id": 30, "url": ENTRY}]}]
        if action == "tab":
            return {"id": 30, "url": ENTRY}
        if action == "evaluate":
            return "1"
        raise AssertionError(action)

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr("system_chrome.chrome_command", command)
    monkeypatch.setattr("system_chrome.asyncio.sleep", no_sleep)
    page = SystemChromePage(
        ENTRY, host="oocl.com", carrier="OOLU", on_permission_wait=events.append,
    )
    await asyncio.wait_for(page.start(), 1)
    assert shown == [ENTRY]
    assert page._target == ChromeTarget(104, 20, 30)
    assert events == [
        {"code": "BROWSER_PERMISSION", "mode": "browser_permission", "timeout_seconds": 600},
        {"code": None},
    ]


@pytest.mark.asyncio
async def test_redirect_retains_tab_id_for_html_and_cleanup(bound_chrome):
    model, page = bound_chrome
    await page.start()
    await page.open_tab(POPUP)
    model.windows[20][31] = RESULT  # The site redirects after adoption.
    assert page.url == RESULT
    await page.focus_tab(POPUP)  # The old URL remains a handle for the same ID.
    assert await page.content() == "event table"
    assert page._target == ChromeTarget(104, 20, 31)
    await page.close_tab(POPUP)
    assert page.url == ENTRY
    await page.close()
    assert model.windows == {99: {98: RESULT}}


@pytest.mark.asyncio
async def test_lost_tab_never_falls_back_to_other_matching_page(bound_chrome):
    model, page = bound_chrome
    await page.start()
    await page.open_tab(POPUP)
    del model.windows[20][31]
    with pytest.raises(SystemChromeError) as error:
        await page.content()
    assert error.value.code == "TAB_NOT_FOUND"
    assert model.windows[99] == {98: RESULT}


@pytest.mark.asyncio
async def test_missing_tab_does_not_enter_ten_minute_permission_wait(bound_chrome, monkeypatch):
    _, page = bound_chrome

    async def missing(script):
        raise SystemChromeError("closed tab", "TAB_NOT_FOUND")

    monkeypatch.setattr(page, "evaluate", missing)
    with pytest.raises(SystemChromeError) as error:
        await asyncio.wait_for(page._wait_until_js_enabled(), 0.1)
    assert error.value.code == "TAB_NOT_FOUND"


@pytest.mark.asyncio
async def test_permission_wait_reports_and_clears_prompt(bound_chrome, monkeypatch):
    _, page = bound_chrome
    events = []
    page.on_permission_wait = events.append
    attempts = 0

    async def probe(script):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SystemChromeError("Allow JavaScript from Apple Events", "BROWSER_PERMISSION")
        return 1

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(page, "evaluate", probe)
    monkeypatch.setattr("system_chrome.asyncio.sleep", no_sleep)
    await page._wait_until_js_enabled()
    assert events[0]["mode"] == "browser_permission"
    assert events[0]["code"] == "BROWSER_PERMISSION"
    assert events[-1] == {"code": None}


@pytest.mark.asyncio
async def test_no_wait_permission_fails_immediately(bound_chrome, monkeypatch):
    _, page = bound_chrome
    page.wait_for_permission = False

    async def denied(script):
        raise SystemChromeError("JavaScript is disabled", "BROWSER_PERMISSION")

    monkeypatch.setattr(page, "evaluate", denied)
    with pytest.raises(SystemChromeError) as error:
        await asyncio.wait_for(page._wait_until_js_enabled(), 0.1)
    assert error.value.code == "BROWSER_PERMISSION"


@pytest.mark.asyncio
async def test_screenshot_uses_the_bound_result_tab(bound_chrome, monkeypatch, tmp_path):
    from system_chrome import _CAPTURE_TARGET

    _, page = bound_chrome
    await page.start()
    await page.open_tab(POPUP)
    captured = []

    def capture(*args, **kwargs):
        captured.append(_CAPTURE_TARGET.get())

    monkeypatch.setattr("system_chrome.capture_chrome_png", capture)
    await page.screenshot(path=str(tmp_path / "shot.png"))
    assert captured == [ChromeTarget(104, 20, 31)]
    assert _CAPTURE_TARGET.get() is None
