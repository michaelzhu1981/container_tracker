"""In-memory job controller for the local tracking UI."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
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
        self.current_index: int | None = None
        self.phase: str | None = None
        self.challenge: dict | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
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
        if carriers:
            allow = {code.strip().upper() for code in carriers if code.strip()}
            unknown = sorted(allow - set(SUPPORTED_CARRIERS))
            if unknown:
                raise JobStartError(f"Unsupported carrier code: {', '.join(unknown)}.")
            rows = [row for row in rows if row["Carrier"] in allow]
        if limit is not None:
            if limit < 1:
                raise JobStartError("Limit must be at least 1.")
            rows = rows[:limit]
        if not rows:
            raise JobStartError("No container rows to track.")

        self.rows = rows
        self.results = [None] * len(rows)
        if resume:
            previous = load_previous_results(self.output_path)
            occurrence: dict[tuple[str, str], int] = {}
            for idx, row in enumerate(rows):
                key = (row["Container"], row["Carrier"])
                seen = occurrence.get(key, 0)
                occurrence[key] = seen + 1
                cells = previous.get((row["Container"], row["Carrier"], seen))
                if cells and cells.get("Status") == "SAILED":
                    self.results[idx] = cells_to_result(
                        row["Container"], row["Carrier"], cells
                    )
        self.state = "running"
        self.current_index = None
        self.phase = None
        self.challenge = None
        self.started_at = _now()
        self.finished_at = None
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
        self.phase = phase
        self.challenge = payload.get("challenge") if phase == "challenge" else None
        if isinstance(idx, int):
            self.current_index = idx
            row = self.rows[idx]
            if phase == "querying":
                self.message = f"Querying {row['Carrier']} {row['Container']}"
            elif phase == "challenge" and self.challenge:
                code = self.challenge["code"]
                mode = self.challenge.get("mode")
                seconds = self.challenge.get("timeout_seconds", 0)
                if mode == "current_browser":
                    action = (
                        "Complete verification in the current Chrome window; keep it open. "
                        f"Resumes automatically (up to {seconds}s). Stop cancels the wait."
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
        if phase == "cancelled":
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
            self.current_index = None
            self.phase = None
            self.challenge = None
            self.finished_at = _now()
            self.task = None

    def snapshot(self) -> dict[str, Any]:
        counts: Counter[str] = Counter()
        rows_out: list[dict[str, Any]] = []
        for idx, row in enumerate(self.rows):
            result = self.results[idx] if idx < len(self.results) else None
            querying = self.current_index == idx and self.phase in {"querying", "challenge"}
            if querying:
                phase = self.phase
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
        return {
            "job": {
                "state": self.state,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "current_index": self.current_index,
                "challenge": self.challenge,
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
        }
