from pathlib import Path

import pytest

from status_engine import evaluate
from trackers.base import TrackerError
from trackers.zim import (
    TRACK_QUERY_URL,
    ZimTracker,
    _SEARCH_FIELD_SELECTORS,
    json_mentions_container,
    parse_zim_html,
    parse_zim_payload,
)

FIXTURES = Path(__file__).parent / "fixtures" / "zim"


def test_parse_sailed_fixture():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_zim_html(html)
    assert events[0].type == "DEPA"
    assert events[0].vessel == "ZIM NEW YORK"
    assert events[0].voyage == "046E"
    result = evaluate(
        events,
        container="TCNU3698035",
        carrier="ZIMU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "YANTIAN"
    assert result.atd == "2026-09-11 03:40"


def test_parse_embedded_json_on_board_waiting():
    html = (FIXTURES / "on_board_waiting.html").read_text(encoding="utf-8")
    events = parse_zim_html(html)
    assert events[0].type == "LOAD"
    assert events[0].vessel == "ZIM NEW YORK"
    result = evaluate(
        events,
        container="TCNU3698035",
        carrier="ZIMU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.pol == "YANTIAN"


def test_parse_zim_payload():
    events = parse_zim_payload(
        {
            "unitActivityList": [
                {
                    "activityDesc": "Vessel Departure",
                    "activityDateTz": "2026-09-11 03:40:00",
                    "placeFromDesc": "YANTIAN",
                    "vesselName": "ZIM NEW YORK",
                    "voyage": "046E",
                }
            ]
        }
    )
    assert events[0].type == "DEPA"
    assert events[0].voyage == "046E"


def test_zim_uses_system_chrome_like_cmdu():
    assert ZimTracker.use_system_chrome is True
    assert ZimTracker.wait_in_current_browser is True
    assert ZimTracker.system_chrome_host == "zim.com"
    assert ZimTracker.system_chrome_challenge == "hCaptcha"
    assert "input[type='text']" not in _SEARCH_FIELD_SELECTORS
    assert "input.chips-input" in _SEARCH_FIELD_SELECTORS


@pytest.mark.asyncio
async def test_search_stays_on_form_instead_of_query_url():
    gotos: list[str] = []

    class Locator:
        def __init__(self, selector: str):
            self.selector = selector
            self.first = self

        async def is_visible(self, timeout=0):
            return "chips-input" in self.selector or "chips-search-button" in self.selector

        async def wait_for(self, **kwargs):
            return None

        async def click(self, **kwargs):
            return None

        async def fill(self, value):
            return None

        async def press(self, key):
            return None

    class Page:
        def locator(self, selector):
            return Locator(selector)

        async def goto(self, url, **kwargs):
            gotos.append(url)

        async def evaluate(self, script, arg=None):
            return ""

        async def wait_for_function(self, script, timeout=0):
            return None

        def on(self, event, handler):
            return None

    await ZimTracker(Page()).search("TCNU3698035")
    assert not any("consnumber=" in url for url in gotos)
    assert TRACK_QUERY_URL.format(number="TCNU3698035") not in gotos


def test_zim_payload_mentions_container():
    payload = {"unitActivityList": [{"activityDesc": "Loaded"}], "container": "TCNU3698035"}
    assert json_mentions_container(payload, "TCNU3698035") is True
    assert json_mentions_container(payload, "OOLU6895702") is False
