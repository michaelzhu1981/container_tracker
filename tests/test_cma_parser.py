from pathlib import Path

from status_engine import evaluate
from trackers.cma import parse_cma_html

FIXTURES = Path(__file__).parent / "fixtures" / "cma"


def test_parse_sailed_fixture():
    html = (FIXTURES / "sailed.html").read_text(encoding="utf-8")
    events = parse_cma_html(html)
    assert [event.type for event in events] == ["GTOT", "GTIN", "LOAD", "DEPA", "ARRI"]
    assert events[1].type == "GTIN"
    assert events[2].vessel == "YM MANDATE"
    assert events[2].voyage == "046E"
    assert events[3].type == "DEPA"
    assert events[4].classifier == "EST"
    result = evaluate(
        events,
        container="CMAU1234567",
        carrier="CMDU",
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
    events = parse_cma_html(html)
    assert events[0].type == "GTIN"
    assert events[1].type == "LOAD"
    assert events[1].event_date == "2026-08-25"
    assert events[1].event_time == "12:48"
    assert events[1].vessel == "ONE MANHATTAN"
    assert events[2].classifier == "EST"
    result = evaluate(
        events,
        container="CMAU0024740",
        carrier="CMDU",
        timeline_order="oldest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "LOADED_WAITING_DEPARTURE"
    assert result.vessel == "ONE MANHATTAN"
    assert result.sailed is False


def test_parse_empty_returned_is_not_loaded():
    html = (FIXTURES / "empty_returned.html").read_text(encoding="utf-8")
    events = parse_cma_html(html)
    result = evaluate(
        events,
        container="CMAU7654321",
        carrier="CMDU",
        timeline_order="oldest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "NOT_LOADED"
    assert result.sailed is False


def test_parse_embedded_response_data():
    html = """
    <script>
    must.tracking.searchdetails.init({
      responseData: "{\\"ContainerReference\\":\\"CMAU1111111\\",\\"PastMoves\\":[{\\"StatusDescription\\":\\"EMPTY TO SHIPPER\\",\\"DateString\\":\\"08-SEP-2026\\",\\"TimeString\\":\\"10:00 AM\\",\\"Location\\":\\"YANTIAN\\",\\"ModeOfTransport\\":\\"TRUCK\\",\\"State\\":\\"done\\"}],\\"CurrentMoves\\":[{\\"StatusDescription\\":\\"LOADED ON BOARD\\",\\"DateString\\":\\"10-SEP-2026\\",\\"TimeString\\":\\"06:20 PM\\",\\"Location\\":\\"YANTIAN\\",\\"Vessel\\":\\"YM MANDATE\\",\\"Voyage\\":\\"046E\\",\\"ModeOfTransport\\":\\"VESSEL\\",\\"State\\":\\"current\\"},{\\"StatusDescription\\":\\"VESSEL DEPARTURE\\",\\"DateString\\":\\"11-SEP-2026\\",\\"TimeString\\":\\"03:40 AM\\",\\"Location\\":\\"YANTIAN\\",\\"Vessel\\":\\"YM MANDATE\\",\\"Voyage\\":\\"046E\\",\\"ModeOfTransport\\":\\"VESSEL\\",\\"State\\":\\"current\\"}],\\"ProvisionalMoves\\":[]}"
    });
    </script>
    """
    events = parse_cma_html(html)
    assert [event.type for event in events] == ["GTOT", "LOAD", "DEPA"]
    assert events[1].vessel == "YM MANDATE"
    assert events[2].voyage == "046E"
    result = evaluate(
        events,
        container="CMAU1111111",
        carrier="CMDU",
        timeline_order="oldest_first",
        checked_at="2026-09-13 00:00:00",
    )
    assert result.status == "SAILED"
    assert result.atd == "2026-09-11 03:40"


def test_parse_kendo_grid_rows():
    html = """
    <table id="gridTrackingDetails">
      <tr class="k-master-row done">
        <td class="date"><span class="calendar">Thursday, 10-SEP-2026</span><span class="time">06:20 PM</span></td>
        <td><span class="capsule">LOADED ON BOARD</span></td>
        <td class="location">YANTIAN</td>
        <td class="vesselVoyage">YM MANDATE (046E)</td>
      </tr>
      <tr class="k-master-row current">
        <td class="date"><span class="calendar">Friday, 11-SEP-2026</span><span class="time">03:40 AM</span></td>
        <td><span class="capsule">VESSEL DEPARTURE</span></td>
        <td class="location">YANTIAN</td>
        <td class="vesselVoyage">YM MANDATE (046E)</td>
      </tr>
    </table>
    """
    events = parse_cma_html(html)
    assert [event.type for event in events] == ["LOAD", "DEPA"]
    assert events[0].event_time == "18:20"
    assert events[1].event_time == "03:40"
    assert events[1].voyage == "046E"
