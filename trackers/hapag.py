"""Hapag-Lloyd public container tracking adapter."""

from __future__ import annotations

import logging
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
from trackers.base import BaseTracker, TrackerError, challenge_code

LOGGER = logging.getLogger("container_tracker")

_EXPAND_BUTTON = (
    "table:has-text('Latest Event') tbody tr.q-tr--hal:visible "
    "button.q-btn--icon-only"
)
_EXPAND_ROW = "table:has-text('Latest Event') tbody tr.q-tr--hal:visible"
_DETAILS = ".hal-event-tracking"

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


_SUMMARY_HEADERS = ("tare", "payload", "container no")
_BETA_EVENT_RE = re.compile(
    r'<div class="(hal-event(?:\s|")[^"]*)">\s*'
    r'<div class="hal-event__dot[^"]*">.*?</div>\s*'
    r'<div class="hal-event__inline">(.*?)</div>',
    re.S,
)


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip().strip(" >")


def _event_from_fields(
    *,
    status: str,
    date_text: str,
    location: str,
    transport: str,
    voyage: str,
    sequence: int,
    is_actual: bool | None,
) -> CanonicalEvent | None:
    if not status or status.lower() in {"status", "event"}:
        return None
    joined = " | ".join(
        part for part in (date_text, status, location, transport, voyage) if part
    )
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
        if transport_mode in {"MOTHER", "FEEDER", "VESSEL"}
        and transport
        and classify_transport(transport) != "TRUCK"
        else None
    )
    if vessel_name and classify_transport(vessel_name) in {"TRUCK", "RAIL", "BARGE"}:
        vessel_name = None
    return CanonicalEvent(
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


def _parse_hapag_beta(html: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    sequence = 0
    for match in _BETA_EVENT_RE.finditer(html):
        classes = match.group(1)
        inner = match.group(2)
        cols: dict[str, str] = {}
        for chunk in inner.split('<span class="hal-event__col" aria-labelledby="')[1:]:
            label, _, rest = chunk.partition('"')
            parts = label.split("-")
            kind = parts[2] if len(parts) >= 3 else label
            cols[kind] = _strip_tags(rest)
        status = cols.get("event", "")
        date_text = " ".join(part for part in (cols.get("date", ""), cols.get("time", "")) if part)
        is_actual = True if "hal-event--active" in classes else False
        event = _event_from_fields(
            status=status,
            date_text=date_text,
            location=cols.get("locationName", ""),
            transport=cols.get("transport", ""),
            voyage=cols.get("voyage", ""),
            sequence=sequence,
            is_actual=is_actual,
        )
        if event is None:
            continue
        events.append(event)
        sequence += 1
    return events


def _parse_hapag_tables(html: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    sequence = 0
    for table in parse_tables(html):
        if len(table) < 2:
            continue
        headers = [cell["text"] for cell in table[0]]
        header_blob = " ".join(headers).lower()
        if any(token in header_blob for token in _SUMMARY_HEADERS):
            continue
        mapping = _header_index(headers)
        body = table[1:]
        if len(mapping) < 2:
            mapping = {"date": 0, "status": 1, "location": 2, "transport": 3, "voyage": 4}
            body = table
        has_bold = any(cell.get("bold") for row in body for cell in row)
        for row in body:
            status = _cell_text(row, mapping.get("status"))
            is_bold = any(cell.get("bold") for cell in row)
            is_actual = True if is_bold else (False if has_bold else None)
            event = _event_from_fields(
                status=status,
                date_text=_cell_text(row, mapping.get("date")),
                location=_cell_text(row, mapping.get("location")),
                transport=_cell_text(row, mapping.get("transport")),
                voyage=_cell_text(row, mapping.get("voyage")),
                sequence=sequence,
                is_actual=is_actual,
            )
            if event is None:
                continue
            events.append(event)
            sequence += 1
    return events


def parse_hapag_html(html: str) -> list[CanonicalEvent]:
    """Parse movement rows from a Hapag tracing HTML snapshot."""
    events = _parse_hapag_beta(html)
    if events:
        return events
    return _parse_hapag_tables(html)


class HapagTracker(BaseTracker):
    carrier_code = "HLCU"
    timeline_order = "oldest_first"
    tracking_url = TRACK_URL
    screenshot_selectors = (
        ".hal-event-tracking",
        "table:has-text('Latest Event')",
    )

    async def expand_result_details(self) -> None:
        """Open the right-hand chevron so movement details are visible."""
        details = self.page.locator(_DETAILS)
        try:
            if await details.first.is_visible(timeout=400):
                return
        except Exception:  # noqa: BLE001
            pass
        for selector in (_EXPAND_BUTTON, _EXPAND_ROW):
            target = self.page.locator(selector)
            try:
                if not await target.first.is_visible(timeout=1500):
                    continue
                await target.first.click(timeout=3_000)
                await details.first.wait_for(state="visible", timeout=8_000)
                await self.page.wait_for_timeout(400)
                return
            except Exception:  # noqa: BLE001
                continue
        LOGGER.info("Hapag result details stayed collapsed; screenshot may lack events.")

    async def prepare_for_screenshot(self) -> None:
        await self.expand_result_details()
        await super().prepare_for_screenshot()

    async def dismiss_onboarding(self) -> None:
        """Close the Tracking Beta welcome tour so the search field is actionable."""
        dialog = self.page.locator(".q-dialog--modal, [role='dialog']")
        try:
            await dialog.first.wait_for(state="visible", timeout=4_000)
        except Exception:  # noqa: BLE001
            return
        close = self.page.locator(
            ".q-dialog--modal button.q-btn--icon-only, "
            ".q-dialog--modal button[aria-label*='close' i], "
            "[role='dialog'] button:has-text('Skip')"
        )
        try:
            if await close.first.is_visible(timeout=800):
                await close.first.click(timeout=3_000, force=True)
                await self.page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(4):
            next_btn = self.page.locator(
                ".q-dialog--modal button:has-text('Next'), "
                ".q-dialog--modal button:has-text('Got it'), "
                ".q-dialog--modal button:has-text('Done'), "
                ".q-dialog--modal button:has-text('Start')"
            )
            try:
                if await next_btn.first.is_visible(timeout=500):
                    await next_btn.first.click(timeout=3_000)
                    await self.page.wait_for_timeout(400)
                    continue
            except Exception:  # noqa: BLE001
                break
            break
        try:
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass

    async def open_page(self) -> None:
        if not await self.open_tracking_or_reuse("hapag-lloyd.com"):
            return
        await self.dismiss_cookies(wait_ms=8_000)
        html = await self.page.content()
        lowered = html.lower()
        if challenge_code(lowered):
            return
        if "outdated browser" in lowered and "container" not in lowered:
            await self.page.goto(TRACING_URL, wait_until="domcontentloaded")
        await self.dismiss_onboarding()

    async def search(self, container: str) -> None:
        await self.dismiss_cookies()
        await self.dismiss_onboarding()
        try:
            await self.page.evaluate(
                """() => {
                    const form = document.querySelector('form.q-form');
                    if (form && !form.dataset.ctBound) {
                        form.dataset.ctBound = '1';
                        form.addEventListener('submit', (event) => event.preventDefault(), true);
                    }
                }"""
            )
        except Exception:  # noqa: BLE001
            pass
        field = None
        for selector in (
            "input[data-cy='tracking-search-input']",
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
        try:
            await field.click(timeout=5_000)
            await field.fill("")
            await field.fill(container)
        except Exception:  # noqa: BLE001
            await field.fill(container, force=True)
        clicked = False
        for selector in (
            "button[title='Search']",
            "button:has-text('Search')",
            "button:has-text('Track')",
            "input[type='submit']",
            "button[type='submit']",
        ):
            button = self.page.locator(selector)
            try:
                if await button.first.is_visible(timeout=1500):
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
        try:
            await self.page.wait_for_function(
                """() => {
                    const text = (document.body && document.body.innerText || "").toLowerCase();
                    return (
                        text.includes("no result") ||
                        text.includes("not found") ||
                        text.includes("could not find") ||
                        text.includes("can't identify") ||
                        text.includes("cannot identify") ||
                        text.includes("no tracking") ||
                        text.includes("number is not valid") ||
                        text.includes("latest event") ||
                        text.includes("shipment details") ||
                        text.includes("tracking details") ||
                        !!document.querySelector(".hal-event") ||
                        !!document.querySelector("table tbody tr")
                    );
                }""",
                timeout=20_000,
            )
        except Exception:  # noqa: BLE001
            try:
                await self.page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:  # noqa: BLE001
                pass

    async def parse_events(self) -> list[CanonicalEvent]:
        html = await self.page.content()
        events = parse_hapag_html(html)
        if events:
            return events
        text = (await self._visible_text()).lower()
        if "number is not valid" in text:
            raise TrackerError("Carrier rejected the container number as invalid.", "INVALID_INPUT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
