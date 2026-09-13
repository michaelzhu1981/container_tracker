"""Batch and single-container tracking runs."""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from pathlib import Path

from artifacts import log_path, relative_to_root
from config import (
    CARRIER_TIMEOUT_MS,
    CIRCUIT_BREAK_CODES,
    CIRCUIT_BREAK_STREAK,
    LOCALE,
    NAV_TIMEOUT_MS,
    OUTPUT_XLSX,
    chrome_profile_dir,
    default_headed_for,
    query_delay_seconds,
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


class CarrierBrowser:
    """One persistent Chrome profile per carrier; never relaunched per box."""

    def __init__(self, playwright, carrier: str, *, headed: bool) -> None:
        self.playwright = playwright
        self.carrier = carrier
        self.headed = headed
        self.context = None
        self.page = None

    async def start(self) -> None:
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:  # noqa: BLE001
                LOGGER.info("Previous %s browser context did not close cleanly.", self.carrier)
            self.context = None
        profile = chrome_profile_dir(self.carrier)
        profile.mkdir(parents=True, exist_ok=True)
        kwargs = {
            "headless": not self.headed,
            "locale": LOCALE,
            "viewport": {"width": 1400, "height": 900},
        }
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(profile), channel="chrome", **kwargs
            )
        except Exception:  # noqa: BLE001
            LOGGER.info("System Chrome not available; using Playwright Chromium.")
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(profile), **kwargs
            )
        self.context.set_default_timeout(CARRIER_TIMEOUT_MS.get(self.carrier, NAV_TIMEOUT_MS))
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def ensure_headed(self) -> bool:
        if self.headed:
            return False
        LOGGER.info("Opening a visible Chrome window for %s using the same profile.", self.carrier)
        self.headed = True
        await self.start()
        return True

    async def close(self) -> None:
        if self.context is None:
            return
        try:
            await self.context.close()
        except Exception:  # noqa: BLE001
            LOGGER.info("Could not close %s browser context.", self.carrier)
        self.context = None
        self.page = None


def should_relaunch_browser_per_box(carrier: str) -> bool:
    """Kept so tests can lock the 'one browser per carrier' rule."""
    return False


def update_circuit(streak: int, error_code: str | None) -> tuple[int, bool]:
    if error_code in CIRCUIT_BREAK_CODES:
        streak += 1
        return streak, streak >= CIRCUIT_BREAK_STREAK
    return 0, False


def _paused_result(row: dict, stamp: str, error_code: str) -> TrackResult:
    return evaluate(
        [],
        container=row["Container"],
        carrier=row["Carrier"],
        checked_at=stamp,
        forced_status="CHECK_FAILED",
        error_code=error_code,
        error=(
            f"Skipped remaining {row['Carrier']} rows after repeated "
            f"{error_code} failures."
        ),
    )


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


async def _track_one(
    page,
    carrier: str,
    container: str,
    *,
    wait_for_challenge: bool = True,
    browser: CarrierBrowser | None = None,
) -> TrackResult:
    tracker_cls = TRACKERS[carrier]
    tracker = tracker_cls(
        page, wait_for_challenge=wait_for_challenge, browser=browser
    )
    LOGGER.info("Tracking %s %s", carrier, container)
    if container_shape_ok(container) and not iso6346_check_digit_ok(container):
        LOGGER.warning("ISO 6346 check digit failed for %s; querying anyway.", container)
    return await tracker.track(container)


async def run_single(
    carrier: str,
    container: str,
    *,
    headed: bool = False,
    wait_for_challenge: bool = True,
) -> TrackResult:
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
        session = CarrierBrowser(
            playwright, carrier, headed=default_headed_for(carrier, headed)
        )
        await session.start()
        result = await _track_one(
            session.page,
            carrier,
            container,
            wait_for_challenge=wait_for_challenge,
            browser=session,
        )
        await session.close()
    return result


async def run_batch(
    rows: list[dict],
    *,
    headed: bool = False,
    wait_for_challenge: bool = True,
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
            session = CarrierBrowser(
                playwright, carrier, headed=default_headed_for(carrier, headed)
            )
            await session.start()
            streak = 0
            paused_code: str | None = None
            for idx in indices:
                if idx in skip_indices:
                    print_progress(idx + 1, len(rows), results[idx])  # type: ignore[arg-type]
                    continue
                if paused_code:
                    results[idx] = _paused_result(rows[idx], checked_at(), paused_code)
                    print_progress(idx + 1, len(rows), results[idx])  # type: ignore[arg-type]
                    written = write_output(written, build_output_frame(rows, results))
                    continue
                if not first:
                    await session.page.wait_for_timeout(
                        int(random.uniform(*query_delay_seconds(carrier)) * 1000)
                    )
                first = False
                row = rows[idx]
                if not container_shape_ok(row["Container"]) or not carrier_supported(
                    row["Carrier"]
                ):
                    results[idx] = _invalid_result(row, checked_at())
                else:
                    results[idx] = await _track_one(
                        session.page,
                        carrier,
                        row["Container"],
                        wait_for_challenge=wait_for_challenge,
                        browser=session,
                    )
                print_progress(idx + 1, len(rows), results[idx])  # type: ignore[arg-type]
                written = write_output(written, build_output_frame(rows, results))
                streak, tripped = update_circuit(
                    streak, results[idx].error_code if results[idx] else None
                )
                if tripped:
                    paused_code = results[idx].error_code if results[idx] else "CLOUDFLARE"
                    LOGGER.info(
                        "Pausing remaining %s rows after repeated %s",
                        carrier,
                        paused_code,
                    )
            await session.close()

    finalized = [item for item in results if item is not None]
    return finalized, written
