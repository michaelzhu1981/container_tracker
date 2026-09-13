from pathlib import Path

import pytest

from status_engine import evaluate
from trackers.oocl import OoclTracker, _SEARCH_FIELD_SELECTORS, parse_oocl_html

FIXTURES = Path(__file__).parent / "fixtures" / "oocl"


def test_parse_sailed_fixture():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_oocl_html(html)
    assert events[0].type == "DEPA"
    assert events[0].vessel == "OOCL SHANGHAI"
    assert events[0].voyage == "046E"
    result = evaluate(
        events,
        container="OOLU6895702",
        carrier="OOLU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "YANTIAN"
    assert result.atd == "2026-09-11 03:40"
    assert result.vessel == "OOCL SHANGHAI"


def test_parse_on_board_waiting():
    html = (FIXTURES / "on_board_waiting.html").read_text(encoding="utf-8")
    events = parse_oocl_html(html)
    result = evaluate(
        events,
        container="OOLU6895702",
        carrier="OOLU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.pol == "YANTIAN"
    assert result.sailed is False


def test_search_field_selectors_skip_site_search_box():
    assert "input[type='text']" not in _SEARCH_FIELD_SELECTORS
    assert "#SEARCH_NUMBER" in _SEARCH_FIELD_SELECTORS


@pytest.mark.asyncio
async def test_first_visible_search_field_ignores_hidden_header_search():
    seen: list[str] = []

    class Locator:
        def __init__(self, selector: str):
            self.selector = selector
            self.first = self

        async def is_visible(self, timeout=0):
            seen.append(self.selector)
            return self.selector == "#SEARCH_NUMBER"

        async def wait_for(self, **kwargs):
            raise AssertionError("should not wait on hidden site search")

        async def scroll_into_view_if_needed(self):
            return None

    class Page:
        def locator(self, selector):
            return Locator(selector)

    field = await OoclTracker(Page())._first_visible_search_field()
    assert field is not None
    assert field.selector == "#SEARCH_NUMBER"
    assert seen[0] == "#SEARCH_NUMBER"
