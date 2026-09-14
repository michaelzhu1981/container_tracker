from pathlib import Path

import pytest

from status_engine import evaluate
from trackers.base import TrackerError
from trackers.one import (
    CHIP_CLEAR_SELECTOR,
    SEARCH_FIELD_SELECTOR,
    OneTracker,
    one_has_result,
    one_search_settled,
    parse_one_html,
)

FIXTURES = Path(__file__).parent / "fixtures" / "one"


def test_parse_sailed_fixture():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_one_html(html)
    assert len(events) == 3
    assert events[0].type == "LOAD"
    assert events[0].classifier == "ACT"
    assert events[0].vessel == "YM MANDATE"
    assert events[0].voyage == "046E"
    assert events[1].type == "DEPA"
    assert events[1].vessel == "YM MANDATE"
    assert events[1].voyage == "046E"
    assert events[2].classifier == "EST"
    result = evaluate(
        events,
        container="ONEU1234567",
        carrier="ONEY",
        timeline_order="oldest_first",
        checked_at="2026-09-12 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "YANTIAN"
    assert result.atd == "2026-09-11 03:40"
    assert result.vessel == "YM MANDATE"
    assert result.voyage == "046E"


def test_parse_on_board_waiting():
    html = (FIXTURES / "on_board_waiting.html").read_text(encoding="utf-8")
    events = parse_one_html(html)
    assert events[1].event_date == "2026-08-25"
    assert events[1].event_time == "12:48"
    assert events[1].vessel == "ONE MANHATTAN"
    assert events[1].voyage == "046E"
    assert events[2].classifier == "EST"
    result = evaluate(
        events,
        container="ONEU0024740",
        carrier="ONEY",
        timeline_order="oldest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.vessel == "ONE MANHATTAN"


def test_parse_empty_returned_is_not_loaded():
    html = (FIXTURES / "empty_returned.html").read_text(encoding="utf-8")
    events = parse_one_html(html)
    result = evaluate(
        events,
        container="ONEU7654321",
        carrier="ONEY",
        timeline_order="oldest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "NOT_LOADED"
    assert result.sailed is False


def test_total_zero_is_not_a_tracking_hit():
    empty = {"text": "Total 0 results\nNo Data Found", "hasTable": False, "hasDetail": False}
    assert one_has_result(empty) is False
    assert one_search_settled({**empty, "loading": True}) is False
    assert one_search_settled({**empty, "loading": False}) is True
    assert one_has_result({"text": "Total 1 result", "hasTable": False, "hasDetail": False})
    assert one_has_result({"text": "", "hasTable": True, "hasDetail": False})


@pytest.mark.asyncio
async def test_parse_events_ignores_not_found_inside_no_data_found_while_loading():
    class Page:
        async def content(self):
            return "<html>Cargo Tracking</html>"

        async def evaluate(self, script):
            return "In progress...\nNo Data Found\nTotal 0 results"

    with pytest.raises(TrackerError, match="not found or could not be parsed"):
        await OneTracker(Page()).parse_events()


@pytest.mark.asyncio
async def test_parse_events_total_zero_after_load_is_no_result():
    class Page:
        async def content(self):
            return "<html>Cargo Tracking</html>"

        async def evaluate(self, script):
            return "Total 0 results\nNo Data Found"

    with pytest.raises(TrackerError) as exc:
        await OneTracker(Page()).parse_events()
    assert exc.value.code == "NO_RESULT"


@pytest.mark.asyncio
async def test_select_container_search_waits_for_type_control():
    seen: list[float] = []

    class Loc:
        def __init__(self):
            self.first = self

        async def is_visible(self, timeout=0):
            seen.append(timeout)
            return False

    class Page:
        def locator(self, selector):
            return Loc()

        def get_by_role(self, role, name=None):
            return Loc()

    await OneTracker(Page())._select_container_search()
    assert seen == [4_000]


@pytest.mark.asyncio
async def test_has_tracking_result_rejects_total_zero():
    class Page:
        async def evaluate(self, script):
            return {
                "text": "Total 0 results\nNo Data Found",
                "hasTable": False,
                "hasDetail": False,
                "loading": False,
            }

    assert await OneTracker(Page())._has_tracking_result() is False


def test_search_field_selector_uses_stable_testid():
    assert "tnt-search-multiple-input" in SEARCH_FIELD_SELECTOR
    assert "SearchMultiple_input" in SEARCH_FIELD_SELECTOR
    assert "tnt-search-multiple-input-chip-clear" in CHIP_CLEAR_SELECTOR


@pytest.mark.asyncio
async def test_wait_for_results_requires_this_container_headline():
    seen: list[str] = []

    class Page:
        async def wait_for_function(self, script, timeout=0, polling=None):
            seen.append(script)
            return True

    await OneTracker(Page())._wait_for_results("ONEU1234567")
    assert seen
    script = seen[0]
    assert "oneu1234567" in script
    assert "TextUnderLine" in script
    assert "if (/in progress/.test(low)) return false" not in script


@pytest.mark.asyncio
async def test_search_reuses_testid_field_and_clears_chip_without_reload():
    waits: list[tuple[str, int]] = []
    clicks: list[str] = []
    fills: list[str] = []
    gotos: list[str] = []
    chip_visible = {"n": 1}

    class Loc:
        def __init__(self, selector: str) -> None:
            self.selector = selector
            self.first = self

        async def wait_for(self, state="visible", timeout=0):
            waits.append((self.selector, timeout))
            if "tnt-search-multiple-input" in self.selector:
                return None
            raise TimeoutError(self.selector)

        async def is_visible(self, timeout=0):
            if "Skip" in self.selector:
                return False
            if "SearchContainer_select-title" in self.selector:
                return True
            if "chip-clear" in self.selector:
                if chip_visible["n"]:
                    chip_visible["n"] -= 1
                    return True
                return False
            if "button-search" in self.selector:
                return True
            return False

        async def click(self, timeout=0, force=False):
            clicks.append(self.selector)

        async def fill(self, value, force=False):
            fills.append(value)

        async def press(self, key):
            return None

        async def inner_text(self):
            return "Container No."

        async def press_sequentially(self, value, delay=0):
            raise AssertionError("sequential typing is not needed for the ONE field")

    class Page:
        url = "https://www.one-line.com/one-ecom/manage-shipment/cargo-tracking"

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
            if arg:
                return True
            if "EventTable" in script:
                return {
                    "text": "ONEU1234567 Total 1 result",
                    "hasTable": True,
                    "hasDetail": True,
                    "loading": False,
                }
            return False

    await OneTracker(Page()).search("ONEU1234567")
    assert not gotos
    assert any("tnt-search-multiple-input" in selector for selector, _timeout in waits)
    assert all(timeout != 12_000 for _selector, timeout in waits)
    assert any("chip-clear" in selector for selector in clicks)
    assert fills[-1] == "ONEU1234567"
    assert any("button-search" in selector for selector in clicks)
