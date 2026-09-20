"""HMM public Track & Trace adapter."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from challenges import CHALLENGE_CODE_JS, challenge_code
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
LOGGER = logging.getLogger("container_tracker")

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

_SUBMIT_SEARCH_JS = r"""(container) => {
    const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden"
            && rect.width > 8 && rect.height > 8;
    };
    const field = Array.from(document.querySelectorAll("input[name='srchCntrNo1']"))
        .find(visible);
    if (!field) {
        return document.querySelector("#thisCntr") ? "result_page" : "field_missing";
    }
    const button = Array.from(document.querySelectorAll("button"))
        .find((el) => visible(el) && (el.innerText || el.textContent || "")
            .trim().toLowerCase().includes("retrieve"));
    if (!button) return "button_missing";

    const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
    const descriptor = proto && Object.getOwnPropertyDescriptor(proto, "value");
    if (descriptor && descriptor.set) descriptor.set.call(field, container);
    else field.value = container;
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
    field.focus();

    // The marker belongs to this document. A successful HMM POST replaces the
    // document, so the result wait cannot accept the previous container page.
    window.__ctHmmDocumentMarker = `container-tracker:${container}`;
    window.setTimeout(() => button.click(), 0);
    return "submitted";
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
    wait_in_current_browser = True
    use_system_chrome = True
    system_chrome_host = "hmm21.com"
    system_chrome_challenge = "HMM access check"
    timeline_order = "newest_first"
    tracking_url = TRACK_URL
    screenshot_cookie_wait_ms = 0
    reuse_parsed_html_for_artifacts = True
    screenshot_selectors = (
        "#shipmentProgress",
        "#trackingInfomationDateResultTable",
        "table:has-text('Status Description')",
        "table:has-text('Vessel Departure from POL')",
    )

    def _record_timing(self, phase: str, started: float) -> None:
        timings = getattr(self, "_phase_timings_ms", None)
        if timings is not None:
            timings[phase] = timings.get(phase, 0.0) + (
                time.perf_counter() - started
            ) * 1000

    async def prepare_session(self) -> None:
        started = time.perf_counter()
        try:
            await super().prepare_session()
        finally:
            LOGGER.info(
                "HDMU_TIMING phase=session_prepare duration_ms=%.0f",
                (time.perf_counter() - started) * 1000,
            )

    async def track(self, container: str, *, session_ready: bool = False):
        self._phase_timings_ms: dict[str, float] = {}
        started = time.perf_counter()
        try:
            return await super().track(container, session_ready=session_ready)
        finally:
            total_ms = (time.perf_counter() - started) * 1000
            known_ms = sum(
                value
                for key, value in self._phase_timings_ms.items()
                if key.startswith(("search_", "parse_"))
                or key == "artifacts_total"
            )
            payload = {
                key: round(value)
                for key, value in sorted(self._phase_timings_ms.items())
            }
            payload["other"] = round(max(0.0, total_ms - known_ms))
            payload["total"] = round(total_ms)
            LOGGER.info(
                "HDMU_TIMING container=%s durations_ms=%s",
                container,
                json.dumps(payload, sort_keys=True),
            )

    async def expand_result_details(self) -> None:
        if getattr(self, "_details_expanded", False):
            return
        try:
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
        finally:
            # Search, parse and screenshot all ask for expanded details. One
            # result page needs at most one pair of Apple Event probes.
            self._details_expanded = True

    async def prepare_for_screenshot(self) -> None:
        started = time.perf_counter()
        try:
            await self.expand_result_details()
            await super().prepare_for_screenshot()
        finally:
            self._record_timing("screenshot_prepare", started)

    async def _screenshot_query_content(self, path: Path) -> bool:
        started = time.perf_counter()
        try:
            if not getattr(self.page, "is_system_chrome", False):
                return await super()._screenshot_query_content(path)
            for selector in self.screenshot_selectors:
                locator = self.page.locator(selector)
                try:
                    if await locator.first.is_visible(timeout=300):
                        # A single visible crop preserves quick evidence without
                        # scrolling and stitching the whole HMM result section.
                        await self.page.screenshot(
                            path=str(path), selector=selector, single_view=True
                        )
                        return True
                except Exception:  # noqa: BLE001
                    continue
            return False
        finally:
            self._record_timing("screenshot_capture", started)

    async def save_artifacts(self, container: str, *, allow_screenshot: bool = True) -> None:
        started = time.perf_counter()
        try:
            await super().save_artifacts(
                container, allow_screenshot=allow_screenshot
            )
        finally:
            self._record_timing("artifacts_total", started)

    async def open_page(self) -> None:
        if not await self.open_tracking_or_reuse("hmm21.com"):
            return
        await self.dismiss_cookies(wait_ms=8_000)

    async def _raise_if_blocked(self) -> None:
        text = await self._visible_text()
        html = ""
        try:
            html = await self.page.content()
        except Exception:  # noqa: BLE001
            html = ""
        code = challenge_code(text) or challenge_code(html)
        if code:
            raise TrackerError("HMM blocked this connection.", code)

    async def search(self, container: str) -> None:
        self._search_submitted = False
        self._details_expanded = False
        started = time.perf_counter()
        await self.dismiss_cookies(wait_ms=0)
        await self._raise_if_blocked()
        self._record_timing("search_preflight", started)

        started = time.perf_counter()
        submit_state = await self.page.evaluate(_SUBMIT_SEARCH_JS, container)
        self._record_timing("search_submit", started)
        if submit_state in {"field_missing", "result_page"}:
            # HMM keeps a hidden search input in result documents. Waiting for
            # that input used to cost 12 seconds for every box after the first.
            started = time.perf_counter()
            await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
            await self.dismiss_cookies(wait_ms=0)
            try:
                await self.page.wait_for_function(
                    """() => {
                        const visible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== "none" && style.visibility !== "hidden"
                                && rect.width > 8 && rect.height > 8;
                        };
                        return Array.from(
                            document.querySelectorAll("input[name='srchCntrNo1']")
                        ).some(visible) || (DETECT_CHALLENGE)();
                    }""".replace("DETECT_CHALLENGE", CHALLENGE_CODE_JS),
                    timeout=8_000,
                )
            except Exception:  # noqa: BLE001
                pass
            await self._raise_if_blocked()
            submit_state = await self.page.evaluate(_SUBMIT_SEARCH_JS, container)
            self._record_timing("search_return_to_form", started)

        if submit_state != "submitted":
            code = "SELECTOR"
            detail = (
                "container search field"
                if submit_state == "field_missing"
                else "Retrieve button"
            )
            raise TrackerError(f"Could not find the {detail}.", code)

        self._search_submitted = True
        started = time.perf_counter()
        await self._wait_for_results(container)
        self._record_timing("search_wait_result", started)
        started = time.perf_counter()
        await self.expand_result_details()
        self._record_timing("search_expand", started)

    async def _wait_for_results(self, container: str) -> None:
        needle = json.dumps(container.strip().lower())
        document_marker = f"container-tracker:{container}"
        try:
            await self.page.wait_for_function(
                """(documentMarker) => {
                    const needle = NEEDLE;
                    const text = (document.body && document.body.innerText || "").toLowerCase();
                    const result = document.querySelector("#thisCntr");
                    const resultContainer = (
                        result && (result.value || result.textContent || "")
                    ).trim().toLowerCase();
                    // HMM usually replaces the result area in the same
                    // document. An exact container match is therefore the
                    // strongest completion signal and must precede the old
                    // document guard.
                    if (resultContainer === needle) return true;
                    if ((DETECT_CHALLENGE)()) return true;
                    if (window.__ctHmmDocumentMarker === documentMarker) return false;
                    return (
                        text.includes("container no. is invalid") ||
                        text.includes("invalid") && text.includes("container")
                    );
                }"""
                .replace("NEEDLE", needle)
                .replace("DETECT_CHALLENGE", CHALLENGE_CODE_JS),
                arg=document_marker,
                timeout=30_000,
            )
        except Exception:  # noqa: BLE001
            pass

    async def parse_events(self) -> list[CanonicalEvent]:
        await self.expand_result_details()
        started = time.perf_counter()
        html = await self.page.content()
        self._record_timing("parse_read_html", started)
        self._parsed_html = html
        started = time.perf_counter()
        events = parse_hmm_html(html)
        self._record_timing("parse_html", started)
        if events:
            return events
        text = await self._visible_text()
        if looks_like_no_result(text) or "container no. is invalid" in text.lower():
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
