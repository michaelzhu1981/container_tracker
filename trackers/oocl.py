"""OOCL public cargo tracking adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from urllib.parse import quote

from challenges import CHALLENGE_CODE_JS
from chrome_control import SystemChromeError
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
from trackers.base import COOKIE_BANNER_WAIT_MS, BaseTracker, TrackerError, looks_like_no_result

LOGGER = logging.getLogger("container_tracker")

TRACK_URL = (
    "https://www.oocl.com/eng/ourservices/eservices/cargotracking/"
    "Pages/cargotracking.aspx"
)
RESULT_POPUP = (
    "https://www.oocl.com/Pages/ExpressLink.aspx?eltype=ct"
    "&businessType=containerNumber&businessNumber={number}&language=en"
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
_SUBMIT_BUTTON_SELECTORS = (
    "#container_btn",
    "a[onclick*='ListeningCargoTrackingBtn']",
    "a.btn-red:has-text('Search')",
    "button:has-text('Search')",
    "input[value='Search']",
)
_ENTRY_URL_MARKERS = ("cargotracking.aspx",)
_RESULT_HOSTS = ("oocl.com", "cargosmart.com")
_BLANK_URLS = ("", "about:blank", "chrome://newtab")
_VIEW_DETAILS_SELECTORS = (
    "a:has-text('View Details')",
    "button:has-text('View Details')",
    "span:has-text('View Details')",
    "a:has-text('Show Details')",
    "button:has-text('Show Details')",
    "a:has-text('查看详情')",
    "button:has-text('查看详情')",
)
_CLICK_VIEW_DETAILS_JS = """() => {
    // OOCL View Details
    const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return r.width > 8 && r.height > 8
            && cs.display !== "none" && cs.visibility !== "hidden";
    };
    const labelOf = (el) => (
        (el.innerText || el.textContent || "")
        + " "
        + (el.getAttribute("aria-label") || "")
        + " "
        + (el.getAttribute("title") || "")
        + " "
        + (el.value || "")
    ).toLowerCase().replace(/\\s+/g, " ").trim();
    const isHide = (s) => /hide details|close details|less details|收起详情/.test(s);
    const isView = (s) => (
        /(^|\\b)(view|show|display) details?\\b/.test(s)
        || s.includes("查看详情")
        || s.includes("查看詳細")
    ) && !isHide(s);
    const nodes = [...document.querySelectorAll(
        "a, button, span, div, input[type=button], input[type=submit], "
        + "[role=button], [onclick]"
    )];
    if (nodes.some((el) => visible(el) && isHide(labelOf(el)))) {
        return 0;
    }
    for (const el of nodes) {
        if (!visible(el)) continue;
        const label = labelOf(el);
        if (!isView(label) || label.length > 48) continue;
        el.scrollIntoView({ block: "center", inline: "nearest" });
        el.click();
        return 1;
    }
    return 0;
}"""
_VISIBLE_EVENT_COUNT_JS = """() => {
    const seen = new Set();
    let n = 0;
    // The current OOCL drawer renders the fixed header and data rows in
    // separate tables. Count dated rows inside event-table directly so an
    // opened drawer is not mistaken for the collapsed summary page.
    for (const tr of document.querySelectorAll(".event-table tbody tr")) {
        const row = (tr.innerText || "").replace(/\\s+/g, " ").trim();
        if (!row || seen.has(row)) continue;
        if (/\\d{4}|\\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\\b/i.test(row)) {
            seen.add(row);
            n += 1;
        }
    }
    for (const table of document.querySelectorAll("table")) {
        const header = (table.innerText || "").toLowerCase();
        if (!/(event|dynamic node|status|activity|movement)/.test(header)) continue;
        for (const tr of table.querySelectorAll("tr")) {
            const row = (tr.innerText || "").replace(/\\s+/g, " ").trim();
            if (!row || seen.has(row) || /^(date|event|status|activity|dynamic node)\\b/i.test(row)) {
                continue;
            }
            if (/\\d{4}|\\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\\b/i.test(row)) {
                seen.add(row);
                n += 1;
            }
        }
    }
    return n;
}"""
_PREPARE_DETAILS_SCREENSHOT_JS = """() => {
    // OOCL places the expanded result in a fixed-height drawer and gives the
    // event table another nested scrollbar. Let both grow for one complete
    // evidence image instead of capturing only the visible viewport.
    const drawer = document.querySelector(".ant-drawer-open");
    if (!drawer) return false;
    let style = document.getElementById("container-tracker-oocl-full-details");
    if (!style) {
        style = document.createElement("style");
        style.id = "container-tracker-oocl-full-details";
        document.head.appendChild(style);
    }
    style.textContent = `
        .ant-drawer-open .ant-drawer-content-wrapper,
        .ant-drawer-open .ant-drawer-content,
        .ant-drawer-open .ant-drawer-wrapper-body,
        .ant-drawer-open .ant-drawer-body,
        .ant-drawer-open .event-table,
        .ant-drawer-open .event-table .ant-spin-nested-loading,
        .ant-drawer-open .event-table .ant-spin-container,
        .ant-drawer-open .event-table .ant-table,
        .ant-drawer-open .event-table .ant-table-container,
        .ant-drawer-open .event-table .ant-table-body {
            height: auto !important;
            max-height: none !important;
            overflow: visible !important;
        }
    `;
    const grow = (selector) => {
        for (const el of drawer.querySelectorAll(selector)) {
            el.style.setProperty("height", "auto", "important");
            el.style.setProperty("max-height", "none", "important");
            el.style.setProperty("overflow", "visible", "important");
        }
    };
    grow(".ant-drawer-content, .ant-drawer-wrapper-body, .ant-drawer-body");
    grow(".event-table, .event-table .ant-spin-nested-loading, .event-table .ant-spin-container");
    grow(".event-table .ant-table, .event-table .ant-table-container, .event-table .ant-table-body");
    const body = drawer.querySelector(".ant-drawer-body");
    if (body) body.scrollTop = 0;
    const tableBody = drawer.querySelector(".event-table .ant-table-body");
    if (tableBody) tableBody.scrollTop = 0;
    // Force layout now. The style element remains in place if Vue refreshes
    // inline styles while the screenshot is being captured.
    void drawer.offsetHeight;
    return {
        rows: drawer.querySelectorAll(".event-table tbody tr.ant-table-row").length,
        height: body ? Math.max(body.scrollHeight, body.getBoundingClientRect().height) : 0,
    };
}"""
_SUBMIT_SEARCH_JS = """(container) => {
    if (typeof allowAllCookiePolicy === "function") {
        try { allowAllCookiePolicy(); } catch (err) {}
    }
    const field = document.getElementById("SEARCH_NUMBER");
    if (field) {
        field.focus();
        const proto = window.HTMLInputElement && HTMLInputElement.prototype;
        const desc = proto && Object.getOwnPropertyDescriptor(proto, "value");
        if (desc && desc.set) desc.set.call(field, container);
        else field.value = container;
        field.dispatchEvent(new Event("input", { bubbles: true }));
        field.dispatchEvent(new Event("change", { bubbles: true }));
    }
    const type = document.getElementById("searchType");
    if (type) type.value = "cont";
    const select = document.getElementById("ooclCargoSelector");
    if (select) select.value = "cont";
    if (window.jQuery) {
        window.jQuery("#ooclCargoSelector").val("cont");
        if (window.jQuery.fn && window.jQuery.fn.selectpicker) {
            window.jQuery("#ooclCargoSelector").selectpicker("refresh");
        }
        if (field) window.jQuery("#SEARCH_NUMBER").val(container);
    }
    if (typeof changeTrackingType === "function") {
        try { changeTrackingType(); } catch (err) {}
    }
    if (typeof CookieModeSwitches === "function" && CookieModeSwitches() === "no") {
        if (typeof allowAllCookiePolicy === "function") {
            try { allowAllCookiePolicy(); } catch (err) {}
        }
    }
    let popupUrl = "";
    window.open = function(url) {
        popupUrl = String(url || "");
        return null;
    };
    if (typeof ListeningCargoTrackingBtn === "function") {
        ListeningCargoTrackingBtn();
    } else {
        const btn = document.getElementById("container_btn");
        if (btn) {
            btn.scrollIntoView({ block: "center", inline: "nearest" });
            btn.click();
        }
    }
    return { submitted: true, popupUrl: popupUrl || null };
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
    if not blob or blob.lower() in {
        "vessel",
        "barge",
        "truck",
        "rail",
        "ocean",
        "ocean vessel",
        "inbound",
        "outbound",
    }:
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


def _is_summary_headers(headers: list[str]) -> bool:
    blob = " ".join(headers).lower()
    return "latest event" in blob or ("container no" in blob and "action" in blob)


def _with_stage_column(headers: list[str], mapping: dict[str, int]) -> dict[str, int]:
    merged = dict(mapping)
    if "stage" not in merged:
        for idx, header in enumerate(headers):
            if "stage" in header.lower():
                merged["stage"] = idx
                break
    if "transport" not in merged and len(headers) >= 5:
        merged["transport"] = 4
    return merged


def _looks_like_detail_data_row(row: list[dict]) -> bool:
    status = _cell_text(row, 0)
    if status.lower() in _SKIP_STATUS or status.lower() == "event":
        return False
    _, day, _ = parse_timestamp(_cell_text(row, 1))
    return bool(day)


def _events_from_mapped_rows(
    rows: list[list[dict]], mapping: dict[str, int], start: int = 0
) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    for row in rows:
        transport = " ".join(
            part
            for part in (
                _cell_text(row, mapping.get("stage")),
                _cell_text(row, mapping.get("transport")),
            )
            if part
        )
        event = _event_from_fields(
            status=_cell_text(row, mapping.get("status")),
            date_text=_cell_text(row, mapping.get("date")),
            location=_cell_text(row, mapping.get("location")),
            transport=transport,
            sequence=start + len(events),
        )
        if event:
            events.append(event)
    return events


def _parse_oocl_tables(html: str) -> list[CanonicalEvent]:
    details: list[CanonicalEvent] = []
    summary: list[CanonicalEvent] = []
    pending: dict[str, int] | None = None
    default_detail = {
        "status": 0,
        "date": 1,
        "location": 2,
        "stage": 3,
        "transport": 4,
    }
    for table in parse_tables(html):
        if not table:
            continue
        headers = [cell["text"] for cell in table[0]]
        if _is_summary_headers(headers):
            mapping = _header_index(headers)
            if "status" in mapping and "date" in mapping and len(table) > 1:
                summary.extend(_events_from_mapped_rows(table[1:], mapping, len(summary)))
            pending = None
            continue
        mapping = _header_index(headers)
        if (
            "status" in mapping
            and "date" in mapping
            and not _looks_like_detail_data_row(table[0])
        ):
            mapping = _with_stage_column(headers, mapping)
            if len(table) == 1:
                pending = mapping
                continue
            details.extend(_events_from_mapped_rows(table[1:], mapping, len(details)))
            pending = None
            continue
        if pending or _looks_like_detail_data_row(table[0]):
            details.extend(
                _events_from_mapped_rows(table, pending or default_detail, len(details))
            )
            pending = None
    return details or summary


def oocl_popup_url(container: str) -> str:
    """Official Search opens this URL via window.open; Chrome blocks scripted popups."""
    return RESULT_POPUP.format(number=quote(container))


def submit_popup_url(payload: object) -> str:
    if isinstance(payload, dict):
        url = payload.get("popupUrl") or payload.get("url") or ""
        return str(url)
    if isinstance(payload, str) and payload.startswith("http"):
        return payload
    return ""


def is_oocl_entry_url(url: str) -> bool:
    blob = (url or "").lower()
    return any(marker in blob for marker in _ENTRY_URL_MARKERS)


def _is_blank_url(url: str) -> bool:
    return (url or "").strip().lower() in _BLANK_URLS


def _is_related_tab(url: str) -> bool:
    blob = (url or "").lower()
    return any(host in blob for host in _RESULT_HOSTS)


def _page_is_closed(page: object) -> bool:
    checker = getattr(page, "is_closed", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001
            return True
    return page is None


def is_oocl_site_error_page(text: str, html: str = "") -> bool:
    """True for OOCL's own 404 / removed-page shell, not a container miss."""
    blob = f"{text}\n{html}".lower()
    return (
        ("oops" in blob and "page not found" in blob)
        or "oocl404" in blob
        or "the page you are looking for might have been removed" in blob
    )


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
    use_system_chrome = True
    system_chrome_host = "oocl.com"
    system_chrome_challenge = "CAPTCHA"
    timeline_order = "newest_first"
    tracking_url = TRACK_URL
    screenshot_cookie_wait_ms = 0
    screenshot_selectors = (
        ".ant-drawer-open .ant-drawer-body",
        ".ant-drawer-open .ant-drawer-content",
        "table:has-text('Container No.')",
        "table:has-text('Vessel Departed')",
        "table:has-text('Departure')",
        "table:has-text('Event')",
        "table:has-text('Dynamic Node')",
        "table:has-text('Container Movement')",
        "[class*='cargo-tracking' i]",
    )

    def __init__(self, page, *, wait_for_challenge: bool = True, browser=None) -> None:
        super().__init__(page, wait_for_challenge=wait_for_challenge, browser=browser)
        self._entry_page = page
        self._result_urls: list[str] = []
        self._details_expanded = False
        self._results_waited = False

    async def _page_challenge_code(self) -> str | None:
        code = await super()._page_challenge_code()
        if code and await self._has_tracking_result():
            return None
        return code

    async def open_page(self) -> None:
        await self._return_to_entry()
        if not await self.open_tracking_or_reuse("oocl.com"):
            return
        await self.dismiss_cookies(wait_ms=12_000)

    async def dismiss_cookies(self, wait_ms: int = COOKIE_BANNER_WAIT_MS) -> None:
        await super().dismiss_cookies(wait_ms=wait_ms)
        try:
            allow = self.page.locator("#allowAll")
            if await allow.first.is_visible(timeout=0):
                await allow.first.click(timeout=3_000, force=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.page.evaluate(
                """() => {
                    if (typeof allowAllCookiePolicy === "function") {
                        allowAllCookiePolicy();
                    }
                }"""
            )
        except Exception:  # noqa: BLE001
            pass

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
                    if (window.jQuery) {
                        window.jQuery("#ooclCargoSelector").val(value);
                        if (window.jQuery.fn && window.jQuery.fn.selectpicker) {
                            window.jQuery("#ooclCargoSelector").selectpicker("refresh");
                        }
                    }
                    if (typeof changeTrackingType === "function") {
                        changeTrackingType();
                    }
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

    def _as_int(self, value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    async def _sleep(self, ms: int) -> None:
        waiter = getattr(self.page, "wait_for_timeout", None)
        if callable(waiter):
            try:
                await waiter(ms)
                return
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(max(ms, 0) / 1000)

    async def _visible_event_count(self) -> int:
        try:
            return self._as_int(await self.page.evaluate(_VISIBLE_EVENT_COUNT_JS))
        except Exception:  # noqa: BLE001
            return 0

    async def _click_view_details(self) -> int:
        try:
            clicked = self._as_int(await self.page.evaluate(_CLICK_VIEW_DETAILS_JS))
            if clicked:
                return clicked
        except Exception:  # noqa: BLE001
            pass
        for selector in _VIEW_DETAILS_SELECTORS:
            try:
                target = self.page.locator(selector)
                if await target.first.is_visible(timeout=800):
                    await target.first.click(timeout=3_000)
                    return 1
            except Exception:  # noqa: BLE001
                continue
        return 0

    async def expand_result_details(self) -> None:
        """Open View Details so movement events exist before status and screenshot."""
        if self._details_expanded:
            return
        before = await self._visible_event_count()
        prior: list[str] = []
        can_see_tabs = callable(getattr(self.page, "list_tab_urls", None)) or getattr(
            getattr(self.page, "context", None), "pages", None
        ) is not None
        if can_see_tabs:
            prior = await self._tab_urls()
        clicked = await self._click_view_details()
        if not clicked:
            if before >= 1:
                self._details_expanded = True
            return
        prior_set = {url for url in prior if not _is_blank_url(url)}
        # Fifteen short checks are enough for a details tab/section to appear;
        # the summary page itself remains parseable when OOCL keeps it folded.
        for _attempt in range(15):
            if prior_set:
                await self._focus_new_result(prior_set)
            now = await self._visible_event_count()
            if now > before:
                await self._sleep(400)
                self._details_expanded = True
                return
            await self._sleep(200)
        self._details_expanded = True
        LOGGER.info("OOCL View Details stayed collapsed; status and screenshot may lack events.")

    async def prepare_for_screenshot(self) -> None:
        await self.expand_result_details()
        # The expanded OOCL result is itself a drawer. The generic overlay
        # cleanup treats dialogs as disposable, so only dismiss cookies here
        # and preserve the result drawer before expanding its scroll regions.
        await self.dismiss_cookies(wait_ms=self.screenshot_cookie_wait_ms)
        try:
            await self.page.evaluate(_PREPARE_DETAILS_SCREENSHOT_JS)
        except Exception:  # noqa: BLE001
            LOGGER.info("Could not expand the OOCL drawer for its screenshot.")

    async def search(self, container: str) -> None:
        self._search_submitted = False
        self._details_expanded = False
        self._results_waited = False
        await self._focus_entry()
        await self.dismiss_cookies(wait_ms=0)
        await self._select_container_search()
        field = await self._first_visible_search_field()
        if field is None:
            await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
            await self.dismiss_cookies(wait_ms=0)
            await self._select_container_search()
            field = await self._first_visible_search_field()
        if field is None:
            raise TrackerError("Could not find the container search field.", "SELECTOR")
        await field.first.click()
        await field.first.fill("")
        await field.first.fill(container)
        await self._pin_entry_tab()
        await self._submit_container_search(container)
        self._search_submitted = True
        await self._wait_for_results()
        await self.expand_result_details()

    async def _pin_entry_tab(self) -> None:
        focus = getattr(self.page, "focus_tab", None)
        if not callable(focus) and not hasattr(self.page, "tab_url"):
            return
        for url in await self._tab_urls():
            if not is_oocl_entry_url(url):
                continue
            if hasattr(self.page, "tab_url"):
                self.page.tab_url = url
            if callable(focus):
                await focus(url)
            return

    async def _result_already_open(self, before: list[str]) -> bool:
        prior = {url for url in before if not _is_blank_url(url)}
        return await self._focus_new_result(prior)

    async def _submit_container_search(self, container: str) -> None:
        before = await self._tab_urls()
        payload = None
        try:
            payload = await self.page.evaluate(_SUBMIT_SEARCH_JS, container)
        except Exception:  # noqa: BLE001
            payload = None
        if await self._result_already_open(before):
            return
        popup = submit_popup_url(payload)
        if not popup:
            await self._click_search_button()
            if await self._result_already_open(before):
                return
            popup = oocl_popup_url(container)
        if await self._result_already_open(before):
            return
        await self._open_result_url(popup)

    async def _open_result_url(self, url: str) -> None:
        if not url:
            return
        existing = await self._tab_urls()
        if any(url.split("?")[0] in item or item in url for item in existing):
            return
        opener = getattr(self.page, "open_tab", None)
        if callable(opener):
            await opener(url)
            self._result_urls.append(url)
            return
        context = getattr(self.page, "context", None)
        new_page = getattr(context, "new_page", None)
        if callable(new_page):
            extra = await new_page()
            await extra.goto(url, wait_until="domcontentloaded")
            self.page = extra
            self._result_urls.append(url)

    async def _click_search_button(self) -> None:
        for selector in _SUBMIT_BUTTON_SELECTORS:
            button = self.page.locator(selector)
            try:
                if await button.first.is_visible(timeout=1_200):
                    await button.first.click(timeout=8_000)
                    return
            except Exception:  # noqa: BLE001
                continue
        await self.page.keyboard.press("Enter")

    async def _tab_urls(self) -> list[str]:
        lister = getattr(self.page, "list_tab_urls", None)
        if callable(lister):
            try:
                return [url for url in await lister() if _is_related_tab(url)]
            except Exception:  # noqa: BLE001
                return []
        context = getattr(self.page, "context", None)
        pages = getattr(context, "pages", None) or []
        urls: list[str] = []
        for extra in pages:
            try:
                url = extra.url or ""
            except Exception:  # noqa: BLE001
                continue
            if _is_related_tab(url) or extra is self.page:
                urls.append(url)
        return urls

    async def _focus_new_result(self, prior: set[str]) -> bool:
        focus = getattr(self.page, "focus_tab", None)
        if callable(focus):
            now = await self._tab_urls()
            new = [url for url in now if url not in prior and not _is_blank_url(url)]
            if not new:
                return False
            chosen = new[-1]
            self._result_urls.append(chosen)
            await focus(chosen)
            return True
        context = getattr(self.page, "context", None)
        pages = getattr(context, "pages", None) or []
        extras = [
            extra
            for extra in pages
            if extra is not self._entry_page
            and not _page_is_closed(extra)
            and (getattr(extra, "url", "") or "") not in prior
            and not _is_blank_url(getattr(extra, "url", "") or "")
        ]
        if not extras:
            return False
        chosen = extras[-1]
        self._result_urls.append(getattr(chosen, "url", "") or "")
        self.page = chosen
        return True

    async def _focus_entry(self) -> None:
        page = self._entry_page or self.page
        if hasattr(page, "tab_url"):
            page.tab_url = None
        if page is not None and not _page_is_closed(page):
            self.page = page
        url = ""
        try:
            url = self.page.url or ""
        except Exception:  # noqa: BLE001
            url = ""
        if is_oocl_entry_url(url):
            return
        try:
            await self.page.goto(self.tracking_url, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass

    async def _return_to_entry(self) -> None:
        await self._close_result_tabs()
        await self._focus_entry()

    async def _close_result_tabs(self) -> None:
        page = self._entry_page or self.page
        closer = getattr(page, "close_tab", None)
        if callable(closer):
            for url in list(self._result_urls):
                try:
                    await closer(url)
                except Exception:  # noqa: BLE001
                    continue
            sweep = getattr(page, "close_other_host_tabs", None)
            if callable(sweep):
                try:
                    await sweep("cargotracking.aspx")
                except Exception:  # noqa: BLE001
                    pass
            extra_close = getattr(page, "close_tab", None)
            if extra_close:
                for url in await self._tab_urls():
                    if is_oocl_entry_url(url) or not _is_related_tab(url):
                        continue
                    try:
                        await extra_close(url)
                    except Exception:  # noqa: BLE001
                        continue
            if hasattr(page, "tab_url"):
                page.tab_url = None
            self._result_urls = []
            if page is not None:
                self.page = page
            return
        context = getattr(self.page, "context", None) or getattr(page, "context", None)
        entry = self._entry_page or self.page
        pages = getattr(context, "pages", None) or []
        for extra in list(pages):
            if extra is entry:
                continue
            try:
                await extra.close()
            except Exception:  # noqa: BLE001
                continue
        self._result_urls = []
        if entry is not None and not _page_is_closed(entry):
            self.page = entry

    async def track(self, container: str, *, session_ready: bool = False):
        self._entry_page = self.page
        self._result_urls = []
        self._details_expanded = False
        try:
            return await super().track(container, session_ready=session_ready)
        finally:
            await self._return_to_entry()

    async def _has_tracking_result(self) -> bool:
        try:
            text = (await self._visible_text()).lower()
        except Exception:  # noqa: BLE001
            return False
        if is_oocl_site_error_page(text):
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
                "tracking result",
                "view details",
                "show details",
                "gate out",
            )
        ) and "unrecognized" not in text

    async def _wait_for_results(self) -> None:
        if self._results_waited:
            return
        self._results_waited = True
        try:
            await self.page.wait_for_function(
                """() => {
                    const text = (document.body && document.body.innerText || "").toLowerCase();
                    const dead = text.includes("page not found") || text.includes("oops!");
                    return (
                        text.includes("vessel departed") ||
                        text.includes("vessel departure") ||
                        text.includes("dynamic node") ||
                        text.includes("container movement") ||
                        text.includes("tracking result") ||
                        text.includes("view details") ||
                        text.includes("show details") ||
                        text.includes("gate out") ||
                        text.includes("unrecognized") ||
                        text.includes("no result") ||
                        text.includes("no tracking") ||
                        dead ||
                        (DETECT_CHALLENGE)() ||
                        (document.readyState !== "loading" && text.trim().length > 80)
                    );
                }""".replace("DETECT_CHALLENGE", CHALLENGE_CODE_JS),
                timeout=5_000,
            )
        except SystemChromeError:
            raise
        except Exception:  # noqa: BLE001
            pass

    async def parse_events(self) -> list[CanonicalEvent]:
        await self._wait_for_results()
        await self.expand_result_details()
        html = await self.page.content()
        text = await self._visible_text()
        if is_oocl_site_error_page(text, html):
            raise TrackerError("OOCL tracking page was not found.", "NAVIGATION")
        if "unrecognized" in text.lower():
            raise TrackerError("OOCL rejected this tracking request.", "NAVIGATION")
        events = parse_oocl_html(html)
        if events:
            return events
        if looks_like_no_result(text):
            raise TrackerError("No tracking result for this container.", "NO_RESULT")
        raise TrackerError("Tracking table was not found or could not be parsed.", "PARSE")
