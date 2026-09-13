from pathlib import Path

import pytest

from config import chrome_profile_dir, default_headed_for, query_delay_seconds
from runner import (
    chrome_commands_using_profile,
    chrome_launch_args,
    playwright_context_kwargs,
    should_relaunch_browser_per_box,
    unlocks_in_current_browser,
    update_circuit,
    uses_system_chrome,
    wait_for_human_after_chrome_handoff,
    wait_for_system_chrome_closed,
)
from cli import build_parser


def test_cmdu_unlocks_in_current_browser():
    assert unlocks_in_current_browser("CMDU") is True
    assert unlocks_in_current_browser("MAEU") is True
    assert unlocks_in_current_browser("HLCU") is False
    assert unlocks_in_current_browser("MSCU") is False
    assert uses_system_chrome("CMDU") is True
    assert uses_system_chrome("MAEU") is False
    assert uses_system_chrome("HLCU") is False


def test_hlcu_does_not_relaunch_browser_per_box():
    assert should_relaunch_browser_per_box("HLCU") is False
    assert should_relaunch_browser_per_box("YMJA") is False


def test_circuit_trips_after_two_cloudflare_failures():
    streak, tripped = update_circuit(0, "CLOUDFLARE")
    assert (streak, tripped) == (1, False)
    streak, tripped = update_circuit(streak, "CLOUDFLARE")
    assert (streak, tripped) == (2, True)


def test_circuit_trips_after_repeated_captcha():
    streak, tripped = update_circuit(0, "CAPTCHA")
    assert (streak, tripped) == (1, False)
    assert update_circuit(streak, "CAPTCHA") == (2, True)


def test_circuit_trips_on_selector_and_resets_on_success():
    streak, tripped = update_circuit(0, "SELECTOR")
    assert tripped is False
    streak, tripped = update_circuit(streak, None)
    assert (streak, tripped) == (0, False)
    streak, tripped = update_circuit(1, "PARSE")
    assert (streak, tripped) == (0, False)


def test_challenge_carrier_defaults():
    assert default_headed_for("HLCU", False) is True
    assert default_headed_for("MSCU", False) is True
    assert default_headed_for("MAEU", False) is True
    assert default_headed_for("CMDU", False) is True
    assert default_headed_for("ONEY", False) is False
    assert default_headed_for("YMJA", False) is False
    assert default_headed_for("YMJA", True) is True
    assert chrome_profile_dir("HLCU").name == "chrome_hlcu"
    hlcu_lo, hlcu_hi = query_delay_seconds("HLCU")
    mscu_lo, mscu_hi = query_delay_seconds("MSCU")
    maeu_lo, maeu_hi = query_delay_seconds("MAEU")
    cmdu_lo, cmdu_hi = query_delay_seconds("CMDU")
    ymja_lo, ymja_hi = query_delay_seconds("YMJA")
    oney_lo, oney_hi = query_delay_seconds("ONEY")
    assert hlcu_lo >= 5
    assert mscu_lo >= 5
    assert maeu_lo >= 5
    assert cmdu_lo >= 5
    assert ymja_hi <= 4
    assert oney_hi <= 4


def test_cli_no_wait_challenge_flag():
    parser = build_parser()
    args = parser.parse_args(["--no-wait-challenge"])
    assert args.no_wait_challenge is True
    default = parser.parse_args([])
    assert default.no_wait_challenge is False


def test_cli_serve_flag():
    parser = build_parser()
    args = parser.parse_args(["--serve", "--port", "9001"])
    assert args.serve is True
    assert args.port == 9001


def test_chrome_launch_args_are_not_automated():
    args = chrome_launch_args(
        "/tmp/sessions/chrome_cmdu",
        "https://www.cma-cgm.com/ebusiness/tracking",
    )
    assert all("remote-debugging" not in item for item in args)
    assert any(item.startswith("--user-data-dir=") for item in args)
    assert args[-1].startswith("https://")


def test_playwright_context_hides_automation_switches():
    kwargs = playwright_context_kwargs(headed=True)
    assert kwargs["headless"] is False
    assert "--enable-automation" in kwargs["ignore_default_args"]
    assert "--disable-blink-features=AutomationControlled" in kwargs["args"]


def test_chrome_commands_using_profile(monkeypatch):
    profile = Path("/tmp/sessions/chrome_hlcu")
    needle = f"--user-data-dir={profile.resolve()}"

    def fake_ps(*args, **kwargs):
        return (
            f"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome {needle} https://x\n"
            "other process\n"
        )

    monkeypatch.setattr("runner.subprocess.check_output", fake_ps)
    assert chrome_commands_using_profile(str(profile))


def test_wait_for_system_chrome_closed_after_exit(monkeypatch):
    calls = {"n": 0}

    def fake(_profile):
        calls["n"] += 1
        return ["chrome"] if calls["n"] < 3 else []

    monkeypatch.setattr("runner.chrome_commands_using_profile", fake)
    assert wait_for_system_chrome_closed("/x", timeout_s=2, appear_s=2, poll_s=0.01)


def test_wait_for_system_chrome_closed_never_starts(monkeypatch):
    monkeypatch.setattr("runner.chrome_commands_using_profile", lambda _p: [])
    assert (
        wait_for_system_chrome_closed("/x", timeout_s=1, appear_s=0.03, poll_s=0.01)
        is False
    )


def test_handoff_without_tty_waits_for_chrome_close(monkeypatch):
    monkeypatch.setattr("runner.stdin_can_accept_enter", lambda: False)
    monkeypatch.setattr("runner.wait_for_system_chrome_closed", lambda _p, **kwargs: True)
    assert wait_for_human_after_chrome_handoff("/tmp/profile") is True


def test_handoff_eof_falls_back_to_chrome_close(monkeypatch):
    monkeypatch.setattr("runner.stdin_can_accept_enter", lambda: True)

    def boom(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr("runner.wait_for_system_chrome_closed", lambda _p, **kwargs: True)
    assert wait_for_human_after_chrome_handoff("/tmp/profile") is True


def test_chrome_wait_aborts(monkeypatch):
    monkeypatch.setattr("runner.chrome_commands_using_profile", lambda _p: ["chrome"])
    assert (
        wait_for_system_chrome_closed(
            "/x", timeout_s=2, appear_s=2, poll_s=0.01, should_abort=lambda: True
        )
        is False
    )


@pytest.mark.asyncio
async def test_run_batch_stops_remaining_on_cancel(tmp_path: Path, monkeypatch):
    import asyncio

    from models import TrackResult
    from runner import run_batch

    tracked: list[str] = []
    cancel = asyncio.Event()

    async def fake_track(page, carrier, container, **kwargs):
        tracked.append(container)
        if container == "HLXU1234567":
            cancel.set()
        return TrackResult(
            container=container,
            carrier=carrier,
            status="NOT_LOADED",
            success=True,
            check_result="SUCCESS",
            checked_at="t",
        )

    class FakePlaywright:
        pass

    class CM:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, *args):
            return False

    class FakeBrowser:
        def __init__(self, playwright, carrier, **kwargs):
            self.page = object()

        async def start(self):
            return None

        async def close(self):
            return None

        async def ensure_open(self):
            return None

        def page_open(self):
            return True

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: CM())
    monkeypatch.setattr("runner.CarrierBrowser", FakeBrowser)
    monkeypatch.setattr("runner._track_one", fake_track)

    rows = [
        {"Container": "HLXU1234567", "Carrier": "HLCU", "extras": {}},
        {"Container": "HLXU7654321", "Carrier": "HLCU", "extras": {}},
        {"Container": "YMLU1234567", "Carrier": "YMJA", "extras": {}},
    ]
    results, written = await run_batch(
        rows,
        output_path=tmp_path / "out.xlsx",
        cancel_event=cancel,
        wait_for_challenge=False,
    )
    assert tracked == ["HLXU1234567"]
    assert [item.container for item in results] == ["HLXU1234567"]
    assert written.exists()


def _fake_playwright_browser(monkeypatch):
    class FakePlaywright:
        pass

    class CM:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, *args):
            return False

    class FakeBrowser:
        def __init__(self, playwright, carrier, **kwargs):
            self.page = object()
            self.on_challenge = None

        async def start(self):
            return None

        async def close(self):
            return None

        async def ensure_open(self):
            return None

        def page_open(self):
            return True

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: CM())
    monkeypatch.setattr("runner.CarrierBrowser", FakeBrowser)


@pytest.mark.asyncio
async def test_cmdu_unlocks_once_then_batches(tmp_path: Path, monkeypatch):
    from models import TrackResult
    from runner import run_batch

    prepared: list[str] = []
    tracked: list[str] = []

    async def fake_prepare(self):
        prepared.append(self.carrier_code)

    async def fake_track(page, carrier, container, **kwargs):
        tracked.append(container)
        return TrackResult(
            container=container,
            carrier=carrier,
            status="NOT_LOADED",
            success=True,
            check_result="SUCCESS",
            checked_at="t",
        )

    async def no_sleep(seconds, cancel_event):
        return False

    monkeypatch.setattr("trackers.cma.CmaTracker.prepare_session", fake_prepare)
    monkeypatch.setattr("runner._track_one", fake_track)
    monkeypatch.setattr("runner.sleep_or_cancel", no_sleep)
    _fake_playwright_browser(monkeypatch)

    rows = [
        {"Container": "ECMU7271573", "Carrier": "CMDU", "extras": {}},
        {"Container": "CMAU7662786", "Carrier": "CMDU", "extras": {}},
    ]
    results, written = await run_batch(
        rows,
        output_path=tmp_path / "out.xlsx",
        wait_for_challenge=True,
    )
    assert prepared == ["CMDU"]
    assert tracked == ["ECMU7271573", "CMAU7662786"]
    assert [item.status for item in results] == ["NOT_LOADED", "NOT_LOADED"]
    assert written.exists()


@pytest.mark.asyncio
async def test_cmdu_unlock_failure_pauses_remaining(tmp_path: Path, monkeypatch):
    from models import TrackResult
    from runner import run_batch
    from trackers.base import TrackerError

    tracked: list[str] = []

    async def fail_prepare(self):
        raise TrackerError("Timed out waiting for the CAPTCHA check to clear.", "CAPTCHA")

    async def fake_track(page, carrier, container, **kwargs):
        tracked.append(container)
        return TrackResult(
            container=container,
            carrier=carrier,
            status="NOT_LOADED",
            success=True,
            check_result="SUCCESS",
            checked_at="t",
        )

    monkeypatch.setattr("trackers.cma.CmaTracker.prepare_session", fail_prepare)
    monkeypatch.setattr("runner._track_one", fake_track)
    _fake_playwright_browser(monkeypatch)

    rows = [
        {"Container": "ECMU7271573", "Carrier": "CMDU", "extras": {}},
        {"Container": "CMAU7662786", "Carrier": "CMDU", "extras": {}},
        {"Container": "YMLU1234567", "Carrier": "YMJA", "extras": {}},
    ]
    results, _written = await run_batch(
        rows,
        output_path=tmp_path / "out.xlsx",
        wait_for_challenge=True,
    )
    assert tracked == ["YMLU1234567"]
    by_container = {item.container: item for item in results}
    assert by_container["ECMU7271573"].status == "MANUAL_CHECK_REQUIRED"
    assert by_container["ECMU7271573"].error_code == "CAPTCHA"
    assert by_container["CMAU7662786"].status == "CHECK_FAILED"
    assert by_container["CMAU7662786"].error_code == "CAPTCHA"
    assert "current Chrome window" in (by_container["CMAU7662786"].error or "")
    assert by_container["YMLU1234567"].status == "NOT_LOADED"
