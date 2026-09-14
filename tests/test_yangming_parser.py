from pathlib import Path

import pytest

from status_engine import evaluate
from trackers.yangming import (
    BACK_BUTTON_SELECTOR,
    YangMingTracker,
    parse_yangming_html,
)

FIXTURES = Path(__file__).parent / "fixtures" / "yangming"


def test_parse_sailed_fixture():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_yangming_html(html)
    assert len(events) == 3
    assert events[0].type == "LOAD"
    assert events[0].classifier == "ACT"
    assert events[2].classifier == "EST"
    result = evaluate(
        events,
        container="YMLU1234567",
        carrier="YMJA",
        timeline_order="oldest_first",
        checked_at="2026-09-12 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "YANTIAN"
    assert result.atd == "2026-09-11 03:40"
    assert result.vessel == "YM MANDATE"
    assert result.voyage == "046E"


def test_parse_on_board_is_sailed():
    html = (FIXTURES / "on_board_waiting.html").read_text(encoding="utf-8")
    events = parse_yangming_html(html)
    assert events[0].event_date == "2026-08-25"
    assert events[0].event_time == "12:48"
    assert events[0].vessel == "ONE MANHATTAN"
    assert events[0].voyage == "046E"
    result = evaluate(
        events,
        container="BMOU5733569",
        carrier="YMJA",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.sailed is True
    assert result.atd == "2026-08-25 12:48"
    assert result.vessel == "ONE MANHATTAN"
    assert result.voyage == "046E"


def test_parse_empty_returned_is_not_loaded():
    html = (FIXTURES / "empty_returned.html").read_text(encoding="utf-8")
    events = parse_yangming_html(html)
    result = evaluate(
        events,
        container="YMMU6826189",
        carrier="YMJA",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "NOT_LOADED"
    assert result.sailed is False


def test_parse_barge_only_fixture():
    html = (FIXTURES / "barge_only.html").read_text(encoding="utf-8")
    events = parse_yangming_html(html)
    result = evaluate(
        events,
        container="YMLU1234567",
        carrier="YMJA",
        timeline_order="oldest_first",
        checked_at="2026-09-12 00:00:00",
    )
    assert result.status == "NOT_LOADED"
    assert result.sailed is False


def test_back_button_selector_covers_result_page():
    assert "Back" in BACK_BUTTON_SELECTOR


@pytest.mark.asyncio
async def test_wait_for_results_requires_this_container_tab():
    seen: list[str] = []

    class Page:
        async def wait_for_function(self, script, timeout=0, polling=None):
            seen.append(script)
            return True

    await YangMingTracker(Page())._wait_for_results("YMMU6654691")
    assert seen
    script = seen[0]
    assert "ymmu6654691" in script
    assert "[role='tab']" in script
    assert "table tbody tr" not in script


@pytest.mark.asyncio
async def test_search_clicks_back_on_result_page_without_reload():
    waits: list[tuple[str, int]] = []
    clicks: list[str] = []
    fills: list[str] = []
    gotos: list[str] = []
    field_ready = {"n": 0}

    class Loc:
        def __init__(self, selector: str) -> None:
            self.selector = selector
            self.first = self
            self.last = self

        async def wait_for(self, state="visible", timeout=0):
            waits.append((self.selector, timeout))
            if "textbox" in self.selector and field_ready["n"]:
                return None
            raise TimeoutError(self.selector)

        async def is_visible(self, timeout=0):
            if "Back" in self.selector:
                return True
            if "Search" in self.selector:
                return True
            return False

        async def click(self, timeout=0, force=False):
            clicks.append(self.selector)
            if "Back" in self.selector:
                field_ready["n"] = 1

        async def fill(self, value, force=False):
            fills.append(value)

        async def press(self, key):
            return None

        async def press_sequentially(self, value, delay=0):
            raise AssertionError("sequential typing is not needed for the YMJA field")

    class Page:
        url = "https://www.yangming.com/en/esolution/tracking/cargo_tracking"

        def locator(self, selector):
            return Loc(selector)

        def get_by_role(self, role, name=None):
            return Loc(f"role:{role}:{name}")

        async def goto(self, url, wait_until=None):
            gotos.append(url)

        async def wait_for_timeout(self, ms):
            return None

        async def wait_for_function(self, script, timeout=0, polling=None):
            return True

        async def evaluate(self, script, arg=None):
            return False

    await YangMingTracker(Page()).search("BEAU4332016")
    assert not gotos
    assert any("Back" in selector for selector in clicks)
    assert any(timeout == 400 for _selector, timeout in waits)
    assert all(timeout != 10_000 for _selector, timeout in waits)
    assert fills[-1] == "BEAU4332016"
    assert any("Search" in selector for selector in clicks)


@pytest.mark.asyncio
async def test_search_uses_visible_field_without_back_or_reload():
    clicks: list[str] = []
    gotos: list[str] = []

    class Loc:
        def __init__(self, selector: str) -> None:
            self.selector = selector
            self.first = self
            self.last = self

        async def wait_for(self, state="visible", timeout=0):
            if "textbox" in self.selector:
                return None
            raise TimeoutError(self.selector)

        async def is_visible(self, timeout=0):
            return "Search" in self.selector

        async def click(self, timeout=0, force=False):
            clicks.append(self.selector)

        async def fill(self, value, force=False):
            return None

        async def press(self, key):
            return None

        async def press_sequentially(self, value, delay=0):
            raise AssertionError("sequential typing is not needed for the YMJA field")

    class Page:
        def locator(self, selector):
            return Loc(selector)

        def get_by_role(self, role, name=None):
            return Loc(f"role:{role}:{name}")

        async def goto(self, url, wait_until=None):
            gotos.append(url)

        async def wait_for_timeout(self, ms):
            return None

        async def wait_for_function(self, script, timeout=0, polling=None):
            return True

        async def evaluate(self, script, arg=None):
            return False

    await YangMingTracker(Page()).search("YMMU6654691")
    assert not gotos
    assert not any("Back" in selector for selector in clicks)
    assert any("Search" in selector for selector in clicks)
