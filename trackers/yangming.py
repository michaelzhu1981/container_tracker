"""Yang Ming public cargo tracking adapter."""

from __future__ import annotations

import re

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

TRACK_URL = "https://www.yangming.com/en/esolution/tracking/cargo_tracking"

_HEADER_HINTS = {
    "date": ("date/time", "date", "time"),
    "status": ("event", "movement", "status", "activity"),
    "location": ("at facility", "place of activity", "location", "place"),
    "transport": ("mode", "transport", "vessel"),
    "voyage": ("voyage", "voy"),
    "classifier": ("event classifier", "classifier"),
}

_VOYAGE_RE = re.compile(r"\b(\d{2,4}[A-Z])\b", re.I)
_CLASSIFIER_VALUES = {"ACT": "ACT", "PLN": "PLN", "EST": "EST", "ACTUAL": "ACT", "PLANNED": "PLN"}


def _header_index(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    lowered = [h.lower() for h in headers]
    for key, hints in _HEADER_HINTS.items():
        for idx, header in enumerate(lowered):
            if key == "date" and "utc+0" in header:
                continue
            if any(hint in header for hint in hints):
                mapping[key] = idx
                break
    return mapping


def _cell_text(row: list[dict], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]["text"]


def _voyage_and_vessel(mode_text: str) -> tuple[str | None, str | None]:
    match = _VOYAGE_RE.search(mode_text)
    voyage = match.group(1).upper() if match else None
    vessel = mode_text
    if match:
        vessel = (mode_text[: match.start()] + mode_text[match.end() :]).strip(" /-")
    vessel = re.sub(r"\(\s*\)", "", vessel)
    vessel = " ".join(vessel.split()).strip(" -/") or None
    return vessel, voyage


def parse_yangming_html(html: str) -> list[CanonicalEvent]:
    """Parse Container Status rows from a Yang Ming tracking HTML snapshot."""
    events: list[CanonicalEvent] = []
    sequence = 0
    for table in parse_tables(html):
        if len(table) < 2:
            continue
        headers = [cell["text"] for cell in table[0]]
        mapping = _header_index(headers)
        if "status" not in mapping or "date" not in mapping:
            continue
        body = table[1:]
        for row in body:
            status = _cell_text(row, mapping.get("status"))
            if not status or status.lower() in {"event", "status", "-"}:
                continue
            date_text = _cell_text(row, mapping.get("date"))
            location = _cell_text(row, mapping.get("location"))
            transport = _cell_text(row, mapping.get("transport"))
            voyage_cell = _cell_text(row, mapping.get("voyage"))
            classifier_cell = _cell_text(row, mapping.get("classifier"))
            vessel_from_mode, voyage_from_mode = _voyage_and_vessel(transport)
            voyage = voyage_cell or voyage_from_mode
            joined = " | ".join(
                part for part in (date_text, status, location, transport, voyage) if part
            )
            classifier_token = _CLASSIFIER_VALUES.get(classifier_cell.strip().upper())
            if classifier_token:
                classifier: Classifier = classifier_token  # type: ignore[assignment]
            else:
                # Do not join destination ETA into classifier text.
                classifier = classify_classifier(
                    " | ".join(part for part in (date_text, status, transport) if part)
                )
            event_type = classify_event_type(status)
            transport_mode = classify_transport(f"{status} {transport}")
            if transport_mode == "UNKNOWN" and transport and event_type in {"LOAD", "DEPA"}:
                transport_mode = "VESSEL"
            _, event_date, event_time = parse_timestamp(date_text or joined)
            vessel_name = (
                vessel_from_mode
                if transport_mode in {"MOTHER", "FEEDER", "VESSEL"} and vessel_from_mode
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


class YangMingTracker(BaseTracker):
    carrier_code = "YMJA"
    timeline_order = "newest_first"
    tracking_url = TRACK_URL
    screenshot_selectors = (
        "table[aria-label*='Container Status' i]",
        "table[aria-label*='container' i]",
    )

    async def open_page(self) -> None:
        if not await self.open_tracking_or_reuse("yangming.com"):
            return
        await self.dismiss_cookies(wait_ms=20_000)

    async def search(self, container: str) -> None:
        await self.dismiss_cookies(wait_ms=8_000)
        field = self.page.get_by_role("textbox").first
        try:
            await field.wait_for(state="visible", timeout=10_000)
        except Exception as exc:  # noqa: BLE001
            await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
            await self.dismiss_cookies(wait_ms=8_000)
            field = self.page.get_by_role("textbox").first
            try:
                await field.wait_for(state="visible", timeout=10_000)
            except Exception as retry_exc:  # noqa: BLE001
                raise TrackerError(
                    "Could not find the container search field.", "SELECTOR"
                ) from retry_exc
        await field.click()
        await field.fill("")
        await field.press_sequentially(container, delay=40)
        search = self.page.locator("button:has-text('Search')")
        submitted = False
        try:
            async with self.page.expect_response(
                lambda response: "CargoTracking/GetTracking" in response.url,
                timeout=30_000,
            ):
                await search.last.click()
                submitted = True
        except Exception:  # noqa: BLE001
            if not submitted:
                try:
                    await search.last.click(force=True)
                except Exception:  # noqa: BLE001
                    await field.press("Enter")
        try:
            await self.page.wait_for_function(
                """() => {
                    const text = document.body.innerText.toLowerCase();
                    return (
                        text.includes("can't identify") ||
                        text.includes("can’t identify") ||
                        text.includes("container status") ||
                        text.includes("current status")
                    );
                }""",
                timeout=30_000,
            )
        except Exception:  # noqa: BLE001
            pass

    async def parse_events(self) -> list[CanonicalEvent]:
        html = await self.page.content()
        blob = html.lower()
        try:
            blob = blob + "\n" + (await self.page.inner_text("body")).lower()
        except Exception:  # noqa: BLE001
            pass
        events = parse_yangming_html(html)
        if events:
            return events
        if "can't identify" in blob or "cannot identify" in blob or "can’t identify" in blob:
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
