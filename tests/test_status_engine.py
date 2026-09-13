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


def test_transshipment_pol_is_first_load():
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
    assert result.atd == "2026-09-02"
    assert result.vessel == "FEEDER ONE"


def test_transshipment_atd_is_first_ocean_departure():
    """FANU1965730: feeder left Qasim 08-07; mother left Salalah 08-18. ATD is origin."""
    events = [
        ev(
            type="LOAD",
            location_raw="MUHAMMAD BIN QASIM",
            event_date="2026-08-07",
            event_time="03:20",
            sequence_index=4,
            vessel="BSG BIMINI",
            voyage="632W",
            raw_text="Loaded MUHAMMAD BIN QASIM",
        ),
        ev(
            type="DEPA",
            location_raw="MUHAMMAD BIN QASIM",
            event_date="2026-08-07",
            event_time="12:01",
            sequence_index=5,
            vessel="BSG BIMINI",
            voyage="632W",
            raw_text="Vessel departed MUHAMMAD BIN QASIM",
        ),
        ev(
            type="LOAD",
            location_raw="SALALAH",
            event_date="2026-08-18",
            event_time="01:59",
            sequence_index=8,
            vessel="TUCAPEL",
            voyage="6132",
            raw_text="Loaded SALALAH",
        ),
        ev(
            type="DEPA",
            location_raw="SALALAH",
            event_date="2026-08-18",
            event_time="07:23",
            sequence_index=9,
            vessel="TUCAPEL",
            voyage="6132",
            raw_text="Vessel departed SALALAH",
        ),
    ]
    result = _eval(events, container="FANU1965730")
    assert result.status == "SAILED"
    assert result.pol == "MUHAMMAD BIN QASIM"
    assert result.atd == "2026-08-07 12:01"
    assert result.vessel == "BSG BIMINI"
    assert result.voyage == "632W"


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


def test_ymja_on_board_is_sailed_with_atd():
    events = [
        ev(
            type="LOAD",
            location_raw="VUNG TAU",
            event_date="2026-08-25",
            event_time="12:48",
            sequence_index=0,
            vessel="ONE MANHATTAN",
            voyage="046E",
            raw_text="On Board VUNG TAU ONE MANHATTAN 046E",
        )
    ]
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
    assert result.pol == "VUNG TAU"
    assert result.vessel == "ONE MANHATTAN"


def test_other_carrier_on_board_is_not_sailed():
    events = [
        ev(
            type="LOAD",
            location_raw="YANTIAN",
            event_date="2026-08-25",
            event_time="12:48",
            sequence_index=0,
            vessel="ONE MANHATTAN",
            voyage="046E",
            raw_text="On Board YANTIAN",
        )
    ]
    result = _eval(events)
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.sailed is False
    assert result.atd is None


def test_discharge_at_other_port_without_departure_is_sailed():
    events = [
        ev(
            type="LOAD",
            location_raw="SHANGHAI",
            event_date="2026-06-01",
            sequence_index=1,
            vessel="MSC BETTINA",
            voyage="QX621W",
            raw_text="Export Loaded on Vessel SHANGHAI",
        ),
        ev(
            type="DISC",
            location_raw="ASHDOD",
            event_date="2026-07-09",
            sequence_index=0,
            vessel="MSC BETTINA",
            voyage="QX621W",
            raw_text="Import Discharged from Vessel ASHDOD",
        ),
    ]
    result = evaluate(
        events,
        container="MEDU9474359",
        carrier="MSCU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.sailed is True
    assert result.atd is None
    assert result.pol == "SHANGHAI"
    assert result.vessel == "MSC BETTINA"


def test_long_ocean_voyage_keeps_export_load_when_inbound_voyage_changes():
    """MSDU7659068: Haiphong 12E load, Savannah 12W discharge 47 days later."""
    events = [
        ev(
            type="OTHER",
            location_raw="Savannah, US",
            event_date="2026-09-13",
            sequence_index=0,
            transport_mode="UNKNOWN",
            raw_text="13/09/2026 | Full Available for Delivery | Savannah, US | LADEN",
        ),
        ev(
            type="DISC",
            location_raw="Savannah, US",
            event_date="2026-09-13",
            sequence_index=1,
            vessel="ZIM MOUNT KILIMANJARO",
            voyage="12W",
            raw_text="13/09/2026 | Import Discharged from Vessel | Savannah, US | ZIM MOUNT KILIMANJARO 12W",
        ),
        ev(
            type="LOAD",
            location_raw="Haiphong, VN",
            event_date="2026-07-28",
            sequence_index=3,
            vessel="ZIM MOUNT KILIMANJARO",
            voyage="12E",
            raw_text="28/07/2026 | Export Loaded on Vessel | Haiphong, VN | ZIM MOUNT KILIMANJARO 12E",
        ),
        ev(
            type="DEPA",
            location_raw="Haiphong, VN",
            event_date="2026-07-28",
            sequence_index=3,
            vessel="ZIM MOUNT KILIMANJARO",
            voyage="12E",
            raw_text="28/07/2026 | Export Loaded on Vessel | Haiphong, VN | ZIM MOUNT KILIMANJARO 12E",
        ),
        ev(
            type="GTOT",
            location_raw="Haiphong, VN",
            event_date="2026-07-20",
            sequence_index=5,
            empty=True,
            transport_mode="VESSEL",
            raw_text="20/07/2026 | Empty to Shipper | Haiphong, VN | EMPTY",
        ),
    ]
    result = evaluate(
        events,
        container="MSDU7659068",
        carrier="MSCU",
        timeline_order="newest_first",
        checked_at="2026-09-13 21:06:41",
    )
    assert result.status == "SAILED"
    assert result.loaded is True
    assert result.sailed is True
    assert result.pol == "HAI PHONG"
    assert result.vessel == "ZIM MOUNT KILIMANJARO"
    assert result.voyage == "12E"
    assert result.atd == "2026-07-28"


def test_same_port_discharge_without_departure_is_not_sailed():
    events = [
        ev(
            type="LOAD",
            location_raw="YANTIAN",
            event_date="2026-09-10",
            sequence_index=0,
            raw_text="Loaded YANTIAN",
        ),
        ev(
            type="DISC",
            location_raw="YANTIAN",
            event_date="2026-09-11",
            sequence_index=1,
            raw_text="Discharged YANTIAN",
        ),
    ]
    result = _eval(events)
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.sailed is False


def test_no_dates_is_ambiguous():
    events = [
        ev(type="LOAD", location_raw="YANTIAN", sequence_index=0, raw_text="Loaded"),
        ev(type="DEPA", location_raw="YANTIAN", sequence_index=1, raw_text="Departed"),
    ]
    result = _eval(events)
    assert result.status == "MANUAL_CHECK_REQUIRED"
    assert result.error_code == "AMBIGUOUS_JOURNEY"
