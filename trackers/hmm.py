"""HMM public Track & Trace adapter."""

from __future__ import annotations

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
from models import CanonicalEvent
from ports import normalize_key
from trackers.base import BaseTracker, TrackerError, looks_like_no_result

TRACK_URL = "https://www.hmm21.com/e-service/general/trackNTrace/TrackNTrace.do"

_HEADER_HINTS = {
    "date": ("date",),
    "time": ("time",),
    "status": ("status description", "status", "movement"),
    "location": ("location",),
    "transport": ("mode", "vessel", "transport"),
}

_VOYAGE_RE = re.compile(r"\b(\d{2,4}[A-Z])\b", re.I)
_SKIP_STATUS = frozenset(
    {
        "",
        "status",
        "status description",
        "movement",
        "display previous moves",
        "hide previous moves",
        "show latest event",
    }
)
_MODE_ONLY = frozenset({"barge", "truck", "rail", "train", "feeder", "vessel"})


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
    if not blob or blob.lower() in _MODE_ONLY:
        return None, None
    blob = re.sub(r"^\[[^\]]+\]\s*", "", blob).strip()
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
) -> CanonicalEvent | None:
    status = " ".join(status.split())
    if status.lower() in _SKIP_STATUS:
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
        voyage = None
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


def parse_hmm_html(html: str) -> list[CanonicalEvent]:
    """Parse Shipment History rows from an HMM tracking HTML snapshot."""
    events: list[CanonicalEvent] = []
    for table in parse_tables(html):
        if len(table) < 2:
            continue
        headers = [cell["text"] for cell in table[0]]
        mapping = _header_index(headers)
        if "status" not in mapping or "date" not in mapping:
            continue
        if "location" not in mapping:
            continue
        joined_headers = " ".join(headers).lower()
        if "status description" not in joined_headers and "mode" not in joined_headers:
            continue
        for row in table[1:]:
            status = _cell_text(row, mapping.get("status"))
            date_text = " ".join(
                part
                for part in (
                    _cell_text(row, mapping.get("date")),
                    _cell_text(row, mapping.get("time")),
                )
                if part
            )
            event = _event_from_fields(
                status=status,
                date_text=date_text,
                location=_cell_text(row, mapping.get("location")),
                transport=_cell_text(row, mapping.get("transport")),
                sequence=len(events),
            )
            if event:
                events.append(event)
        if events:
            break
    return events


class HmmTracker(BaseTracker):
    carrier_code = "HDMU"
    timeline_order = "newest_first"
    tracking_url = TRACK_URL
    screenshot_selectors = (
        "#shipmentProgress",
        "#trackingInfomationDateResultTable",
        "table:has-text('Status Description')",
        "table:has-text('Vessel Departure from POL')",
    )

    async def expand_result_details(self) -> None:
        for selector in (
            "a.clsShowedMoves",
            "a:has-text('Display Previous Moves')",
        ):
            link = self.page.locator(selector)
            try:
                if await link.first.is_visible(timeout=800):
                    await link.first.click(timeout=3_000)
                    await self.page.wait_for_timeout(400)
                    return
            except Exception:  # noqa: BLE001
                continue

    async def prepare_for_screenshot(self) -> None:
        await self.expand_result_details()
        await super().prepare_for_screenshot()

    async def open_page(self) -> None:
        if not await self.open_tracking_or_reuse("hmm21.com"):
            return
        await self.dismiss_cookies(wait_ms=8_000)

    async def search(self, container: str) -> None:
        self._search_submitted = False
        await self.dismiss_cookies(wait_ms=0)
        field = self.page.locator("input[name='srchCntrNo1']")
        try:
            await field.first.wait_for(state="visible", timeout=12_000)
        except Exception as exc:  # noqa: BLE001
            await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
            await self.dismiss_cookies(wait_ms=0)
            field = self.page.locator("input[name='srchCntrNo1']")
            try:
                await field.first.wait_for(state="visible", timeout=12_000)
            except Exception as retry_exc:  # noqa: BLE001
                raise TrackerError(
                    "Could not find the container search field.", "SELECTOR"
                ) from retry_exc
        await field.first.click()
        await field.first.fill("")
        await field.first.fill(container)
        retrieve = self.page.locator("button:has-text('Retrieve')")
        try:
            await retrieve.first.click(timeout=8_000)
        except Exception:  # noqa: BLE001
            await field.first.press("Enter")
        self._search_submitted = True
        await self._wait_for_results()
        await self.expand_result_details()

    async def _wait_for_results(self) -> None:
        try:
            await self.page.wait_for_function(
                """() => {
                    const text = (document.body && document.body.innerText || "").toLowerCase();
                    return (
                        !!document.querySelector("#shipmentProgress") ||
                        !!document.querySelector("#thisCntr") ||
                        !!document.querySelector("tr.clsMoves") ||
                        text.includes("shipment history") ||
                        text.includes("vessel departure from pol") ||
                        text.includes("container no. is invalid") ||
                        text.includes("invalid") && text.includes("container") ||
                        (DETECT_CHALLENGE)()
                    );
                }""".replace("DETECT_CHALLENGE", CHALLENGE_CODE_JS),
                timeout=30_000,
            )
        except Exception:  # noqa: BLE001
            pass

    async def parse_events(self) -> list[CanonicalEvent]:
        await self.expand_result_details()
        html = await self.page.content()
        events = parse_hmm_html(html)
        if events:
            return events
        text = await self._visible_text()
        if looks_like_no_result(text) or "container no. is invalid" in text.lower():
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
