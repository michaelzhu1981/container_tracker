from event_text import classify_event_type, parse_timestamp


def test_one_and_msc_event_types():
    assert classify_event_type("Empty Container Release to Shipper") == "GTOT"
    assert classify_event_type("Gate In to Outbound Terminal") == "GTIN"
    assert classify_event_type("Loaded on Vessel at Port of Loading") == "LOAD"
    assert classify_event_type("Vessel Departure from Port of Loading") == "DEPA"
    assert classify_event_type("Empty Container Returned from Customer") == "GTIN"
    assert classify_event_type("Empty to Shipper") == "GTOT"
    assert classify_event_type("Export received at CY") == "GTIN"
    assert classify_event_type("Export Loaded on Vessel") == "LOAD"
    assert classify_event_type("Import Discharged from Vessel") == "DISC"
    assert classify_event_type("Import to consignee") == "GTOT"
    assert classify_event_type("Empty received at CY") == "GTIN"
    assert classify_event_type("Vessel Arrival at Port of Discharge") == "ARRI"
    assert classify_event_type("Unloaded from Vessel at Port of Discharging") == "DISC"


def test_parse_one_and_msc_dates():
    _, day, time = parse_timestamp("2026-07-19 00:58")
    assert (day, time) == ("2026-07-19", "00:58")
    _, day, time = parse_timestamp("01/06/2026")
    assert (day, time) == ("2026-06-01", None)
    _, day, time = parse_timestamp("202607271544")
    assert (day, time) == ("2026-07-27", "15:44")
