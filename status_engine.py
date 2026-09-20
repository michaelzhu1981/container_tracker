"""Journey selection and loaded/sailed status. No Playwright."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from event_text import format_event_time, is_empty_return, is_on_board
from models import CanonicalEvent, CheckResult, Status, TimelineOrder, TrackResult
from ports import display_port, normalize_key

OCEAN_MODES = {"MOTHER", "FEEDER", "VESSEL"}
SUCCESS_STATUS: dict[Status, CheckResult] = {
    "NOT_LOADED": "SUCCESS",
    "LOADED_WAITING_DEPARTURE": "SUCCESS",
    "SAILED": "SUCCESS",
    "MANUAL_CHECK_REQUIRED": "MANUAL",
    "CHECK_FAILED": "FAILED",
}


def later_key(event: CanonicalEvent, timeline_order: TimelineOrder) -> tuple:
    seq = event.sequence_index
    if timeline_order == "newest_first":
        seq = -seq
    return (event.event_date or "", event.event_time or "", seq)


def is_later(a: CanonicalEvent, b: CanonicalEvent, timeline_order: TimelineOrder) -> bool:
    return later_key(a, timeline_order) > later_key(b, timeline_order)


def _place_key(event: CanonicalEvent) -> str:
    return event.location_norm or normalize_key(event.location_raw)


def _same_place(left: CanonicalEvent, right: CanonicalEvent) -> bool:
    a = _place_key(left)
    b = _place_key(right)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def implies_sailed_without_departure(
    journey: list[CanonicalEvent],
    first_load: CanonicalEvent,
    timeline_order: TimelineOrder,
) -> bool:
    """True when an Actual discharge/arrival happens later at another port."""
    for event in journey:
        if event.classifier != "ACT" or event.type not in {"DISC", "ARRI"}:
            continue
        if not is_later(event, first_load, timeline_order):
            continue
        if _same_place(event, first_load):
            continue
        return True
    return False


def is_laden_ocean(event: CanonicalEvent) -> bool:
    if event.classifier != "ACT":
        return False
    if event.type not in ("LOAD", "DEPA"):
        return False
    if event.transport_mode not in OCEAN_MODES:
        return False
    if event.empty is True:
        return False
    return True


def _is_evergreen_loaded_fcl_on_vessel(event: CanonicalEvent) -> bool:
    compact = "".join(event.raw_text.lower().split())
    return "loaded(fcl)onvessel" in compact


def _is_evergreen_sailed_without_origin_details(event: CanonicalEvent) -> bool:
    compact = "".join(event.raw_text.lower().split())
    return any(
        marker in compact
        for marker in (
            "transshipcontainerloadedonvessel",
            "emptycontainerreturned",
            "pick-upbymerchanthaulage",
            "received(fcl)",
            "discharged(fcl)",
            "dischargedandwaitingfortransshipping",
        )
    )


def _latest(events: Iterable[CanonicalEvent], timeline_order: TimelineOrder) -> CanonicalEvent | None:
    dated = list(events)
    if not dated:
        return None
    return max(dated, key=lambda e: later_key(e, timeline_order))


def _earliest(events: Iterable[CanonicalEvent], timeline_order: TimelineOrder) -> CanonicalEvent | None:
    dated = list(events)
    if not dated:
        return None
    return min(dated, key=lambda e: later_key(e, timeline_order))


def _date_value(event: CanonicalEvent) -> datetime | None:
    if not event.event_date:
        return None
    return datetime.strptime(event.event_date, "%Y-%m-%d")


def _group_latest(
    events: list[CanonicalEvent],
    key_fn,
    timeline_order: TimelineOrder,
) -> list[CanonicalEvent] | None:
    groups: dict[str, list[CanonicalEvent]] = {}
    for event in events:
        key = key_fn(event)
        if not key:
            continue
        groups.setdefault(key, []).append(event)
    if not groups:
        return None
    best_key = None
    best_event = None
    for key, group in groups.items():
        candidate = _latest(group, timeline_order)
        if candidate is None:
            continue
        if best_event is None or is_later(candidate, best_event, timeline_order):
            best_event = candidate
            best_key = key
    return groups.get(best_key or "", None)


def _cluster_by_gap(
    events: list[CanonicalEvent], timeline_order: TimelineOrder
) -> list[CanonicalEvent]:
    dated = [e for e in events if e.event_date]
    if not dated:
        return events
    ordered = sorted(dated, key=lambda e: later_key(e, timeline_order))
    clusters: list[list[CanonicalEvent]] = [[ordered[0]]]
    for event in ordered[1:]:
        prev = clusters[-1][-1]
        prev_d = _date_value(prev)
        cur_d = _date_value(event)
        if prev_d and cur_d and (cur_d - prev_d) > timedelta(days=30):
            if event.type in {"DISC", "ARRI"}:
                clusters[-1].append(event)
            else:
                clusters.append([event])
        else:
            clusters[-1].append(event)
    latest_event = _latest(dated, timeline_order)
    chosen = clusters[-1]
    for cluster in clusters:
        if latest_event in cluster:
            chosen = cluster
            break
    seqs = {e.sequence_index for e in chosen}
    undated = [e for e in events if not e.event_date]
    if seqs:
        lo, hi = min(seqs), max(seqs)
        chosen = chosen + [e for e in undated if lo <= e.sequence_index <= hi]
    else:
        chosen = chosen + undated
    chosen.sort(key=lambda e: e.sequence_index)
    return chosen


def _destination_only_voyage_group(
    latest_group: list[CanonicalEvent],
    pool: list[CanonicalEvent],
    timeline_order: TimelineOrder,
) -> bool:
    """True when the newest vessel/voyage is just POD discharge of an earlier load."""
    if any(is_laden_ocean(event) and event.type == "LOAD" for event in latest_group):
        return False
    if not any(
        event.classifier == "ACT" and event.type in {"DISC", "ARRI"}
        for event in latest_group
    ):
        return False
    loads = [event for event in pool if is_laden_ocean(event) and event.type == "LOAD"]
    first_load = _earliest(loads, timeline_order)
    return first_load is not None and implies_sailed_without_departure(
        pool, first_load, timeline_order
    )


def _is_single_gap_cluster(
    events: list[CanonicalEvent], timeline_order: TimelineOrder
) -> bool:
    """True when dated events sit in one 30-day window (transshipment, not reuse)."""
    dated = [e for e in events if e.event_date]
    if len(dated) <= 1:
        return True
    ordered = sorted(dated, key=lambda e: later_key(e, timeline_order))
    for prev, current in zip(ordered, ordered[1:]):
        prev_d = _date_value(prev)
        cur_d = _date_value(current)
        if prev_d and cur_d and (cur_d - prev_d) > timedelta(days=30):
            return False
    return True


def _apply_empty_return_split(
    events: list[CanonicalEvent],
    timeline_order: TimelineOrder,
    *,
    preserve_completed: bool = False,
) -> list[CanonicalEvent]:
    boundaries = [e for e in events if is_empty_return(e)]
    if not boundaries:
        return events
    last = _latest(boundaries, timeline_order)
    if last is None:
        return events
    after = [e for e in events if is_later(e, last, timeline_order)]
    if after:
        # A later event means the returned box has started another cycle.
        return after
    if not preserve_completed:
        return []

    # OOCL keeps the completed shipment in the same result drawer after empty
    # return. If no newer cycle has begun, retain that voyage so Loaded, Sailed,
    # POL and ATD describe the shipment rather than the box's current emptiness.
    before = [e for e in events if e is not last and not is_later(e, last, timeline_order)]
    ocean = [e for e in before if is_laden_ocean(e)]
    if any(event.type == "DEPA" for event in ocean):
        return events
    loads = [event for event in ocean if event.type == "LOAD"]
    if any(
        implies_sailed_without_departure(before, load, timeline_order)
        for load in loads
    ):
        return events
    return []


def select_latest_journey(
    events: list[CanonicalEvent],
    timeline_order: TimelineOrder = "oldest_first",
    *,
    preserve_completed_after_return: bool = False,
) -> list[CanonicalEvent] | None:
    if not events:
        return []
    act = [e for e in events if e.classifier == "ACT"]
    pool = act or events
    if any(is_empty_return(e) for e in pool):
        pool = _apply_empty_return_split(
            pool,
            timeline_order,
            preserve_completed=preserve_completed_after_return,
        )
        if not pool:
            return []

    grouped = _group_latest(
        pool, lambda e: (e.booking or "").strip().upper() or None, timeline_order
    )
    if grouped is None:
        voyage_grouped = _group_latest(
            pool,
            lambda e: (
                f"{(e.vessel or '').strip().upper()}|{(e.voyage or '').strip().upper()}"
                if (e.vessel and e.voyage)
                else None
            ),
            timeline_order,
        )
        # One booking can change vessel/voyage at transshipment. Do not drop the
        # origin Actual DEPA just because a later mother-vessel group exists.
        if voyage_grouped is not None and not _is_single_gap_cluster(pool, timeline_order):
            if _destination_only_voyage_group(voyage_grouped, pool, timeline_order):
                grouped = pool
            else:
                grouped = voyage_grouped
    if grouped is None:
        if not any(e.event_date for e in pool):
            return None
        grouped = _cluster_by_gap(pool, timeline_order)

    return _apply_empty_return_split(
        grouped,
        timeline_order,
        preserve_completed=preserve_completed_after_return,
    )


def _latest_event_text(event: CanonicalEvent) -> str:
    location = event.location_norm or event.location_raw
    if event.raw_text.strip():
        text = re_short(event.raw_text)
        if location and location.lower() not in text.lower():
            return f"{text} {location}".strip()
        return text
    return f"{event.type} {location}".strip()


def re_short(text: str, limit: int = 80) -> str:
    compact = " ".join(text.split())
    return compact[:limit]


def evaluate(
    events: list[CanonicalEvent],
    *,
    container: str,
    carrier: str,
    timeline_order: TimelineOrder = "oldest_first",
    checked_at: str,
    screenshot_path: str | None = None,
    html_path: str | None = None,
    error_code: str | None = None,
    error: str | None = None,
    forced_status: Status | None = None,
) -> TrackResult:
    result = TrackResult(
        container=container,
        carrier=carrier,
        checked_at=checked_at,
        screenshot_path=screenshot_path,
        html_path=html_path,
        events=events,
    )
    if forced_status:
        return _finalize(result, forced_status, error_code, error)

    if carrier == "EGLV":
        actual = [event for event in events if event.classifier == "ACT"] or events
        latest = _latest(actual, timeline_order)
        if latest and _is_evergreen_sailed_without_origin_details(latest):
            result.loaded = True
            result.sailed = True
            result.vessel = latest.vessel
            result.voyage = latest.voyage
            result.latest_event = _latest_event_text(latest)
            # The anonymous EGLV response exposes only the latest event. A
            # A downstream handling event proves the ocean journey has sailed,
            # but cannot identify its origin load port or departure time.
            result.pol = None
            result.atd = None
            return _finalize(result, "SAILED", None, None)

    journey = select_latest_journey(
        events,
        timeline_order,
        # OOCL and HMM retain the completed shipment timeline after the
        # consignee returns the empty box. Preserve a proven ocean departure
        # so the shipment remains SAILED rather than becoming NOT_LOADED.
        preserve_completed_after_return=carrier in {"OOLU", "HDMU"},
    )
    if journey is None:
        return _finalize(
            result,
            "MANUAL_CHECK_REQUIRED",
            "AMBIGUOUS_JOURNEY",
            "Cannot separate container reuse cycles without event dates.",
        )
    if not journey:
        result.loaded = False
        result.sailed = False
        empty_returns = [e for e in events if e.classifier == "ACT" and is_empty_return(e)]
        last_return = _latest(empty_returns, timeline_order)
        if last_return:
            result.latest_event = _latest_event_text(last_return)
        return _finalize(result, "NOT_LOADED", None, None)

    ocean = [e for e in journey if is_laden_ocean(e)]
    depa = [e for e in ocean if e.type == "DEPA"]
    load = [e for e in ocean if e.type == "LOAD"]

    if depa:
        chosen = _earliest(depa, timeline_order)
        result.sailed = True
        result.loaded = True
        result.atd = format_event_time(chosen) if chosen else None
        result.vessel = chosen.vessel if chosen else None
        result.voyage = chosen.voyage if chosen else None
        first_load = _earliest(load, timeline_order)
        last_load = _latest(load, timeline_order)
        if last_load:
            if not result.vessel:
                result.vessel = last_load.vessel
            if not result.voyage:
                result.voyage = last_load.voyage
        if first_load:
            result.pol = display_port(first_load.location_raw) or first_load.location_norm
        status: Status = "SAILED"
    elif load:
        chosen = _latest(load, timeline_order)
        first_load = _earliest(load, timeline_order)
        result.loaded = True
        result.vessel = chosen.vessel if chosen else None
        result.voyage = chosen.voyage if chosen else None
        if first_load:
            result.pol = display_port(first_load.location_raw) or first_load.location_norm
        on_board = None
        if carrier == "YMJA":
            on_board = _earliest([e for e in load if is_on_board(e)], timeline_order)
        elif carrier == "EGLV":
            on_board = _earliest(
                [e for e in load if _is_evergreen_loaded_fcl_on_vessel(e)],
                timeline_order,
            )
        if first_load and implies_sailed_without_departure(journey, first_load, timeline_order):
            result.sailed = True
            status = "SAILED"
            if on_board:
                result.atd = format_event_time(on_board)
        elif on_board:
            result.sailed = True
            result.atd = format_event_time(on_board)
            status = "SAILED"
        else:
            result.sailed = False
            status = "LOADED_WAITING_DEPARTURE"
    else:
        result.loaded = False
        result.sailed = False
        status = "NOT_LOADED"

    act_for_latest = [e for e in journey if e.classifier == "ACT"] or journey
    latest = _latest(act_for_latest, timeline_order)
    if latest:
        result.latest_event = _latest_event_text(latest)

    return _finalize(result, status, None, None)


def _finalize(
    result: TrackResult,
    status: Status,
    error_code: str | None,
    error: str | None,
) -> TrackResult:
    result.status = status
    result.check_result = SUCCESS_STATUS[status]
    result.success = result.check_result == "SUCCESS"
    result.error_code = error_code
    result.error = error
    if not result.success:
        result.loaded = None
        result.sailed = None
        result.pol = None
        result.vessel = None
        result.voyage = None
        result.atd = None
        if status != "MANUAL_CHECK_REQUIRED":
            result.latest_event = None
    return result
