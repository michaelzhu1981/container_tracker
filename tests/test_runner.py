from pathlib import Path

import pytest

from config import chrome_profile_dir, default_headed_for, query_delay_seconds
from runner import (
    chrome_commands_using_profile,
    should_relaunch_browser_per_box,
    update_circuit,
    wait_for_human_after_chrome_handoff,
    wait_for_system_chrome_closed,
)
from cli import build_parser


def test_hlcu_does_not_relaunch_browser_per_box():
    assert should_relaunch_browser_per_box("HLCU") is False
    assert should_relaunch_browser_per_box("YMJA") is False


def test_circuit_trips_after_two_cloudflare_failures():
    streak, tripped = update_circuit(0, "CLOUDFLARE")
    assert (streak, tripped) == (1, False)
    streak, tripped = update_circuit(streak, "CLOUDFLARE")
    assert (streak, tripped) == (2, True)


def test_circuit_trips_on_selector_and_resets_on_success():
    streak, tripped = update_circuit(0, "SELECTOR")
    assert tripped is False
    streak, tripped = update_circuit(streak, None)
    assert (streak, tripped) == (0, False)
    streak, tripped = update_circuit(1, "PARSE")
    assert (streak, tripped) == (0, False)


def test_challenge_carrier_defaults():
    assert default_headed_for("HLCU", False) is True
    assert default_headed_for("YMJA", False) is False
    assert default_headed_for("YMJA", True) is True
    assert chrome_profile_dir("HLCU").name == "chrome_hlcu"
    hlcu_lo, hlcu_hi = query_delay_seconds("HLCU")
    ymja_lo, ymja_hi = query_delay_seconds("YMJA")
    assert hlcu_lo >= 5
    assert ymja_hi <= 4


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
