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


def test_completed_hmm_journey_stays_sailed_after_empty_return():
    html = """
    <table>
      <tr><th>Date</th><th>Time</th><th>Location</th><th>Status Description</th><th>Mode</th></tr>
      <tr><td>2026-09-05</td><td>11:50</td><td>LOS ANGELES, CA</td><td>Import Empty Container Returned</td><td>Truck</td></tr>
      <tr><td>2026-09-02</td><td>08:50</td><td>LOS ANGELES, CA</td><td>Vessel Discharged at POD</td><td>HMM JAKARTA 0145E</td></tr>
      <tr><td>2026-08-15</td><td>17:12</td><td>HAI PHONG, VIETNAM</td><td>Vessel Departure from POL</td><td>HMM JAKARTA 0145E</td></tr>
      <tr><td>2026-08-15</td><td>04:05</td><td>HAI PHONG, VIETNAM</td><td>Vessel Loading at POL</td><td>HMM JAKARTA 0145E</td></tr>
      <tr><td>2026-08-10</td><td>06:50</td><td>HAI PHONG, VIETNAM</td><td>Export Empty Container Released</td><td>Truck</td></tr>
    </table>
    """
    events = parse_hmm_html(html)
    result = evaluate(
        events,
        container="TGBU6339574",
        carrier="HDMU",
        timeline_order="newest_first",
        checked_at="2026-09-20 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.loaded is True
    assert result.sailed is True
    assert result.pol == "HAI PHONG"
    assert result.atd == "2026-08-15 17:12"
    assert result.vessel == "HMM JAKARTA"
    assert result.voyage == "0145E"


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


@pytest.mark.asyncio
async def test_search_reloads_result_page_without_long_field_wait():
    gotos: list[str] = []
    submitted: list[str] = []
    result_waits: list[tuple[str, str, int]] = []
    form_waits: list[int] = []
    detail_probes: list[tuple[str, int]] = []

    class Locator:
        def __init__(self, selector: str):
            self.selector = selector
            self.first = self

        async def is_visible(self, timeout=0):
            detail_probes.append((self.selector, timeout))
            return False

    class Page:
        async def content(self):
            return "<html><body></body></html>"

        async def evaluate(self, script, arg=None):
            if "setTimeout(() => button.click()" in script:
                submitted.append(arg)
                return "result_page" if len(submitted) == 1 else "submitted"
            if "document.body && document.body.innerText" in script:
                return ""
            return False

        async def goto(self, url, wait_until=None):
            gotos.append(url)

        async def wait_for_function(self, script, arg=None, timeout=0):
            if "srchCntrNo1" in script:
                form_waits.append(timeout)
                return True
            result_waits.append((script, arg, timeout))
            return True

        def locator(self, selector):
            return Locator(selector)

    tracker = HmmTracker(Page())
    await tracker.search("HMMU4474348")

    assert len(gotos) == 1
    assert submitted == ["HMMU4474348", "HMMU4474348"]
    assert form_waits == [8_000]
    assert len(result_waits) == 1
    script, marker, timeout = result_waits[0]
    assert marker == "container-tracker:HMMU4474348"
    assert "resultContainer === needle" in script
    assert script.index("resultContainer === needle") < script.index(
        "__ctHmmDocumentMarker === documentMarker"
    )
    assert "hmmu4474348" in script
    assert timeout == 30_000
    assert detail_probes == [
        ("a.clsShowedMoves", 800),
        ("a:has-text('Display Previous Moves')", 800),
    ]


@pytest.mark.asyncio
async def test_expand_result_details_only_probes_once_per_result():
    probes: list[str] = []

    class Locator:
        def __init__(self, selector: str):
            self.selector = selector
            self.first = self

        async def is_visible(self, timeout=0):
            probes.append(self.selector)
            return False

    class Page:
        def locator(self, selector):
            return Locator(selector)

    tracker = HmmTracker(Page())
    await tracker.expand_result_details()
    await tracker.expand_result_details()
    await tracker.expand_result_details()

    assert probes == [
        "a.clsShowedMoves",
        "a:has-text('Display Previous Moves')",
    ]


@pytest.mark.asyncio
async def test_success_screenshot_uses_single_visible_system_chrome_crop(tmp_path):
    calls: list[dict] = []

    class Locator:
        def __init__(self, selector: str):
            self.selector = selector
            self.first = self

        async def is_visible(self, timeout=0):
            return self.selector == "#shipmentProgress"

    class Page:
        is_system_chrome = True

        def locator(self, selector):
            return Locator(selector)

        async def screenshot(self, **kwargs):
            calls.append(kwargs)

    path = tmp_path / "hmm.png"
    tracker = HmmTracker(Page())
    assert await tracker._screenshot_query_content(path) is True
    assert calls == [
        {
            "path": str(path),
            "selector": "#shipmentProgress",
            "single_view": True,
        }
    ]
