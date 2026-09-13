from pathlib import Path

from status_engine import evaluate
from trackers.yangming import parse_yangming_html

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
