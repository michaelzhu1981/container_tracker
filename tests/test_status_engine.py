from models import CanonicalEvent
from status_engine import evaluate, select_latest_journey


def ev(**kwargs) -> CanonicalEvent:
    defaults = dict(
        classifier="ACT",
        type="OTHER",
        location_raw="",
        location_norm="",
        timestamp_raw="",
        event_date=None,
        event_time=None,
        sequence_index=0,
        vessel=None,
        voyage=None,
        booking=None,
        empty=None,
        transport_mode="VESSEL",
        raw_text="",
    )
    defaults.update(kwargs)
    return CanonicalEvent(**defaults)  # type: ignore[arg-type]


def _eval(events, container="HLXU1234567"):
    return evaluate(
        events,
        container=container,
        carrier="HLCU",
        timeline_order="oldest_first",
        checked_at="2026-09-12 00:00:00",
    )


def test_same_day_load_then_depart_without_time_is_sailed():
    events = [
        ev(
            type="LOAD",
            location_raw="YANTIAN",
            event_date="2026-09-11",
            sequence_index=0,
            vessel="MONTEVIDEO EXPRESS",
            voyage="2632E",
            raw_text="Loaded YANTIAN",
        ),
        ev(
            type="DEPA",
            location_raw="YANTIAN",
            event_date="2026-09-11",
            sequence_index=1,
            vessel="MONTEVIDEO EXPRESS",
            voyage="2632E",
            raw_text="Vessel departed YANTIAN",
        ),
    ]
    result = _eval(events)
    assert result.status == "SAILED"
    assert result.loaded is True
    assert result.sailed is True
    assert result.pol == "YANTIAN"
    assert result.atd == "2026-09-11"


def test_barge_departure_is_not_sailed():
    events = [
        ev(
            type="DEPA",
            location_raw="NANSHA",
            event_date="2026-09-10",
            sequence_index=0,
            transport_mode="BARGE",
            raw_text="Barge departed NANSHA",
        )
    ]
    result = _eval(events)
    assert result.status == "NOT_LOADED"
    assert result.sailed is False


def test_empty_load_is_not_sailed():
    events = [
        ev(
            type="LOAD",
            location_raw="YANTIAN",
            event_date="2026-09-10",
            sequence_index=0,
            empty=True,
            raw_text="Empty load YANTIAN",
        ),
        ev(
            type="DEPA",
            location_raw="YANTIAN",
            event_date="2026-09-10",
            sequence_index=1,
            empty=True,
            raw_text="Empty vessel departed YANTIAN",
        ),
    ]
    result = _eval(events)
    assert result.status == "NOT_LOADED"


def test_planned_departure_is_not_sailed():
    events = [
        ev(
            type="LOAD",
            location_raw="YANTIAN",
            event_date="2026-09-10",
            sequence_index=0,
            raw_text="Loaded YANTIAN",
        ),
        ev(
            classifier="PLN",
            type="DEPA",
            location_raw="YANTIAN",
            event_date="2026-09-20",
            sequence_index=1,
            raw_text="Planned vessel departure YANTIAN",
        ),
    ]
    result = _eval(events)
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.sailed is False


def test_cnytn_normalizes_to_yantian_pol():
    events = [
        ev(
            type="LOAD",
            location_raw="CNYTN",
            event_date="2026-09-10",
            sequence_index=0,
            raw_text="Loaded CNYTN",
        )
    ]
    result = _eval(events)
    assert result.pol == "YANTIAN"
    assert result.load_port == "YANTIAN"


def test_transshipment_pol_is_first_load_port_is_latest():
    events = [
        ev(
            type="LOAD",
            location_raw="YANTIAN",
            event_date="2026-09-01",
            sequence_index=0,
            booking="BK1",
            vessel="FEEDER ONE",
            voyage="001E",
        ),
        ev(
            type="DEPA",
            location_raw="YANTIAN",
            event_date="2026-09-02",
            sequence_index=1,
            booking="BK1",
            vessel="FEEDER ONE",
            voyage="001E",
        ),
        ev(
            type="LOAD",
            location_raw="SINGAPORE",
            event_date="2026-09-08",
            sequence_index=2,
            booking="BK1",
            vessel="MOTHER TWO",
            voyage="100W",
        ),
    ]
    result = _eval(events)
    assert result.status == "SAILED"
    assert result.pol == "YANTIAN"
    assert result.load_port == "SINGAPORE"
    assert result.vessel == "FEEDER ONE"


def test_transshipment_without_booking_keeps_origin_departure():
    events = [
        ev(
            type="LOAD",
            location_raw="MUHAMMAD BIN QASIM",
            event_date="2026-08-28",
            event_time="01:23",
            sequence_index=2,
            vessel="BSG BIMINI",
            voyage="635W",
            raw_text="Loaded MUHAMMAD BIN QASIM",
        ),
        ev(
            type="DEPA",
            location_raw="MUHAMMAD BIN QASIM",
            event_date="2026-08-28",
            event_time="12:48",
            sequence_index=3,
            vessel="BSG BIMINI",
            voyage="635W",
            raw_text="Vessel departed MUHAMMAD BIN QASIM",
        ),
        ev(
            type="LOAD",
            location_raw="SALALAH",
            event_date="2026-09-07",
            event_time="03:06",
            sequence_index=6,
            vessel="BREMEN EXPRESS",
            voyage="6135",
            raw_text="Loaded SALALAH",
        ),
        ev(
            classifier="PLN",
            type="DEPA",
            location_raw="SALALAH",
            event_date="2026-09-07",
            event_time="04:45",
            sequence_index=7,
            vessel="BREMEN EXPRESS",
            voyage="6135",
            raw_text="Vessel departed SALALAH",
        ),
    ]
    result = _eval(events)
    assert result.status == "SAILED"
    assert result.pol == "MUHAMMAD BIN QASIM"
    assert result.atd == "2026-08-28 12:48"
    assert result.load_port == "SALALAH"
    assert result.vessel == "BSG BIMINI"
    assert result.voyage == "635W"


def test_reuse_without_booking_uses_latest_voyage():
    events = [
        ev(
            type="LOAD",
            location_raw="ROTTERDAM",
            event_date="2025-01-01",
            sequence_index=0,
            vessel="OLD SHIP",
            voyage="001E",
            raw_text="Loaded ROTTERDAM",
        ),
        ev(
            type="DEPA",
            location_raw="ROTTERDAM",
            event_date="2025-01-02",
            sequence_index=1,
            vessel="OLD SHIP",
            voyage="001E",
            raw_text="Vessel departed ROTTERDAM",
        ),
        ev(
            type="LOAD",
            location_raw="YANTIAN",
            event_date="2026-09-10",
            sequence_index=2,
            vessel="NEW SHIP",
            voyage="100W",
            raw_text="Loaded YANTIAN",
        ),
    ]
    result = _eval(events)
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.pol == "YANTIAN"
    assert result.vessel == "NEW SHIP"


def test_reuse_gap_keeps_latest_cycle():
    events = [
        ev(
            type="DEPA",
            location_raw="ROTTERDAM",
            event_date="2025-01-01",
            sequence_index=0,
            booking="OLD",
            raw_text="Vessel departed ROTTERDAM",
        ),
        ev(
            type="LOAD",
            location_raw="YANTIAN",
            event_date="2026-09-10",
            sequence_index=1,
            booking="NEW",
            raw_text="Loaded YANTIAN",
        ),
    ]
    result = _eval(events)
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.pol == "YANTIAN"


def test_empty_return_splits_before_voyage_grouping():
    events = [
        ev(
            type="LOAD",
            location_raw="SHANGHAI",
            event_date="2026-07-05",
            event_time="10:30",
            sequence_index=3,
            vessel="YM UNIFORM",
            voyage="249E",
            raw_text="On Board SHANGHAI YM UNIFORM 249E",
        ),
        ev(
            type="DISC",
            location_raw="LOS ANGELES",
            event_date="2026-07-22",
            event_time="16:02",
            sequence_index=2,
            transport_mode="UNKNOWN",
            raw_text="Discharged LOS ANGELES",
        ),
        ev(
            type="GTIN",
            location_raw="LOS ANGELES",
            event_date="2026-08-06",
            event_time="09:03",
            sequence_index=0,
            empty=True,
            transport_mode="UNKNOWN",
            raw_text="Empty Returned LOS ANGELES",
        ),
    ]
    result = evaluate(
        events,
        container="YMMU6826189",
        carrier="YMJA",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "NOT_LOADED"
    assert result.loaded is False
    assert result.sailed is False


def test_empty_return_starts_new_cycle():
    events = [
        ev(
            type="DEPA",
            location_raw="ROTTERDAM",
            event_date="2026-08-01",
            sequence_index=0,
            raw_text="Vessel departed ROTTERDAM",
        ),
        ev(
            type="GTIN",
            location_raw="ROTTERDAM",
            event_date="2026-08-20",
            sequence_index=1,
            empty=True,
            transport_mode="TRUCK",
            raw_text="Empty return ROTTERDAM",
        ),
        ev(
            type="GTIN",
            location_raw="YANTIAN",
            event_date="2026-08-25",
            sequence_index=2,
            transport_mode="TRUCK",
            raw_text="Gate in YANTIAN",
        ),
    ]
    result = _eval(events)
    assert result.status == "NOT_LOADED"
    journey = select_latest_journey(events, "oldest_first")
    assert journey is not None
    assert [e.sequence_index for e in journey] == [2]


def test_unknown_transport_does_not_count_as_ocean():
    events = [
        ev(
            type="DEPA",
            location_raw="YANTIAN",
            event_date="2026-09-11",
            sequence_index=0,
            transport_mode="UNKNOWN",
            raw_text="Departed YANTIAN",
        )
    ]
    result = _eval(events)
    assert result.status == "NOT_LOADED"


def test_no_dates_is_ambiguous():
    events = [
        ev(type="LOAD", location_raw="YANTIAN", sequence_index=0, raw_text="Loaded"),
        ev(type="DEPA", location_raw="YANTIAN", sequence_index=1, raw_text="Departed"),
    ]
    result = _eval(events)
    assert result.status == "MANUAL_CHECK_REQUIRED"
    assert result.error_code == "AMBIGUOUS_JOURNEY"
