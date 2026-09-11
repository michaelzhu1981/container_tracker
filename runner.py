"""Batch and single-container tracking runs."""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from pathlib import Path

from artifacts import log_path, relative_to_root
from config import (
    CARRIER_TIMEOUT_MS,
    LOCALE,
    NAV_TIMEOUT_MS,
    OUTPUT_XLSX,
    QUERY_DELAY_SECONDS,
    session_path,
)
from excel_io import (
    build_output_frame,
    load_previous_results,
    result_to_cells,
    write_output,
)
from models import TrackResult
from status_engine import evaluate
from trackers import TRACKERS
from validate import carrier_supported, container_shape_ok, iso6346_check_digit_ok

LOGGER = logging.getLogger("container_tracker")


def configure_logging() -> Path:
    path = log_path()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger("container_tracker")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(stream)
    return path


def print_progress(index: int, total: int, result: TrackResult) -> None:
    print(f"[{index}/{total}] {result.carrier}  {result.container}")
    if result.status == "SAILED":
        print(f"        Loaded: YES")
        print(f"        Sailed: YES")
        if result.atd:
            print(f"        ATD:    {result.atd}")
        print(f"        Status: {result.status}")
    elif result.status == "LOADED_WAITING_DEPARTURE":
        print(f"        Loaded: YES")
        print(f"        Sailed: NO")
        print(f"        Status: {result.status}")
    elif result.status == "NOT_LOADED":
        print(f"        Loaded: NO")
        print(f"        Sailed: NO")
        print(f"        Status: {result.status}")
    else:
        print(f"        Status: {result.status}")
        if result.error_code:
            print(f"        Error:  {result.error_code}")
    print()


def print_summary(results: list[TrackResult], output: Path | None) -> None:
    counts: dict[str, int] = defaultdict(int)
    for result in results:
        counts[result.status] += 1
    print("===================================")
    print("Summary")
    for status in (
        "SAILED",
        "LOADED_WAITING_DEPARTURE",
        "NOT_LOADED",
        "MANUAL_CHECK_REQUIRED",
        "CHECK_FAILED",
    ):
        print(f"  {status:<28} {counts.get(status, 0)}")
    if output:
        print()
        print(f"Output: {relative_to_root(output) or output}")


def _invalid_result(row: dict, checked_at: str) -> TrackResult:
    container = row["Container"]
    carrier = row["Carrier"]
    if not container or not container_shape_ok(container):
        return evaluate(
            [],
            container=container,
            carrier=carrier,
            checked_at=checked_at,
            forced_status="CHECK_FAILED",
            error_code="INVALID_INPUT",
            error="Container number is missing or not AAAA#######.",
        )
    if not carrier_supported(carrier):
        return evaluate(
            [],
            container=container,
            carrier=carrier,
            checked_at=checked_at,
            forced_status="CHECK_FAILED",
            error_code="UNSUPPORTED_CARRIER",
            error=f"Unsupported carrier code: {carrier or '(empty)'}.",
        )
    raise AssertionError("row is valid")


def _cells_to_result(container: str, carrier: str, cells: dict) -> TrackResult:
    loaded = cells.get("Loaded")
    sailed = cells.get("Sailed")
    loaded_b = True if loaded == "YES" else False if loaded == "NO" else None
    sailed_b = True if sailed == "YES" else False if sailed == "NO" else None
    status = cells.get("Status") or "CHECK_FAILED"
    return TrackResult(
        container=container,
        carrier=carrier,
        pol=cells.get("POL") or None,
        loaded=loaded_b,
        sailed=sailed_b,
        vessel=cells.get("Vessel") or None,
        voyage=cells.get("Voyage") or None,
        load_port=cells.get("Load Port") or None,
        load_time=cells.get("Load Time") or None,
        atd=cells.get("ATD") or None,
        latest_event=cells.get("Latest Event") or None,
        status=status,  # type: ignore[arg-type]
        success=cells.get("Check Result") == "SUCCESS",
        check_result=cells.get("Check Result") or "FAILED",  # type: ignore[arg-type]
        error_code=cells.get("Error Code") or None,
        error=cells.get("Error") or None,
        checked_at=cells.get("Checked At") or "",
        screenshot_path=cells.get("Screenshot") or None,
    )


async def _track_one(page, carrier: str, container: str) -> TrackResult:
    tracker_cls = TRACKERS[carrier]
    tracker = tracker_cls(page)
    LOGGER.info("Tracking %s %s", carrier, container)
    if container_shape_ok(container) and not iso6346_check_digit_ok(container):
        LOGGER.warning("ISO 6346 check digit failed for %s; querying anyway.", container)
    return await tracker.track(container)


async def run_single(carrier: str, container: str, *, headed: bool = False) -> TrackResult:
    from artifacts import checked_at
    from playwright.async_api import async_playwright

    if not container_shape_ok(container):
        return _invalid_result(
            {"Container": container, "Carrier": carrier}, checked_at()
        )
    if not carrier_supported(carrier):
        return _invalid_result(
            {"Container": container, "Carrier": carrier}, checked_at()
        )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not headed)
        context_kwargs = {"locale": LOCALE}
        session = session_path(carrier)
        if session.exists():
            context_kwargs["storage_state"] = str(session)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            **context_kwargs,
        )
        context.set_default_timeout(CARRIER_TIMEOUT_MS.get(carrier, NAV_TIMEOUT_MS))
        page = await context.new_page()
        result = await _track_one(page, carrier, container)
        session.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(session))
        await context.close()
        await browser.close()
    return result


async def run_batch(
    rows: list[dict],
    *,
    headed: bool = False,
    resume: bool = False,
    output_path: Path = OUTPUT_XLSX,
    previous_path: Path | None = None,
) -> tuple[list[TrackResult], Path]:
    from artifacts import checked_at
    from playwright.async_api import async_playwright

    previous = load_previous_results(previous_path or output_path) if resume else {}
    results: list[TrackResult | None] = [None] * len(rows)
    occurrence: dict[tuple[str, str], int] = {}

    skip_indices: set[int] = set()
    if resume:
        for idx, row in enumerate(rows):
            key = (row["Container"], row["Carrier"])
            seen = occurrence.get(key, 0)
            occurrence[key] = seen + 1
            cells = previous.get((row["Container"], row["Carrier"], seen))
            if cells and cells.get("Status") == "SAILED":
                results[idx] = _cells_to_result(row["Container"], row["Carrier"], cells)
                skip_indices.add(idx)
        occurrence = {}

    written = write_output(output_path, build_output_frame(rows, results))

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not headed)
        by_carrier: dict[str, list[int]] = defaultdict(list)
        for idx, row in enumerate(rows):
            by_carrier[row["Carrier"]].append(idx)

        first = True
        for carrier, indices in by_carrier.items():
            if not carrier_supported(carrier):
                for idx in indices:
                    results[idx] = _invalid_result(rows[idx], checked_at())
                written = write_output(written, build_output_frame(rows, results))
                continue
            context_kwargs = {"locale": LOCALE}
            session = session_path(carrier)
            if session.exists():
                context_kwargs["storage_state"] = str(session)
            context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            **context_kwargs,
        )
            context.set_default_timeout(CARRIER_TIMEOUT_MS.get(carrier, NAV_TIMEOUT_MS))
            page = await context.new_page()
            for idx in indices:
                if idx in skip_indices:
                    print_progress(idx + 1, len(rows), results[idx])  # type: ignore[arg-type]
                    continue
                if not first:
                    await page.wait_for_timeout(
                        int(random.uniform(*QUERY_DELAY_SECONDS) * 1000)
                    )
                first = False
                row = rows[idx]
                if not container_shape_ok(row["Container"]) or not carrier_supported(
                    row["Carrier"]
                ):
                    results[idx] = _invalid_result(row, checked_at())
                else:
                    results[idx] = await _track_one(page, carrier, row["Container"])
                print_progress(idx + 1, len(rows), results[idx])  # type: ignore[arg-type]
                written = write_output(written, build_output_frame(rows, results))
            session.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(session))
            await context.close()
        await browser.close()

    finalized = [item for item in results if item is not None]
    return finalized, written
