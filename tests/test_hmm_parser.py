from pathlib import Path

from status_engine import evaluate
from trackers.hmm import parse_hmm_html

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
