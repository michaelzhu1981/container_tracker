"""Batch and single-container tracking runs."""

from __future__ import annotations

import asyncio
import logging
import random
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from artifacts import log_path, relative_to_root
from config import (
    CARRIER_TIMEOUT_MS,
    CIRCUIT_BREAK_CODES,
    CIRCUIT_BREAK_STREAK,
    LOCALE,
    MANUAL_CHROME_APPEAR_SECONDS,
    MANUAL_CHROME_WAIT_SECONDS,
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


def stdin_can_accept_enter() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def chrome_commands_using_profile(profile: str) -> list[str]:
    needle = f"--user-data-dir={Path(profile).resolve()}"
    try:
        output = subprocess.check_output(
            ["ps", "-ax", "-o", "command="],
            text=True,
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line for line in output.splitlines() if needle in line]


def wait_for_system_chrome_closed(
    profile: str,
    *,
    timeout_s: float = MANUAL_CHROME_WAIT_SECONDS,
    appear_s: float = MANUAL_CHROME_APPEAR_SECONDS,
    poll_s: float = 1.0,
    should_abort: Callable[[], bool] | None = None,
) -> bool:
    """True after a Chrome using this profile appears and then exits."""
    appear_deadline = time.monotonic() + appear_s
    while time.monotonic() < appear_deadline:
        if should_abort and should_abort():
            return False
        if chrome_commands_using_profile(profile):
            break
        time.sleep(poll_s)
    else:
        return False

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if should_abort and should_abort():
            return False
        if not chrome_commands_using_profile(profile):
            time.sleep(min(0.8, poll_s))
            if should_abort and should_abort():
                return False
            if not chrome_commands_using_profile(profile):
                return True
        time.sleep(poll_s)
    return False


def wait_for_human_after_chrome_handoff(
    profile: str,
    *,
    reopen: bool = False,
    allow_stdin: bool = True,
    should_abort: Callable[[], bool] | None = None,
) -> bool:
    prompt = (
        "Press Enter..."
        if reopen
        else "Press Enter after the search box is visible and you have closed Chrome..."
    )
    if allow_stdin and stdin_can_accept_enter():
        try:
            input(prompt)
            return True
        except EOFError:
            print("No terminal input. Waiting for you to close Google Chrome...")
    else:
        print("No terminal input. Complete the check in Google Chrome, then close that window.")
    if wait_for_system_chrome_closed(profile, should_abort=should_abort):
        return True
    print("Timed out waiting for the system Chrome window to close.")
    return False


async def sleep_or_cancel(
    seconds: float, cancel_event: asyncio.Event | None
) -> bool:
    """Sleep. Return True if cancel_event was set first."""
    if cancel_event is not None and cancel_event.is_set():
        return True
    if seconds <= 0:
        return False
    if cancel_event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


class CarrierBrowser:
    """One persistent Chrome profile per carrier; never relaunched per box."""

    def __init__(
        self,
        playwright,
        carrier: str,
        *,
        headed: bool,
        allow_stdin: bool = True,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        self.playwright = playwright
        self.carrier = carrier
        self.headed = headed
        self.allow_stdin = allow_stdin
        self.should_abort = should_abort
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

    def page_open(self) -> bool:
        try:
            return self.page is not None and not self.page.is_closed()
        except Exception:  # noqa: BLE001
            return False

    async def ensure_open(self) -> None:
        if self.page_open():
            return
        LOGGER.info("Relaunching %s browser after the window closed.", self.carrier)
        await self.start()

    def _spawn_system_chrome(self, url: str) -> None:
        profile = str(chrome_profile_dir(self.carrier).resolve())
        subprocess.Popen(
            [
                "open",
                "-na",
                "Google Chrome",
                "--args",
                f"--user-data-dir={profile}",
                "--new-window",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def hand_off_to_system_chrome(self, url: str) -> bool:
        """Let the user pass Cloudflare in real Chrome, then reopen this profile."""
        print()
        print("Automated Chrome cannot complete this check reliably.")
        print("1. The automated window will close.")
        print("2. Google Chrome will open the tracking page with the same profile.")
        print("3. Complete the check and wait until the container search box is visible.")
        print("4. Close that Chrome window (so the profile is not locked).")
        print("5. Return here and press Enter to resume tracking.")
        print()
        await self.close()
        await asyncio.sleep(1.2)
        try:
            self._spawn_system_chrome(url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Could not open system Chrome: %s", exc)
            await self.start()
            return False
        profile = str(chrome_profile_dir(self.carrier).resolve())
        if not await asyncio.to_thread(
            wait_for_human_after_chrome_handoff,
            profile,
            allow_stdin=self.allow_stdin,
            should_abort=self.should_abort,
        ):
            try:
                await self.start()
            except Exception:  # noqa: BLE001
                LOGGER.info("Could not reopen %s after a failed Chrome handoff.", self.carrier)
            return False
        try:
            await self.start()
        except Exception:  # noqa: BLE001
            print("Could not reopen the profile. Close Google Chrome, then press Enter again.")
            if not await asyncio.to_thread(
                wait_for_human_after_chrome_handoff,
                profile,
                reopen=True,
                allow_stdin=self.allow_stdin,
                should_abort=self.should_abort,
            ):
                return False
            await self.start()
        if url and self.page is not None:
            try:
                await self.page.goto(url, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                LOGGER.info("Could not open %s after manual Chrome unlock.", url)
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


def cells_to_result(container: str, carrier: str, cells: dict) -> TrackResult:
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


def _cancelled(cancel_event: asyncio.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


async def run_batch(
    rows: list[dict],
    *,
    headed: bool = False,
    wait_for_challenge: bool = True,
    resume: bool = False,
    output_path: Path = OUTPUT_XLSX,
    previous_path: Path | None = None,
    cancel_event: asyncio.Event | None = None,
    on_progress: Callable[[dict], None] | None = None,
    allow_stdin: bool = True,
) -> tuple[list[TrackResult], Path]:
    from artifacts import checked_at
    from playwright.async_api import async_playwright

    def notify(payload: dict) -> None:
        if on_progress:
            on_progress(payload)

    previous = load_previous_results(previous_path or output_path) if resume else {}
    results: list[TrackResult | None] = [None] * len(rows)
    occurrence: dict[tuple[str, str], int] = {}
    total = len(rows)

    skip_indices: set[int] = set()
    if resume:
        for idx, row in enumerate(rows):
            key = (row["Container"], row["Carrier"])
            seen = occurrence.get(key, 0)
            occurrence[key] = seen + 1
            cells = previous.get((row["Container"], row["Carrier"], seen))
            if cells and cells.get("Status") == "SAILED":
                results[idx] = cells_to_result(row["Container"], row["Carrier"], cells)
                skip_indices.add(idx)
        occurrence = {}

    written = write_output(output_path, build_output_frame(rows, results))

    async with async_playwright() as playwright:
        by_carrier: dict[str, list[int]] = defaultdict(list)
        for idx, row in enumerate(rows):
            by_carrier[row["Carrier"]].append(idx)

        first = True
        cancelled = False
        for carrier, indices in by_carrier.items():
            if cancelled or _cancelled(cancel_event):
                cancelled = True
                break
            if not carrier_supported(carrier):
                for idx in indices:
                    results[idx] = _invalid_result(rows[idx], checked_at())
                    notify(
                        {
                            "index": idx,
                            "total": total,
                            "phase": "done",
                            "result": results[idx],
                        }
                    )
                written = write_output(written, build_output_frame(rows, results))
                continue
            session = CarrierBrowser(
                playwright,
                carrier,
                headed=default_headed_for(carrier, headed),
                allow_stdin=allow_stdin,
                should_abort=lambda: _cancelled(cancel_event),
            )
            await session.start()
            streak = 0
            paused_code: str | None = None
            try:
                for idx in indices:
                    if _cancelled(cancel_event):
                        cancelled = True
                        break
                    if idx in skip_indices:
                        print_progress(idx + 1, total, results[idx])  # type: ignore[arg-type]
                        notify(
                            {
                                "index": idx,
                                "total": total,
                                "phase": "done",
                                "result": results[idx],
                            }
                        )
                        continue
                    if paused_code:
                        results[idx] = _paused_result(rows[idx], checked_at(), paused_code)
                        print_progress(idx + 1, total, results[idx])  # type: ignore[arg-type]
                        notify(
                            {
                                "index": idx,
                                "total": total,
                                "phase": "done",
                                "result": results[idx],
                            }
                        )
                        written = write_output(written, build_output_frame(rows, results))
                        continue
                    if not first:
                        delay = random.uniform(*query_delay_seconds(carrier))
                        if await sleep_or_cancel(delay, cancel_event):
                            cancelled = True
                            break
                    await session.ensure_open()
                    first = False
                    row = rows[idx]
                    notify(
                        {
                            "index": idx,
                            "total": total,
                            "phase": "querying",
                            "result": None,
                        }
                    )
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
                    print_progress(idx + 1, total, results[idx])  # type: ignore[arg-type]
                    notify(
                        {
                            "index": idx,
                            "total": total,
                            "phase": "done",
                            "result": results[idx],
                        }
                    )
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
            finally:
                await session.close()

    if cancelled:
        print("Stopped. Remaining rows were not queried.")
        notify({"index": None, "total": total, "phase": "cancelled", "result": None})

    finalized = [item for item in results if item is not None]
    return finalized, written
