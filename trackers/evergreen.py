"""Evergreen China public cargo-tracking adapter."""

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
from models import CanonicalEvent
from ports import normalize_key
from trackers.base import BaseTracker, TrackerError

TRACK_URL = "https://www.evergreen-shipping.cn/servlet/TDB1_CargoTracking.do"

_HEADER_HINTS = {
    "container": ("箱号", "container no", "container number"),
    "date": ("日期", "date"),
    "status": ("货柜动态", "current status", "container moves", "status"),
    "location": ("地点", "location", "place"),
    "vessel": ("船名 航次", "船名/航次", "vessel voyage", "vessel/voyage"),
}
_VOYAGE_RE = re.compile(r"\b(?:\d{3,5}(?:-\d{3})?[A-Z]|\d{2,4}[A-Z])\b", re.I)


def _header_index(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    lowered = [header.lower() for header in headers]
    for key, hints in _HEADER_HINTS.items():
        for index, header in enumerate(lowered):
            if any(hint in header for hint in hints):
                mapping[key] = index
                break
    return mapping


def _cell_text(row: list[dict], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]["text"]


def _vessel_and_voyage(text: str) -> tuple[str | None, str | None]:
    value = " ".join(text.split()).strip()
    if not value:
        return None, None
    match = _VOYAGE_RE.search(value)
    voyage = match.group(0).upper() if match else None
    vessel = value
    if match:
        vessel = (value[: match.start()] + value[match.end() :]).strip(" /-()")
    return vessel or None, voyage


def parse_evergreen_html(html: str) -> list[CanonicalEvent]:
    """Parse the latest public container event returned by ShipmentLink.

    Evergreen's anonymous container-number search exposes the current event only;
    it does not expose the full move history available from a B/L search.
    """
    events: list[CanonicalEvent] = []
    for table in parse_tables(html):
        if len(table) < 2:
            continue
        header_row = None
        mapping: dict[str, int] = {}
        for index, row in enumerate(table[:-1]):
            candidate = _header_index([cell["text"] for cell in row])
            if {"container", "date", "status"} <= candidate.keys():
                header_row = index
                mapping = candidate
                break
        if header_row is None:
            continue
        for row in table[header_row + 1 :]:
            status = _cell_text(row, mapping.get("status"))
            if not status:
                continue
            date_text = _cell_text(row, mapping.get("date"))
            location = _cell_text(row, mapping.get("location"))
            vessel_text = _cell_text(row, mapping.get("vessel"))
            vessel, voyage = _vessel_and_voyage(vessel_text)
            joined = " | ".join(
                part for part in (date_text, status, location, vessel_text) if part
            )
            event_type = classify_event_type(status)
            transport_mode = classify_transport(f"{status} {vessel_text}")
            if vessel and event_type in {"LOAD", "DEPA", "DISC", "ARRI"}:
                transport_mode = "VESSEL"
            _, event_date, event_time = parse_timestamp(date_text)
            events.append(
                CanonicalEvent(
                    classifier=classify_classifier(joined),
                    type=event_type,
                    location_raw=location,
                    location_norm=normalize_key(location) if location else "",
                    timestamp_raw=date_text,
                    event_date=event_date,
                    event_time=event_time,
                    sequence_index=len(events),
                    vessel=vessel if transport_mode == "VESSEL" else None,
                    voyage=voyage if transport_mode == "VESSEL" else None,
                    empty=classify_empty(joined),
                    transport_mode=transport_mode,
                    raw_text=joined,
                )
            )
        if events:
            break
    return events


class EvergreenTracker(BaseTracker):
    carrier_code = "EGLV"
    timeline_order = "newest_first"
    tracking_url = TRACK_URL
    screenshot_selectors = (
        'table.ec-table.ec-table-sm:has-text("提单货柜信息和当前动态")',
        'table.ec-table.ec-table-sm:has-text("Container(s) Information on B/L and Current Status")',
    )

    async def open_page(self) -> None:
        await self.open_tracking_or_reuse("evergreen-shipping.cn")

    async def search(self, container: str) -> None:
        try:
            response = await self.page.context.request.post(
                self.tracking_url,
                form={
                    "SEL": "s_cntr",
                    "NO": container,
                    "BL": "",
                    "CNTR": container,
                    "bkno": "",
                    "TYPE": "CNTR",
                },
                headers={
                    "Referer": self.tracking_url,
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=45_000,
            )
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status}")
            html = await response.text()
            lowered = html.lower()
            if container.lower() not in lowered or not any(
                marker in lowered
                for marker in (
                    "提单货柜信息和当前动态",
                    "container(s) information on b/l and current status",
                    "货柜动态",
                    "current status",
                    "没有找到您要查询的货柜信息",
                    "no container information was found",
                )
            ):
                raise RuntimeError("Evergreen returned an incomplete tracking page")
            html = html.replace(
                "<head>",
                '<head><base href="https://www.evergreen-shipping.cn/">',
                1,
            )
            await self.page.set_content(html, wait_until="domcontentloaded")
            self._search_submitted = True
        except Exception as exc:  # noqa: BLE001
            raise TrackerError(
                "Could not submit the Evergreen container search request.", "NAVIGATION"
            ) from exc

    async def parse_events(self) -> list[CanonicalEvent]:
        return parse_evergreen_html(await self.page.content())
