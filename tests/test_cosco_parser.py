from pathlib import Path

from status_engine import evaluate
from trackers.cosco import parse_cosco_html

FIXTURES = Path(__file__).parent / "fixtures" / "cosco"


def test_parse_sailed_fixture_synthesizes_pol_load():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_cosco_html(html)
    assert any(event.type == "DEPA" for event in events)
    assert any(event.type == "LOAD" for event in events)
    result = evaluate(
        events,
        container="CSNU6609294",
        carrier="COSU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "YANTIAN"
    assert result.atd == "2026-08-29 15:14"


def test_parse_on_board_waiting():
    html = (FIXTURES / "on_board_waiting.html").read_text(encoding="utf-8")
    events = parse_cosco_html(html)
    assert events[0].type == "LOAD"
    result = evaluate(
        events,
        container="CSNU6609294",
        carrier="COSU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.pol == "YANTIAN"
    assert result.sailed is False
