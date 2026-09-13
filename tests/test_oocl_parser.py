from pathlib import Path

from status_engine import evaluate
from trackers.oocl import parse_oocl_html

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
