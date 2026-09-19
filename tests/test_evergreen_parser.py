from pathlib import Path

import pytest

from status_engine import evaluate
from trackers.evergreen import EvergreenTracker, parse_evergreen_html

FIXTURES = Path(__file__).parent / "fixtures" / "evergreen"


def _result(name: str, container: str):
    html = (FIXTURES / name).read_text(encoding="utf-8")
    events = parse_evergreen_html(html)
    return events, evaluate(
        events,
        container=container,
        carrier="EGLV",
        timeline_order="newest_first",
        checked_at="2026-09-19 00:00:00",
    )


def test_parse_loaded_latest_event():
    events, result = _result("loaded.html", "EGHU8519309")
    assert len(events) == 1
    assert events[0].event_date == "2026-09-10"
    assert events[0].vessel == "EVER LASTING"
    assert events[0].voyage == "0018-095E"
    assert result.status == "SAILED"
    assert result.loaded is True
    assert result.sailed is True
    assert result.atd == "2026-09-10"
    assert result.pol == "SHANGHAI"


def test_parse_departed_latest_event():
    events, result = _result("departed.html", "EMCU6201761")
    assert events[0].type == "DEPA"
    assert events[0].transport_mode == "VESSEL"
    assert result.status == "SAILED"
    assert result.atd == "2026-09-11"


def test_parse_empty_returned_live_shape():
    events, result = _result("empty_returned.html", "EGHU8519309")
    assert events[0].type == "GTIN"
    assert events[0].empty is True
    assert result.status == "SAILED"
    assert result.loaded is True
    assert result.sailed is True
    assert result.pol is None
    assert result.atd is None
    assert result.latest_event == "SEP-12-2026 | Empty container returned | MANILA (NORTH PORT) (PH)"


def test_parse_transship_loaded_is_sailed_without_origin_details():
    events, result = _result("transship_loaded.html", "EGSU9773522")
    assert events[0].type == "LOAD"
    assert events[0].vessel == "EVER MACH"
    assert events[0].voyage == "1472-018E"
    assert result.status == "SAILED"
    assert result.loaded is True
    assert result.sailed is True
    assert result.vessel == "EVER MACH"
    assert result.voyage == "1472-018E"
    assert result.pol is None
    assert result.atd is None


def test_tracker_contract():
    assert EvergreenTracker.carrier_code == "EGLV"
    assert EvergreenTracker.timeline_order == "newest_first"
    assert "evergreen-shipping.cn" in EvergreenTracker.tracking_url
    assert "提单货柜信息和当前动态" in EvergreenTracker.screenshot_selectors[0]
    assert "Current Status" in EvergreenTracker.screenshot_selectors[1]


@pytest.mark.asyncio
async def test_search_posts_official_container_form():
    html = (FIXTURES / "empty_returned.html").read_text(encoding="utf-8")
    posted: dict = {}
    rendered: list[str] = []

    class Response:
        ok = True
        status = 200

        async def text(self):
            return html

    class Request:
        async def post(self, url, **kwargs):
            posted["url"] = url
            posted.update(kwargs)
            return Response()

    class Context:
        request = Request()

    class Page:
        context = Context()

        async def set_content(self, value, wait_until=None):
            rendered.append(value)

    tracker = EvergreenTracker(Page())
    await tracker.search("EGHU8519309")
    assert posted["form"]["TYPE"] == "CNTR"
    assert posted["form"]["CNTR"] == "EGHU8519309"
    assert posted["form"]["SEL"] == "s_cntr"
    assert rendered and "EGHU8519309" in rendered[0]
