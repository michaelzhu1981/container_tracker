from pathlib import Path

from status_engine import evaluate
from trackers.one import parse_one_html

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
