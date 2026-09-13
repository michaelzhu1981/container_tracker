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
