from pathlib import Path

from status_engine import evaluate
from trackers.zim import json_mentions_container, parse_zim_html, parse_zim_payload

FIXTURES = Path(__file__).parent / "fixtures" / "zim"


def test_parse_sailed_fixture():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_zim_html(html)
    assert events[0].type == "DEPA"
    assert events[0].vessel == "ZIM NEW YORK"
    assert events[0].voyage == "046E"
    result = evaluate(
        events,
        container="TCNU3698035",
        carrier="ZIMU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "YANTIAN"
    assert result.atd == "2026-09-11 03:40"


def test_parse_embedded_json_on_board_waiting():
    html = (FIXTURES / "on_board_waiting.html").read_text(encoding="utf-8")
    events = parse_zim_html(html)
    assert events[0].type == "LOAD"
    assert events[0].vessel == "ZIM NEW YORK"
    result = evaluate(
        events,
        container="TCNU3698035",
        carrier="ZIMU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.pol == "YANTIAN"


def test_parse_zim_payload():
    events = parse_zim_payload(
        {
            "unitActivityList": [
                {
                    "activityDesc": "Vessel Departure",
                    "activityDateTz": "2026-09-11 03:40:00",
                    "placeFromDesc": "YANTIAN",
                    "vesselName": "ZIM NEW YORK",
                    "voyage": "046E",
                }
            ]
        }
    )
    assert events[0].type == "DEPA"
    assert events[0].voyage == "046E"


def test_zim_payload_mentions_container():
    payload = {"unitActivityList": [{"activityDesc": "Loaded"}], "container": "TCNU3698035"}
    assert json_mentions_container(payload, "TCNU3698035") is True
    assert json_mentions_container(payload, "OOLU6895702") is False
