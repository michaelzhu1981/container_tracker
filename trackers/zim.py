"""ZIM public Track a Shipment adapter."""

from __future__ import annotations

import asyncio
import json
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

TRACK_URL = "https://www.zim.com/tools/track-a-shipment"
TRACK_QUERY_URL = "https://www.zim.com/tools/track-a-shipment?consnumber={number}"

_HEADER_HINTS = {
    "date": ("date", "time"),
    "status": ("activity", "event", "status", "movement"),
    "location": ("location", "place"),
    "transport": ("vessel", "voyage", "transport"),
}

_JSON_STATUS_KEYS = ("activitydesc", "activity", "event", "status", "description")
_JSON_DATE_KEYS = ("activitydatetz", "activitydate", "eventdate", "date")
_JSON_LOC_KEYS = ("placefromdesc", "location", "portname", "place")
_JSON_COUNTRY_KEYS = ("countryfromname", "countryname")
_JSON_VSL_KEYS = ("vesselname", "vessel")
_JSON_VOY_KEYS = ("voyage", "voyageno")

_VOYAGE_RE = re.compile(r"\b(\d{2,4}[A-Z])\b", re.I)
_SLASH_VOYAGE_RE = re.compile(r"(?:/|\s)(\d{1,4})\s*/\s*([A-Z])\b", re.I)
_SKIP_STATUS = frozenset({"", "activity", "event", "status", "movement"})
_SEARCH_FIELD_SELECTORS = (
    "input.chips-input",
    "input[placeholder*='container' i]",
    "input[placeholder*='B/L' i]",
    "input[placeholder*='Insert' i]",
    "input[name*='cons' i]",
)
_SUBMIT_BUTTON_SELECTORS = (
    "input.chips-search-button",
    "button:has-text('Search')",
    "button[type='submit']",
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
    slash = _SLASH_VOYAGE_RE.search(blob)
    if slash:
        voyage = f"{slash.group(1)}{slash.group(2).upper()}"
        vessel = " ".join(blob[: slash.start()].split()).strip(" -/()") or None
        return vessel, voyage
    spaced = " ".join(blob.replace("/", " ").split())
    match = _VOYAGE_RE.search(spaced)
    voyage = match.group(1).upper() if match else None
    vessel = spaced
    if match:
        vessel = (spaced[: match.start()] + spaced[match.end() :]).strip(" /-")
    vessel = " ".join(vessel.split()).strip(" -/()") or None
    return vessel, voyage


def _strip_html(blob: str) -> str:
    text = re.sub(r"<[^>]+>", " ", blob)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    return " ".join(text.split())


def _match_balanced_tag(html: str, start: int) -> str | None:
    if start < 0 or start >= len(html) or html[start] != "<":
        return None
    name_end = start + 1
    while name_end < len(html) and html[name_end] not in " \t\n\r/>":
        name_end += 1
    name = html[start + 1 : name_end].lower()
    if not name:
        return None
    lower = html.lower()
    open_pat = f"<{name}"
    close_pat = f"</{name}>"
    depth = 0
    i = start
    n = len(html)
    while i < n:
        if lower.startswith(close_pat, i):
            depth -= 1
            if depth == 0:
                return html[start : i + len(close_pat)]
            i += len(close_pat)
            continue
        if lower.startswith(open_pat, i):
            nxt = i + len(open_pat)
            if nxt < n and html[nxt] in " \t\n\r/>":
                gt = html.find(">", i)
                if gt != -1 and html[gt - 1] == "/":
                    i = gt + 1
                    continue
                depth += 1
                i = nxt
                continue
        i += 1
    return None


def _html_blocks_with_class(html: str, token: str) -> list[str]:
    token_l = token.lower()
    blocks: list[str] = []
    lower = html.lower()
    pos = 0
    while True:
        idx = lower.find(token_l, pos)
        if idx < 0:
            break
        start = html.rfind("<", 0, idx)
        tag_end = html.find(">", start) if start >= 0 else -1
        if start < 0 or tag_end < 0:
            pos = idx + 1
            continue
        tag = html[start : tag_end + 1].lower()
        if "class=" not in tag or token_l not in tag:
            pos = idx + 1
            continue
        block = _match_balanced_tag(html, start)
        if not block:
            pos = tag_end + 1
            continue
        blocks.append(block)
        pos = start + len(block)
    return blocks


def _inner_by_id_suffix(block: str, suffix: str) -> str:
    match = re.search(rf'id="[^"]*{re.escape(suffix)}"', block, re.I)
    if not match:
        return ""
    start = block.rfind("<", 0, match.start())
    tagged = _match_balanced_tag(block, start)
    if not tagged:
        return ""
    inner = tagged[tagged.find(">") + 1 :]
    if inner.lower().endswith("</div>"):
        inner = inner[: -len("</div>")]
    return _strip_html(inner)


def _event_from_card_block(block: str, sequence: int) -> CanonicalEvent | None:
    status = _inner_by_id_suffix(block, "activityDesc")
    date_text = _inner_by_id_suffix(block, "activityDateTz")
    location = _inner_by_id_suffix(block, "placeFromDesc")
    transport = _inner_by_id_suffix(block, "vessel")
    return _event_from_fields(
        status=status,
        date_text=date_text,
        location=location,
        transport=transport,
        sequence=sequence,
    )


def _parse_zim_cards(html: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    seen: set[tuple] = set()

    def add(event: CanonicalEvent | None) -> None:
        if event is None:
            return
        key = (event.type, event.event_date, event.event_time, event.location_norm, event.raw_text)
        if key in seen:
            return
        seen.add(key)
        events.append(event)

    for block in _html_blocks_with_class(html, "card-container-v2"):
        head = re.split(r'class="[^"]*activity-card-holder', block, maxsplit=1, flags=re.I)[0]
        add(_event_from_card_block(head, len(events)))
    for block in _html_blocks_with_class(html, "card-container-activity"):
        add(_event_from_card_block(block, len(events)))
    return events


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
            if "unitactivitylist" in keys and isinstance(node.get("unitActivityList"), list):
                for item in node["unitActivityList"]:
                    if isinstance(item, dict):
                        found.append(item)
            if keys & set(_JSON_STATUS_KEYS) and keys & set(_JSON_DATE_KEYS):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    events: list[CanonicalEvent] = []
    seen: set[tuple] = set()
    for item in found:
        location = _mapping_value(item, _JSON_LOC_KEYS)
        country = _mapping_value(item, _JSON_COUNTRY_KEYS)
        if location and country and country.lower() not in location.lower():
            location = f"{location}, {country}"
        event = _event_from_fields(
            status=_mapping_value(item, _JSON_STATUS_KEYS),
            date_text=_mapping_value(item, _JSON_DATE_KEYS),
            location=location,
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
        if event is None:
            continue
        key = (event.type, event.event_date, event.event_time, event.location_norm, event.raw_text)
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    return events


def _parse_zim_tables(html: str) -> list[CanonicalEvent]:
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


def parse_zim_html(html: str) -> list[CanonicalEvent]:
    """Parse ZIM tracking activities from an HTML snapshot or embedded JSON."""
    events = _parse_zim_tables(html)
    if events:
        return events
    events = _parse_zim_cards(html)
    if events:
        return events
    for match in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        blob = match.group(1).strip()
        if "unitActivityList" not in blob and "activityDesc" not in blob:
            continue
        try:
            events = _events_from_json(json.loads(blob))
        except json.JSONDecodeError:
            continue
        if events:
            return events
    return []


def parse_zim_payload(payload: object) -> list[CanonicalEvent]:
    """Parse a captured ZIM tracking JSON payload."""
    return _events_from_json(payload)


def json_mentions_container(payload: object, container: str) -> bool:
    """True when a tracking payload is for this box, not a leftover response."""
    if not container:
        return True
    try:
        return container.upper() in json.dumps(payload).upper()
    except TypeError:
        return True


def _looks_like_zim_payload(payload: object) -> bool:
    try:
        blob = json.dumps(payload)
    except TypeError:
        return False
    return "unitActivityList" in blob or "activityDesc" in blob


class ZimTracker(BaseTracker):
    carrier_code = "ZIMU"
    wait_in_current_browser = True
    use_system_chrome = True
    system_chrome_host = "zim.com"
    system_chrome_challenge = "hCaptcha"
    timeline_order = "newest_first"
    tracking_url = TRACK_URL
    screenshot_selectors = (
        ".tracing-result-wrapper",
        ".activity-card-holder",
        "[class*='card-container-activity']",
        "table:has-text('Activity')",
        "[class*='unitActivity' i]",
        "[class*='track-shipment' i]",
        "table:has-text('Vessel Departure')",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._tracking_json: object | None = None

    def _captured_tracking_json(self) -> object | None:
        if self._tracking_json is not None:
            return self._tracking_json
        return getattr(self.page, "_ct_zim_json", None)

    def _clear_captured_json(self) -> None:
        self._tracking_json = None
        if self.page is not None:
            setattr(self.page, "_ct_zim_json", None)
            event = getattr(self.page, "_ct_zim_response_event", None)
            if event is not None:
                event.clear()

    async def _bind_tracking_response(self) -> None:
        page = self.page
        if page is None or getattr(page, "_ct_zim_bound", False):
            return
        if getattr(page, "is_system_chrome", False):
            return
        event = asyncio.Event()
        setattr(page, "_ct_zim_response_event", event)

        async def handle(response: object) -> None:
            url = str(getattr(response, "url", "") or "").lower()
            if "zim.com" not in url and "azure-api" not in url:
                return
            if not any(token in url for token in ("track", "shipment", "consign", "activity")):
                return
            try:
                payload = await response.json()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                return
            if not _looks_like_zim_payload(payload):
                return
            expected = getattr(page, "_ct_zim_expected", None)
            if expected and not json_mentions_container(payload, expected):
                return
            setattr(page, "_ct_zim_json", payload)
            event.set()

        page.on("response", handle)
        setattr(page, "_ct_zim_bound", True)

    async def _page_challenge_code(self) -> str | None:
        code = await super()._page_challenge_code()
        if code and await self._has_tracking_result():
            return None
        return code

    async def open_page(self) -> None:
        await self._bind_tracking_response()
        if not await self.open_tracking_or_reuse("zim.com"):
            return
        await self.dismiss_cookies(wait_ms=12_000)

    async def _first_visible_search_field(self):
        for selector in _SEARCH_FIELD_SELECTORS:
            locator = self.page.locator(selector)
            try:
                if await locator.first.is_visible(timeout=1_500):
                    return locator
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _click_search_button(self) -> bool:
        for selector in _SUBMIT_BUTTON_SELECTORS:
            button = self.page.locator(selector)
            try:
                if await button.first.is_visible(timeout=800):
                    await button.first.click(timeout=8_000)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def search(self, container: str) -> None:
        self._search_submitted = False
        await self._bind_tracking_response()
        if self.page is not None:
            setattr(self.page, "_ct_zim_expected", container)
        self._clear_captured_json()
        await self.dismiss_cookies(wait_ms=0)
        field = await self._first_visible_search_field()
        if field is None:
            await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
            await self.dismiss_cookies(wait_ms=0)
            field = await self._first_visible_search_field()
        if field is None:
            raise TrackerError("Could not find the container search field.", "SELECTOR")
        await field.first.click()
        await field.first.fill("")
        await field.first.fill(container)
        if not await self._click_search_button():
            await field.first.press("Enter")
        self._search_submitted = True
        await self._wait_for_results()

    async def _has_tracking_result(self) -> bool:
        try:
            text = (await self._visible_text()).lower()
        except Exception:  # noqa: BLE001
            return False
        return any(
            token in text
            for token in (
                "vessel departure",
                "vessel arrived",
                "last activity",
                "unit activity",
                "container was loaded",
            )
        )

    async def _wait_for_results(self) -> None:
        response_event = getattr(self.page, "_ct_zim_response_event", None)
        dom_wait = asyncio.create_task(
            self.page.wait_for_function(
                """() => {
                    const text = (document.body && document.body.innerText || "").toLowerCase();
                    return (
                        text.includes("vessel departure") ||
                        text.includes("last activity") ||
                        text.includes("unit activity") ||
                        !!document.querySelector(".tracing-result-wrapper") ||
                        !!document.querySelector(".card-container-activity") ||
                        !!document.querySelector("[class*='unitActivity']") ||
                        text.includes("no result") ||
                        text.includes("not found") ||
                        (DETECT_CHALLENGE)()
                    );
                }""".replace("DETECT_CHALLENGE", CHALLENGE_CODE_JS),
                timeout=35_000,
            )
        )
        waits = {dom_wait}
        if response_event is not None:
            waits.add(asyncio.create_task(response_event.wait()))
        try:
            done, _pending = await asyncio.wait(
                waits,
                timeout=35,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
        except Exception:  # noqa: BLE001
            pass
        finally:
            for task in waits:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def prepare_for_screenshot(self) -> None:
        try:
            await self.page.evaluate(
                """() => {
                    const root = document.querySelector(".tracing-result-wrapper")
                        || document.querySelector(".activity-card-holder");
                    if (root) root.scrollIntoView({ block: "start", inline: "nearest" });
                }"""
            )
        except Exception:  # noqa: BLE001
            pass
        await super().prepare_for_screenshot()

    async def parse_events(self) -> list[CanonicalEvent]:
        captured = self._captured_tracking_json()
        if captured is not None:
            events = parse_zim_payload(captured)
            if events:
                return events
        html = await self.page.content()
        events = parse_zim_html(html)
        if events:
            return events
        text = await self._visible_text()
        if looks_like_no_result(text):
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
