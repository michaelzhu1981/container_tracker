"""Hapag-Lloyd public container tracking adapter."""

from __future__ import annotations

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

TRACK_URL = (
    "https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html"
)
TRACING_URL = (
    "https://www.hapag-lloyd.com/en/online-business/tracing/tracing-by-container.html"
)

_HEADER_HINTS = {
    "date": ("date", "time", "event date"),
    "status": ("status", "event", "movement", "activity description"),
    "location": ("place", "location", "place of activity", "facility"),
    "transport": ("transport", "vessel", "mode"),
    "voyage": ("voyage", "voy"),
}


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


def parse_hapag_html(html: str) -> list[CanonicalEvent]:
    """Parse movement rows from a Hapag tracing HTML snapshot."""
    events: list[CanonicalEvent] = []
    sequence = 0
    for table in parse_tables(html):
        if len(table) < 2:
            continue
        headers = [cell["text"] for cell in table[0]]
        mapping = _header_index(headers)
        body = table[1:]
        if len(mapping) < 2:
            mapping = {"date": 0, "status": 1, "location": 2, "transport": 3, "voyage": 4}
            body = table
        has_bold = any(cell.get("bold") for row in body for cell in row)
        for row in body:
            status = _cell_text(row, mapping.get("status"))
            if not status or status.lower() in {"status", "event"}:
                continue
            date_text = _cell_text(row, mapping.get("date"))
            location = _cell_text(row, mapping.get("location"))
            transport = _cell_text(row, mapping.get("transport"))
            voyage = _cell_text(row, mapping.get("voyage"))
            joined = " | ".join(
                part for part in (date_text, status, location, transport, voyage) if part
            )
            is_bold = any(cell.get("bold") for cell in row)
            is_actual = True if is_bold else (False if has_bold else None)
            classifier = classify_classifier(joined, is_actual=is_actual)
            event_type = classify_event_type(status)
            transport_mode = classify_transport(f"{status} {transport}")
            if transport_mode == "UNKNOWN" and transport:
                # Hapag often puts the ocean vessel name in the Transport column.
                if classify_transport(status) == "VESSEL":
                    transport_mode = "VESSEL"
                elif event_type in {"LOAD", "DEPA"}:
                    transport_mode = "VESSEL"
            _, event_date, event_time = parse_timestamp(date_text or joined)
            vessel_name = (
                transport
                if transport_mode in {"MOTHER", "FEEDER", "VESSEL"} and transport
                else None
            )
            events.append(
                CanonicalEvent(
                    classifier=classifier,
                    type=event_type,
                    location_raw=location,
                    location_norm=normalize_key(location) if location else "",
                    timestamp_raw=date_text,
                    event_date=event_date,
                    event_time=event_time,
                    sequence_index=sequence,
                    vessel=vessel_name,
                    voyage=voyage or None,
                    empty=classify_empty(joined),
                    transport_mode=transport_mode,
                    raw_text=joined,
                )
            )
            sequence += 1
    return events


class HapagTracker(BaseTracker):
    carrier_code = "HLCU"
    timeline_order = "oldest_first"
    tracking_url = TRACK_URL

    async def open_page(self) -> None:
        await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
        await self.dismiss_cookies()
        html = await self.page.content()
        lowered = html.lower()
        if "checking your browser" in lowered or "managed challenge" in lowered:
            return
        if "outdated browser" in lowered and "container" not in lowered:
            await self.page.goto(TRACING_URL, wait_until="domcontentloaded")

    async def search(self, container: str) -> None:
        await self.dismiss_cookies()
        field = None
        for selector in (
            "input[placeholder*='Container' i]",
            "input[name*='container' i]",
            "input[id*='container' i]",
            "input[aria-label*='Container' i]",
            "textbox",
        ):
            locator = (
                self.page.get_by_role("textbox").first
                if selector == "textbox"
                else self.page.locator(selector)
            )
            try:
                if await locator.first.is_visible(timeout=3000):
                    field = locator.first
                    break
            except Exception:  # noqa: BLE001
                continue
        if field is None:
            raise TrackerError("Could not find the container search field.", "SELECTOR")
        await field.fill("")
        await field.fill(container)
        clicked = False
        for selector in (
            "button:has-text('Search')",
            "button:has-text('Track')",
            "input[type='submit']",
            "button[type='submit']",
        ):
            button = self.page.locator(selector)
            try:
                if await button.first.is_visible(timeout=1500):
                    await button.first.click()
                    clicked = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not clicked:
            await field.press("Enter")
        try:
            await self.page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass

    async def parse_events(self) -> list[CanonicalEvent]:
        html = await self.page.content()
        events = parse_hapag_html(html)
        if events:
            return events
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
