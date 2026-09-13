"""OOCL public cargo tracking adapter."""

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

TRACK_URL = (
    "https://www.oocl.com/eng/ourservices/eservices/cargotracking/"
    "Pages/cargotracking.aspx"
)
EXPRESS_LINK = (
    "https://www.oocl.com/eng/ourservices/eservices/cargotracking/"
    "Pages/ExpressLink.aspx?eltype=ct&businessType=containerNumber"
    "&businessNumber={number}&language=en"
)

_HEADER_HINTS = {
    "date": ("date", "time"),
    "status": ("event", "status", "activity", "movement", "dynamic node"),
    "location": ("location", "place", "facility"),
    "transport": ("vessel", "voyage", "transport", "mode"),
}

_JSON_STATUS_KEYS = (
    "eventname",
    "status",
    "activity",
    "movement",
    "dynamicnode",
    "description",
)
_JSON_DATE_KEYS = ("eventtime", "eventdate", "date", "time")
_JSON_LOC_KEYS = ("location", "eventlocation", "locationname", "place")
_JSON_VSL_KEYS = ("vesselname", "vessel", "vslname")
_JSON_VOY_KEYS = ("voyage", "voyageno", "voy")

_VOYAGE_RE = re.compile(r"\b(\d{2,4}[A-Z])\b", re.I)
_SKIP_STATUS = frozenset({"", "event", "status", "activity", "movement", "dynamic node"})
_SEARCH_FIELD_SELECTORS = (
    "#SEARCH_NUMBER",
    "input[name='SEARCH_NUMBER']",
    "input[placeholder*='Container' i]",
    ".function-input input.filter-input",
)


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
            transport=" ".join(
                part
                for part in (
                    _mapping_value(item, _JSON_VSL_KEYS),
                    _mapping_value(item, _JSON_VOY_KEYS),
                )
                if part
            ),
            sequence=len(events),
            vessel=_mapping_value(item, _JSON_VSL_KEYS) or None,
            voyage=_mapping_value(item, _JSON_VOY_KEYS) or None,
        )
        if event:
            events.append(event)
    return events


def _parse_oocl_tables(html: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    for table in parse_tables(html):
        if len(table) < 2:
            continue
        headers = [cell["text"] for cell in table[0]]
        mapping = _header_index(headers)
        if "status" not in mapping or "date" not in mapping:
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


def parse_oocl_html(html: str) -> list[CanonicalEvent]:
    """Parse OOCL cargo-tracking event rows from an HTML snapshot."""
    events = _parse_oocl_tables(html)
    if events:
        return events
    for match in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        blob = match.group(1).strip()
        if not any(token in blob for token in ("eventName", "eventDate", "dynamicNode")):
            continue
        try:
            events = _events_from_json(json.loads(blob))
        except json.JSONDecodeError:
            continue
        if events:
            return events
    return []


class OoclTracker(BaseTracker):
    carrier_code = "OOLU"
    wait_in_current_browser = True
    timeline_order = "newest_first"
    tracking_url = TRACK_URL
    screenshot_selectors = (
        "table:has-text('Vessel Departed')",
        "table:has-text('Event')",
        "table:has-text('Dynamic Node')",
        "[class*='cargo-tracking' i]",
    )

    async def _page_challenge_code(self) -> str | None:
        code = await super()._page_challenge_code()
        if code and await self._has_tracking_result():
            return None
        return code

    async def open_page(self) -> None:
        if not await self.open_tracking_or_reuse("oocl.com"):
            return
        await self.dismiss_cookies(wait_ms=12_000)

    async def _select_container_search(self) -> None:
        toggle = self.page.locator("button[data-id='ooclCargoSelector']")
        try:
            if await toggle.first.is_visible(timeout=1_200):
                label = (await toggle.first.inner_text()).lower()
                if "container" in label:
                    await self._set_search_type("cont")
                    return
                await toggle.first.click()
                option = self.page.locator(
                    ".dropdown-menu.open a:has-text('Container #'), "
                    "a:has-text('Container #')"
                )
                await option.first.click(timeout=2_000)
                await self._set_search_type("cont")
                return
        except Exception:  # noqa: BLE001
            pass
        for selector in (
            "#ooclCargoSelector",
            "select[name='ooclCargoSelector']",
            "select:has(option:has-text('Container'))",
        ):
            locator = self.page.locator(selector)
            try:
                await locator.first.select_option(label="Container #")
                await self._set_search_type("cont")
                return
            except Exception:  # noqa: BLE001
                try:
                    await locator.first.select_option(value="cont")
                    await self._set_search_type("cont")
                    return
                except Exception:  # noqa: BLE001
                    continue
        await self._set_search_type("cont")

    async def _set_search_type(self, value: str) -> None:
        try:
            await self.page.evaluate(
                """(value) => {
                    const type = document.getElementById("searchType");
                    if (type) type.value = value;
                    const select = document.getElementById("ooclCargoSelector");
                    if (select) select.value = value;
                }""",
                value,
            )
        except Exception:  # noqa: BLE001
            pass

    async def _first_visible_search_field(self):
        for selector in _SEARCH_FIELD_SELECTORS:
            locator = self.page.locator(selector)
            try:
                if await locator.first.is_visible(timeout=1_500):
                    return locator
            except Exception:  # noqa: BLE001
                continue
        locator = self.page.locator("#SEARCH_NUMBER")
        try:
            await locator.first.wait_for(state="attached", timeout=3_000)
            await locator.first.scroll_into_view_if_needed()
            return locator
        except Exception:  # noqa: BLE001
            return None

    async def _open_express_link(self, container: str) -> None:
        await self.page.goto(
            EXPRESS_LINK.format(number=quote(container)),
            wait_until="domcontentloaded",
        )
        await self.dismiss_cookies(wait_ms=0)

    async def search(self, container: str) -> None:
        self._search_submitted = False
        await self.dismiss_cookies(wait_ms=0)
        await self._select_container_search()
        field = await self._first_visible_search_field()
        if field is None:
            await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
            await self.dismiss_cookies(wait_ms=0)
            await self._select_container_search()
            field = await self._first_visible_search_field()
        if field is None:
            await self._open_express_link(container)
            self._search_submitted = True
            await self._wait_for_results()
            return
        await field.first.click()
        await field.first.fill("")
        await field.first.fill(container)
        await self._submit_search()
        self._search_submitted = True
        if not await self._has_tracking_result() and not await self._page_challenge_code():
            await self._open_express_link(container)
        await self._wait_for_results()

    async def _submit_search(self) -> None:
        button = self.page.locator(
            "#btn_cargoTracking, a#ListeningCargoTrackingBtn, "
            "a[onclick*='ListeningCargoTrackingBtn'], "
            "button:has-text('Search'), input[value='Search']"
        )
        try:
            async with self.page.expect_popup(timeout=8_000) as popup_info:
                if await button.first.is_visible(timeout=1_500):
                    await button.first.click(timeout=8_000)
                else:
                    await self.page.keyboard.press("Enter")
            page = await popup_info.value
            self.page = page
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            if await button.first.is_visible(timeout=800):
                await button.first.click(timeout=8_000)
                return
        except Exception:  # noqa: BLE001
            pass
        await self.page.keyboard.press("Enter")

    async def _has_tracking_result(self) -> bool:
        try:
            text = (await self._visible_text()).lower()
        except Exception:  # noqa: BLE001
            return False
        return any(
            token in text
            for token in (
                "vessel departed",
                "vessel departure",
                "loaded",
                "dynamic node",
                "container movement",
                "cargo tracking result",
            )
        ) and "unrecognized" not in text

    async def _wait_for_results(self) -> None:
        try:
            await self.page.wait_for_function(
                """() => {
                    const text = (document.body && document.body.innerText || "").toLowerCase();
                    return (
                        text.includes("vessel departed") ||
                        text.includes("vessel departure") ||
                        text.includes("dynamic node") ||
                        text.includes("container movement") ||
                        text.includes("unrecognized") ||
                        text.includes("no result") ||
                        text.includes("not found") ||
                        (DETECT_CHALLENGE)()
                    );
                }""".replace("DETECT_CHALLENGE", CHALLENGE_CODE_JS),
                timeout=35_000,
            )
        except Exception:  # noqa: BLE001
            pass

    async def parse_events(self) -> list[CanonicalEvent]:
        html = await self.page.content()
        text = await self._visible_text()
        if "unrecognized" in text.lower():
            raise TrackerError("OOCL rejected this tracking request.", "NAVIGATION")
        events = parse_oocl_html(html)
        if events:
            return events
        if looks_like_no_result(text):
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
