"""Classify tracking text into canonical event fields."""

from __future__ import annotations

import re
from datetime import datetime

from models import CanonicalEvent, Classifier, EventType, TransportMode

_EMPTY_TRUE = (
    "empty container",
    "empty pickup",
    "empty return",
    "empty load",
    "empty in",
    "empty out",
    "empty gate",
    "returned to depot",
    "空箱",
)
_EMPTY_TRUE_WORDS = ("empty",)
_EMPTY_FALSE = ("laden", "fcl", "loaded (full)", "重箱")
_EMPTY_FALSE_WORDS = ("full",)

_BARGE = ("barge vessel", "barge", "lighter", "驳船")
_FEEDER = ("feeder",)
_MOTHER = ("mother", "ocean vessel", "deep sea", "deep-sea")
_TRUCK = ("truck", "trailer", "road")
_RAIL = ("rail", "railway", "train")
_VESSEL_WORDS = ("vessel", "ship", "mv ", "m/v")

_PLANNED = ("planned", "plan ", "schedule")
_ESTIMATED = ("estimated", "estimate", "eta", "etd")

_WEEKDAY_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*",
    re.I,
)
_AMPM_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AP]M)\b", re.I)
_AMPM_DATE_RE = re.compile(
    r"\b(\d{1,2}[- ][A-Za-z]{3}[- ]\d{4})\s+(\d{1,2}):(\d{2})\s*([AP]M)\b",
    re.I,
)
_DATE_PATTERNS = [
    ("%Y-%m-%d %H:%M:%S", re.compile(r"\b(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\b")),
    ("%Y-%m-%d %H:%M", re.compile(r"\b(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\b")),
    ("%Y/%m/%d %H:%M:%S", re.compile(r"\b(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\b")),
    ("%Y/%m/%d %H:%M", re.compile(r"\b(\d{4}/\d{2}/\d{2} \d{2}:\d{2})\b")),
    ("%d-%b-%Y %H:%M", re.compile(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})\b")),
    ("%d %b %Y %H:%M", re.compile(r"\b(\d{1,2} [A-Za-z]{3} \d{4} \d{2}:\d{2})\b")),
    ("%d/%m/%Y %H:%M", re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4} \d{2}:\d{2})\b")),
    ("%Y-%m-%d", re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")),
    ("%Y/%m/%d", re.compile(r"\b(\d{4}/\d{2}/\d{2})\b")),
    ("%d-%b-%Y", re.compile(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b")),
    ("%d %b %Y", re.compile(r"\b(\d{1,2} [A-Za-z]{3} \d{4})\b")),
    ("%d/%m/%Y", re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")),
    ("%d.%m.%Y", re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b")),
]
_ISO_TZ_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_COMPACT_DT_RE = re.compile(r"\b(20\d{6})(\d{4,6})\b")


def _lower(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, flags=re.I) is not None


def classify_empty(text: str) -> bool | None:
    blob = _lower(text)
    if any(token in blob for token in _EMPTY_TRUE):
        return True
    if _has_word(blob, "mt"):
        return True
    if any(token in blob for token in _EMPTY_FALSE):
        return False
    if _has_word(blob, "empty"):
        return True
    if _has_word(blob, "full"):
        return False
    return None


def classify_transport(text: str) -> TransportMode:
    blob = _lower(text)
    if any(token in blob for token in _BARGE):
        return "BARGE"
    if any(token in blob for token in _TRUCK):
        return "TRUCK"
    if any(token in blob for token in _RAIL):
        return "RAIL"
    if any(token in blob for token in _FEEDER):
        return "FEEDER"
    if any(token in blob for token in _MOTHER):
        return "MOTHER"
    if any(token in blob for token in _VESSEL_WORDS):
        return "VESSEL"
    return "UNKNOWN"


def classify_event_type(text: str) -> EventType:
    blob = _lower(text)
    if any(k in blob for k in ("ready to be loaded", "ready for loading")):
        return "GTIN"
    if any(k in blob for k in ("vessel departed", "vessel departure", "sailed")):
        return "DEPA"
    if re.search(r"\bdeparted\b", blob) and "gate" not in blob:
        return "DEPA"
    if any(k in blob for k in ("unloaded", "discharged")):
        return "DISC"
    if "discharge" in blob and not any(k in blob for k in ("arrival", "arrived")):
        return "DISC"
    if any(k in blob for k in ("on board", "onboard", "loaded on", "load on vessel", "laden on")):
        return "LOAD"
    if any(k in blob for k in ("vessel loading", "loading at pol", "loading at first pol")):
        return "LOAD"
    if re.search(r"(?<!un)\bloaded\b", blob) or re.search(r"\bload\b", blob):
        if "download" not in blob:
            return "LOAD"
    if any(
        k in blob
        for k in (
            "empty returned",
            "empty return",
            "empty container returned",
            "empty received",
        )
    ):
        return "GTIN"
    if any(
        k in blob
        for k in (
            "empty container release",
            "empty to shipper",
            "empty dispatched",
        )
    ):
        return "GTOT"
    if any(
        k in blob
        for k in (
            "full to consignee",
            "full container delivery",
            "import to consignee",
        )
    ):
        return "GTOT"
    if "export received" in blob:
        return "GTIN"
    if any(
        k in blob
        for k in (
            "laden return",
            "laden returned",
            "full return",
            "full returned",
        )
    ):
        return "GTIN"
    if "gate in" in blob or "gated in" in blob or "gate-in" in blob:
        return "GTIN"
    if "gate out" in blob or "gated out" in blob or "gate-out" in blob:
        return "GTOT"
    if any(k in blob for k in ("arrived", "arrival", "vessel arrived")):
        return "ARRI"
    return "OTHER"


def classify_classifier(text: str, is_actual: bool | None = None) -> Classifier:
    blob = _lower(text)
    if any(token in blob for token in _ESTIMATED) or _has_word(blob, "eta") or _has_word(blob, "etd"):
        return "EST"
    if any(token in blob for token in _PLANNED):
        return "PLN"
    if is_actual is True:
        return "ACT"
    if is_actual is False:
        return "PLN"
    return "ACT"


def _normalize_timestamp_text(text: str) -> str:
    cleaned = _WEEKDAY_RE.sub("", text)
    cleaned = re.sub(r"(\d{4}),\s*", r"\1 ", cleaned)
    cleaned = _AMPM_TIME_RE.sub(
        lambda match: f"{int(match.group(1)):02d}:{match.group(2)} {match.group(3).upper()}",
        cleaned,
    )
    return cleaned


def _hour_from_ampm(hour: int, meridian: str) -> int:
    meridian = meridian.upper()
    hour = hour % 12
    if meridian == "PM":
        hour += 12
    return hour


def parse_timestamp(text: str) -> tuple[str, str | None, str | None]:
    """Return (raw, event_date, event_time). Never invent 00:00."""
    raw = text.strip()
    text = _normalize_timestamp_text(raw)
    ampm = _AMPM_DATE_RE.search(text)
    if ampm:
        day_token = ampm.group(1).replace(" ", "-")
        try:
            parsed = datetime.strptime(
                f"{day_token} {int(ampm.group(2)):02d}:{ampm.group(3)}",
                "%d-%b-%Y %H:%M",
            )
        except ValueError:
            parsed = None
        if parsed is not None:
            hour = _hour_from_ampm(int(ampm.group(2)), ampm.group(4))
            return raw, parsed.strftime("%Y-%m-%d"), f"{hour:02d}:{ampm.group(3)}"
    iso = _ISO_TZ_RE.search(text)
    if iso:
        token = iso.group(1).replace("T", " ")
        try:
            parsed = datetime.strptime(token, "%Y-%m-%d %H:%M:%S")
            return raw, parsed.strftime("%Y-%m-%d"), parsed.strftime("%H:%M")
        except ValueError:
            pass
    compact = _COMPACT_DT_RE.search(text)
    if compact:
        digits = compact.group(1) + compact.group(2)
        if len(digits) >= 12:
            try:
                parsed = datetime.strptime(digits[:12], "%Y%m%d%H%M")
                return raw, parsed.strftime("%Y-%m-%d"), parsed.strftime("%H:%M")
            except ValueError:
                pass
    for fmt, pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        token = match.group(1).replace("T", " ")
        try:
            parsed = datetime.strptime(token, fmt)
        except ValueError:
            continue
        day = parsed.strftime("%Y-%m-%d")
        if "%H" in fmt:
            return raw, day, parsed.strftime("%H:%M")
        return raw, day, None
    return raw, None, None


def is_on_board(event: CanonicalEvent) -> bool:
    blob = _lower(event.raw_text)
    return "on board" in blob or "onboard" in blob


def is_empty_return(event: CanonicalEvent) -> bool:
    blob = _lower(event.raw_text)
    if event.classifier != "ACT":
        return False
    if any(
        token in blob
        for token in (
            "empty return",
            "empty received",
            "returned to depot",
            "empty in",
            "empty gate in",
        )
    ):
        return True
    return bool(event.empty is True and event.type == "GTIN")


def format_event_time(event: CanonicalEvent) -> str | None:
    if not event.event_date:
        return None
    if event.event_time:
        return f"{event.event_date} {event.event_time}"
    return event.event_date
