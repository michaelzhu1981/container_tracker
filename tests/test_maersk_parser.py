from pathlib import Path

from status_engine import evaluate
from trackers.maersk import parse_maersk_html, parse_maersk_json

FIXTURES = Path(__file__).parent / "fixtures" / "maersk"


def test_parse_sailed_fixture():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_maersk_html(html)
    assert [event.type for event in events] == ["GTIN", "LOAD", "DEPA"]
    assert events[0].empty is False
    assert events[1].vessel == "YM MANDATE"
    assert events[1].voyage == "046E"
    assert events[2].event_date == "2026-09-11"
    assert events[2].event_time == "03:40"
    result = evaluate(
        events,
        container="MSKU1234567",
        carrier="MAEU",
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
    events = parse_maersk_html(html)
    assert events[1].type == "LOAD"
    assert events[1].event_date == "2026-08-25"
    assert events[1].event_time == "12:48"
    assert events[2].classifier == "EST"
    result = evaluate(
        events,
        container="MSKU0024740",
        carrier="MAEU",
        timeline_order="oldest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.vessel == "ONE MANHATTAN"
    assert result.sailed is False


def test_parse_empty_returned_is_not_loaded():
    html = (FIXTURES / "empty_returned.html").read_text(encoding="utf-8")
    events = parse_maersk_html(html)
    assert events[-1].type == "GTIN"
    assert events[-1].empty is True
    result = evaluate(
        events,
        container="MSKU7654321",
        carrier="MAEU",
        timeline_order="oldest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "NOT_LOADED"
    assert result.sailed is False


def test_parse_synergy_json_directly():
    events = parse_maersk_json(
        {
            "containers": [
                {
                    "locations": [
                        {
                            "city": "Yantian",
                            "events": [
                                {
                                    "activity": "Loaded",
                                    "event_time": "2026-09-10T18:20:00Z",
                                    "vessel_name": "MAERSK ESSEN",
                                    "voyage_num": "123W",
                                },
                                {
                                    "activity": "Vessel Departed",
                                    "event_time": "2026-09-11T08:00:00Z",
                                    "vessel_name": "MAERSK ESSEN",
                                    "voyage_num": "123W",
                                },
                            ],
                        }
                    ]
                }
            ]
        }
    )
    assert [event.type for event in events] == ["LOAD", "DEPA"]
    assert events[1].vessel == "MAERSK ESSEN"
    assert events[1].voyage == "123W"
