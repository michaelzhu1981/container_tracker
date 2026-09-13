import asyncio
from pathlib import Path

import pytest
from openpyxl import Workbook

from job import JobBusyError, JobIdleError, JobManager, JobStartError
from models import TrackResult


def _write_input(path: Path, pairs: list[tuple[str, str]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "Container"
    sheet["B1"] = "Carrier"
    for index, (container, carrier) in enumerate(pairs, start=2):
        sheet.cell(index, 1, container)
        sheet.cell(index, 2, carrier)
    workbook.save(path)


@pytest.mark.asyncio
async def test_job_start_stop_and_busy(tmp_path: Path):
    source = tmp_path / "in.xlsx"
    _write_input(source, [("HLXU1234567", "HLCU"), ("YMLU1234567", "YMJA")])
    started = asyncio.Event()

    async def fake_run_batch(rows, **kwargs):
        kwargs["on_progress"](
            {"index": 0, "total": len(rows), "phase": "querying", "result": None}
        )
        started.set()
        await kwargs["cancel_event"].wait()
        return [], kwargs["output_path"]

    manager = JobManager(
        run_batch_fn=fake_run_batch,
        input_path=source,
        output_path=tmp_path / "out.xlsx",
    )
    assert manager.snapshot()["job"]["total"] == 2

    manager.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    snap = manager.snapshot()
    assert snap["job"]["state"] == "running"
    assert snap["counts"]["QUERYING"] == 1
    with pytest.raises(JobBusyError):
        manager.start()
    with pytest.raises(JobBusyError):
        manager.reload_from_disk()

    manager.request_stop()
    assert manager.snapshot()["job"]["state"] == "stopping"
    if manager.task is not None:
        await manager.task
    assert manager.snapshot()["job"]["state"] == "stopped"
    assert manager.snapshot()["job"]["message"].startswith("Stopped")


def test_job_start_rejects_empty(tmp_path: Path):
    source = tmp_path / "in.xlsx"
    _write_input(source, [])
    manager = JobManager(
        run_batch_fn=lambda *a, **k: None,
        input_path=source,
        output_path=tmp_path / "out.xlsx",
    )
    with pytest.raises(JobStartError):
        manager.start()
    with pytest.raises(JobIdleError):
        manager.request_stop()


def test_challenge_progress_exposes_action_and_clears_on_resume(tmp_path):
    source = tmp_path / "in.xlsx"
    _write_input(source, [("ECMU7271573", "CMDU")])
    manager = JobManager(input_path=source, output_path=tmp_path / "out.xlsx")
    manager._on_progress({
        "index": 0, "phase": "challenge",
        "challenge": {"code": "CAPTCHA", "mode": "current_browser", "timeout_seconds": 180},
    })
    snap = manager.snapshot()
    assert snap["rows"][0]["phase"] == "challenge"
    assert snap["counts"]["QUERYING"] == 1
    assert snap["counts"]["PENDING"] == 0
    assert "keep it open" in snap["job"]["message"]
    assert snap["job"]["challenge"]["code"] == "CAPTCHA"
    manager._on_progress({"index": 0, "phase": "querying"})
    assert manager.snapshot()["job"]["challenge"] is None


def test_challenge_progress_carrier_unlock_mentions_batch(tmp_path):
    source = tmp_path / "in.xlsx"
    _write_input(source, [("ECMU7271573", "CMDU"), ("CMAU7662786", "CMDU")])
    manager = JobManager(input_path=source, output_path=tmp_path / "out.xlsx")
    manager._on_progress({
        "index": 0, "phase": "challenge",
        "challenge": {
            "code": "CAPTCHA",
            "mode": "current_browser",
            "timeout_seconds": 600,
            "scope": "carrier",
        },
    })
    message = manager.snapshot()["job"]["message"]
    assert "Batch query" in message
    assert "keep it open" in message
    assert "up to 10 min" in message


def test_job_snapshot_merges_previous_results(tmp_path: Path):
    from excel_io import build_output_frame, write_output

    source = tmp_path / "in.xlsx"
    output = tmp_path / "out.xlsx"
    _write_input(source, [("HLXU1234567", "HLCU")])
    write_output(
        output,
        build_output_frame(
            [{"Container": "HLXU1234567", "Carrier": "HLCU", "extras": {}}],
            [
                TrackResult(
                    container="HLXU1234567",
                    carrier="HLCU",
                    status="SAILED",
                    success=True,
                    check_result="SUCCESS",
                    loaded=True,
                    sailed=True,
                    checked_at="2026-09-13 00:00:00",
                )
            ],
        ),
    )
    manager = JobManager(input_path=source, output_path=output)
    row = manager.snapshot()["rows"][0]
    assert row["status"] == "SAILED"
    assert row["phase"] == "done"
