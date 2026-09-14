"""In-memory job controller for the local tracking UI."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Awaitable

from artifacts import relative_to_root
from config import INPUT_XLSX, OUTPUT_XLSX, SUPPORTED_CARRIERS
from excel_io import (
    ExcelReadError,
    load_previous_results,
    order_rows_by_carrier,
    read_input,
    result_to_cells,
)
from models import TrackResult
from runner import cells_to_result, run_batch

LOGGER = logging.getLogger("container_tracker")

RunBatchFn = Callable[..., Awaitable[tuple[list[TrackResult], Path]]]

STATUS_KEYS = (
    "SAILED",
    "LOADED_WAITING_DEPARTURE",
    "NOT_LOADED",
    "MANUAL_CHECK_REQUIRED",
    "CHECK_FAILED",
)


class JobError(Exception):
    pass


class JobBusyError(JobError):
    pass


class JobIdleError(JobError):
    pass


class JobStartError(JobError):
    pass


def load_board(
    input_path: Path = INPUT_XLSX, output_path: Path = OUTPUT_XLSX
) -> tuple[list[dict], list[TrackResult | None]]:
    rows = order_rows_by_carrier(read_input(input_path))
    previous = load_previous_results(output_path)
    results: list[TrackResult | None] = []
    occurrence: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row["Container"], row["Carrier"])
        seen = occurrence.get(key, 0)
        occurrence[key] = seen + 1
        cells = previous.get((row["Container"], row["Carrier"], seen))
        if cells and cells.get("Status"):
            results.append(cells_to_result(row["Container"], row["Carrier"], cells))
        else:
            results.append(None)
    return rows, results


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_ms() -> int:
    return int(time.time() * 1000)


def job_elapsed_ms(
    started_ms: int | None,
    finished_ms: int | None,
    *,
    now_ms: int | None = None,
) -> int | None:
    """Wall-clock duration of the current or last job."""
    if started_ms is None:
        return None
    end = finished_ms if finished_ms is not None else (
        _now_ms() if now_ms is None else now_ms
    )
    return max(0, end - started_ms)


def carrier_query_times(
    rows: list[dict],
    query_times: dict[int, dict[str, int]],
    *,
    now_ms: int | None = None,
) -> dict[str, dict[str, int]]:
    """Wall-clock total and mean query time per carrier for this job."""
    clock = _now_ms() if now_ms is None else now_ms
    by_carrier: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for idx, row in enumerate(rows):
        timing = query_times.get(idx)
        if not timing:
            continue
        started = timing.get("started_ms")
        if started is None:
            continue
        ended = timing.get("finished_ms", clock)
        by_carrier[row["Carrier"]].append((started, ended))
    out: dict[str, dict[str, int]] = {}
    for carrier, spans in by_carrier.items():
        first = min(start for start, _end in spans)
        last = max(end for _start, end in spans)
        durations = [max(0, end - start) for start, end in spans]
        out[carrier] = {
            "total_ms": max(0, last - first),
            "avg_ms": round(sum(durations) / len(durations)),
            "queried": len(durations),
        }
    return out


class JobManager:
    def __init__(
        self,
        *,
        run_batch_fn: RunBatchFn | None = None,
        input_path: Path = INPUT_XLSX,
        output_path: Path = OUTPUT_XLSX,
    ) -> None:
        self._run_batch = run_batch_fn or run_batch
        self.input_path = input_path
        self.output_path = output_path
        self.state = "idle"
        self.rows: list[dict] = []
        self.results: list[TrackResult | None] = []
        self.active: dict[int, dict[str, Any]] = {}
        self.query_times: dict[int, dict[str, int]] = {}
        self.current_index: int | None = None
        self.phase: str | None = None
        self.challenge: dict | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.started_ms: int | None = None
        self.finished_ms: int | None = None
        self.error: str | None = None
        self.message = ""
        self.output: str | None = relative_to_root(output_path) or str(output_path)
        self.options: dict[str, Any] = {
            "resume": False,
            "wait_for_challenge": True,
            "headed": False,
            "carriers": None,
        }
        self.cancel_event: asyncio.Event | None = None
        self.task: asyncio.Task | None = None
        try:
            self.reload_from_disk()
        except ExcelReadError as exc:
            self.error = str(exc)
            self.message = str(exc)

    def reload_from_disk(self) -> None:
        if self.state in {"running", "stopping"}:
            raise JobBusyError("Cannot reload while a job is running.")
        self.rows, self.results = load_board(self.input_path, self.output_path)
        self.active = {}
        self.query_times = {}
        self.current_index = None
        self.phase = None
        self.challenge = None
        self.error = None
        self.output = relative_to_root(self.output_path) or str(self.output_path)
        n = len(self.rows)
        done = sum(1 for item in self.results if item is not None)
        self.message = (
            f"Loaded {n} rows from {relative_to_root(self.input_path) or self.input_path}"
            + (f"; {done} have previous results." if done else ".")
        )

    def start(
        self,
        *,
        resume: bool = False,
        wait_for_challenge: bool = True,
        headed: bool = False,
        carriers: list[str] | None = None,
        limit: int | None = None,
    ) -> None:
        if self.state in {"running", "stopping"}:
            raise JobBusyError("A job is already running.")
        rows = order_rows_by_carrier(read_input(self.input_path))
        allow: set[str] | None = None
        if carriers is not None:
            allow = {code.strip().upper() for code in carriers if code.strip()}
            unknown = sorted(allow - set(SUPPORTED_CARRIERS))
            if unknown:
                raise JobStartError(f"Unsupported carrier code: {', '.join(unknown)}.")
            if not allow:
                raise JobStartError("No carrier selected.")
            if not any(row["Carrier"] in allow for row in rows):
                raise JobStartError("No container rows to track.")
        if limit is not None:
            if limit < 1:
                raise JobStartError("Limit must be at least 1.")
            rows = rows[:limit]
        if not rows:
            raise JobStartError("No container rows to track.")

        self.rows = rows
        self.results = [None] * len(rows)
        previous = load_previous_results(self.output_path)
        occurrence: dict[tuple[str, str], int] = {}
        for idx, row in enumerate(rows):
            key = (row["Container"], row["Carrier"])
            seen = occurrence.get(key, 0)
            occurrence[key] = seen + 1
            cells = previous.get((row["Container"], row["Carrier"], seen))
            skipped = allow is not None and row["Carrier"] not in allow
            if skipped:
                if cells and cells.get("Status"):
                    self.results[idx] = cells_to_result(
                        row["Container"], row["Carrier"], cells
                    )
            elif resume and cells and cells.get("Status") == "SAILED":
                self.results[idx] = cells_to_result(
                    row["Container"], row["Carrier"], cells
                )
        self.state = "running"
        self.active = {}
        self.query_times = {}
        self.current_index = None
        self.phase = None
        self.challenge = None
        self.started_at = _now()
        self.finished_at = None
        self.started_ms = _now_ms()
        self.finished_ms = None
        self.error = None
        self.message = "Starting job…"
        self.output = relative_to_root(self.output_path) or str(self.output_path)
        self.options = {
            "resume": resume,
            "wait_for_challenge": wait_for_challenge,
            "headed": headed,
            "carriers": carriers,
        }
        self.cancel_event = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    def request_stop(self) -> None:
        if self.state != "running":
            raise JobIdleError("No running job to stop.")
        assert self.cancel_event is not None
        self.state = "stopping"
        self.message = "Stopping after the current container…"
        self.cancel_event.set()

    def _on_progress(self, payload: dict) -> None:
        idx = payload.get("index")
        phase = payload.get("phase")
        if isinstance(idx, int):
            row = self.rows[idx]
            if phase in {"querying", "challenge"}:
                self.active[idx] = {
                    "phase": phase,
                    "challenge": payload.get("challenge") if phase == "challenge" else None,
                }
                self.query_times.setdefault(idx, {"started_ms": _now_ms()})
            elif phase == "done":
                self.active.pop(idx, None)
                timing = self.query_times.get(idx)
                if timing is not None and "finished_ms" not in timing:
                    timing["finished_ms"] = _now_ms()
            self.current_index = idx
            if phase == "challenge" and payload.get("challenge"):
                current_challenge = payload["challenge"]
                code = current_challenge["code"]
                mode = current_challenge.get("mode")
                seconds = current_challenge.get("timeout_seconds", 0)
                if mode == "current_browser":
                    wait_label = (
                        f"up to {seconds // 60} min"
                        if seconds >= 60
                        else f"up to {seconds}s"
                    )
                    if current_challenge.get("scope") == "carrier":
                        action = (
                            "Complete verification in the current Chrome window; "
                            "keep it open. Batch query starts after it clears "
                            f"({wait_label}). Stop cancels the wait."
                        )
                    else:
                        action = (
                            "Complete verification in the current Chrome window; "
                            "keep it open. "
                            f"Resumes automatically ({wait_label}). Stop cancels the wait."
                        )
                elif mode == "system_chrome":
                    action = "Complete verification in the new system Chrome, then quit that Chrome to resume."
                else:
                    action = f"Waiting up to {seconds}s for the security check."
                self.message = f"{row['Carrier']} {row['Container']}: {code}. {action}"
            elif phase == "done" and payload.get("result") is not None:
                self.results[idx] = payload["result"]
                result = payload["result"]
                self.message = f"[{idx + 1}/{len(self.rows)}] {result.carrier} {result.container} {result.status}"
            elif phase == "querying":
                active_carriers = {
                    self.rows[index]["Carrier"] for index in self.active
                }
                self.message = (
                    f"Querying {len(self.active)} container(s) across "
                    f"{len(active_carriers)} carrier(s)"
                )
        active_indices = sorted(self.active)
        self.current_index = active_indices[0] if active_indices else None
        self.phase = (
            self.active[self.current_index]["phase"]
            if self.current_index is not None
            else phase
        )
        active_challenges = [
            state["challenge"]
            for state in self.active.values()
            if state.get("challenge")
        ]
        self.challenge = active_challenges[0] if active_challenges else None
        if phase == "cancelled":
            self.active.clear()
            self.current_index = None
            self.phase = "cancelled"
            self.challenge = None
            self.message = "Stopped. Remaining rows were not queried."

    async def _run(self) -> None:
        assert self.cancel_event is not None
        try:
            _results, written = await self._run_batch(
                self.rows,
                headed=self.options["headed"],
                wait_for_challenge=self.options["wait_for_challenge"],
                resume=self.options["resume"],
                output_path=self.output_path,
                cancel_event=self.cancel_event,
                on_progress=self._on_progress,
                allow_stdin=False,
                carriers=self.options["carriers"],
            )
            self.output = relative_to_root(written) or str(written)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Tracking job failed")
            self.state = "failed"
            self.error = str(exc)
            self.message = f"Job failed: {exc}"
        else:
            if self.cancel_event.is_set():
                self.state = "stopped"
                if not self.message.startswith("Stopped"):
                    self.message = "Stopped. Remaining rows were not queried."
            else:
                self.state = "completed"
                self.message = "Job completed."
        finally:
            self.active.clear()
            self.current_index = None
            self.phase = None
            self.challenge = None
            self.finished_at = _now()
            closed_at = _now_ms()
            self.finished_ms = closed_at
            for timing in self.query_times.values():
                timing.setdefault("finished_ms", closed_at)
            self.task = None

    def snapshot(self) -> dict[str, Any]:
        counts: Counter[str] = Counter()
        rows_out: list[dict[str, Any]] = []
        for idx, row in enumerate(self.rows):
            result = self.results[idx] if idx < len(self.results) else None
            active = self.active.get(idx)
            if active:
                phase = active["phase"]
                counts["QUERYING"] += 1
            elif result is None:
                phase = "pending"
                counts["PENDING"] += 1
            else:
                phase = "done"
                counts[result.status] += 1
            cells = result_to_cells(result)
            rows_out.append(
                {
                    "index": idx,
                    "container": row["Container"],
                    "carrier": row["Carrier"],
                    "phase": phase,
                    "challenge": active.get("challenge") if active else None,
                    "pol": cells["POL"],
                    "status": cells["Status"],
                    "loaded": cells["Loaded"],
                    "sailed": cells["Sailed"],
                    "vessel": cells["Vessel"],
                    "voyage": cells["Voyage"],
                    "atd": cells["ATD"],
                    "latest_event": cells["Latest Event"],
                    "checked_at": cells["Checked At"],
                    "check_result": cells["Check Result"],
                    "error_code": cells["Error Code"],
                    "error": cells["Error"],
                    "screenshot": cells["Screenshot"],
                }
            )
        done = sum(1 for item in self.results if item is not None)
        challenges = [
            {"index": idx, **state["challenge"]}
            for idx, state in sorted(self.active.items())
            if state.get("challenge")
        ]
        return {
            "job": {
                "state": self.state,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_ms": job_elapsed_ms(self.started_ms, self.finished_ms),
                "current_index": self.current_index,
                "current_indices": sorted(self.active),
                "challenge": self.challenge,
                "challenges": challenges,
                "total": len(self.rows),
                "done": done,
                "input": relative_to_root(self.input_path) or str(self.input_path),
                "output": self.output,
                "error": self.error,
                "message": self.message,
                "options": self.options,
            },
            "counts": {
                **{key: counts.get(key, 0) for key in STATUS_KEYS},
                "PENDING": counts.get("PENDING", 0),
                "QUERYING": counts.get("QUERYING", 0),
            },
            "rows": rows_out,
            "carriers": list(SUPPORTED_CARRIERS),
            "carrier_times": carrier_query_times(self.rows, self.query_times),
        }
