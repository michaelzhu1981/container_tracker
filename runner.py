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
    EXCEL_BATCH_SIZE,
    EXCEL_FLUSH_SECONDS,
    HEADED_SERIAL_CARRIERS,
    HEADLESS_PARALLEL_CARRIERS,
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
from system_chrome import SystemChromeError, SystemChromePage
from trackers import TRACKERS
from trackers.base import TrackerError
from validate import carrier_supported, container_shape_ok, iso6346_check_digit_ok

LOGGER = logging.getLogger("container_tracker")

_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def playwright_context_kwargs(*, headed: bool) -> dict:
    return {
        "headless": not headed,
        "locale": LOCALE,
        "viewport": {"width": 1400, "height": 900},
        "ignore_default_args": ["--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled"],
    }


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


def chrome_launch_args(profile: str, url: str) -> list[str]:
    return [
        f"--user-data-dir={Path(profile).resolve()}",
        "--new-window",
        url,
    ]


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
        else "Press Enter after you finish on that page and have closed Chrome..."
    )
    if allow_stdin and stdin_can_accept_enter():
        try:
            input(prompt)
            return True
        except EOFError:
            print("No terminal input. Waiting for you to close Google Chrome...")
    else:
        print(
            "No terminal input. Complete the check in Google Chrome, "
            "then close that window. Tracking will search automatically."
        )
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
        manual_challenge_lock: asyncio.Lock | None = None,
    ) -> None:
        self.playwright = playwright
        self.carrier = carrier
        self.headed = headed
        self.allow_stdin = allow_stdin
        self.should_abort = should_abort
        self.manual_challenge_lock = manual_challenge_lock
        self.on_challenge: Callable[[dict], None] | None = None
        self.context = None
        self.page = None

    async def start(self) -> None:
        if uses_system_chrome(self.carrier):
            await self._start_system_chrome()
            return
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:  # noqa: BLE001
                LOGGER.info("Previous %s browser context did not close cleanly.", self.carrier)
            self.context = None
        profile = chrome_profile_dir(self.carrier)
        profile.mkdir(parents=True, exist_ok=True)
        kwargs = playwright_context_kwargs(headed=self.headed)
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(profile), channel="chrome", **kwargs
            )
        except Exception:  # noqa: BLE001
            LOGGER.info("System Chrome not available; using Playwright Chromium.")
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(profile), **kwargs
            )
        try:
            await self.context.add_init_script(_STEALTH_INIT_JS)
        except Exception:  # noqa: BLE001
            LOGGER.info("Could not install stealth init script for %s.", self.carrier)
        self.context.set_default_timeout(CARRIER_TIMEOUT_MS.get(self.carrier, NAV_TIMEOUT_MS))
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def _start_system_chrome(self) -> None:
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:  # noqa: BLE001
                LOGGER.info("Closed Playwright before opening system Chrome for %s.", self.carrier)
            self.context = None
        settings = system_chrome_settings(self.carrier)
        self.page = SystemChromePage(
            settings["url"],
            host=settings["host"],
            carrier=settings["carrier"],
            challenge_name=settings["challenge_name"],
            should_abort=self.should_abort,
        )
        await self.page.start()

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
            ["open", "-na", "Google Chrome", "--args", *chrome_launch_args(profile, url)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def hand_off_to_system_chrome(self, url: str) -> bool:
        """Open a normal Chrome so the user can pass DataDome, then search after it closes."""
        print()
        print("Automated Chrome cannot complete this check reliably.")
        print("1. The automated window will close.")
        print("2. A normal Google Chrome will open the tracking page.")
        print("3. Complete the check and wait until the search box is visible.")
        print("4. You do not need to type the container. Close that Chrome (Cmd+Q);")
        print("   tracking will search automatically with the unlocked session.")
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
        if isinstance(self.page, SystemChromePage):
            await self.page.close()
            self.page = None
            return
        if self.context is None:
            return
        try:
            await self.context.close()
        except Exception:  # noqa: BLE001
            LOGGER.info("Could not close %s browser context.", self.carrier)
        else:
            if self.headed:
                LOGGER.info("Closed the Chrome window for %s.", self.carrier)
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


def _paused_result(
    row: dict,
    stamp: str,
    error_code: str,
    *,
    reason: str | None = None,
) -> TrackResult:
    return evaluate(
        [],
        container=row["Container"],
        carrier=row["Carrier"],
        checked_at=stamp,
        forced_status="CHECK_FAILED",
        error_code=error_code,
        error=reason
        or (
            f"Skipped remaining {row['Carrier']} rows after repeated "
            f"{error_code} failures."
        ),
    )


def _unlock_failed_result(row: dict, stamp: str, error_code: str) -> TrackResult:
    status = (
        "MANUAL_CHECK_REQUIRED"
        if error_code in {"CAPTCHA", "CLOUDFLARE"}
        else "CHECK_FAILED"
    )
    return evaluate(
        [],
        container=row["Container"],
        carrier=row["Carrier"],
        checked_at=stamp,
        forced_status=status,
        error_code=error_code,
        error=(
            f"{error_code} still blocked the current Chrome window. "
            "Complete verification there, keep the window open, and retry."
        ),
    )


def _session_failed_result(
    row: dict,
    stamp: str,
    error_code: str,
    error: str,
    *,
    wait_for_challenge: bool,
) -> TrackResult:
    status = "CHECK_FAILED"
    if wait_for_challenge and error_code in {"CAPTCHA", "CLOUDFLARE"}:
        status = "MANUAL_CHECK_REQUIRED"
    return evaluate(
        [],
        container=row["Container"],
        carrier=row["Carrier"],
        checked_at=stamp,
        forced_status=status,
        error_code=error_code,
        error=error,
    )


def unlocks_in_current_browser(carrier: str) -> bool:
    tracker_cls = TRACKERS.get(carrier)
    return bool(tracker_cls and getattr(tracker_cls, "wait_in_current_browser", False))


def uses_system_chrome(carrier: str) -> bool:
    """DataDome / Cloudflare fail under Playwright CDP; use the user's Chrome."""
    tracker_cls = TRACKERS.get(carrier)
    return bool(tracker_cls and getattr(tracker_cls, "use_system_chrome", False))


def system_chrome_settings(carrier: str) -> dict[str, str]:
    tracker_cls = TRACKERS.get(carrier)
    return {
        "url": getattr(tracker_cls, "tracking_url", "") if tracker_cls else "",
        "host": getattr(tracker_cls, "system_chrome_host", "") if tracker_cls else "",
        "carrier": getattr(tracker_cls, "carrier_code", carrier) if tracker_cls else carrier,
        "challenge_name": (
            getattr(tracker_cls, "system_chrome_challenge", "the security check")
            if tracker_cls
            else "the security check"
        ),
    }


async def unlock_carrier_session(
    session: CarrierBrowser,
    carrier: str,
    *,
    wait_for_challenge: bool,
) -> str | None:
    """Open the tracking page and wait once. Return an error code on failure."""
    if not wait_for_challenge or not unlocks_in_current_browser(carrier):
        return None
    tracker_cls = TRACKERS[carrier]
    tracker = tracker_cls(session.page, wait_for_challenge=True, browser=session)
    print(
        f"Unlocking {carrier} in the current Chrome window. "
        "Complete the check there and keep the window open; "
        "batch query starts after it clears."
    )
    try:
        await tracker.prepare_session()
    except TrackerError as exc:
        return exc.code
    except SystemChromeError as exc:
        LOGGER.info("System Chrome unlock failed for %s: %s", carrier, exc)
        print(str(exc))
        return "CAPTCHA"
    session.page = tracker.page
    return None


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
    tracker=None,
    session_ready: bool = False,
) -> TrackResult:
    if tracker is None:
        tracker_cls = TRACKERS[carrier]
        tracker = tracker_cls(
            page, wait_for_challenge=wait_for_challenge, browser=browser
        )
    else:
        tracker.page = page
    LOGGER.info("Tracking %s %s", carrier, container)
    if container_shape_ok(container) and not iso6346_check_digit_ok(container):
        LOGGER.warning("ISO 6346 check digit failed for %s; querying anyway.", container)
    return await tracker.track(container, session_ready=session_ready)


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


def carrier_schedule_lanes(
    carriers: list[str] | set[str],
) -> tuple[list[str], list[str]]:
    """Run headless ONEY/YMJA/COSU together; headed carriers stay serial."""
    present = set(carriers)
    parallel = [code for code in HEADLESS_PARALLEL_CARRIERS if code in present]
    serial = [code for code in HEADED_SERIAL_CARRIERS if code in present]
    known = set(HEADLESS_PARALLEL_CARRIERS) | set(HEADED_SERIAL_CARRIERS)
    leftovers = [code for code in carriers if code not in known]
    seen: set[str] = set()
    for code in leftovers:
        if code in seen:
            continue
        seen.add(code)
        serial.append(code)
    return parallel, serial


class BatchCheckpointWriter:
    """Serialize periodic workbook snapshots without blocking carrier workers."""

    def __init__(
        self,
        path: Path,
        rows: list[dict],
        results: list[TrackResult | None],
        *,
        batch_size: int = EXCEL_BATCH_SIZE,
        flush_seconds: float = EXCEL_FLUSH_SECONDS,
    ) -> None:
        self.path = path
        self.rows = rows
        self.results = results
        self.batch_size = max(1, batch_size)
        self.flush_seconds = max(0.1, flush_seconds)
        self._dirty = 0
        self._event = asyncio.Event()
        self._closing = False
        self._task: asyncio.Task | None = None

    async def start(self) -> Path:
        await self._flush()
        self._task = asyncio.create_task(self._run())
        return self.path

    def mark_completed(self) -> None:
        self._dirty += 1
        if self._dirty >= self.batch_size:
            self._event.set()

    async def close(self) -> Path:
        self._closing = True
        self._event.set()
        if self._task is not None:
            await self._task
            self._task = None
        return self.path

    async def _run(self) -> None:
        while True:
            timed_out = False
            try:
                await asyncio.wait_for(
                    self._event.wait(), timeout=self.flush_seconds
                )
            except asyncio.TimeoutError:
                timed_out = True
            self._event.clear()
            if self._dirty and (
                self._closing or timed_out or self._dirty >= self.batch_size
            ):
                await self._flush()
            if self._closing:
                return

    async def _flush(self) -> None:
        snapshot = list(self.results)
        self._dirty = 0
        frame = build_output_frame(self.rows, snapshot)
        self.path = await asyncio.to_thread(write_output, self.path, frame)


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

    by_carrier: dict[str, list[int]] = defaultdict(list)
    for idx, row in enumerate(rows):
        if idx in skip_indices:
            continue
        if not container_shape_ok(row["Container"]) or not carrier_supported(
            row["Carrier"]
        ):
            results[idx] = _invalid_result(row, checked_at())
            continue
        by_carrier[row["Carrier"]].append(idx)

    writer = BatchCheckpointWriter(output_path, rows, results)
    written = await writer.start()

    for idx, result in enumerate(results):
        if result is not None:
            notify(
                {
                    "index": idx,
                    "total": total,
                    "phase": "done",
                    "result": result,
                }
            )

    def finish(idx: int, result: TrackResult) -> None:
        results[idx] = result
        print_progress(idx + 1, total, result)
        notify(
            {
                "index": idx,
                "total": total,
                "phase": "done",
                "result": result,
            }
        )
        writer.mark_completed()

    manual_challenge_lock = asyncio.Lock()

    try:
        async with async_playwright() as playwright:

            async def run_carrier(carrier: str, indices: list[int]) -> None:
                if _cancelled(cancel_event) or not indices:
                    return
                session = CarrierBrowser(
                    playwright,
                    carrier,
                    headed=default_headed_for(carrier, headed),
                    allow_stdin=allow_stdin,
                    should_abort=lambda: _cancelled(cancel_event),
                    manual_challenge_lock=manual_challenge_lock,
                )
                first_idx = indices[0]
                try:
                    await session.start()
                except SystemChromeError as exc:
                    LOGGER.info(
                        "Could not open system Chrome for %s: %s", carrier, exc
                    )
                    print(str(exc))
                    finish(
                        first_idx,
                        _session_failed_result(
                            rows[first_idx],
                            checked_at(),
                            "CAPTCHA",
                            str(exc),
                            wait_for_challenge=wait_for_challenge,
                        ),
                    )
                    for idx in indices[1:]:
                        finish(
                            idx,
                            _paused_result(
                                rows[idx],
                                checked_at(),
                                "CAPTCHA",
                                reason=(
                                    f"Skipped remaining {carrier} rows; "
                                    "open Google Chrome, complete "
                                    f"{system_chrome_settings(carrier)['challenge_name']}, "
                                    "and retry."
                                ),
                            ),
                        )
                    await session.close()
                    return

                tracker_cls = TRACKERS[carrier]
                tracker = tracker_cls(
                    session.page,
                    wait_for_challenge=wait_for_challenge,
                    browser=session,
                )
                streak = 0
                paused_code: str | None = None
                paused_reason: str | None = None
                queried = False
                session_ready = False

                def set_challenge_callback(index: int, *, scope: str | None = None) -> None:
                    def report(challenge: dict) -> None:
                        payload = challenge if challenge.get("code") else None
                        if payload and scope:
                            payload = {**payload, "scope": scope}
                        notify(
                            {
                                "index": index,
                                "total": total,
                                "phase": (
                                    "challenge"
                                    if challenge.get("code")
                                    else "querying"
                                ),
                                "challenge": payload,
                                "result": None,
                            }
                        )

                    session.on_challenge = report

                async def prepare(index: int) -> str | None:
                    nonlocal session_ready
                    tracker.page = session.page
                    set_challenge_callback(index, scope="carrier")
                    notify(
                        {
                            "index": index,
                            "total": total,
                            "phase": "querying",
                            "result": None,
                        }
                    )
                    try:
                        await tracker.prepare_session()
                    except TrackerError as exc:
                        if exc.code == "CANCELLED":
                            return "CANCELLED"
                        finish(
                            index,
                            _session_failed_result(
                                rows[index],
                                checked_at(),
                                exc.code,
                                str(exc),
                                wait_for_challenge=wait_for_challenge,
                            ),
                        )
                        return exc.code
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.exception("Could not initialize %s session", carrier)
                        finish(
                            index,
                            _session_failed_result(
                                rows[index],
                                checked_at(),
                                "NAVIGATION",
                                str(exc),
                                wait_for_challenge=wait_for_challenge,
                            ),
                        )
                        return "NAVIGATION"
                    session.page = tracker.page
                    session_ready = True
                    return None

                try:
                    prepare_error = await prepare(first_idx)
                    if prepare_error:
                        if prepare_error == "CANCELLED":
                            return
                        paused_code = prepare_error
                        if (
                            prepare_error in {"CAPTCHA", "CLOUDFLARE"}
                            and unlocks_in_current_browser(carrier)
                        ):
                            paused_reason = (
                                f"Skipped remaining {carrier} rows; complete "
                                f"{prepare_error} in the current Chrome window, "
                                "keep it open, and retry."
                            )
                        else:
                            paused_reason = (
                                f"Skipped remaining {carrier} rows because its "
                                f"session initialization failed: {prepare_error}."
                            )

                    for idx in indices:
                        if _cancelled(cancel_event):
                            return
                        if results[idx] is not None:
                            continue
                        if paused_code:
                            finish(
                                idx,
                                _paused_result(
                                    rows[idx],
                                    checked_at(),
                                    paused_code,
                                    reason=paused_reason,
                                ),
                            )
                            continue
                        if queried:
                            delay = random.uniform(*query_delay_seconds(carrier))
                            if await sleep_or_cancel(delay, cancel_event):
                                return

                        old_page = session.page
                        await session.ensure_open()
                        if session.page is not old_page:
                            session_ready = False
                            recovery_error = await prepare(idx)
                            if recovery_error:
                                if recovery_error == "CANCELLED":
                                    return
                                paused_code = recovery_error
                                paused_reason = (
                                    f"Skipped remaining {carrier} rows because "
                                    f"the browser session could not recover: "
                                    f"{recovery_error}."
                                )
                                continue

                        row = rows[idx]
                        tracker.page = session.page
                        set_challenge_callback(idx)
                        notify(
                            {
                                "index": idx,
                                "total": total,
                                "phase": "querying",
                                "result": None,
                            }
                        )
                        result = await _track_one(
                            session.page,
                            carrier,
                            row["Container"],
                            wait_for_challenge=wait_for_challenge,
                            browser=session,
                            tracker=tracker,
                            session_ready=session_ready,
                        )
                        session.page = tracker.page
                        queried = True
                        finish(idx, result)
                        streak, tripped = update_circuit(
                            streak, result.error_code
                        )
                        if tripped:
                            paused_code = result.error_code or "CLOUDFLARE"
                            paused_reason = None
                            LOGGER.info(
                                "Pausing remaining %s rows after repeated %s",
                                carrier,
                                paused_code,
                            )
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("Carrier worker failed for %s", carrier)
                    for idx in indices:
                        if results[idx] is None and not _cancelled(cancel_event):
                            finish(
                                idx,
                                _session_failed_result(
                                    rows[idx],
                                    checked_at(),
                                    "NAVIGATION",
                                    str(exc),
                                    wait_for_challenge=wait_for_challenge,
                                ),
                            )
                finally:
                    await session.close()

            parallel, serial = carrier_schedule_lanes(list(by_carrier))

            async def run_parallel() -> None:
                if not parallel:
                    return
                await asyncio.gather(
                    *(
                        run_carrier(carrier, by_carrier[carrier])
                        for carrier in parallel
                    )
                )

            async def run_serial() -> None:
                for carrier in serial:
                    if _cancelled(cancel_event):
                        return
                    await run_carrier(carrier, by_carrier[carrier])

            await asyncio.gather(run_parallel(), run_serial())
    finally:
        written = await writer.close()

    if _cancelled(cancel_event):
        print("Stopped. Remaining rows were not queried.")
        notify({"index": None, "total": total, "phase": "cancelled", "result": None})

    finalized = [item for item in results if item is not None]
    return finalized, written
