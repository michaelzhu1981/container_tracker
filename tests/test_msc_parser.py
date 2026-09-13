from pathlib import Path

from status_engine import evaluate
from trackers.msc import parse_msc_html

FIXTURES = Path(__file__).parent / "fixtures" / "msc"


def test_parse_sailed_fixture():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_msc_html(html)
    assert [event.type for event in events] == ["DEPA", "LOAD", "GTIN"]
    assert events[0].vessel == "YM MANDATE"
    assert events[0].voyage == "046E"
    result = evaluate(
        events,
        container="MSCU1234567",
        carrier="MSCU",
        timeline_order="newest_first",
        checked_at="2026-09-12 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.pol == "YANTIAN"
    assert result.atd == "2026-09-11"
    assert result.vessel == "YM MANDATE"
    assert result.voyage == "046E"


def test_parse_export_loaded_counts_as_sailed():
    html = (FIXTURES / "on_board_waiting.html").read_text(encoding="utf-8")
    events = parse_msc_html(html)
    assert events[0].type == "LOAD"
    assert events[0].event_date == "2026-08-25"
    assert events[0].vessel == "ONE MANHATTAN"
    assert any(event.type == "DEPA" and event.event_date == "2026-08-25" for event in events)
    result = evaluate(
        events,
        container="MEDU1111111",
        carrier="MSCU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.atd == "2026-08-25"
    assert result.vessel == "ONE MANHATTAN"
    assert result.pol == "YANTIAN"


def test_parse_empty_returned_is_not_loaded():
    html = (FIXTURES / "empty_returned.html").read_text(encoding="utf-8")
    events = parse_msc_html(html)
    assert events[0].type == "GTIN"
    assert events[0].empty is True
    result = evaluate(
        events,
        container="MEDU2222222",
        carrier="MSCU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "NOT_LOADED"
    assert result.sailed is False


def test_skips_empty_alpine_template_before_vessel():
    html = """
    <div class="msc-flow-tracking__step">
      <div class="msc-flow-tracking__cell--two"><span class="data-value">01/06/2026</span></div>
      <div class="msc-flow-tracking__cell--three"><span class="data-value">Shanghai, CN</span></div>
      <div class="msc-flow-tracking__cell--four"><span class="data-value">Export Loaded on Vessel</span></div>
      <div class="msc-flow-tracking__cell--five">
        <span class="data-value"><span></span></span>
        <span class="data-value"><span>MSC BETTINA QX621W</span></span>
      </div>
    </div>
    """
    events = parse_msc_html(html)
    assert events[0].type == "LOAD"
    assert events[0].vessel == "MSC BETTINA"
    assert events[0].voyage == "QX621W"
    assert events[1].type == "DEPA"
    assert events[1].voyage == "QX621W"


def test_discharge_without_departure_is_sailed():
    html = (FIXTURES / "discharged_without_departure.html").read_text(encoding="utf-8")
    events = parse_msc_html(html)
    assert events[0].type == "DISC"
    assert events[1].type == "LOAD"
    assert events[1].voyage == "QX621W"
    assert events[1].vessel == "MSC BETTINA"
    result = evaluate(
        events,
        container="MEDU9474359",
        carrier="MSCU",
        timeline_order="newest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.atd == "2026-06-01"
    assert result.vessel == "MSC BETTINA"
    assert result.voyage == "QX621W"
    assert result.pol == "SHANGHAI"


def test_inbound_voyage_suffix_change_after_long_transit_is_sailed():
    html = """
    <div class="msc-flow-tracking__step">
      <div class="msc-flow-tracking__cell--two"><span class="data-value">13/09/2026</span></div>
      <div class="msc-flow-tracking__cell--three"><span class="data-value">Savannah, US</span></div>
      <div class="msc-flow-tracking__cell--four"><span class="data-value">Import Discharged from Vessel</span></div>
      <div class="msc-flow-tracking__cell--five"><span class="data-value">ZIM MOUNT KILIMANJARO 12W</span></div>
    </div>
    <div class="msc-flow-tracking__step">
      <div class="msc-flow-tracking__cell--two"><span class="data-value">28/07/2026</span></div>
      <div class="msc-flow-tracking__cell--three"><span class="data-value">Haiphong, VN</span></div>
      <div class="msc-flow-tracking__cell--four"><span class="data-value">Export Loaded on Vessel</span></div>
      <div class="msc-flow-tracking__cell--five"><span class="data-value">ZIM MOUNT KILIMANJARO 12E</span></div>
    </div>
    """
    events = parse_msc_html(html)
    result = evaluate(
        events,
        container="MSDU7659068",
        carrier="MSCU",
        timeline_order="newest_first",
        checked_at="2026-09-13 21:06:41",
    )
    assert result.status == "SAILED"
    assert result.pol in {"HAIPHONG", "HAIPHONG VN"}
    assert result.vessel == "ZIM MOUNT KILIMANJARO"
    assert result.atd == "2026-07-28"
