"""COSCO Shipping Lines public cargo tracking adapter."""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from challenges import CHALLENGE_CODE_JS
from event_text import (
    classify_classifier,
    classify_empty,
    classify_event_type,
    classify_transport,
    parse_timestamp,
)
from html_tables import parse_tables
from models import CanonicalEvent
from ports import normalize_key
from trackers.base import BaseTracker, TrackerError, looks_like_no_result

TRACK_URL = "https://elines.coscoshipping.com/ebusiness/cargoTracking"
TRACK_RESULT_URL = (
    "https://elines.coscoshipping.com/scct/public/ct/base"
    "?lang=en&trackingType=CONTAINER&number={number}"
)

_HEADER_HINTS = {
    "date": ("event time", "date", "time"),
    "status": ("dynamic node", "event", "status", "activity", "node"),
    "location": ("event location", "location", "place"),
    "transport": ("transport mode", "transport", "mode", "vessel"),
}

_JSON_STATUS_KEYS = (
    "dynamicnode",
    "eventname",
    "statusdescription",
    "status",
    "activity",
    "nodedesc",
)
_JSON_DATE_KEYS = ("eventtime", "eventdate", "time", "date")
_JSON_LOC_KEYS = ("eventlocation", "location", "locationname", "nodelocation")
_JSON_MODE_KEYS = ("transportmode", "mode", "transport")
_JSON_VSL_KEYS = ("vesselname", "vessel", "vslname")
_JSON_VOY_KEYS = ("voyage", "voyageno", "voy")

_VOYAGE_RE = re.compile(r"\b(\d{2,4}[A-Z])\b", re.I)
_TITLE_CONTAINER_RE = re.compile(
    r"<div[^>]*(?:font-weight:\s*bold|font-size:\s*20px)[^>]*>\s*([A-Z]{4}\d{6,7})\s*</div>",
    re.I,
)
_SKIP_STATUS = frozenset({"", "dynamic node", "event", "status", "activity", "node"})
_POL_DEPA = ("from first pol", "from pol", "departure from first")
_RESULT_CONTAINER_JS = """() => {
    const nodes = [...document.querySelectorAll("div,span,h1,h2,h3,strong")];
    const match = (el) => {
        if (el.children.length) return "";
        const text = (el.textContent || "").trim().toUpperCase();
        return /^[A-Z]{4}\\d{6,7}$/.test(text) ? text : "";
    };
    const titled = nodes.find((el) => {
        const text = match(el);
        if (!text) return false;
        const style = window.getComputedStyle(el);
        return Number(style.fontWeight) >= 600 || parseFloat(style.fontSize) >= 18;
    });
    if (titled) return match(titled);
    const any = nodes.find((el) => match(el));
    return any ? match(any) : "";
}"""


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
    if not blob or blob.lower() in {"vessel", "barge", "truck", "rail"}:
        return None, None
    match = _VOYAGE_RE.search(blob)
    voyage = match.group(1).upper() if match else None
    vessel = blob
    if match:
        vessel = (blob[: match.start()] + blob[match.end() :]).strip(" /-")
    vessel = " ".join(vessel.split()).strip(" -/()") or None
    return vessel, voyage


def _event_from_fields(
    *,
    status: str,
    date_text: str,
    location: str,
    transport: str,
    sequence: int,
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
    event_type = classify_event_type(status)
    transport_mode = classify_transport(f"{status} {transport}")
    if transport_mode == "UNKNOWN" and event_type in {"LOAD", "DEPA", "ARRI", "DISC"}:
        if vessel_name or "vessel" in f"{status} {transport}".lower():
            transport_mode = "VESSEL"
    _, event_date, event_time = parse_timestamp(date_text or joined)
    if vessel_name and transport_mode not in {"MOTHER", "FEEDER", "VESSEL"}:
        vessel_name = None
        voyage_no = None
    return CanonicalEvent(
        classifier=classify_classifier(joined),
        type=event_type,
        location_raw=location,
        location_norm=normalize_key(location) if location else "",
        timestamp_raw=date_text,
        event_date=event_date,
        event_time=event_time,
        sequence_index=sequence,
        vessel=vessel_name,
        voyage=voyage_no,
        empty=classify_empty(joined),
        transport_mode=transport_mode,
        raw_text=joined,
    )


def _mapping_value(item: dict, keys: tuple[str, ...]) -> str:
    lowered = {str(key).lower(): value for key, value in item.items()}
    for key in keys:
        value = lowered.get(key)
        if value is None or value == "":
            continue
        return str(value)
    return ""


def _events_from_json(payload: object) -> list[CanonicalEvent]:
    found: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            keys = {str(key).lower() for key in node}
            if keys & set(_JSON_STATUS_KEYS) and keys & set(_JSON_DATE_KEYS):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    events: list[CanonicalEvent] = []
    for item in found:
        event = _event_from_fields(
            status=_mapping_value(item, _JSON_STATUS_KEYS),
            date_text=_mapping_value(item, _JSON_DATE_KEYS),
            location=_mapping_value(item, _JSON_LOC_KEYS),
            transport=_mapping_value(item, _JSON_MODE_KEYS),
            sequence=len(events),
            vessel=_mapping_value(item, _JSON_VSL_KEYS) or None,
            voyage=_mapping_value(item, _JSON_VOY_KEYS) or None,
        )
        if event:
            events.append(event)
    return events


def _parse_cosco_tables(html: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    for table in parse_tables(html):
        if len(table) < 2:
            continue
        headers = [cell["text"] for cell in table[0]]
        mapping = _header_index(headers)
        if "status" not in mapping or "date" not in mapping:
            continue
        joined_headers = " ".join(headers).lower()
        if "dynamic node" not in joined_headers and "event time" not in joined_headers:
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
        if events:
            break
    return events


def _ensure_pol_load(events: list[CanonicalEvent]) -> list[CanonicalEvent]:
    if any(event.type == "LOAD" and event.transport_mode in {"MOTHER", "FEEDER", "VESSEL"} for event in events):
        return events
    extras: list[CanonicalEvent] = []
    for event in events:
        blob = event.raw_text.lower()
        if event.type != "DEPA" or not any(token in blob for token in _POL_DEPA):
            continue
        extras.append(
            CanonicalEvent(
                classifier=event.classifier,
                type="LOAD",
                location_raw=event.location_raw,
                location_norm=event.location_norm,
                timestamp_raw=event.timestamp_raw,
                event_date=event.event_date,
                event_time=event.event_time,
                sequence_index=event.sequence_index,
                vessel=event.vessel,
                voyage=event.voyage,
                booking=event.booking,
                empty=event.empty,
                transport_mode=event.transport_mode or "VESSEL",
                raw_text=f"Loaded at First POL | {event.raw_text}",
            )
        )
        break
    return extras + events


def displayed_container(html: str) -> str | None:
    """Return the COSCO result heading, ignoring the search input value."""
    match = _TITLE_CONTAINER_RE.search(html)
    if match:
        return match.group(1).upper()
    return None


def result_is_stale(html: str, container: str) -> bool:
    shown = displayed_container(html)
    return bool(shown and shown != container.upper())


def parse_cosco_html(html: str) -> list[CanonicalEvent]:
    """Parse COSCO cargo-tracking milestones from an HTML snapshot."""
    events = _parse_cosco_tables(html)
    if not events:
        for match in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
            blob = match.group(1).strip()
            if "eventTime" not in blob and "dynamicNode" not in blob:
                continue
            try:
                events = _events_from_json(json.loads(blob))
            except json.JSONDecodeError:
                continue
            if events:
                break
    return _ensure_pol_load(events)


class CoscoTracker(BaseTracker):
    carrier_code = "COSU"
    timeline_order = "newest_first"
    tracking_url = TRACK_URL
    screenshot_selectors = (
        "table:has-text('Dynamic Node')",
        "table:has-text('Event Time')",
        ".main-content",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._expected = ""

    async def open_page(self) -> None:
        if not await self.open_tracking_or_reuse("coscoshipping.com"):
            return
        await self.dismiss_cookies(wait_ms=10_000)

    async def _fill_search(self, container: str) -> bool:
        field = self.page.locator(
            "input.ant-input, input[placeholder*='container' i], input[type='text']"
        )
        try:
            if not await field.first.is_visible(timeout=4_000):
                return False
            await field.first.click()
            await field.first.fill("")
            await field.first.fill(container)
        except Exception:  # noqa: BLE001
            return False
        search = self.page.locator("button:has-text('Search')")
        try:
            await search.first.click(timeout=8_000)
        except Exception:  # noqa: BLE001
            await field.first.press("Enter")
        return True

    async def _open_result_url(self, container: str) -> None:
        await self.page.goto(
            TRACK_RESULT_URL.format(number=quote(container)),
            wait_until="domcontentloaded",
        )
        await self.dismiss_cookies(wait_ms=0)

    async def search(self, container: str) -> None:
        self._search_submitted = False
        self._expected = container.upper()
        await self.dismiss_cookies(wait_ms=0)
        if await self._fill_search(container):
            self._search_submitted = True
            await self._wait_for_results(container)
            if await self._result_matches(container) or await self._page_challenge_code():
                return
        await self._open_result_url(container)
        self._search_submitted = True
        await self._wait_for_results(container)

    async def _displayed_container(self) -> str:
        try:
            value = await self.page.evaluate(_RESULT_CONTAINER_JS)
        except Exception:  # noqa: BLE001
            return ""
        return str(value or "").upper()

    async def _result_matches(self, container: str) -> bool:
        shown = await self._displayed_container()
        if shown == container.upper():
            return await self._has_tracking_result()
        return False

    async def _has_tracking_result(self) -> bool:
        try:
            text = (await self._visible_text()).lower()
        except Exception:  # noqa: BLE001
            return False
        return (
            "dynamic node" in text
            or "laden return" in text
            or "vessel departure from first pol" in text
            or "event time" in text
            and "event location" in text
        )

    async def _wait_for_results(self, container: str) -> None:
        expected = container.upper()
        try:
            await self.page.wait_for_function(
                """(expected) => {
                    if ((DETECT_CHALLENGE)()) return true;
                    const leaf = [...document.querySelectorAll("div,span,h1,h2,h3,strong")].some((el) => {
                        if (el.children.length) return false;
                        return (el.textContent || "").trim().toUpperCase() === expected;
                    });
                    if (!leaf) return false;
                    const low = (document.body && document.body.innerText || "").toLowerCase();
                    return (
                        low.includes("dynamic node") ||
                        low.includes("laden return") ||
                        (low.includes("event time") && low.includes("event location")) ||
                        low.includes("no data") ||
                        low.includes("not found")
                    );
                }""".replace("DETECT_CHALLENGE", CHALLENGE_CODE_JS),
                arg=expected,
                timeout=30_000,
            )
        except Exception:  # noqa: BLE001
            pass

    async def parse_events(self) -> list[CanonicalEvent]:
        html = await self.page.content()
        expected = self._expected
        if expected and result_is_stale(html, expected):
            await self._open_result_url(expected)
            await self._wait_for_results(expected)
            html = await self.page.content()
        if expected and result_is_stale(html, expected):
            shown = displayed_container(html) or "another container"
            raise TrackerError(
                f"Tracking page still shows {shown} instead of {expected}.",
                "PARSE",
            )
        events = parse_cosco_html(html)
        if events:
            return events
        text = await self._visible_text()
        if looks_like_no_result(text):
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
