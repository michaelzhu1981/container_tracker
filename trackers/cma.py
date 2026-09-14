"""CMA CGM public shipment tracking adapter."""

from __future__ import annotations

import json
import logging
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
from models import CanonicalEvent, Classifier, TransportMode
from ports import normalize_key
from trackers.base import BaseTracker, TrackerError, looks_like_no_result

LOGGER = logging.getLogger("container_tracker")

TRACK_URL = "https://www.cma-cgm.com/ebusiness/tracking"

_CLICK_PREVIOUS_MOVES_JS = """() => {
    const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return r.width > 8 && r.height > 8
            && cs.display !== "none" && cs.visibility !== "hidden";
    };
    const seen = new Set();
    let n = 0;
    const targets = document.querySelectorAll(
        "a[aria-label='Display Previous Moves'], "
        + "a[aria-label='Display Details'], "
        + ".k-hierarchy-cell[aria-expanded='false'] a"
    );
    for (const el of targets) {
        if (seen.has(el) || !visible(el)) continue;
        const label = (el.getAttribute("aria-label") || el.innerText || "").toLowerCase();
        if (label.includes("hide previous")) continue;
        seen.add(el);
        const cell = el.closest(".k-hierarchy-cell") || el;
        cell.click();
        if (cell !== el) el.click();
        n += 1;
    }
    return n;
}"""

_VISIBLE_EVENT_COUNT_JS = """() => {
    const skip = new Set(["pol", "pod"]);
    let n = 0;
    for (const el of document.querySelectorAll("#gridTrackingDetails .capsule, .capsule")) {
        const text = (el.innerText || "").trim().toLowerCase();
        if (!text || skip.has(text)) continue;
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        if (r.height > 8 && r.width > 8 && cs.display !== "none" && cs.visibility !== "hidden") {
            n += 1;
        }
    }
    return n;
}"""

_HEADER_HINTS = {
    "date": ("date", "time"),
    "status": ("moves", "move", "event", "status", "activity"),
    "location": ("location", "place"),
    "transport": ("vessel", "voyage", "transport"),
}

_VOYAGE_RE = re.compile(r"\(([A-Z0-9]{4,})\)|/ *([A-Z0-9]{4,})\b", re.I)
_RESPONSE_DATA_RE = re.compile(
    r"responseData\s*:\s*('(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")",
    re.S,
)
_GRID_ROW_RE = re.compile(
    r'<tr[^>]*class="[^"]*\b(done|current|coming)\b[^"]*"[^>]*>(.*?)</tr>',
    re.S | re.I,
)
_CALENDAR_RE = re.compile(r'class="calendar"[^>]*>(.*?)</span>', re.S | re.I)
_TIME_RE = re.compile(r'class="time"[^>]*>(.*?)</span>', re.S | re.I)
_CAPSULE_RE = re.compile(r'class="capsule"[^>]*>(.*?)</span>', re.S | re.I)
_LOCATION_RE = re.compile(
    r'class="[^"]*\blocation\b[^"]*"[^>]*>(.*?)</td>',
    re.S | re.I,
)
_VESSEL_CELL_RE = re.compile(
    r'class="[^"]*vesselVoyage[^"]*"[^>]*>(.*?)</td>',
    re.S | re.I,
)
_SKIP_STATUS = frozenset({"", "moves", "move", "event", "status", "activity"})
_EXPAND_POLLS = 4

_DISMISS_SESSION_TIMEOUT_JS = """() => {
    const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return r.width > 8 && r.height > 8
            && cs.display !== "none" && cs.visibility !== "hidden";
    };
    const buttons = [...document.querySelectorAll("button")].filter(visible);
    const labelOf = (el) => (
        (el.innerText || el.textContent || "") + " " + (el.getAttribute("aria-label") || "")
    ).toLowerCase();
    const login = buttons.find((el) => labelOf(el).includes("go to the login page"));
    if (login) return "login";
    const stay = buttons.find((el) => {
        const label = labelOf(el);
        return label.includes("i am here") || label.includes("let's continue")
            || label.includes("lets continue");
    });
    if (!stay) return false;
    stay.click();
    return true;
}"""
_TRANSPORT_MAP: dict[str, TransportMode] = {
    "VESSEL": "VESSEL",
    "FEEDER": "FEEDER",
    "BARGE": "BARGE",
    "TRUCK": "TRUCK",
    "RAIL": "RAIL",
    "TRAIN": "RAIL",
}


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
    if not blob:
        return None, None
    match = _VOYAGE_RE.search(blob)
    voyage = None
    vessel = blob
    if match:
        voyage = (match.group(1) or match.group(2) or "").upper() or None
        vessel = (blob[: match.start()] + blob[match.end() :]).strip(" /-")
    vessel = " ".join(vessel.split()).strip(" -/()") or None
    return vessel, voyage


def _transport_mode(status: str, transport: str, mode_hint: str = "") -> TransportMode:
    mapped = _TRANSPORT_MAP.get(mode_hint.strip().upper())
    if mapped:
        return mapped
    mode = classify_transport(f"{status} {transport} {mode_hint}")
    if mode == "UNKNOWN" and classify_event_type(status) in {"LOAD", "DEPA", "ARRI", "DISC"}:
        if _voyage_and_vessel(transport)[0] or "vessel" in f"{status} {transport}".lower():
            return "VESSEL"
    return mode


def _cma_classifier(joined: str, *, state: str = "", estimated: bool = False) -> Classifier:
    blob = joined.lower()
    if estimated or "forthcoming" in blob or "coming" in state.lower() or "provisional" in blob:
        return "EST"
    if state.lower() in {"done", "current"}:
        return "ACT"
    return classify_classifier(joined)


def _event_from_fields(
    *,
    status: str,
    date_text: str,
    location: str,
    transport: str,
    sequence: int,
    mode_hint: str = "",
    state: str = "",
    estimated: bool = False,
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
    transport_mode = _transport_mode(status, transport, mode_hint)
    _, event_date, event_time = parse_timestamp(date_text or joined)
    if vessel_name and transport_mode not in {"MOTHER", "FEEDER", "VESSEL"}:
        vessel_name = None
        voyage_no = None
    return CanonicalEvent(
        classifier=_cma_classifier(joined, state=state, estimated=estimated),
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


def _events_from_cma_payload(payload: object) -> list[CanonicalEvent]:
    documents: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            keys = {str(key).lower() for key in node}
            if {"pastmoves", "currentmoves", "provisionalmoves"} & keys:
                documents.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    events: list[CanonicalEvent] = []
    for document in documents or (
        [payload] if isinstance(payload, dict) and "StatusDescription" in payload else []
    ):
        groups = (
            (document.get("PastMoves") or [], "done", False),
            (document.get("CurrentMoves") or [], "current", False),
            (document.get("ProvisionalMoves") or [], "coming", True),
        )
        for moves, state, estimated in groups:
            if not isinstance(moves, list):
                continue
            for item in moves:
                if not isinstance(item, dict):
                    continue
                status = _mapping_value(item, ("statusdescription", "moves", "status", "event"))
                date_text = " ".join(
                    part
                    for part in (
                        _mapping_value(item, ("datestring", "date")),
                        _mapping_value(item, ("timestring", "time")),
                    )
                    if part
                )
                location = _mapping_value(item, ("location", "locationterminal"))
                vessel = _mapping_value(item, ("vessel",)) or None
                voyage = _mapping_value(item, ("voyage",)) or None
                transport = " ".join(part for part in (vessel or "", f"({voyage})" if voyage else "") if part)
                event = _event_from_fields(
                    status=status,
                    date_text=date_text,
                    location=location,
                    transport=transport,
                    sequence=len(events),
                    mode_hint=_mapping_value(item, ("modeoftransport",)),
                    state=_mapping_value(item, ("state",)) or state,
                    estimated=estimated,
                    vessel=vessel,
                    voyage=voyage,
                )
                if event:
                    events.append(event)
    if events:
        return events

    found: list[dict] = []

    def walk_moves(node: object) -> None:
        if isinstance(node, dict):
            keys = {str(key).lower() for key in node}
            if "statusdescription" in keys and (
                "datestring" in keys or "date" in keys
            ):
                found.append(node)
            for value in node.values():
                walk_moves(value)
        elif isinstance(node, list):
            for item in node:
                walk_moves(item)

    walk_moves(payload)
    for item in found:
        event = _event_from_fields(
            status=_mapping_value(item, ("statusdescription", "moves", "status")),
            date_text=" ".join(
                part
                for part in (
                    _mapping_value(item, ("datestring", "date")),
                    _mapping_value(item, ("timestring", "time")),
                )
                if part
            ),
            location=_mapping_value(item, ("location",)),
            transport=" ".join(
                part
                for part in (
                    _mapping_value(item, ("vessel",)),
                    f"({_mapping_value(item, ('voyage',))})"
                    if _mapping_value(item, ("voyage",))
                    else "",
                )
                if part
            ),
            sequence=len(events),
            mode_hint=_mapping_value(item, ("modeoftransport",)),
            state=_mapping_value(item, ("state",)),
            vessel=_mapping_value(item, ("vessel",)) or None,
            voyage=_mapping_value(item, ("voyage",)) or None,
        )
        if event:
            events.append(event)
    return events


def _decode_js_string(token: str) -> str | None:
    try:
        return json.loads(token)
    except json.JSONDecodeError:
        try:
            return json.loads('"' + token.strip("'\"") + '"')
        except json.JSONDecodeError:
            return None


def _events_from_embedded_json(html: str) -> list[CanonicalEvent]:
    for match in _RESPONSE_DATA_RE.finditer(html):
        blob = _decode_js_string(match.group(1))
        if not blob:
            continue
        try:
            events = _events_from_cma_payload(json.loads(blob))
        except json.JSONDecodeError:
            continue
        if events:
            return events
    for match in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        blob = match.group(1).strip()
        if "PastMoves" not in blob and "StatusDescription" not in blob:
            continue
        for candidate in re.finditer(r"\{(?:[^{}]|\{[^{}]*\})*\}", blob):
            text = candidate.group(0)
            if "PastMoves" not in text and "StatusDescription" not in text:
                continue
            try:
                events = _events_from_cma_payload(json.loads(text))
            except json.JSONDecodeError:
                continue
            if events:
                return events
    return []


def _parse_cma_grid(html: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    for match in _GRID_ROW_RE.finditer(html):
        state = match.group(1).lower()
        row = match.group(2)
        calendar = _CALENDAR_RE.search(row)
        time_match = _TIME_RE.search(row)
        capsule = _CAPSULE_RE.search(row)
        location_match = _LOCATION_RE.search(row)
        vessel_match = _VESSEL_CELL_RE.search(row)
        status = _strip_tags(capsule.group(1)) if capsule else ""
        date_text = " ".join(
            part
            for part in (
                _strip_tags(calendar.group(1)) if calendar else "",
                _strip_tags(time_match.group(1)) if time_match else "",
            )
            if part
        )
        location = _strip_tags(location_match.group(1)) if location_match else ""
        transport = _strip_tags(vessel_match.group(1)) if vessel_match else ""
        event = _event_from_fields(
            status=status,
            date_text=date_text,
            location=location,
            transport=transport,
            sequence=len(events),
            state=state,
            estimated=state == "coming",
        )
        if event:
            events.append(event)
    return events


def _parse_cma_tables(html: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    for table in parse_tables(html):
        if len(table) < 2:
            continue
        headers = [cell["text"] for cell in table[0]]
        mapping = _header_index(headers)
        if "status" not in mapping:
            continue
        for row in table[1:]:
            classes = " ".join(cell.get("class", "") for cell in row).lower()
            state = next((token for token in ("done", "current", "coming") if token in classes), "")
            event = _event_from_fields(
                status=_cell_text(row, mapping.get("status")),
                date_text=_cell_text(row, mapping.get("date")),
                location=_cell_text(row, mapping.get("location")),
                transport=_cell_text(row, mapping.get("transport")),
                sequence=len(events),
                state=state,
                estimated=state == "coming" or "coming" in classes,
            )
            if event:
                events.append(event)
    return events


def parse_cma_html(html: str) -> list[CanonicalEvent]:
    """Parse movement rows from a CMA CGM tracking HTML snapshot."""
    events = _events_from_embedded_json(html)
    if not events and "gridTrackingDetails" in html:
        events = _parse_cma_grid(html)
    if not events:
        events = _parse_cma_tables(html)
    return events


class CmaTracker(BaseTracker):
    carrier_code = "CMDU"
    wait_in_current_browser = True
    use_system_chrome = True
    system_chrome_host = "cma-cgm.com"
    system_chrome_challenge = "DataDome"
    timeline_order = "oldest_first"
    tracking_url = TRACK_URL
    screenshot_selectors = (
        "#trackingsearchsection",
        "#gridTrackingDetails",
        "table:has-text('Moves')",
        "table:has-text('LOADED ON BOARD')",
        ".result-card--details",
    )

    async def dismiss_session_timeout(self) -> None:
        try:
            handled = await self.page.evaluate(_DISMISS_SESSION_TIMEOUT_JS)
        except Exception:  # noqa: BLE001
            return
        if handled == "login":
            try:
                await self.page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            return
        if handled:
            await self.page.wait_for_timeout(300)

    async def _visible_event_count(self) -> int:
        try:
            return int(await self.page.evaluate(_VISIBLE_EVENT_COUNT_JS) or 0)
        except Exception:  # noqa: BLE001
            return 0

    async def _click_previous_moves(self) -> int:
        try:
            return int(await self.page.evaluate(_CLICK_PREVIOUS_MOVES_JS) or 0)
        except Exception:  # noqa: BLE001
            return 0

    async def expand_result_details(self) -> None:
        if getattr(self, "_details_expanded", False):
            return
        before = await self._visible_event_count()
        # Grid/JSON already has the timeline. Clicking "Previous Moves" via
        # Apple Events often reports success without adding capsules, then
        # the old 8s wait ran again from parse and screenshot.
        if before > 1:
            self._details_expanded = True
            return
        clicked = await self._click_previous_moves()
        if clicked:
            for _ in range(_EXPAND_POLLS):
                now = await self._visible_event_count()
                if now > before:
                    await self.page.wait_for_timeout(400)
                    break
                await self.page.wait_for_timeout(200)
            else:
                LOGGER.info("CMA previous moves stayed collapsed; screenshot may lack older events.")
        self._details_expanded = True

    async def prepare_for_screenshot(self) -> None:
        await self.expand_result_details()
        await super().prepare_for_screenshot()

    async def open_page(self) -> None:
        if not await self.open_tracking_or_reuse("cma-cgm.com"):
            return
        await self.dismiss_cookies(wait_ms=8_000)
        await self.dismiss_session_timeout()

    async def search(self, container: str) -> None:
        self._search_submitted = False
        self._details_expanded = False
        if await self._page_challenge_code():
            return
        await self.dismiss_cookies(wait_ms=0)
        await self.dismiss_session_timeout()
        field = None
        for selector in (
            "#Reference",
            "input[name='SearchViewModel.Reference']",
            "input[placeholder*='Container' i]",
            "input[placeholder*='ABCD' i]",
        ):
            locator = self.page.locator(selector)
            try:
                if await locator.first.is_visible(timeout=3_000):
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
            "#btnTracking",
            "button:has-text('Search')",
            "button:has-text('Track')",
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
        await self.expand_result_details()

    async def _wait_for_results(self) -> None:
        try:
            await self.page.wait_for_function(
                """() => {
                    const text = (document.body && document.body.innerText || "").toLowerCase();
                    const err = document.querySelector("#trackingAlertError");
                    const errVisible = !!(err && getComputedStyle(err).display !== "none");
                    return (
                        !!document.querySelector("#gridTrackingDetails .capsule") ||
                        !!document.querySelector("#gridTrackingDetails tbody tr") ||
                        text.includes("loaded on board") ||
                        text.includes("empty to shipper") ||
                        text.includes("ready to be loaded") ||
                        errVisible ||
                        (DETECT_CHALLENGE)()
                    );
                }""".replace("DETECT_CHALLENGE", CHALLENGE_CODE_JS),
                timeout=40_000,
            )
        except Exception:  # noqa: BLE001
            pass

    async def parse_events(self) -> list[CanonicalEvent]:
        await self.expand_result_details()
        html = await self.page.content()
        events = parse_cma_html(html)
        if events:
            return events
        text = await self._visible_text()
        if looks_like_no_result(text):
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
