import asyncio
from pathlib import Path

from status_engine import evaluate
from trackers.hapag import HapagTracker, parse_hapag_html

FIXTURES = Path(__file__).parent / "fixtures" / "hapag"


def test_parse_sailed_fixture():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_hapag_html(html)
    assert len(events) == 3
    result = evaluate(
        events,
        container="HLXU1234567",
        carrier="HLCU",
        timeline_order="oldest_first",
        checked_at="2026-09-12 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "YANTIAN"
    assert result.atd == "2026-09-11 03:40"
    assert result.vessel == "MONTEVIDEO EXPRESS"


def test_parse_beta_transship_loaded_fixture():
    html = (FIXTURES / "beta_transship_loaded.html").read_text(encoding="utf-8")
    events = parse_hapag_html(html)
    assert len(events) >= 8
    actual_loads = [e for e in events if e.classifier == "ACT" and e.type == "LOAD"]
    assert actual_loads[-1].location_raw == "SALALAH"
    assert actual_loads[-1].event_time == "03:06"
    assert actual_loads[-1].vessel == "BREMEN EXPRESS"
    planned_depa = [e for e in events if e.classifier == "PLN" and e.type == "DEPA"]
    assert planned_depa
    result = evaluate(
        events,
        container="CAIU7012411",
        carrier="HLCU",
        timeline_order="oldest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "MUHAMMAD BIN QASIM"
    assert result.atd == "2026-08-28 12:48"
    assert result.load_port == "SALALAH"
    assert result.load_time == "2026-09-07 03:06"
    assert result.vessel == "BSG BIMINI"
    assert result.voyage == "635W"
    assert result.sailed is True


def test_parse_beta_not_loaded_planned_ocean():
    html = (FIXTURES / "beta_not_loaded.html").read_text(encoding="utf-8")
    events = parse_hapag_html(html)
    result = evaluate(
        events,
        container="TXGU7127206",
        carrier="HLCU",
        timeline_order="oldest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "NOT_LOADED"
    assert result.sailed is False
    assert any(e.classifier == "PLN" and e.type == "LOAD" for e in events)


def test_parse_barge_only_fixture():
    html = (FIXTURES / "barge_only.html").read_text(encoding="utf-8")
    events = parse_hapag_html(html)
    result = evaluate(
        events,
        container="HLXU1234567",
        carrier="HLCU",
        timeline_order="oldest_first",
        checked_at="2026-09-12 00:00:00",
    )
    assert result.status == "NOT_LOADED"
    assert result.sailed is False


class _ExpandLocator:
    def __init__(self, page: "_ExpandPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_ExpandLocator":
        return self

    async def is_visible(self, timeout: int = 0) -> bool:
        if "hal-event-tracking" in self.selector:
            return self.page.details_visible
        return "Latest Event" in self.selector

    async def click(self, timeout: int = 0) -> None:
        self.page.clicks.append(self.selector)
        self.page.details_visible = True

    async def wait_for(self, state: str | None = None, timeout: int = 0) -> None:
        if state == "visible" and "hal-event-tracking" in self.selector:
            if not self.page.details_visible:
                raise TimeoutError("details still collapsed")


class _ExpandPage:
    def __init__(self) -> None:
        self.details_visible = False
        self.clicks: list[str] = []

    def locator(self, selector: str) -> _ExpandLocator:
        return _ExpandLocator(self, selector)

    async def wait_for_timeout(self, ms: int) -> None:
        return None


def test_expand_result_details_clicks_chevron_before_screenshot():
    page = _ExpandPage()
    tracker = HapagTracker(page)
    asyncio.run(tracker.expand_result_details())
    assert page.details_visible is True
    assert any("q-btn--icon-only" in selector for selector in page.clicks)
