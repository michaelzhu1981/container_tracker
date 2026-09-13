"""Ocean Network Express public cargo tracking adapter."""

from __future__ import annotations

import json
import re
from urllib.parse import quote

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
from challenges import CHALLENGE_CODE_JS
from trackers.base import BaseTracker, TrackerError

TRACK_URL = "https://www.one-line.com/one-ecom/manage-shipment/cargo-tracking"
_TOTAL_RESULT_RE = re.compile(r"total\s+(\d+)\s+result", re.I)

ONE_RESULT_STATE_JS = """() => {
    const text = (document.body && document.body.innerText) || "";
    return {
        text,
        hasTable: !!document.querySelector("table[class*='EventTable']"),
        hasDetail: !!document.querySelector("[class*='CargoTrackingDetail']"),
        loading: /in progress/i.test(text),
    };
}"""


def one_has_result(state: dict) -> bool:
    """True when the page shows a real container hit, not Total 0 / empty."""
    if state.get("hasTable") or state.get("hasDetail"):
        return True
    text = str(state.get("text") or "").lower()
    match = _TOTAL_RESULT_RE.search(text)
    if match and int(match.group(1)) > 0:
        return True
    return "loaded on vessel" in text


def one_search_settled(state: dict) -> bool:
    """True when loading finished and a table, a hit, or a settled miss is visible."""
    if state.get("loading"):
        return False
    if one_has_result(state):
        return True
    text = str(state.get("text") or "").lower()
    match = _TOTAL_RESULT_RE.search(text)
    if match:
        return True
    return "没有查询结果" in text or "查无" in text

_VOYAGE_RE = re.compile(r"\b(\d{2,4}[A-Z])\b", re.I)
_EVENT_ROW_RE = re.compile(
    r'<tr[^>]*EventTable_table-row[^>]*>(.*?)</tr>',
    re.S | re.I,
)
_COUNTRY_RE = re.compile(r'EventTable_country-name[^>]*>(.*?)</div>', re.S | re.I)
_EVENT_NAME_RE = re.compile(
    r'EventTable_event-name-vessel-group[^>]*>\s*<div[^>]*>(.*?)</div>',
    re.S | re.I,
)
_VESSEL_LINK_RE = re.compile(
    r'EventTable_event-name-vessel-group.*?<a[^>]*>\s*<span[^>]*>(.*?)</span>',
    re.S | re.I,
)
_DATE_BLOCK_RE = re.compile(
    r'EventDate_event-date-container[^>]*>(.*?)</div>\s*</div>',
    re.S | re.I,
)

_HEADER_HINTS = {
    "date": ("date", "time"),
    "status": ("event", "status", "activity", "movement"),
    "location": ("location", "place", "facility", "terminal"),
    "transport": ("vessel", "transport", "mode"),
    "voyage": ("voyage", "voy"),
}

_JSON_STATUS_KEYS = (
    "eventname",
    "statusnm",
    "evntnm",
    "status",
    "copstsdesc",
    "description",
)
_JSON_DATE_KEYS = ("eventdate", "eventdt", "evntdt", "actdt", "date")
_JSON_LOC_KEYS = ("locationname", "nodnm", "nodcd", "location")
_JSON_VSL_KEYS = ("vesselengname", "vslengnm", "vslnm", "vessel")
_JSON_VOY_KEYS = ("schedulevoyagenumber", "skdvoyno", "voyage", "voyageno")


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
    blob = re.sub(r"\([^)]*\)", "", blob).strip()
    match = _VOYAGE_RE.search(blob)
    voyage = match.group(1).upper() if match else None
    vessel = blob
    if match:
        vessel = (blob[: match.start()] + blob[match.end() :]).strip(" /-")
    vessel = " ".join(vessel.split()).strip(" -/") or None
    return vessel, voyage


def _one_classifier(row_html: str, joined: str) -> Classifier:
    if "text-ds-grey-darker-2" in row_html or "text-ds-grey-darker-3" in row_html:
        return "EST"
    if "text-ds-grey-darker-1" in row_html or "text-ds-primary" in row_html:
        return "ACT"
    return classify_classifier(joined)


def _event_from_fields(
    *,
    status: str,
    date_text: str,
    location: str,
    transport: str,
    sequence: int,
    classifier: Classifier | None = None,
) -> CanonicalEvent | None:
    if not status or status.lower() in {"status", "event", "latest event"}:
        return None
    vessel_name, voyage = _voyage_and_vessel(transport)
    joined = " | ".join(part for part in (date_text, status, location, transport) if part)
    event_type = classify_event_type(status)
    transport_mode = classify_transport(f"{status} {transport}")
    if transport_mode == "UNKNOWN" and event_type in {"LOAD", "DEPA"}:
        transport_mode = "VESSEL"
    _, event_date, event_time = parse_timestamp(date_text or joined)
    if vessel_name and transport_mode not in {"MOTHER", "FEEDER", "VESSEL"}:
        vessel_name = None
    return CanonicalEvent(
        classifier=classifier or classify_classifier(joined),
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


def _fill_forward(events: list[CanonicalEvent]) -> list[CanonicalEvent]:
    last_location = ""
    last_vessel = None
    last_voyage = None
    filled: list[CanonicalEvent] = []
    for event in events:
        location = event.location_raw or last_location
        if location:
            last_location = location
        if event.vessel:
            last_vessel = event.vessel
            last_voyage = event.voyage
        elif event.type in {"LOAD", "DEPA", "ARRI", "DISC"}:
            event.vessel = last_vessel
            event.voyage = last_voyage
        if location and not event.location_raw:
            event.location_raw = location
            event.location_norm = normalize_key(location)
        filled.append(event)
    return filled


def _parse_event_table(html: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    sequence = 0
    last_country = ""
    for match in _EVENT_ROW_RE.finditer(html):
        row_html = match.group(1)
        country = _strip_tags(m.group(1)) if (m := _COUNTRY_RE.search(row_html)) else ""
        if country:
            last_country = country
        status = _strip_tags(m.group(1)) if (m := _EVENT_NAME_RE.search(row_html)) else ""
        vessel = _strip_tags(m.group(1)) if (m := _VESSEL_LINK_RE.search(row_html)) else ""
        date_text = _strip_tags(m.group(1)) if (m := _DATE_BLOCK_RE.search(row_html)) else ""
        if not status:
            continue
        event = _event_from_fields(
            status=status,
            date_text=date_text,
            location=country or last_country,
            transport=vessel,
            sequence=sequence,
            classifier=_one_classifier(row_html, " | ".join(part for part in (date_text, status) if part)),
        )
        if event is None:
            continue
        events.append(event)
        sequence += 1
    return events


def _parse_one_tables(html: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    sequence = 0
    last_location = ""
    for table in parse_tables(html):
        if len(table) < 2:
            continue
        headers = [cell["text"] for cell in table[0]]
        mapping = _header_index(headers)
        body = table[1:]
        if "status" not in mapping:
            if len(table[0]) >= 2 and not any(
                token in " ".join(headers).lower() for token in ("booking", "container no")
            ):
                mapping = {"location": 0, "status": 1}
                body = table
            else:
                continue
        for row in body:
            status = _cell_text(row, mapping.get("status"))
            location = _cell_text(row, mapping.get("location")) or last_location
            if location:
                last_location = location
            date_text = _cell_text(row, mapping.get("date"))
            transport = " ".join(
                part
                for part in (
                    _cell_text(row, mapping.get("transport")),
                    _cell_text(row, mapping.get("voyage")),
                )
                if part
            )
            if not date_text:
                date_text = status
            event = _event_from_fields(
                status=status,
                date_text=date_text,
                location=location,
                transport=transport,
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
            nested = value.get("locationName") or value.get("name") or value.get("code")
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
        status = _mapping_value(item, _JSON_STATUS_KEYS)
        date_text = _mapping_value(item, _JSON_DATE_KEYS)
        location = _mapping_value(item, _JSON_LOC_KEYS)
        if isinstance(item.get("location"), dict):
            location = location or str(item["location"].get("locationName") or "")
        transport = " ".join(
            part
            for part in (
                _mapping_value(item, _JSON_VSL_KEYS),
                _mapping_value(item, _JSON_VOY_KEYS),
            )
            if part
        )
        event = _event_from_fields(
            status=status,
            date_text=date_text,
            location=location,
            transport=transport,
            sequence=index,
        )
        if event:
            events.append(event)
    return events


def parse_one_html(html: str) -> list[CanonicalEvent]:
    """Parse cargo-tracking event rows from an ONE HTML snapshot."""
    events = _parse_event_table(html)
    if not events:
        events = _parse_one_tables(html)
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
    return _fill_forward(events)


class OneTracker(BaseTracker):
    carrier_code = "ONEY"
    timeline_order = "oldest_first"
    tracking_url = TRACK_URL
    screenshot_selectors = (
        "table[class*='EventTable']",
        "[class*='CargoTrackingDetail_event']",
        "[class*='event-table-container']",
    )

    async def dismiss_onboarding(self) -> None:
        skip = self.page.locator("button:has-text('Skip')")
        try:
            if await skip.first.is_visible(timeout=300):
                await skip.first.click(timeout=3_000)
                await self.page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass

    async def _select_container_search(self) -> None:
        type_btn = self.page.locator(
            "[class*='SearchContainer_select-title'], "
            "button:has-text('Container No.'), "
            "button:has-text('BL No.'), "
            "button:has-text('All')"
        )
        try:
            if not await type_btn.first.is_visible(timeout=4_000):
                return
            label = (await type_btn.first.inner_text()).strip().lower()
            if "container" in label and "bl" not in label:
                return
            await type_btn.first.click(timeout=3_000)
            option = self.page.get_by_role("option", name="Container No.")
            await option.click(timeout=3_000)
        except Exception:  # noqa: BLE001
            try:
                await self.page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass

    async def open_page(self) -> None:
        if not await self.open_tracking_or_reuse("one-line.com"):
            return
        await self.dismiss_cookies(wait_ms=12_000)
        await self.dismiss_onboarding()

    async def search(self, container: str) -> None:
        await self.dismiss_cookies(wait_ms=0)
        await self.dismiss_onboarding()
        await self._select_container_search()
        field = self.page.locator(
            "input[placeholder*='Container' i], input[placeholder*='Search by' i]"
        )
        try:
            await field.first.wait_for(state="visible", timeout=12_000)
        except Exception as exc:  # noqa: BLE001
            await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
            await self.dismiss_cookies(wait_ms=0)
            await self.dismiss_onboarding()
            await self._select_container_search()
            field = self.page.locator(
                "input[placeholder*='Container' i], input[placeholder*='Search by' i]"
            )
            try:
                await field.first.wait_for(state="visible", timeout=12_000)
            except Exception as retry_exc:  # noqa: BLE001
                raise TrackerError(
                    "Could not find the container search field.", "SELECTOR"
                ) from retry_exc
        await field.first.click()
        await field.first.fill("")
        await field.first.press_sequentially(container, delay=30)
        search = self.page.locator("button[class*='ContainerListFilters_button-search']")
        clicked = False
        try:
            if await search.first.is_visible(timeout=800):
                await search.first.click(timeout=8_000)
                clicked = True
        except Exception:  # noqa: BLE001
            clicked = False
        if not clicked:
            await field.first.press("Enter")
        await self._wait_for_results()
        if (
            not await self._has_tracking_result()
            and not await self._page_challenge_code()
        ):
            await self.page.goto(
                f"{TRACK_URL}?trakNoParam={quote(container)}&trakNoTpCdParam=C",
                wait_until="domcontentloaded",
            )
            await self.dismiss_cookies(wait_ms=0)
            await self.dismiss_onboarding()
            await self._wait_for_results()

    async def _has_tracking_result(self) -> bool:
        try:
            state = await self.page.evaluate(ONE_RESULT_STATE_JS)
        except Exception:  # noqa: BLE001
            return False
        if isinstance(state, dict):
            return one_has_result(state)
        return bool(state)

    async def _wait_for_results(self) -> None:
        try:
            await self.page.wait_for_function(
                """() => {
                    const text = (document.body && document.body.innerText) || "";
                    const low = text.toLowerCase();
                    if (/in progress/.test(low)) return false;
                    if (document.querySelector("table[class*='EventTable']")) return true;
                    if (document.querySelector("[class*='CargoTrackingDetail']")) return true;
                    if (/total\\s+\\d+\\s+result/.test(low)) return true;
                    if (low.includes("没有查询结果") || low.includes("查无")) return true;
                    return (DETECT_CHALLENGE)();
                }""".replace("DETECT_CHALLENGE", CHALLENGE_CODE_JS),
                timeout=20_000,
            )
        except Exception:  # noqa: BLE001
            pass

    async def parse_events(self) -> list[CanonicalEvent]:
        html = await self.page.content()
        events = parse_one_html(html)
        if events:
            return events
        text = (await self._visible_text()).lower()
        if "in progress" in text:
            raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
        total = _TOTAL_RESULT_RE.search(text)
        if total and int(total.group(1)) == 0:
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        if "no data found" in text:
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
