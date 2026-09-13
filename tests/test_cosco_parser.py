from pathlib import Path

import pytest

from status_engine import evaluate
from trackers.base import TrackerError
from trackers.cosco import CoscoTracker, displayed_container, parse_cosco_html, result_is_stale

FIXTURES = Path(__file__).parent / "fixtures" / "cosco"


def test_parse_sailed_fixture_synthesizes_pol_load():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_cosco_html(html)
    assert any(event.type == "DEPA" for event in events)
    assert any(event.type == "LOAD" for event in events)
    result = evaluate(
        events,
        container="CSNU6609294",
        carrier="COSU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "YANTIAN"
    assert result.atd == "2026-08-29 15:14"


def test_parse_on_board_waiting():
    html = (FIXTURES / "on_board_waiting.html").read_text(encoding="utf-8")
    events = parse_cosco_html(html)
    assert events[0].type == "LOAD"
    result = evaluate(
        events,
        container="CSNU6609294",
        carrier="COSU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.pol == "YANTIAN"
    assert result.sailed is False


def test_parse_laden_return_is_not_loaded():
    html = (FIXTURES / "laden_return.html").read_text(encoding="utf-8")
    events = parse_cosco_html(html)
    assert events[0].type == "GTIN"
    assert events[0].empty is False
    assert events[0].transport_mode == "TRUCK"
    result = evaluate(
        events,
        container="FFAU5563273",
        carrier="COSU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "NOT_LOADED"
    assert result.loaded is False
    assert result.sailed is False
    assert result.atd is None
    assert "Laden Return" in (result.latest_event or "")


def test_displayed_container_ignores_search_input():
    stale = (FIXTURES / "stale_other_container.html").read_text(encoding="utf-8")
    fresh = (FIXTURES / "laden_return.html").read_text(encoding="utf-8")
    assert displayed_container(stale) == "OOCU5001715"
    assert displayed_container(fresh) == "FFAU5563273"
    assert result_is_stale(stale, "FFAU5563273") is True
    assert result_is_stale(fresh, "FFAU5563273") is False


@pytest.mark.asyncio
async def test_parse_events_rejects_stale_previous_container():
    html = (FIXTURES / "stale_other_container.html").read_text(encoding="utf-8")

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

        async def goto(self, *args, **kwargs):
            return None

        async def evaluate(self, script, arg=None):
            return "OOCU5001715"

        async def wait_for_function(self, *args, **kwargs):
            return None

        def locator(self, selector):
            return Locator()

    tracker = CoscoTracker(Page())
    tracker._expected = "FFAU5563273"
    with pytest.raises(TrackerError) as exc:
        await tracker.parse_events()
    assert exc.value.code == "PARSE"
    assert "OOCU5001715" in str(exc.value)
