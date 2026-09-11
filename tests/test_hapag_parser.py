from pathlib import Path

from status_engine import evaluate
from trackers.hapag import parse_hapag_html

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
