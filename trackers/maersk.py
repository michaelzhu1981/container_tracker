"""Maersk public container tracking adapter."""

from __future__ import annotations

import json
import re

from challenges import CHALLENGE_CODE_JS
from event_text import (
    classify_classifier,
    classify_empty,
    classify_event_type,
    classify_transport,
    parse_timestamp,
)
from html_tables import parse_tables
from models import CanonicalEvent, Classifier
from ports import normalize_key
from trackers.base import BaseTracker, TrackerError

TRACK_URL = "https://www.maersk.com/tracking/"

_HEADER_HINTS = {
    "date": ("date", "time"),
    "status": ("event", "activity", "status", "description", "milestone"),
    "location": ("location", "place", "city", "facility"),
    "transport": ("vessel", "voyage", "transport"),
}

_VOYAGE_RE = re.compile(r"\b(\d{2,4}[A-Z])\b", re.I)
_SKIP_STATUS = frozenset(
    {"", "event", "activity", "status", "description", "milestone", "latest event"}
)
_JSON_STATUS_KEYS = ("activity", "event", "description", "status")
_JSON_DATE_KEYS = ("event_time", "eventtime", "date", "eventdate")
_JSON_LOC_KEYS = ("city", "location", "terminal", "facility")
_JSON_VSL_KEYS = ("vessel_name", "vesselname", "vessel")
_JSON_VOY_KEYS = ("voyage_num", "voyagenum", "voyage", "voyage_number")


def _header_index(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    lowered = [h.lower() for h in headers]
    for key, hints in _HEADER_HINTS.items():
        for idx, header in enumerate(lowered):
            if any(hint in header for hint in hints):
                mapping[key] = idx
                break
    return mapping


def _cell_text(row: list[dict], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]["text"]


def _voyage_and_vessel(mode_text: str) -> tuple[str | None, str | None]:
    blob = " ".join(mode_text.split())
    if not blob:
        return None, None
    match = _VOYAGE_RE.search(blob)
    voyage = match.group(1).upper() if match else None
    vessel = blob
    if match:
        vessel = (blob[: match.start()] + blob[match.end() :]).strip(" /-")
    vessel = " ".join(vessel.split()).strip(" -/") or None
    return vessel, voyage


def _maersk_classifier(joined: str, item: dict | None = None) -> Classifier:
    if item:
        lowered = {str(key).lower(): value for key, value in item.items()}
        time_type = str(lowered.get("event_time_type") or "").strip().upper()
        if time_type:
            return {
                "ACTUAL": "ACT",
                "EXPECTED": "EST",
                "ESTIMATED": "EST",
                "PLANNED": "PLN",
            }.get(time_type, "UNKNOWN")
        for key in ("is_estimated", "estimated", "isestimated", "is_estimate"):
            if lowered.get(key) is True:
                return "EST"
        classifier = str(lowered.get("eventclassifiercode") or lowered.get("classifier") or "")
        if classifier.upper() in {"EST", "PLN", "ACT"}:
            return classifier.upper()  # type: ignore[return-value]
    return classify_classifier(joined)


def _event_from_fields(
    *,
    status: str,
    date_text: str,
    location: str,
    transport: str,
    sequence: int,
    item: dict | None = None,
    vessel: str | None = None,
    voyage: str | None = None,
) -> CanonicalEvent | None:
    status = " ".join(status.split())
    if status.lower() in _SKIP_STATUS:
        return None
    parsed_vessel, parsed_voyage = _voyage_and_vessel(transport)
    vessel_name = vessel or parsed_vessel
    voyage_no = voyage or parsed_voyage
    joined = " | ".join(part for part in (date_text, status, location, transport) if part)
    event_type = {
        "CONTAINER DEPARTURE": "DEPA",
        "CONTAINER ARRIVAL": "ARRI",
        "CONTAINER RETURN": "GTIN",
        "DISCHARG": "DISC",
        "CUSTOMER_GATE_OUT": "GTOT",
    }.get(status.upper(), classify_event_type(status))
    transport_mode = classify_transport(f"{status} {transport}")
    if item:
        mode = str(item.get("transport_mode") or "").upper()
        if mode == "MVS":
            transport_mode = "VESSEL"
        elif mode == "TRK":
            transport_mode = "TRUCK"
    if transport_mode == "UNKNOWN" and event_type in {"LOAD", "DEPA", "ARRI", "DISC"}:
        if vessel_name or "vessel" in status.lower():
            transport_mode = "VESSEL"
    _, event_date, event_time = parse_timestamp(date_text or joined)
    if vessel_name and transport_mode not in {"MOTHER", "FEEDER", "VESSEL"}:
        vessel_name = None
        voyage_no = None
    return CanonicalEvent(
        classifier=_maersk_classifier(joined, item),
        type=event_type,
        location_raw=location,
        location_norm=normalize_key(location) if location else "",
        timestamp_raw=date_text,
        event_date=event_date,
        event_time=event_time,
        sequence_index=sequence,
        vessel=vessel_name,
        voyage=voyage_no,
        empty=(
            item["stempty"]
            if item and isinstance(item.get("stempty"), bool)
            else classify_empty(joined)
        ),
        transport_mode=transport_mode,
        raw_text=joined,
    )


def _mapping_value(item: dict, keys: tuple[str, ...]) -> str:
    lowered = {str(key).lower(): value for key, value in item.items()}
    for key in keys:
        value = lowered.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            nested = value.get("city") or value.get("name") or value.get("terminal")
            if nested:
                return str(nested)
        if value != "":
            return str(value)
    return ""


def _events_from_locations(container: dict) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    for location in container.get("locations") or []:
        if not isinstance(location, dict):
            continue
        city = str(location.get("city") or location.get("terminal") or "")
        for item in location.get("events") or []:
            if not isinstance(item, dict):
                continue
            vessel = _mapping_value(item, _JSON_VSL_KEYS) or None
            voyage = _mapping_value(item, _JSON_VOY_KEYS) or None
            event = _event_from_fields(
                status=_mapping_value(item, _JSON_STATUS_KEYS),
                date_text=_mapping_value(item, _JSON_DATE_KEYS),
                location=city or _mapping_value(item, _JSON_LOC_KEYS),
                transport=" ".join(part for part in (vessel or "", voyage or "") if part),
                sequence=len(events),
                item=item,
                vessel=vessel,
                voyage=voyage,
            )
            if event:
                events.append(event)
    return events


def parse_maersk_json(payload: object) -> list[CanonicalEvent]:
    """Parse synergy/tracking JSON or a nested copy of that payload."""
    events: list[CanonicalEvent] = []

    def walk(node: object) -> None:
        nonlocal events
        if events:
            return
        if isinstance(node, dict):
            containers = node.get("containers")
            if isinstance(containers, list):
                for container in containers:
                    if isinstance(container, dict):
                        events.extend(_events_from_locations(container))
                if events:
                    return
            keys = {str(key).lower() for key in node}
            if keys & set(_JSON_STATUS_KEYS) and keys & set(_JSON_DATE_KEYS):
                events.append(
                    _event_from_fields(  # type: ignore[arg-type]
                        status=_mapping_value(node, _JSON_STATUS_KEYS),
                        date_text=_mapping_value(node, _JSON_DATE_KEYS),
                        location=_mapping_value(node, _JSON_LOC_KEYS),
                        transport=" ".join(
                            part
                            for part in (
                                _mapping_value(node, _JSON_VSL_KEYS),
                                _mapping_value(node, _JSON_VOY_KEYS),
                            )
                            if part
                        ),
                        sequence=len(events),
                        item=node,
                        vessel=_mapping_value(node, _JSON_VSL_KEYS) or None,
                        voyage=_mapping_value(node, _JSON_VOY_KEYS) or None,
                    )
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return [event for event in events if event is not None]


def _events_from_embedded_json(html: str) -> list[CanonicalEvent]:
    for match in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        blob = match.group(1).strip()
        if "event_time" not in blob and "eventTime" not in blob and "activity" not in blob:
            continue
        if not blob.startswith("{") and not blob.startswith("["):
            start = blob.find("{")
            if start < 0:
                continue
            blob = blob[start:]
        try:
            events = parse_maersk_json(json.loads(blob))
        except json.JSONDecodeError:
            continue
        if events:
            return events
    return []


def _parse_maersk_tables(html: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    for table in parse_tables(html):
        if len(table) < 2:
            continue
        headers = [cell["text"] for cell in table[0]]
        mapping = _header_index(headers)
        if "status" not in mapping:
            continue
        for row in table[1:]:
            event = _event_from_fields(
                status=_cell_text(row, mapping.get("status")),
                date_text=_cell_text(row, mapping.get("date")),
                location=_cell_text(row, mapping.get("location")),
                transport=_cell_text(row, mapping.get("transport")),
                sequence=len(events),
            )
            if event:
                events.append(event)
    return events


def parse_maersk_html(html: str) -> list[CanonicalEvent]:
    """Parse movement rows from a Maersk tracking HTML snapshot."""
    events = _events_from_embedded_json(html)
    if not events:
        events = _parse_maersk_tables(html)
    return events


class MaerskTracker(BaseTracker):
    carrier_code = "MAEU"
    wait_in_current_browser = True
    timeline_order = "oldest_first"
    tracking_url = TRACK_URL
    screenshot_selectors = (
        "[class*='transport-plan' i]",
        "[class*='tracking-details' i]",
        "table:has-text('Vessel Departed')",
        "table:has-text('Gate In Full')",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._tracking_json: object | None = None

    def _captured_tracking_json(self) -> object | None:
        if self._tracking_json is not None:
            return self._tracking_json
        return getattr(self.page, "_ct_maersk_json", None)

    def _clear_captured_json(self) -> None:
        self._tracking_json = None
        if self.page is not None:
            setattr(self.page, "_ct_maersk_json", None)

    async def _bind_tracking_response(self) -> None:
        page = self.page
        if getattr(page, "_ct_maersk_bound", False):
            return

        async def handle(response: object) -> None:
            url = str(getattr(response, "url", "") or "")
            if "synergy/tracking" not in url.lower() and "/tracking/" not in url.lower():
                return
            if "synergy/tracking" not in url.lower() and "api" not in url.lower():
                return
            try:
                payload = await response.json()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                return
            if isinstance(payload, (dict, list)):
                setattr(page, "_ct_maersk_json", payload)

        page.on("response", handle)
        setattr(page, "_ct_maersk_bound", True)

    async def open_page(self) -> None:
        await self._bind_tracking_response()
        if not await self.open_tracking_or_reuse("maersk.com"):
            return
        await self.dismiss_cookies(wait_ms=8_000)

    async def search(self, container: str) -> None:
        self._search_submitted = False
        await self._bind_tracking_response()
        self._clear_captured_json()
        if await self._page_challenge_code():
            return
        await self.dismiss_cookies()
        field = await self._find_search_field()
        if field is None:
            await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
            if await self._page_challenge_code():
                return
            await self.dismiss_cookies()
            field = await self._find_search_field()
        if field is None:
            raise TrackerError("Could not find the container search field.", "SELECTOR")
        try:
            await field.click(timeout=5_000)
            await field.fill("")
            await field.fill(container)
        except Exception:  # noqa: BLE001
            await field.fill(container, force=True)
        clicked = False
        for selector in (
            "button:has-text('Track')",
            "button:has-text('Search')",
            "button[type='submit']",
        ):
            button = self.page.locator(selector)
            try:
                if await button.first.is_visible(timeout=1_500):
                    try:
                        await button.first.click(timeout=5_000)
                    except Exception:  # noqa: BLE001
                        await button.first.click(timeout=5_000, force=True)
                    clicked = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not clicked:
            await field.press("Enter")
        self._search_submitted = True
        await self._wait_for_results()

    async def _find_search_field(self):
        for selector in (
            "input[name='track-input']",
            "#track-input input",
            "input[placeholder*='container' i]",
            "input[placeholder*='BL' i]",
            "input[name*='container' i]",
            "input[aria-label*='container' i]",
            "textbox",
        ):
            locator = (
                self.page.get_by_role("textbox").first
                if selector == "textbox"
                else self.page.locator(selector)
            )
            try:
                if await locator.first.is_visible(timeout=3_000):
                    return locator.first
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _wait_for_results(self) -> None:
        try:
            await self.page.wait_for_function(
                """() => {
                    const text = (document.body && document.body.innerText || "").toLowerCase();
                    return (
                        text.includes("vessel departed") ||
                        text.includes("vessel departure") ||
                        text.includes("gate in full") ||
                        text.includes("gate out empty") ||
                        text.includes("empty return") ||
                        text.includes("cannot find") ||
                        text.includes("could not find") ||
                        text.includes("no shipments") ||
                        text.includes("not found") ||
                        (DETECT_CHALLENGE)()
                    );
                }""".replace("DETECT_CHALLENGE", CHALLENGE_CODE_JS),
                timeout=45_000,
            )
        except Exception:  # noqa: BLE001
            pass

    async def parse_events(self) -> list[CanonicalEvent]:
        captured = self._captured_tracking_json()
        if captured is not None:
            events = parse_maersk_json(captured)
            if events:
                return events
        html = await self.page.content()
        events = parse_maersk_html(html)
        if events:
            return events
        text = (await self._visible_text()).lower()
        if any(
            token in text
            for token in (
                "cannot find",
                "could not find",
                "couldn't find",
                "no shipments",
                "not found",
            )
        ):
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
