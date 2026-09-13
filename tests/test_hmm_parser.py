from pathlib import Path

import pytest

from status_engine import evaluate
from trackers.base import TrackerError
from trackers.hmm import HmmTracker, parse_hmm_html

FIXTURES = Path(__file__).parent / "fixtures" / "hmm"


def test_parse_sailed_fixture():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_hmm_html(html)
    assert [event.type for event in events[:3]] == ["DEPA", "LOAD", "ARRI"]
    assert events[0].vessel == "ONE MANHATTAN"
    assert events[0].voyage == "0046E"
    assert events[0].transport_mode == "VESSEL"
    assert events[2].transport_mode == "BARGE"
    result = evaluate(
        events,
        container="DFSU7369437",
        carrier="HDMU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "VUNG TAU"
    assert result.atd == "2026-08-26 03:13"
    assert result.vessel == "ONE MANHATTAN"
    assert result.voyage == "0046E"


def test_parse_on_board_waiting():
    html = (FIXTURES / "on_board_waiting.html").read_text(encoding="utf-8")
    events = parse_hmm_html(html)
    assert events[0].type == "LOAD"
    result = evaluate(
        events,
        container="DFSU7369437",
        carrier="HDMU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.pol == "VUNG TAU"
    assert result.sailed is False


def test_parse_empty_returned_is_not_loaded():
    html = (FIXTURES / "empty_returned.html").read_text(encoding="utf-8")
    events = parse_hmm_html(html)
    result = evaluate(
        events,
        container="DFSU7369437",
        carrier="HDMU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "NOT_LOADED"
    assert result.sailed is False


@pytest.mark.asyncio
async def test_search_reports_hmm_access_denied_as_cloudflare():
    html = """<html><head><title> Access Denied </title></head><body>
    <p>Thank you for using HMM e-service.<br>
    Your access to this site has been limited due to abnormal connection.</p>
    </body></html>"""

    class Locator:
        def __init__(self):
            self.first = self

        async def is_visible(self, timeout=0):
            return False

        async def wait_for(self, **kwargs):
            return None

        async def click(self, **kwargs):
            return None

    class Page:
        async def content(self):
            return html

        async def evaluate(self, script, arg=None):
            return (
                "Thank you for using HMM e-service.\n"
                "Your access to this site has been limited due to abnormal connection."
            )

        def locator(self, selector):
            return Locator()

    tracker = HmmTracker(Page())
    with pytest.raises(TrackerError) as exc:
        await tracker.search("DFSU7369437")
    assert exc.value.code == "CLOUDFLARE"
