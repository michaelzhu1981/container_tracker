"""Canonical event and track result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Classifier = Literal["ACT", "PLN", "EST", "UNKNOWN"]
EventType = Literal["LOAD", "DEPA", "DISC", "GTIN", "GTOT", "ARRI", "OTHER"]
TransportMode = Literal[
    "MOTHER", "FEEDER", "VESSEL", "BARGE", "TRUCK", "RAIL", "UNKNOWN"
]
Status = Literal[
    "NOT_LOADED",
    "LOADED_WAITING_DEPARTURE",
    "SAILED",
    "MANUAL_CHECK_REQUIRED",
    "CHECK_FAILED",
]
CheckResult = Literal["SUCCESS", "MANUAL", "FAILED"]
TimelineOrder = Literal["oldest_first", "newest_first"]


@dataclass
class CanonicalEvent:
    classifier: Classifier
    type: EventType
    location_raw: str
    location_norm: str
    timestamp_raw: str
    event_date: str | None
    event_time: str | None
    sequence_index: int
    vessel: str | None = None
    voyage: str | None = None
    booking: str | None = None
    empty: bool | None = None
    transport_mode: TransportMode = "UNKNOWN"
    raw_text: str = ""


@dataclass
class TrackResult:
    container: str
    carrier: str
    pol: str | None = None
    loaded: bool | None = None
    sailed: bool | None = None
    vessel: str | None = None
    voyage: str | None = None
    load_port: str | None = None
    load_time: str | None = None
    atd: str | None = None
    latest_event: str | None = None
    status: Status = "CHECK_FAILED"
    success: bool = False
    check_result: CheckResult = "FAILED"
    error_code: str | None = None
    error: str | None = None
    checked_at: str = ""
    screenshot_path: str | None = None
    html_path: str | None = None
    events: list[CanonicalEvent] = field(default_factory=list)
