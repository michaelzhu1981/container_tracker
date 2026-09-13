import asyncio
from pathlib import Path

import pytest

from status_engine import evaluate
from trackers.maersk import (
    MaerskTracker,
    json_mentions_container,
    parse_maersk_html,
    parse_maersk_json,
)

FIXTURES = Path(__file__).parent / "fixtures" / "maersk"


@pytest.mark.asyncio
async def test_tracking_response_finishes_wait_without_dom_timeout():
    class Page:
        def __init__(self):
            self._ct_maersk_response_event = asyncio.Event()

        async def wait_for_function(self, *args, **kwargs):
            await asyncio.Event().wait()

    page = Page()
    tracker = MaerskTracker(page)
    waiting = asyncio.create_task(tracker._wait_for_results())
    await asyncio.sleep(0)
    page._ct_maersk_response_event.set()
    await asyncio.wait_for(waiting, timeout=0.2)


@pytest.mark.asyncio
async def test_leftover_vessel_departure_does_not_finish_wait():
    class Page:
        def __init__(self):
            self._ct_maersk_response_event = asyncio.Event()
            self.dom_script = ""

        async def wait_for_function(self, script, timeout=0, polling=None):
            self.dom_script = script
            await asyncio.Event().wait()

    page = Page()
    tracker = MaerskTracker(page)
    tracker._search_submitted = True
    waiting = asyncio.create_task(tracker._wait_for_results())
    await asyncio.sleep(0.05)
    assert not waiting.done()
    assert "vessel departure" not in page.dom_script.lower()
    page._ct_maersk_response_event.set()
    await asyncio.wait_for(waiting, timeout=0.2)


def test_json_mentions_container_ignores_other_box():
    payload = {"containers": [{"container_num": "HASU4566923"}]}
    assert json_mentions_container(payload, "HASU4566923")
    assert not json_mentions_container(payload, "TRHU6217353")


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


def _wire_event(activity, day, *, time_type="ACTUAL", empty=False, mode="MVS"):
    return {
        "activity": activity,
        "event_time": f"2026-09-{day:02d}T11:03:00.000",
        "event_time_type": time_type,
        "stempty": empty,
        "transport_mode": mode,
        "vessel_name": "TEST VESSEL" if mode == "MVS" else "",
        "voyage_num": "123E" if mode == "MVS" else "",
    }


def _wire_events(*items):
    return parse_maersk_json({
        "containers": [{"locations": [{"city": "Haiphong", "events": list(items)}]}]
    })


def _evaluate_wire(events):
    return evaluate(events, container="MSKU1234567", carrier="MAEU", checked_at="2026-09-13")


def test_wire_departure_provides_atd_and_excludes_expected_events():
    events = _wire_events(
        _wire_event("LOAD", 12),
        _wire_event("CONTAINER DEPARTURE", 13),
        _wire_event("CONTAINER ARRIVAL", 18, time_type="EXPECTED"),
    )
    assert [(e.type, e.classifier) for e in events] == [
        ("LOAD", "ACT"), ("DEPA", "ACT"), ("ARRI", "EST")
    ]
    result = _evaluate_wire(events)
    assert result.status == "SAILED"
    assert result.atd == "2026-09-13 11:03"
    assert result.vessel == "TEST VESSEL"
    assert "CONTAINER DEPARTURE" in result.latest_event


def test_expected_departure_does_not_mark_loaded_container_sailed():
    events = _wire_events(
        _wire_event("LOAD", 12),
        _wire_event("CONTAINER DEPARTURE", 18, time_type="EXPECTED"),
        _wire_event("CONTAINER ARRIVAL", 23, time_type="EXPECTED"),
    )
    assert _evaluate_wire(events).status == "LOADED_WAITING_DEPARTURE"


def test_wire_empty_return_ends_previous_journey():
    events = _wire_events(
        _wire_event("LOAD", 1),
        _wire_event("CONTAINER DEPARTURE", 2),
        _wire_event("CONTAINER RETURN", 12, empty=True, mode="TRK"),
    )
    assert events[-1].empty is True
    assert events[-1].type == "GTIN"
    result = _evaluate_wire(events)
    assert result.status == "NOT_LOADED"
    assert result.sailed is False
    assert "CONTAINER RETURN" in result.latest_event


def test_wire_truck_departure_is_not_ocean_departure():
    event = _wire_event("CONTAINER DEPARTURE", 12, mode="TRK")
    events = _wire_events(event)
    assert events[0].transport_mode == "TRUCK"
    assert _evaluate_wire(events).status == "NOT_LOADED"


def test_wire_discharge_and_customer_gate_out_codes():
    events = _wire_events(
        _wire_event("DISCHARG", 12),
        _wire_event("CUSTOMER_GATE_OUT", 13, mode="TRK"),
    )
    assert [e.type for e in events] == ["DISC", "GTOT"]
    assert events[1].empty is False


def test_unknown_wire_time_type_is_not_assumed_actual():
    events = _wire_events(_wire_event("CONTAINER DEPARTURE", 12, time_type="UNRECOGNIZED"))
    assert events[0].classifier == "UNKNOWN"
