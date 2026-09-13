"""MSC public shipment tracking adapter."""

from __future__ import annotations

import json
import re
from dataclasses import replace

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
from trackers.base import BaseTracker, TrackerError

TRACK_URL = "https://www.msc.com/en/track-a-shipment"

_STEP_START_RE = re.compile(
    r'<div[^>]*class="[^"]*msc-flow-tracking__step[^"]*"',
    re.I,
)
_CELL_RE = re.compile(
    r'<div[^>]*class="[^"]*msc-flow-tracking__cell--(two|three|four|five)[^"]*"[^>]*>',
    re.I,
)
_DATA_VALUE_RE = re.compile(
    r'<span[^>]*class="[^"]*data-value[^"]*"[^>]*>(.*?)</span>',
    re.S | re.I,
)
_VOYAGE_RE = re.compile(r"\b([A-Z]{1,3}\d{2,4}[A-Z]|\d{2,4}[A-Z])\b", re.I)
_SKIP_TRANSPORT = frozenset({"EMPTY", "LADEN", "FULL", "MT", "FCL", "LCL"})

_HEADER_HINTS = {
    "date": ("date", "time"),
    "status": ("description", "event", "status", "activity", "movement"),
    "location": ("location", "place"),
    "transport": ("vessel", "empty/laden", "voyage", "transport"),
}

_JSON_STATUS_KEYS = ("description", "eventname", "status")
_JSON_DATE_KEYS = ("date", "eventdate", "event_date")
_JSON_LOC_KEYS = ("location",)
_JSON_DETAIL_KEYS = ("detail", "vesselname", "vessel")


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip().strip(" >")


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
    if not blob or blob.upper() in _SKIP_TRANSPORT:
        return None, None
    match = _VOYAGE_RE.search(blob)
    voyage = match.group(1).upper() if match else None
    vessel = blob
    if match:
        vessel = (blob[: match.start()] + blob[match.end() :]).strip(" /-")
    vessel = " ".join(vessel.split()).strip(" -/") or None
    if vessel and vessel.upper() in _SKIP_TRANSPORT:
        vessel = None
    return vessel, voyage


def _event_from_fields(
    *,
    status: str,
    date_text: str,
    location: str,
    transport: str,
    sequence: int,
) -> CanonicalEvent | None:
    if not status or status.lower() in {"status", "event", "description", "latest move"}:
        return None
    vessel_name, voyage = _voyage_and_vessel(transport)
    joined = " | ".join(part for part in (date_text, status, location, transport) if part)
    event_type = classify_event_type(status)
    transport_mode = classify_transport(f"{status} {transport}")
    if transport_mode == "UNKNOWN" and event_type in {"LOAD", "DEPA", "ARRI", "DISC"}:
        if vessel_name or "vessel" in status.lower():
            transport_mode = "VESSEL"
    _, event_date, event_time = parse_timestamp(date_text or joined)
    if vessel_name and transport_mode not in {"MOTHER", "FEEDER", "VESSEL"}:
        vessel_name = None
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
        voyage=voyage,
        empty=classify_empty(joined),
        transport_mode=transport_mode,
        raw_text=joined,
    )


def _first_data_value(html: str) -> str:
    for match in _DATA_VALUE_RE.finditer(html):
        text = _strip_tags(match.group(1))
        if text:
            return text
    return ""


def _slice_cells(step_html: str) -> dict[str, str]:
    starts = list(_CELL_RE.finditer(step_html))
    values: dict[str, str] = {}
    for index, match in enumerate(starts):
        kind = match.group(1).lower()
        end = starts[index + 1].start() if index + 1 < len(starts) else len(step_html)
        values[kind] = _first_data_value(step_html[match.end() : end])
    return values


def _parse_steps(html: str) -> list[CanonicalEvent]:
    starts = [match.start() for match in _STEP_START_RE.finditer(html)]
    events: list[CanonicalEvent] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else min(len(html), start + 12_000)
        chunk = html[start:end]
        if "msc-flow-tracking__step--intermediate" in chunk:
            continue
        cells = _slice_cells(chunk)
        event = _event_from_fields(
            status=cells.get("four", ""),
            date_text=cells.get("two", ""),
            location=cells.get("three", ""),
            transport=cells.get("five", ""),
            sequence=len(events),
        )
        if event is None:
            continue
        if not event.event_date:
            continue
        if event.type == "OTHER" and "etd" in event.raw_text.lower():
            continue
        events.append(event)
    return events


def _parse_msc_tables(html: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    sequence = 0
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
                sequence=sequence,
            )
            if event is None:
                continue
            events.append(event)
            sequence += 1
    return events


def _mapping_value(item: dict, keys: tuple[str, ...]) -> str:
    lowered = {str(key).lower(): value for key, value in item.items()}
    for key in keys:
        value = lowered.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            nested = value.get("Name") or value.get("name") or value.get("Location")
            if nested:
                return str(nested)
        if value != "":
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
    for index, item in enumerate(found):
        event = _event_from_fields(
            status=_mapping_value(item, _JSON_STATUS_KEYS),
            date_text=_mapping_value(item, _JSON_DATE_KEYS),
            location=_mapping_value(item, _JSON_LOC_KEYS),
            transport=_mapping_value(item, _JSON_DETAIL_KEYS),
            sequence=index,
        )
        if event:
            events.append(event)
    return events


def _has_actual_ocean_departure(events: list[CanonicalEvent]) -> bool:
    return any(
        event.classifier == "ACT"
        and event.type == "DEPA"
        and event.empty is not True
        and event.transport_mode in {"MOTHER", "FEEDER", "VESSEL"}
        for event in events
    )


def _treat_export_load_as_departure(events: list[CanonicalEvent]) -> list[CanonicalEvent]:
    """MSC often omits Vessel Departed; Actual export load is the sail signal."""
    if _has_actual_ocean_departure(events):
        return events
    expanded: list[CanonicalEvent] = []
    for event in events:
        expanded.append(event)
        if (
            event.classifier == "ACT"
            and event.type == "LOAD"
            and event.empty is not True
            and event.transport_mode in {"MOTHER", "FEEDER", "VESSEL"}
            and "export loaded" in event.raw_text.lower()
        ):
            expanded.append(replace(event, type="DEPA"))
    return expanded


def parse_msc_html(html: str) -> list[CanonicalEvent]:
    """Parse movement steps from an MSC tracking HTML snapshot."""
    events = _parse_steps(html)
    if not events:
        events = _parse_msc_tables(html)
    if not events:
        for match in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
            blob = match.group(1).strip()
            if not blob.startswith("{") and not blob.startswith("["):
                continue
            try:
                events = _events_from_json(json.loads(blob))
            except json.JSONDecodeError:
                continue
            if events:
                break
    return _treat_export_load_as_departure(events)


class MscTracker(BaseTracker):
    carrier_code = "MSCU"
    timeline_order = "newest_first"
    tracking_url = TRACK_URL
    screenshot_selectors = (
        ".msc-flow-tracking__results",
        ".msc-flow-tracking__container",
        ".msc-flow-tracking__port",
        ".msc-flow-tracking__step",
    )

    async def expand_result_details(self) -> None:
        steps = self.page.locator(".msc-flow-tracking__step")
        try:
            if await steps.first.is_visible(timeout=800):
                return
        except Exception:  # noqa: BLE001
            pass
        bar = self.page.locator(".msc-flow-tracking__bar")
        try:
            if await bar.first.is_visible(timeout=1500):
                await bar.first.click(timeout=3_000)
                await steps.first.wait_for(state="visible", timeout=8_000)
        except Exception:  # noqa: BLE001
            pass

    async def prepare_for_screenshot(self) -> None:
        await self.expand_result_details()
        await super().prepare_for_screenshot()

    async def open_page(self) -> None:
        if not await self.open_tracking_or_reuse("msc.com"):
            return
        html = (await self.page.content()).lower()
        if "access denied" in html or "errors.edgesuite.net" in html:
            raise TrackerError(
                "MSC blocked this browser (Access Denied). Use a headed Chrome window.",
                "NAVIGATION",
            )
        await self.dismiss_cookies(wait_ms=12_000)

    async def search(self, container: str) -> None:
        await self.dismiss_cookies(wait_ms=0)
        radio = self.page.locator("#containeradio")
        try:
            if await radio.first.is_visible(timeout=2_000):
                await radio.first.check(force=True)
        except Exception:  # noqa: BLE001
            pass
        field = self.page.locator("#trackingNumber")
        try:
            await field.first.wait_for(state="visible", timeout=12_000)
        except Exception as exc:  # noqa: BLE001
            await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
            await self.dismiss_cookies(wait_ms=4_000)
            field = self.page.locator("#trackingNumber")
            try:
                await field.first.wait_for(state="visible", timeout=12_000)
            except Exception as retry_exc:  # noqa: BLE001
                raise TrackerError(
                    "Could not find the container search field.", "SELECTOR"
                ) from retry_exc
        await field.first.click()
        await field.first.fill("")
        await field.first.press_sequentially(container, delay=40)
        search = self.page.locator("button.msc-search-autocomplete__search")
        try:
            await search.first.wait_for(state="visible", timeout=5_000)
            await search.first.click(timeout=8_000)
        except Exception:  # noqa: BLE001
            await field.first.press("Enter")
        try:
            await self.page.wait_for_function(
                """() => {
                    const text = (document.body && document.body.innerText || "").toLowerCase();
                    return (
                        !!document.querySelector(".msc-flow-tracking__step") ||
                        text.includes("no result") ||
                        text.includes("not found") ||
                        text.includes("unable to find") ||
                        text.includes("could not find") ||
                        text.includes("not valid") ||
                        text.includes("export loaded") ||
                        text.includes("empty to shipper") ||
                        (DETECT_CHALLENGE)()
                    );
                }""".replace("DETECT_CHALLENGE", CHALLENGE_CODE_JS),
                timeout=40_000,
            )
        except Exception:  # noqa: BLE001
            pass
        await self.expand_result_details()

    async def parse_events(self) -> list[CanonicalEvent]:
        await self.expand_result_details()
        html = await self.page.content()
        events = parse_msc_html(html)
        if events:
            return events
        text = (await self._visible_text()).lower()
        if any(
            token in text
            for token in (
                "no result",
                "not found",
                "unable to find",
                "could not find",
                "not valid",
            )
        ):
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
